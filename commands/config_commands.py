"""Consolidated /config command — modal and panel-based configuration.

Sections:
  global   — timezone, mod channel, bypass roles
  welcome  — welcome & leave channel + message template
  roles    — greeter / denizen / nsfw / veteran role, log, announce, message
  xp       — XP log channels + current-channel XP toggle
  prune    — inactivity prune role + threshold
  spoiler  — spoiler-guard channel list + current-channel toggle
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from db_utils import (
    add_config_id,
    add_grant_permission,
    clear_config_id_bucket,
    delete_grant_role,
    get_grant_permissions,
    remove_grant_permission,
    upsert_grant_role,
)
from services.inactivity_prune_service import (
    get_prune_rule,
    remove_prune_rule,
    run_prune_for_guild,
    upsert_prune_rule,
)
from utils import get_guild_channel_or_thread
from xp_system import DEFAULT_XP_SETTINGS

if TYPE_CHECKING:
    from app_context import AppContext, Bot


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_channel(text: str, current_channel_id: int) -> int | None:
    """'here' → current channel, 'off'/'0' → 0 (disabled), else parse as ID."""
    t = text.strip().lower()
    if t in ("here", "current", ""):
        return current_channel_id
    if t in ("off", "0", "none", "disable", "disabled"):
        return 0
    try:
        return int(text.strip())
    except ValueError:
        return None


def _parse_role(text: str) -> int | None:
    """'off'/'0'/empty → 0 (cleared), else parse as ID."""
    t = text.strip().lower()
    if t in ("off", "0", "none", ""):
        return 0
    try:
        return int(text.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Global settings modal
# ---------------------------------------------------------------------------


class _GlobalModal(discord.ui.Modal, title="Global Settings"):
    def __init__(self, ctx: AppContext, current_channel_id: int) -> None:
        super().__init__()
        self._ctx = ctx
        self._current_channel_id = current_channel_id

        tz_default = str(ctx.tz_offset_hours) if ctx.tz_offset_hours != 0.0 else "0"
        self.tz_offset: discord.ui.TextInput = discord.ui.TextInput(
            label="UTC offset (e.g. 1, -5, 5.5)",
            default=tz_default,
            placeholder="0 = UTC  ·  1 = UTC+1  ·  -5 = UTC-5",
            required=False,
            max_length=10,
        )
        self.mod_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Mod channel (ID · 'here' · 'off')",
            default=str(ctx.mod_channel_id) if ctx.mod_channel_id > 0 else "off",
            required=False,
            max_length=30,
        )
        self.bypass_roles: discord.ui.TextInput = discord.ui.TextInput(
            label="Bypass role IDs (space/comma-separated)",
            default=", ".join(str(r) for r in sorted(ctx.bypass_role_ids))
            if ctx.bypass_role_ids
            else "",
            placeholder="Roles that bypass spoiler guard, etc.",
            required=False,
            max_length=500,
        )
        self.add_item(self.tz_offset)
        self.add_item(self.mod_channel)
        self.add_item(self.bypass_roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re

        errors: list[str] = []

        # Timezone
        tz_raw = self.tz_offset.value.strip() or "0"
        try:
            tz_hours = float(tz_raw)
            if not -24 < tz_hours < 24:
                raise ValueError
        except ValueError:
            errors.append(
                f"Invalid UTC offset: `{tz_raw}` — use a number like 1, -5, or 5.5"
            )
            tz_hours = None

        # Mod channel
        mc = _parse_channel(self.mod_channel.value, self._current_channel_id)
        if mc is None:
            errors.append(f"Invalid mod channel: `{self.mod_channel.value}`")

        # Bypass roles
        bypass_raw = self.bypass_roles.value.strip()
        bypass_ids: list[int] = []
        bypass_valid = True
        if bypass_raw:
            tokens = re.split(r"[\s,]+", bypass_raw)
            for tok in tokens:
                if not tok:
                    continue
                if tok.isdigit():
                    bypass_ids.append(int(tok))
                else:
                    errors.append(f"Invalid bypass role ID: `{tok}`")
                    bypass_valid = False
                    break

        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        # Save timezone
        assert tz_hours is not None
        self._ctx.tz_offset_hours = float(
            self._ctx.set_config_value("tz_offset_hours", str(tz_hours))
        )

        # Save mod channel
        assert mc is not None
        self._ctx.mod_channel_id = int(
            self._ctx.set_config_value("mod_channel_id", str(mc))
        )

        # Save bypass roles — replace the full set
        assert bypass_valid
        with self._ctx.open_db() as conn:
            clear_config_id_bucket(conn, "bypass_role_ids", self._ctx.guild_id)
            for rid in bypass_ids:
                add_config_id(conn, "bypass_role_ids", rid, self._ctx.guild_id)
            from db_utils import get_config_id_set

            self._ctx.bypass_role_ids = get_config_id_set(
                conn, "bypass_role_ids", self._ctx.guild_id
            )

        tz_label = f"UTC{tz_hours:+g}" if tz_hours != 0 else "UTC"
        mc_label = f"<#{mc}>" if mc > 0 else "off"
        bypass_label = (
            ", ".join(f"<@&{r}>" for r in sorted(self._ctx.bypass_role_ids)) or "none"
        )
        await interaction.response.send_message(
            f"Saved.  Timezone → {tz_label}  ·  Mod channel → {mc_label}\n"
            f"Bypass roles → {bypass_label}",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Welcome & Leave modal
# ---------------------------------------------------------------------------


class _WelcomeLeaveModal(discord.ui.Modal, title="Welcome & Leave Config"):
    def __init__(self, ctx: AppContext, current_channel_id: int) -> None:
        super().__init__()
        self._ctx = ctx
        self._current_channel_id = current_channel_id

        self.welcome_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Welcome channel  (ID · 'here' · 'off')",
            default=str(ctx.welcome_channel_id)
            if ctx.welcome_channel_id > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.welcome_msg: discord.ui.TextInput = discord.ui.TextInput(
            label="Welcome message",
            style=discord.TextStyle.paragraph,
            default=ctx.welcome_message,
            placeholder="{member} {member_name} {server} {member_count}",
            required=False,
            max_length=1000,
        )
        self.leave_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Leave channel  (ID · 'here' · 'off')",
            default=str(ctx.leave_channel_id) if ctx.leave_channel_id > 0 else "off",
            required=False,
            max_length=30,
        )
        self.welcome_ping_role: discord.ui.TextInput = discord.ui.TextInput(
            label="Welcome ping role  (ID · 'off')",
            default=str(ctx.welcome_ping_role_id)
            if ctx.welcome_ping_role_id > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.leave_msg: discord.ui.TextInput = discord.ui.TextInput(
            label="Leave message",
            style=discord.TextStyle.paragraph,
            default=ctx.leave_message,
            placeholder="{member_name} {server}",
            required=False,
            max_length=1000,
        )
        self.add_item(self.welcome_channel)
        self.add_item(self.welcome_msg)
        self.add_item(self.welcome_ping_role)
        self.add_item(self.leave_channel)
        self.add_item(self.leave_msg)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        wc = _parse_channel(self.welcome_channel.value, self._current_channel_id)
        lc = _parse_channel(self.leave_channel.value, self._current_channel_id)
        pr = _parse_role(self.welcome_ping_role.value)
        errors: list[str] = []
        if wc is None:
            errors.append(f"Invalid welcome channel: `{self.welcome_channel.value}`")
        if lc is None:
            errors.append(f"Invalid leave channel: `{self.leave_channel.value}`")
        if pr is None:
            errors.append(
                f"Invalid welcome ping role: `{self.welcome_ping_role.value}`"
            )
        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        assert wc is not None and lc is not None and pr is not None
        self._ctx.welcome_channel_id = int(
            self._ctx.set_config_value("welcome_channel_id", str(wc))
        )
        self._ctx.welcome_message = self._ctx.set_config_value(
            "welcome_message", self.welcome_msg.value
        )
        self._ctx.welcome_ping_role_id = int(
            self._ctx.set_config_value("welcome_ping_role_id", str(pr))
        )
        self._ctx.leave_channel_id = int(
            self._ctx.set_config_value("leave_channel_id", str(lc))
        )
        self._ctx.leave_message = self._ctx.set_config_value(
            "leave_message", self.leave_msg.value
        )

        w_label = f"<#{wc}>" if wc > 0 else "disabled"
        l_label = f"<#{lc}>" if lc > 0 else "disabled"
        p_label = f"<@&{pr}>" if pr > 0 else "disabled"
        await interaction.response.send_message(
            f"Saved.  Welcome → {w_label}  ·  Ping → {p_label}  ·  Leave → {l_label}\n"
            "Use `/welcome_preview` or `/leave_preview` to check the templates.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Roles — select which role type, then open a modal
# ---------------------------------------------------------------------------


class _GreeterModal(discord.ui.Modal, title="Greeter Role Config"):
    def __init__(self, ctx: AppContext, current_channel_id: int) -> None:
        super().__init__()
        self._ctx = ctx
        self._current_channel_id = current_channel_id
        self.role_id: discord.ui.TextInput = discord.ui.TextInput(
            label="Greeter role ID  (right-click role → Copy ID)",
            default=str(ctx.greeter_role_id) if ctx.greeter_role_id > 0 else "",
            placeholder="Role ID or '0' to clear",
            required=False,
            max_length=25,
        )
        self.chat_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Greeter chat channel  (ID · 'here' · 'off')",
            default=str(ctx.greeter_chat_channel_id)
            if ctx.greeter_chat_channel_id > 0
            else "off",
            placeholder="Channel to ping @here when a new member joins",
            required=False,
            max_length=30,
        )
        self.add_item(self.role_id)
        self.add_item(self.chat_channel)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rid = _parse_role(self.role_id.value)
        gc = _parse_channel(self.chat_channel.value, self._current_channel_id)
        errors: list[str] = []
        if rid is None:
            errors.append(f"Invalid role ID: `{self.role_id.value}`")
        if gc is None:
            errors.append(f"Invalid greeter chat channel: `{self.chat_channel.value}`")
        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        assert rid is not None and gc is not None
        self._ctx.greeter_role_id = int(
            self._ctx.set_config_value("greeter_role_id", str(rid))
        )
        self._ctx.greeter_chat_channel_id = int(
            self._ctx.set_config_value("greeter_chat_channel_id", str(gc))
        )
        role_label = f"<@&{rid}>" if rid > 0 else "cleared"
        chat_label = f"<#{gc}>" if gc > 0 else "disabled"
        await interaction.response.send_message(
            f"Greeter role set to {role_label}. Greeter chat → {chat_label}.\n"
            "Members with this role can use `/grant`.",
            ephemeral=True,
        )


class _FullRoleModal(discord.ui.Modal):
    def __init__(
        self,
        ctx: AppContext,
        grant_name: str,
        current_channel_id: int,
        *,
        original: discord.Interaction | None = None,
        invoker_id: int = 0,
    ) -> None:
        cfg = ctx.grant_roles.get(grant_name)
        label = cfg["label"] if cfg else grant_name.title()
        super().__init__(title=f"{label} Role Config")
        self._ctx = ctx
        self._grant_name = grant_name
        self._label = label
        self._current_channel_id = current_channel_id
        self._original = original
        self._invoker_id = invoker_id

        self.role_id: discord.ui.TextInput = discord.ui.TextInput(
            label="Role ID  (right-click role → Copy ID)",
            default=str(cfg["role_id"]) if cfg and cfg["role_id"] > 0 else "",
            placeholder="Role ID or '0' to clear",
            required=False,
            max_length=25,
        )
        self.log_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Log channel  (ID · 'here' · 'off')",
            default=str(cfg["log_channel_id"])
            if cfg and cfg["log_channel_id"] > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.announce_channel: discord.ui.TextInput = discord.ui.TextInput(
            label="Announce channel  (ID · 'here' · 'off')",
            default=str(cfg["announce_channel_id"])
            if cfg and cfg["announce_channel_id"] > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.grant_message: discord.ui.TextInput = discord.ui.TextInput(
            label="Grant message template",
            style=discord.TextStyle.paragraph,
            default=cfg["grant_message"] if cfg else "",
            placeholder="{member} {member_name} {role} {role_name} {actor}",
            required=False,
            max_length=1000,
        )
        self.add_item(self.role_id)
        self.add_item(self.log_channel)
        self.add_item(self.announce_channel)
        self.add_item(self.grant_message)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rid = _parse_role(self.role_id.value)
        lc = _parse_channel(self.log_channel.value, self._current_channel_id)
        ac = _parse_channel(self.announce_channel.value, self._current_channel_id)
        errors: list[str] = []
        if rid is None:
            errors.append(f"Invalid role ID: `{self.role_id.value}`")
        if lc is None:
            errors.append(f"Invalid log channel: `{self.log_channel.value}`")
        if ac is None:
            errors.append(f"Invalid announce channel: `{self.announce_channel.value}`")
        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        assert rid is not None and lc is not None and ac is not None
        guild = interaction.guild
        if guild is None:
            return

        with self._ctx.open_db() as conn:
            upsert_grant_role(
                conn,
                guild.id,
                self._grant_name,
                label=self._label,
                role_id=rid,
                log_channel_id=lc,
                announce_channel_id=ac,
                grant_message=self.grant_message.value,
            )
        self._ctx.reload_grant_roles()

        if self._original:
            await interaction.response.defer()
            embed, view = _build_grant_role_panel(
                self._ctx,
                self._grant_name,
                self._original,
                self._invoker_id,
            )
            await self._original.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(
                f"{self._label} config saved.\n"
                f"Role: {'<@&' + str(rid) + '>' if rid else 'cleared'}  ·  "
                f"Log: {'<#' + str(lc) + '>' if lc else 'off'}  ·  "
                f"Announce: {'<#' + str(ac) + '>' if ac else 'off'}",
                ephemeral=True,
            )


def _parse_mention(text: str) -> tuple[str, int] | None:
    """Parse a role/user mention or raw ID. Returns (entity_type, entity_id) or None."""
    text = text.strip()
    m = re.match(r"<@!?(\d+)>", text)
    if m:
        return ("user", int(m.group(1)))
    m = re.match(r"<@&(\d+)>", text)
    if m:
        return ("role", int(m.group(1)))
    if text.isdigit():
        return ("unknown", int(text))
    return None


def _build_roles_embed(ctx: AppContext) -> discord.Embed:
    def _ch(val: int) -> str:
        return f"<#{val}>" if val > 0 else "—"

    def _role(val: int) -> str:
        return f"<@&{val}>" if val > 0 else "—"

    embed = discord.Embed(
        title="Role Grant Config", color=discord.Color.from_str("#57F287")
    )
    embed.add_field(
        name="Greeter", value=f"Role: {_role(ctx.greeter_role_id)}", inline=False
    )
    with ctx.open_db() as conn:
        for grant_name, cfg in ctx.grant_roles.items():
            perms = get_grant_permissions(conn, ctx.guild_id, grant_name)
            perm_str = (
                ", ".join(
                    f"<@&{eid}>" if et == "role" else f"<@{eid}>" for et, eid in perms
                )
                if perms
                else "mod-only"
            )
            embed.add_field(
                name=cfg["label"],
                value=(
                    f"Role: {_role(cfg['role_id'])}  ·  Log: {_ch(cfg['log_channel_id'])}  ·  "
                    f"Announce: {_ch(cfg['announce_channel_id'])}\n"
                    f"Granters: {perm_str}"
                ),
                inline=False,
            )
    embed.set_footer(text="Select a role type below to edit its settings.")
    return embed


# --- Grant role sub-panel (config + permissions for one role type) ---------


def _build_grant_role_panel(
    ctx: AppContext,
    grant_name: str,
    original: discord.Interaction,
    invoker_id: int,
) -> tuple[discord.Embed, _GrantRolePanel]:
    cfg = ctx.grant_roles.get(grant_name)
    label = cfg["label"] if cfg else grant_name.title()

    def _r(val: int) -> str:
        return f"<@&{val}>" if val > 0 else "not set"

    def _c(val: int) -> str:
        return f"<#{val}>" if val > 0 else "off"

    embed = discord.Embed(
        title=f"Config: {label}", color=discord.Color.from_str("#57F287")
    )
    embed.add_field(
        name="Role", value=_r(cfg["role_id"]) if cfg else "not set", inline=True
    )
    embed.add_field(
        name="Log", value=_c(cfg["log_channel_id"]) if cfg else "off", inline=True
    )
    embed.add_field(
        name="Announce",
        value=_c(cfg["announce_channel_id"]) if cfg else "off",
        inline=True,
    )

    guild = original.guild
    guild_id = guild.id if guild else 0
    with ctx.open_db() as conn:
        perms = get_grant_permissions(conn, guild_id, grant_name)
    if perms:
        entries = [f"<@&{eid}>" if et == "role" else f"<@{eid}>" for et, eid in perms]
        embed.add_field(name="Allowed granters", value=", ".join(entries), inline=False)
    else:
        embed.add_field(name="Allowed granters", value="Mods only", inline=False)

    embed.set_footer(text="Mods always have access.")

    current_channel_id = original.channel_id or 0
    view = _GrantRolePanel(
        ctx, grant_name, perms, original, invoker_id, current_channel_id
    )
    return embed, view


class _AddPermissionModal(discord.ui.Modal, title="Add Grant Permission"):
    def __init__(
        self,
        ctx: AppContext,
        grant_name: str,
        guild_id: int,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._grant_name = grant_name
        self._guild_id = guild_id
        self._original = original
        self._invoker_id = invoker_id
        self.target: discord.ui.TextInput = discord.ui.TextInput(
            label="User or role  (mention or ID)",
            placeholder="@user, @role, or a numeric ID",
            required=True,
            max_length=50,
        )
        self.add_item(self.target)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed = _parse_mention(self.target.value)
        if parsed is None:
            await interaction.response.send_message(
                f"Could not parse `{self.target.value}` as a user or role.",
                ephemeral=True,
            )
            return
        entity_type, entity_id = parsed
        guild = interaction.guild
        if entity_type == "unknown" and guild:
            entity_type = "role" if guild.get_role(entity_id) else "user"
        with self._ctx.open_db() as conn:
            add_grant_permission(
                conn, self._guild_id, self._grant_name, entity_type, entity_id
            )
        await interaction.response.defer()
        embed, view = _build_grant_role_panel(
            self._ctx,
            self._grant_name,
            self._original,
            self._invoker_id,
        )
        await self._original.edit_original_response(embed=embed, view=view)


class _RemovePermissionSelect(discord.ui.Select):
    def __init__(
        self,
        ctx: AppContext,
        grant_name: str,
        guild_id: int,
        permissions: list[tuple[str, int]],
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        self._ctx = ctx
        self._grant_name = grant_name
        self._guild_id = guild_id
        self._original = original
        self._invoker_id = invoker_id
        options = [
            discord.SelectOption(
                label=f"{'Role' if et == 'role' else 'User'}: {eid}",
                value=f"{et}:{eid}",
            )
            for et, eid in permissions
        ]
        super().__init__(placeholder="Remove a permission…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        entity_type, entity_id_str = self.values[0].split(":", 1)
        with self._ctx.open_db() as conn:
            remove_grant_permission(
                conn,
                self._guild_id,
                self._grant_name,
                entity_type,
                int(entity_id_str),
            )
        await interaction.response.defer()
        embed, view = _build_grant_role_panel(
            self._ctx,
            self._grant_name,
            self._original,
            self._invoker_id,
        )
        await self._original.edit_original_response(embed=embed, view=view)


class _GrantRolePanel(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        grant_name: str,
        permissions: list[tuple[str, int]],
        original: discord.Interaction,
        invoker_id: int,
        current_channel_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._grant_name = grant_name
        self._original = original
        self._invoker_id = invoker_id
        self._current_channel_id = current_channel_id

        back_btn: discord.ui.Button[_GrantRolePanel] = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        back_btn.callback = self._on_back  # type: ignore[method-assign]
        self.add_item(back_btn)

        edit_btn: discord.ui.Button[_GrantRolePanel] = discord.ui.Button(
            label="Edit Config",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        edit_btn.callback = self._on_edit  # type: ignore[method-assign]
        self.add_item(edit_btn)

        add_btn: discord.ui.Button[_GrantRolePanel] = discord.ui.Button(
            label="Add Permission",
            style=discord.ButtonStyle.success,
            row=0,
        )
        add_btn.callback = self._on_add  # type: ignore[method-assign]
        self.add_item(add_btn)

        remove_btn: discord.ui.Button[_GrantRolePanel] = discord.ui.Button(
            label="Remove Role",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        remove_btn.callback = self._on_remove_role  # type: ignore[method-assign]
        self.add_item(remove_btn)

        if permissions:
            guild_id = original.guild.id if original.guild else 0
            self.add_item(
                _RemovePermissionSelect(
                    ctx,
                    grant_name,
                    guild_id,
                    permissions,
                    original,
                    invoker_id,
                )
            )

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        embed = _build_roles_embed(self._ctx)
        view = _RolesView(
            self._ctx,
            self._invoker_id,
            self._current_channel_id,
            self._original,
        )
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _FullRoleModal(
                self._ctx,
                self._grant_name,
                self._current_channel_id,
                original=self._original,
                invoker_id=self._invoker_id,
            )
        )

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        guild_id = interaction.guild.id if interaction.guild else 0
        await interaction.response.send_modal(
            _AddPermissionModal(
                self._ctx,
                self._grant_name,
                guild_id,
                self._original,
                self._invoker_id,
            )
        )

    async def _on_remove_role(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        guild = interaction.guild
        if guild is None:
            return
        with self._ctx.open_db() as conn:
            delete_grant_role(conn, guild.id, self._grant_name)
        self._ctx.reload_grant_roles()
        await interaction.response.defer()
        embed = _build_roles_embed(self._ctx)
        view = _RolesView(
            self._ctx,
            self._invoker_id,
            self._current_channel_id,
            self._original,
        )
        await self._original.edit_original_response(embed=embed, view=view)


class _AddGrantRoleModal(discord.ui.Modal, title="Add Grant Role"):
    def __init__(
        self,
        ctx: AppContext,
        original: discord.Interaction,
        invoker_id: int,
        current_channel_id: int,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._original = original
        self._invoker_id = invoker_id
        self._current_channel_id = current_channel_id
        self.role_key: discord.ui.TextInput = discord.ui.TextInput(
            label="Key  (short lowercase identifier)",
            placeholder="e.g. denizen, nsfw, veteran",
            required=True,
            max_length=30,
        )
        self.role_label: discord.ui.TextInput = discord.ui.TextInput(
            label="Display name",
            placeholder="e.g. Denizen, NSFW, Veteran",
            required=True,
            max_length=50,
        )
        self.add_item(self.role_key)
        self.add_item(self.role_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        key = self.role_key.value.strip().lower().replace(" ", "_")
        label = self.role_label.value.strip()
        if not key or not label:
            await interaction.response.send_message(
                "Key and label are required.", ephemeral=True
            )
            return
        if key == "greeter":
            await interaction.response.send_message(
                "'greeter' is reserved.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            return
        with self._ctx.open_db() as conn:
            upsert_grant_role(
                conn,
                guild.id,
                key,
                label=label,
                role_id=0,
                log_channel_id=0,
                announce_channel_id=0,
                grant_message="",
            )
        self._ctx.reload_grant_roles()
        await interaction.response.defer()
        embed = _build_roles_embed(self._ctx)
        view = _RolesView(
            self._ctx, self._invoker_id, self._current_channel_id, self._original
        )
        await self._original.edit_original_response(embed=embed, view=view)


class _RoleTypeSelect(discord.ui.Select):
    def __init__(
        self,
        ctx: AppContext,
        invoker_id: int,
        current_channel_id: int,
        original: discord.Interaction,
    ) -> None:
        self._ctx = ctx
        self.invoker_id = invoker_id
        self._current_channel_id = current_channel_id
        self._original = original
        options = [
            discord.SelectOption(
                label="Greeter",
                value="greeter",
                description="Who can use grant commands",
            ),
        ]
        for grant_name, cfg in ctx.grant_roles.items():
            options.append(
                discord.SelectOption(
                    label=cfg["label"],
                    value=grant_name,
                    description="Role, permissions, channels, message",
                )
            )
        super().__init__(
            placeholder="Choose a role type to configure…", options=options[:25]
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        role_type = self.values[0]
        if role_type == "greeter":
            await interaction.response.send_modal(
                _GreeterModal(self._ctx, self._current_channel_id)
            )
        else:
            embed, view = _build_grant_role_panel(
                self._ctx,
                role_type,
                self._original,
                self.invoker_id,
            )
            await interaction.response.edit_message(embed=embed, view=view)


class _RolesView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        invoker_id: int,
        current_channel_id: int,
        original: discord.Interaction,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._invoker_id = invoker_id
        self._current_channel_id = current_channel_id
        self._original = original
        self.add_item(_RoleTypeSelect(ctx, invoker_id, current_channel_id, original))

        add_btn: discord.ui.Button[_RolesView] = discord.ui.Button(
            label="Add Role",
            style=discord.ButtonStyle.success,
            row=1,
        )
        add_btn.callback = self._on_add  # type: ignore[method-assign]
        self.add_item(add_btn)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _AddGrantRoleModal(
                self._ctx,
                self._original,
                self._invoker_id,
                self._current_channel_id,
            )
        )


# ---------------------------------------------------------------------------
# XP — log channels modal + current-channel XP toggle buttons
# ---------------------------------------------------------------------------


class _XpLogModal(discord.ui.Modal, title="XP Log Channels"):
    def __init__(self, ctx: AppContext, current_channel_id: int) -> None:
        super().__init__()
        self._ctx = ctx
        self._current_channel_id = current_channel_id

        self.levelup: discord.ui.TextInput = discord.ui.TextInput(
            label="Level-up log channel  (ID · 'here' · 'off')",
            default=str(ctx.level_up_log_channel_id)
            if ctx.level_up_log_channel_id > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.level5: discord.ui.TextInput = discord.ui.TextInput(
            label="Level-5 log channel  (ID · 'here' · 'off')",
            default=str(ctx.level_5_log_channel_id)
            if ctx.level_5_log_channel_id > 0
            else "off",
            required=False,
            max_length=30,
        )
        self.add_item(self.levelup)
        self.add_item(self.level5)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        luc = _parse_channel(self.levelup.value, self._current_channel_id)
        l5c = _parse_channel(self.level5.value, self._current_channel_id)
        errors: list[str] = []
        if luc is None:
            errors.append(f"Invalid level-up log channel: `{self.levelup.value}`")
        if l5c is None:
            errors.append(f"Invalid level-5 log channel: `{self.level5.value}`")
        if errors:
            await interaction.response.send_message("\n".join(errors), ephemeral=True)
            return

        assert luc is not None and l5c is not None
        self._ctx.level_up_log_channel_id = int(
            self._ctx.set_config_value("xp_level_up_log_channel_id", str(luc))
        )
        self._ctx.level_5_log_channel_id = int(
            self._ctx.set_config_value("xp_level_5_log_channel_id", str(l5c))
        )
        await interaction.response.send_message(
            f"XP log channels saved.\n"
            f"Level-up: {'<#' + str(luc) + '>' if luc else 'off'}  ·  "
            f"Level-5: {'<#' + str(l5c) + '>' if l5c else 'off'}",
            ephemeral=True,
        )


def _build_xp_embed(
    ctx: AppContext, guild: discord.Guild, current_channel_id: int
) -> discord.Embed:
    luc = ctx.level_up_log_channel_id
    l5c = ctx.level_5_log_channel_id
    excluded = current_channel_id in ctx.xp_excluded_channel_ids
    embed = discord.Embed(
        title="🔧  XP Config", color=discord.Color.from_str("#2ECC71")
    )
    embed.add_field(
        name="Log Channels",
        value=(
            f"Level-up log: {'<#' + str(luc) + '>' if luc else '—'}\n"
            f"Level-{DEFAULT_XP_SETTINGS.role_grant_level} log: {'<#' + str(l5c) + '>' if l5c else '—'}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Current Channel",
        value=f"XP **{'excluded' if excluded else 'active'}** in <#{current_channel_id}>",
        inline=False,
    )
    if ctx.xp_grant_allowed_user_ids:
        labels = []
        for uid in sorted(ctx.xp_grant_allowed_user_ids):
            m = guild.get_member(uid)
            labels.append(m.mention if m else f"`{uid}`")
        allowlist_value = ", ".join(labels)
    else:
        allowlist_value = "Mods only"
    embed.add_field(name="Grant Allowlist", value=allowlist_value, inline=False)
    embed.set_footer(text="Use the buttons below to edit.")
    return embed


class _XpAllowlistModal(discord.ui.Modal, title="XP Grant Allowlist"):
    """Add or remove users from the /xp_give allowlist by ID."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self._ctx = ctx
        self.add_ids: discord.ui.TextInput = discord.ui.TextInput(
            label="Add user IDs (space or comma-separated)",
            placeholder="Right-click member → Copy ID",
            required=False,
            max_length=500,
        )
        self.remove_ids: discord.ui.TextInput = discord.ui.TextInput(
            label="Remove user IDs (space or comma-separated)",
            placeholder="Right-click member → Copy ID",
            required=False,
            max_length=500,
        )
        self.add_item(self.add_ids)
        self.add_item(self.remove_ids)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re

        def _parse(text: str) -> list[int]:
            return [
                int(tok) for tok in re.split(r"[\s,]+", text.strip()) if tok.isdigit()
            ]

        added: list[int] = []
        removed: list[int] = []
        for uid in _parse(self.add_ids.value):
            self._ctx.xp_grant_allowed_user_ids = self._ctx.add_config_id_value(
                "xp_grant_allowed_user_ids", uid
            )
            added.append(uid)
        for uid in _parse(self.remove_ids.value):
            self._ctx.xp_grant_allowed_user_ids = self._ctx.remove_config_id_value(
                "xp_grant_allowed_user_ids", uid
            )
            removed.append(uid)

        parts: list[str] = []
        if added:
            parts.append(f"Added: {', '.join(f'`{uid}`' for uid in added)}")
        if removed:
            parts.append(f"Removed: {', '.join(f'`{uid}`' for uid in removed)}")
        if not parts:
            parts.append("No changes made.")
        await interaction.response.send_message("\n".join(parts), ephemeral=True)


class _XpView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        invoker_id: int,
        guild: discord.Guild,
        current_channel_id: int,
        original_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self.invoker_id = invoker_id
        self._guild = guild
        self._current_channel_id = current_channel_id
        self._original = original_interaction

        excluded = current_channel_id in ctx.xp_excluded_channel_ids
        self.log_btn: discord.ui.Button = discord.ui.Button(
            label="Set Log Channels", style=discord.ButtonStyle.primary
        )
        self.log_btn.callback = self._on_log  # type: ignore[method-assign]
        self.add_item(self.log_btn)

        self.toggle_btn: discord.ui.Button = discord.ui.Button(
            label="Exclude this channel" if not excluded else "Include this channel",
            style=discord.ButtonStyle.danger
            if not excluded
            else discord.ButtonStyle.success,
        )
        self.toggle_btn.callback = self._on_toggle  # type: ignore[method-assign]
        self.add_item(self.toggle_btn)

        self.allowlist_btn: discord.ui.Button = discord.ui.Button(
            label="Manage Grant Allowlist", style=discord.ButtonStyle.secondary
        )
        self.allowlist_btn.callback = self._on_allowlist  # type: ignore[method-assign]
        self.add_item(self.allowlist_btn)

    async def _on_allowlist(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(_XpAllowlistModal(self._ctx))

    async def _on_log(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _XpLogModal(self._ctx, self._current_channel_id)
        )

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        excluded = self._current_channel_id in self._ctx.xp_excluded_channel_ids
        if excluded:
            self._ctx.xp_excluded_channel_ids = self._ctx.remove_config_id_value(
                "xp_excluded_channel_ids", self._current_channel_id
            )
        else:
            self._ctx.xp_excluded_channel_ids = self._ctx.add_config_id_value(
                "xp_excluded_channel_ids", self._current_channel_id
            )
        await interaction.response.defer()
        embed = _build_xp_embed(self._ctx, self._guild, self._current_channel_id)
        new_excluded = self._current_channel_id in self._ctx.xp_excluded_channel_ids
        self.toggle_btn.label = (
            "Include this channel" if new_excluded else "Exclude this channel"
        )
        self.toggle_btn.style = (
            discord.ButtonStyle.success if new_excluded else discord.ButtonStyle.danger
        )
        await self._original.edit_original_response(embed=embed, view=self)


# ---------------------------------------------------------------------------
# Prune — setup modal + status/disable/run panel
# ---------------------------------------------------------------------------


class _PruneSetupModal(discord.ui.Modal, title="Inactivity Prune Setup"):
    def __init__(
        self,
        ctx: AppContext,
        guild_id: int,
        original_interaction: discord.Interaction,
        invoker_id: int,
        current_role_id: int = 0,
        current_days: int = 30,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._guild_id = guild_id
        self._original = original_interaction
        self._invoker_id = invoker_id

        self.role_id: discord.ui.TextInput = discord.ui.TextInput(
            label="Role ID to prune  (right-click role → Copy ID)",
            default=str(current_role_id) if current_role_id > 0 else "",
            placeholder="Role ID",
            required=True,
            max_length=25,
        )
        self.days: discord.ui.TextInput = discord.ui.TextInput(
            label="Inactivity threshold (days)",
            default=str(current_days),
            placeholder="e.g. 30",
            required=True,
            max_length=5,
        )
        self.add_item(self.role_id)
        self.add_item(self.days)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        rid = _parse_role(self.role_id.value)
        if not rid:
            await interaction.response.send_message(
                f"Invalid role ID: `{self.role_id.value}`", ephemeral=True
            )
            return
        try:
            days = int(self.days.value.strip())
            if days < 1 or days > 365:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "Days must be a whole number between 1 and 365.", ephemeral=True
            )
            return

        upsert_prune_rule(self._ctx.db_path, self._guild_id, rid, days)
        await interaction.response.defer()
        guild = interaction.guild
        assert guild is not None
        embed, view = _build_prune_panel(
            self._ctx, guild, self._original, self._invoker_id
        )
        await self._original.edit_original_response(embed=embed, view=view)


def _build_prune_panel(
    ctx: AppContext,
    guild: discord.Guild,
    original_interaction: discord.Interaction,
    invoker_id: int,
) -> tuple[discord.Embed, discord.ui.View]:
    rule = get_prune_rule(ctx.db_path, guild.id)
    embed = discord.Embed(
        title="✂️  Inactivity Prune", color=discord.Color.from_str("#E67E22")
    )
    if rule:
        role = guild.get_role(int(rule["role_id"]))
        role_label = (
            f"<@&{rule['role_id']}>" if role else f"<deleted role {rule['role_id']}>"
        )
        embed.add_field(name="Role", value=role_label, inline=True)
        embed.add_field(
            name="Threshold", value=f"{rule['inactivity_days']} days", inline=True
        )
        embed.add_field(name="Schedule", value="Daily at midnight UTC", inline=True)
    else:
        embed.description = "No prune rule configured."
    embed.set_footer(text="Use the buttons below to configure.")
    view = _PruneView(ctx, guild, invoker_id, original_interaction, rule)
    return embed, view


class _PruneView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        invoker_id: int,
        original_interaction: discord.Interaction,
        rule: object,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._guild = guild
        self.invoker_id = invoker_id
        self._original = original_interaction
        self._rule = rule

        self.setup_btn: discord.ui.Button = discord.ui.Button(
            label="Set Up / Edit", style=discord.ButtonStyle.primary
        )
        self.setup_btn.callback = self._on_setup  # type: ignore[method-assign]
        self.add_item(self.setup_btn)

        self.disable_btn: discord.ui.Button = discord.ui.Button(
            label="Disable", style=discord.ButtonStyle.danger, disabled=(rule is None)
        )
        self.disable_btn.callback = self._on_disable  # type: ignore[method-assign]
        self.add_item(self.disable_btn)

        self.run_btn: discord.ui.Button = discord.ui.Button(
            label="Run Now",
            style=discord.ButtonStyle.secondary,
            disabled=(rule is None),
        )
        self.run_btn.callback = self._on_run  # type: ignore[method-assign]
        self.add_item(self.run_btn)

    async def _on_setup(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        rule = self._rule
        current_role = int(rule["role_id"]) if rule else 0  # type: ignore[index]
        current_days = int(rule["inactivity_days"]) if rule else 30  # type: ignore[index]
        await interaction.response.send_modal(
            _PruneSetupModal(
                self._ctx,
                self._guild.id,
                self._original,
                self.invoker_id,
                current_role,
                current_days,
            )
        )

    async def _on_disable(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        remove_prune_rule(self._ctx.db_path, self._guild.id)
        await interaction.response.defer()
        embed, view = _build_prune_panel(
            self._ctx, self._guild, self._original, self.invoker_id
        )
        await self._original.edit_original_response(embed=embed, view=view)

    async def _on_run(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        rule = self._rule
        if rule is None:
            await interaction.response.defer()
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await run_prune_for_guild(
            self._ctx.bot,
            self._ctx.db_path,
            self._guild.id,
            int(rule["role_id"]),  # type: ignore[index]
            int(rule["inactivity_days"]),  # type: ignore[index]
        )
        await interaction.followup.send("Inactivity prune completed.", ephemeral=True)


# ---------------------------------------------------------------------------
# Spoiler — channel list + toggle buttons
# ---------------------------------------------------------------------------


def _build_spoiler_embed(
    ctx: AppContext, guild: discord.Guild, current_channel_id: int
) -> discord.Embed:
    embed = discord.Embed(
        title="🛡️  Spoiler Guard", color=discord.Color.from_str("#E74C3C")
    )
    if ctx.spoiler_required_channels:
        labels = []
        for cid in sorted(ctx.spoiler_required_channels):
            ch = get_guild_channel_or_thread(guild, cid)
            labels.append(ch.mention if ch else f"`{cid}`")
        embed.add_field(name="Guarded channels", value="\n".join(labels), inline=False)
    else:
        embed.description = "No channels are currently under spoiler guard."
    guarded = current_channel_id in ctx.spoiler_required_channels
    embed.add_field(
        name="Current channel",
        value=f"<#{current_channel_id}> is **{'guarded' if guarded else 'not guarded'}**",
        inline=False,
    )
    embed.set_footer(text="Use the buttons below to toggle the current channel.")
    return embed


class _SpoilerView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        invoker_id: int,
        current_channel_id: int,
        original_interaction: discord.Interaction,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._guild = guild
        self.invoker_id = invoker_id
        self._current_channel_id = current_channel_id
        self._original = original_interaction

        guarded = current_channel_id in ctx.spoiler_required_channels
        self.guard_btn: discord.ui.Button = discord.ui.Button(
            label="Guard this channel",
            style=discord.ButtonStyle.danger,
            disabled=guarded,
        )
        self.guard_btn.callback = self._on_guard  # type: ignore[method-assign]
        self.add_item(self.guard_btn)

        self.unguard_btn: discord.ui.Button = discord.ui.Button(
            label="Unguard this channel",
            style=discord.ButtonStyle.secondary,
            disabled=not guarded,
        )
        self.unguard_btn.callback = self._on_unguard  # type: ignore[method-assign]
        self.add_item(self.unguard_btn)

    async def _do_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        guarded = self._current_channel_id in self._ctx.spoiler_required_channels
        self.guard_btn.disabled = guarded
        self.unguard_btn.disabled = not guarded
        embed = _build_spoiler_embed(self._ctx, self._guild, self._current_channel_id)
        await self._original.edit_original_response(embed=embed, view=self)

    async def _on_guard(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        self._ctx.spoiler_required_channels = self._ctx.add_config_id_value(
            "spoiler_required_channels", self._current_channel_id
        )
        await self._do_refresh(interaction)

    async def _on_unguard(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.defer()
            return
        self._ctx.spoiler_required_channels = self._ctx.remove_config_id_value(
            "spoiler_required_channels", self._current_channel_id
        )
        await self._do_refresh(interaction)


# ---------------------------------------------------------------------------
# Booster Roles — cosmetic role picker for server boosters
# ---------------------------------------------------------------------------


def _build_booster_overview(
    ctx: AppContext,
    guild: discord.Guild,
    original: discord.Interaction,
) -> tuple[discord.Embed, _BoosterConfigView]:
    from services.booster_roles import get_booster_roles, get_swatch_directory

    with ctx.open_db() as conn:
        roles = get_booster_roles(conn, guild.id)
    swatch_dir = get_swatch_directory(ctx.db_path)

    embed = discord.Embed(
        title="Booster Cosmetic Roles", color=discord.Color.from_str("#F47FFF")
    )
    embed.add_field(
        name="Swatch directory",
        value=f"`{swatch_dir}`" if swatch_dir else "*not set*",
        inline=False,
    )
    if roles:
        for r in roles:
            role_str = f"<@&{r['role_id']}>" if r["role_id"] > 0 else "not set"
            img_str = r["image_path"] or "none"
            embed.add_field(
                name=r["label"],
                value=f"Role: {role_str}\nImage: `{img_str}`",
                inline=True,
            )
    else:
        embed.description = "No booster roles configured yet."
    embed.set_footer(text="Sync swatches to auto-create roles from image files.")

    view = _BoosterConfigView(ctx, roles, guild, original, original.user.id)
    return embed, view


class _AddBoosterRoleModal(discord.ui.Modal, title="Add Booster Role"):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._guild = guild
        self._original = original
        self._invoker_id = invoker_id

        self.role_key: discord.ui.TextInput = discord.ui.TextInput(
            label="Key  (short lowercase identifier)",
            placeholder="e.g. ruby, sapphire, emerald",
            required=True,
            max_length=30,
        )
        self.role_label: discord.ui.TextInput = discord.ui.TextInput(
            label="Display name",
            placeholder="e.g. Ruby, Sapphire, Emerald",
            required=True,
            max_length=50,
        )
        self.role_id_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Role ID  (right-click role → Copy ID)",
            placeholder="Role ID",
            required=True,
            max_length=25,
        )
        self.image_path: discord.ui.TextInput = discord.ui.TextInput(
            label="Image file path  (absolute path on server)",
            placeholder="/path/to/image.png",
            required=False,
            max_length=200,
        )
        self.add_item(self.role_key)
        self.add_item(self.role_label)
        self.add_item(self.role_id_input)
        self.add_item(self.image_path)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.booster_roles import get_booster_roles, upsert_booster_role

        key = self.role_key.value.strip().lower().replace(" ", "_")
        label = self.role_label.value.strip()
        if not key or not label:
            await interaction.response.send_message(
                "Key and label are required.", ephemeral=True
            )
            return

        rid = _parse_role(self.role_id_input.value)
        if rid is None:
            await interaction.response.send_message(
                f"Invalid role ID: `{self.role_id_input.value}`",
                ephemeral=True,
            )
            return

        with self._ctx.open_db() as conn:
            existing = get_booster_roles(conn, self._guild.id)
            if len(existing) >= 10 and not any(r["role_key"] == key for r in existing):
                await interaction.response.send_message(
                    "Maximum 10 booster roles allowed.",
                    ephemeral=True,
                )
                return
            sort_order = max((r["sort_order"] for r in existing), default=-1) + 1
            upsert_booster_role(
                conn,
                self._guild.id,
                key,
                label=label,
                role_id=rid,
                image_path=self.image_path.value.strip(),
                sort_order=sort_order,
            )

        await interaction.response.defer()
        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await self._original.edit_original_response(embed=embed, view=view)


class _EditBoosterRoleModal(discord.ui.Modal, title="Edit Booster Role"):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        role_key: str,
        current: BoosterRoleRow,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._guild = guild
        self._role_key = role_key
        self._original = original
        self._invoker_id = invoker_id

        self.role_label: discord.ui.TextInput = discord.ui.TextInput(
            label="Display name",
            default=current["label"],
            required=True,
            max_length=50,
        )
        self.role_id_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Role ID",
            default=str(current["role_id"]) if current["role_id"] > 0 else "",
            required=True,
            max_length=25,
        )
        self.image_path: discord.ui.TextInput = discord.ui.TextInput(
            label="Image file path",
            default=current["image_path"],
            required=False,
            max_length=200,
        )
        self.sort_order_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Sort order  (0 = first)",
            default=str(current["sort_order"]),
            required=False,
            max_length=5,
        )
        self.add_item(self.role_label)
        self.add_item(self.role_id_input)
        self.add_item(self.image_path)
        self.add_item(self.sort_order_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from services.booster_roles import upsert_booster_role

        rid = _parse_role(self.role_id_input.value)
        if rid is None:
            await interaction.response.send_message(
                f"Invalid role ID: `{self.role_id_input.value}`",
                ephemeral=True,
            )
            return

        try:
            sort_order = int(self.sort_order_input.value.strip() or "0")
        except ValueError:
            sort_order = 0

        with self._ctx.open_db() as conn:
            upsert_booster_role(
                conn,
                self._guild.id,
                self._role_key,
                label=self.role_label.value.strip(),
                role_id=rid,
                image_path=self.image_path.value.strip(),
                sort_order=sort_order,
            )

        await interaction.response.defer()
        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await self._original.edit_original_response(embed=embed, view=view)


if TYPE_CHECKING:
    from services.booster_roles import BoosterRoleRow


class _BoosterRoleSelect(discord.ui.Select):
    def __init__(
        self,
        ctx: AppContext,
        roles: list[BoosterRoleRow],
        guild: discord.Guild,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        self._ctx = ctx
        self._roles = {r["role_key"]: r for r in roles}
        self._guild = guild
        self._original = original
        self._invoker_id = invoker_id
        options = [
            discord.SelectOption(label=r["label"], value=r["role_key"]) for r in roles
        ]
        super().__init__(placeholder="Select a role to edit…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        key = self.values[0]
        role = self._roles.get(key)
        if role is None:
            await interaction.response.defer()
            return
        embed = discord.Embed(
            title=f"Booster Role: {role['label']}",
            color=discord.Color.from_str("#F47FFF"),
        )
        role_str = f"<@&{role['role_id']}>" if role["role_id"] > 0 else "not set"
        embed.add_field(name="Role", value=role_str, inline=True)
        embed.add_field(
            name="Image",
            value=f"`{role['image_path']}`" if role["image_path"] else "none",
            inline=True,
        )
        embed.add_field(name="Sort order", value=str(role["sort_order"]), inline=True)
        view = _BoosterRoleDetailView(
            self._ctx,
            self._guild,
            key,
            role,
            self._original,
            self._invoker_id,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class _BoosterRoleDetailView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        role_key: str,
        role: BoosterRoleRow,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._guild = guild
        self._role_key = role_key
        self._role = role
        self._original = original
        self._invoker_id = invoker_id

        back_btn: discord.ui.Button[_BoosterRoleDetailView] = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        back_btn.callback = self._on_back  # type: ignore[method-assign]
        self.add_item(back_btn)

        edit_btn: discord.ui.Button[_BoosterRoleDetailView] = discord.ui.Button(
            label="Edit",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        edit_btn.callback = self._on_edit  # type: ignore[method-assign]
        self.add_item(edit_btn)

        remove_btn: discord.ui.Button[_BoosterRoleDetailView] = discord.ui.Button(
            label="Remove",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        remove_btn.callback = self._on_remove  # type: ignore[method-assign]
        self.add_item(remove_btn)

    async def _on_back(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _on_edit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _EditBoosterRoleModal(
                self._ctx,
                self._guild,
                self._role_key,
                self._role,
                self._original,
                self._invoker_id,
            )
        )

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        from services.booster_roles import delete_booster_role

        with self._ctx.open_db() as conn:
            delete_booster_role(conn, self._guild.id, self._role_key)
        await interaction.response.defer()
        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await self._original.edit_original_response(embed=embed, view=view)


class _SwatchPathModal(discord.ui.Modal, title="Set Swatch Directory"):
    def __init__(
        self,
        ctx: AppContext,
        guild: discord.Guild,
        original: discord.Interaction,
    ) -> None:
        super().__init__()
        self._ctx = ctx
        self._guild = guild
        self._original = original

        from services.booster_roles import get_swatch_directory

        current = get_swatch_directory(ctx.db_path)

        self.path_input: discord.ui.TextInput = discord.ui.TextInput(
            label="Absolute path to swatch image directory",
            placeholder="/path/to/swatches",
            default=current or "",
            required=True,
            max_length=300,
        )
        self.add_item(self.path_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._ctx.set_config_value("booster_swatch_dir", self.path_input.value.strip())
        await interaction.response.defer()
        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await self._original.edit_original_response(embed=embed, view=view)


class _BoosterConfigView(discord.ui.View):
    def __init__(
        self,
        ctx: AppContext,
        roles: list[BoosterRoleRow],
        guild: discord.Guild,
        original: discord.Interaction,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._ctx = ctx
        self._guild = guild
        self._original = original
        self._invoker_id = invoker_id

        if roles:
            self.add_item(_BoosterRoleSelect(ctx, roles, guild, original, invoker_id))

        add_btn: discord.ui.Button[_BoosterConfigView] = discord.ui.Button(
            label="Add Role",
            style=discord.ButtonStyle.success,
            row=1,
        )
        add_btn.callback = self._on_add  # type: ignore[method-assign]
        self.add_item(add_btn)

        path_btn: discord.ui.Button[_BoosterConfigView] = discord.ui.Button(
            label="Set Swatch Path",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        path_btn.callback = self._on_set_path  # type: ignore[method-assign]
        self.add_item(path_btn)

        sync_btn: discord.ui.Button[_BoosterConfigView] = discord.ui.Button(
            label="Sync Swatches",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        sync_btn.callback = self._on_sync  # type: ignore[method-assign]
        self.add_item(sync_btn)

        post_btn: discord.ui.Button[_BoosterConfigView] = discord.ui.Button(
            label="Post / Update Panel",
            style=discord.ButtonStyle.primary,
            row=2,
        )
        post_btn.callback = self._on_post  # type: ignore[method-assign]
        self.add_item(post_btn)

    async def _on_add(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _AddBoosterRoleModal(
                self._ctx,
                self._guild,
                self._original,
                self._invoker_id,
            )
        )

    async def _on_set_path(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        await interaction.response.send_modal(
            _SwatchPathModal(self._ctx, self._guild, self._original),
        )

    async def _on_sync(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        from services.booster_roles import sync_swatches

        await interaction.response.defer(ephemeral=True)
        try:
            created, removed = await sync_swatches(self._ctx.db_path, self._guild)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        parts: list[str] = []
        if created:
            parts.append(f"**Created:** {', '.join(created)}")
        if removed:
            parts.append(f"**Removed:** {', '.join(removed)}")
        if not parts:
            parts.append("Everything is already in sync.")
        await interaction.followup.send("\n".join(parts), ephemeral=True)

        embed, view = _build_booster_overview(self._ctx, self._guild, self._original)
        await self._original.edit_original_response(embed=embed, view=view)

    async def _on_post(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self._invoker_id:
            await interaction.response.defer()
            return
        from services.booster_roles import post_or_update_booster_panel

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Run this in the text channel where the panel should appear.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        msgs = await post_or_update_booster_panel(
            self._ctx.db_path, self._guild, channel
        )
        if not msgs:
            await interaction.followup.send(
                "No booster roles configured.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Panel posted/updated in {channel.mention} ({len(msgs)} messages).",
                ephemeral=True,
            )


# ---------------------------------------------------------------------------
# /config command
# ---------------------------------------------------------------------------

_SECTION_CHOICES = [
    app_commands.Choice(name="Global", value="global"),
    app_commands.Choice(name="Welcome & Leave", value="welcome"),
    app_commands.Choice(name="Role Grants", value="roles"),
    app_commands.Choice(name="XP Logging", value="xp"),
    app_commands.Choice(name="Inactivity Prune", value="prune"),
    app_commands.Choice(name="Spoiler Guard", value="spoiler"),
    app_commands.Choice(name="Booster Roles", value="booster"),
]


def register_config_commands(bot: Bot, ctx: AppContext) -> None:

    @bot.tree.command(
        name="config",
        description="Open the settings panel for a bot feature.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(section="Feature to configure.")
    @app_commands.choices(section=_SECTION_CHOICES)
    async def config_cmd(
        interaction: discord.Interaction,
        section: str,
    ) -> None:
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        current_channel_id: int = interaction.channel_id or 0

        if section == "global":
            await interaction.response.send_modal(_GlobalModal(ctx, current_channel_id))

        elif section == "welcome":
            await interaction.response.send_modal(
                _WelcomeLeaveModal(ctx, current_channel_id)
            )

        elif section == "roles":
            await interaction.response.defer(ephemeral=True)
            await interaction.followup.send(
                embed=_build_roles_embed(ctx),
                view=_RolesView(
                    ctx, interaction.user.id, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "xp":
            await interaction.response.send_message(
                embed=_build_xp_embed(ctx, guild, current_channel_id),
                view=_XpView(
                    ctx, interaction.user.id, guild, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "prune":
            await interaction.response.defer(ephemeral=True)
            prune_embed, prune_view = _build_prune_panel(
                ctx, guild, interaction, interaction.user.id
            )
            await interaction.followup.send(
                embed=prune_embed, view=prune_view, ephemeral=True
            )

        elif section == "spoiler":
            await interaction.response.send_message(
                embed=_build_spoiler_embed(ctx, guild, current_channel_id),
                view=_SpoilerView(
                    ctx, guild, interaction.user.id, current_channel_id, interaction
                ),
                ephemeral=True,
            )

        elif section == "booster":
            await interaction.response.defer(ephemeral=True)
            embed, view = _build_booster_overview(ctx, guild, interaction)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
