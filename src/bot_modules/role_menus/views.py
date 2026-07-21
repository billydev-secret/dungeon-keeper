"""Persistent components + interaction handling for role menus.

Buttons and the dropdown are ``DynamicItem``s whose custom_ids carry the menu
and *role* ids (``rolemenu:btn:{menu_id}:{role_id}`` / ``rolemenu:sel:
{menu_id}``) — role ids are stable across option edits, so a click on a
slightly stale message still resolves to the right role or degrades politely.
State is rebuilt from the custom_id on every click, which is what makes the
menus survive restarts.

Every response here is ephemeral (spec §4: nothing public, ever, from a
member's click). Config-drift failures (role deleted, hierarchy above the bot)
apologise to the member and alert the mods **once** per menu via the
``alerted_at`` stamp.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.utils import get_bot_member
from bot_modules.role_menus import db as menus_db
from bot_modules.role_menus.logic import (
    ERR_AT_CAP,
    ERR_NO_CHANGE,
    ERR_PERMANENT,
    CooldownGate,
    Outcome,
    resolve_click,
    resolve_selection,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.role_menus")

_BUTTON_STYLES = {
    "secondary": discord.ButtonStyle.secondary,
    "primary": discord.ButtonStyle.primary,
    "success": discord.ButtonStyle.success,
    "danger": discord.ButtonStyle.danger,
}

_cooldowns = CooldownGate()

MSG_BROKEN = "Sorry — something went wrong on our side. The mods have been alerted."


def _parse_emoji(raw: str) -> discord.PartialEmoji | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return discord.PartialEmoji.from_str(raw)
    except ValueError:
        return None


def build_view(menu: dict, options: list[dict]) -> discord.ui.View:
    """Build the live view for a published menu message."""
    view = discord.ui.View(timeout=None)
    if menu["style"] == "dropdown":
        view.add_item(_build_select(menu, options))
        return view
    for pos, opt in enumerate(options):
        button = RoleMenuButton(
            menu["id"],
            opt["role_id"],
            label=opt["label"],
            emoji=opt["emoji"],
            color=opt["button_color"],
            row=pos // 5,
        )
        view.add_item(button)
    return view


def build_disabled_view(menu: dict, options: list[dict]) -> discord.ui.View:
    """Same layout, every component disabled — the 'unpublished decor' state.

    Plain (non-dynamic) items: a disabled component never dispatches, so there
    is nothing to route.
    """
    view = discord.ui.View(timeout=None)
    if menu["style"] == "dropdown":
        select = _build_select(menu, options)
        select.item.disabled = True
        view.add_item(select.item)
        return view
    for pos, opt in enumerate(options):
        view.add_item(
            discord.ui.Button(
                label=opt["label"] or None,
                style=_BUTTON_STYLES.get(opt["button_color"], discord.ButtonStyle.secondary),
                emoji=_parse_emoji(opt["emoji"]),
                row=pos // 5,
                disabled=True,
                custom_id=f"rolemenu:off:{menu['id']}:{opt['role_id']}",
            )
        )
    return view


def _build_select(menu: dict, options: list[dict]) -> "RoleMenuSelect":
    mode = menu["mode"]
    single_pick = mode in ("unique", "binding")
    if single_pick:
        max_values = 1
    elif menu["max_roles"] > 0:
        max_values = min(menu["max_roles"], len(options))
    else:
        max_values = len(options)
    # verify/binding submissions are meaningless when empty; the set-semantics
    # modes allow an empty submit ("uncheck everything") to clear/remove.
    min_values = 1 if mode in ("verify", "binding") else 0
    select_options = [
        discord.SelectOption(
            label=opt["label"] or "(unnamed)",
            value=str(opt["role_id"]),
            description=opt["description"] or None,
            emoji=_parse_emoji(opt["emoji"]),
        )
        for opt in options
    ]
    return RoleMenuSelect(
        menu["id"],
        placeholder=menu["placeholder"] or None,
        min_values=min_values,
        max_values=max(1, max_values),
        options=select_options,
    )


class RoleMenuButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rolemenu:btn:(?P<menu_id>\d+):(?P<role_id>\d+)",
):
    def __init__(
        self,
        menu_id: int,
        role_id: int,
        *,
        label: str = "",
        emoji: str = "",
        color: str = "secondary",
        row: int | None = None,
    ) -> None:
        self.menu_id = menu_id
        self.role_id = role_id
        super().__init__(
            discord.ui.Button(
                custom_id=f"rolemenu:btn:{menu_id}:{role_id}",
                label=label or None,
                style=_BUTTON_STYLES.get(color, discord.ButtonStyle.secondary),
                emoji=_parse_emoji(emoji),
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["menu_id"]), int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_menu_interaction(
            interaction, self.menu_id, clicked_role_id=self.role_id
        )


class RoleMenuSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"rolemenu:sel:(?P<menu_id>\d+)",
):
    def __init__(
        self,
        menu_id: int,
        *,
        placeholder: str | None = None,
        min_values: int = 0,
        max_values: int = 1,
        options: list[discord.SelectOption] | None = None,
    ) -> None:
        self.menu_id = menu_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"rolemenu:sel:{menu_id}",
                placeholder=placeholder,
                min_values=min_values,
                max_values=max_values,
                options=options or [],
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["menu_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        selected: list[int] = []
        for v in values:
            try:
                selected.append(int(v))
            except (TypeError, ValueError):
                continue
        await handle_menu_interaction(interaction, self.menu_id, selected=selected)


ROLE_MENU_DYNAMIC_ITEMS = (RoleMenuButton, RoleMenuSelect)


# ── the shared interaction path ─────────────────────────────────────

async def handle_menu_interaction(
    interaction: discord.Interaction,
    menu_id: int,
    *,
    clicked_role_id: int | None = None,
    selected: list[int] | None = None,
) -> None:
    guild = interaction.guild
    member = interaction.user
    ctx: "AppContext | None" = getattr(interaction.client, "ctx", None)
    if guild is None or ctx is None or not isinstance(member, discord.Member):
        return
    await interaction.response.defer(ephemeral=True)

    def _load():
        with ctx.open_db() as conn:
            menu = menus_db.get_menu(conn, menu_id)
            options = menus_db.list_options(conn, menu_id) if menu else []
            binding = (
                menus_db.get_binding(conn, menu_id, member.id)
                if menu and menu["mode"] == "binding"
                else None
            )
            return menu, options, binding

    menu, options, binding_role_id = await asyncio.to_thread(_load)
    if menu is None or menu["guild_id"] != guild.id or not options:
        await _reply(interaction, "This menu no longer exists.")
        return
    try:
        await _handle_loaded(
            interaction, ctx, guild, member, menu, options, binding_role_id,
            clicked_role_id, selected,
        )
    finally:
        # A used select keeps displaying the member's picks until the message
        # is edited; re-attach a fresh view so it reads as a prompt again.
        if selected is not None:
            await _reset_select(interaction, menu, options)


async def _handle_loaded(
    interaction: discord.Interaction,
    ctx: "AppContext",
    guild: discord.Guild,
    member: discord.Member,
    menu: dict,
    options: list[dict],
    binding_role_id: int | None,
    clicked_role_id: int | None,
    selected: list[int] | None,
) -> None:
    menu_id = menu["id"]
    if not menu["enabled"]:
        await _reply(interaction, "This menu is currently turned off.")
        return

    if menu["required_role_id"] > 0:
        req_role = guild.get_role(menu["required_role_id"])
        if req_role is None:
            await _reply(interaction, MSG_BROKEN)
            await _alert_mods_once(
                ctx, guild, menu, "its required role no longer exists"
            )
            return
        if req_role not in member.roles:
            await _reply(
                interaction, f"This menu requires the **@{req_role.name}** role."
            )
            return

    wait = _cooldowns.check(
        menu_id, member.id, menu["cooldown_seconds"], time.monotonic()
    )
    if wait > 0:
        await _reply(interaction, "Slow down — try again in a few seconds.")
        return

    menu_role_ids = [opt["role_id"] for opt in options]
    held = {r.id for r in member.roles}

    if clicked_role_id is not None:
        if clicked_role_id not in menu_role_ids:
            # Stale component from before an edit — spec §5: ignore gracefully.
            await _reply(interaction, "That choice isn't part of this menu anymore.")
            return
        outcome = resolve_click(
            menu["mode"], menu_role_ids, held, clicked_role_id,
            menu["max_roles"], binding_role_id,
        )
    else:
        outcome = resolve_selection(
            menu["mode"], menu_role_ids, held, selected or [],
            menu["max_roles"], binding_role_id,
        )

    if outcome.error:
        await _reply(interaction, _error_text(outcome.error, menu))
        return

    await _apply_outcome(interaction, ctx, guild, member, menu, options, outcome)


async def _reset_select(
    interaction: discord.Interaction, menu: dict, options: list[dict]
) -> None:
    msg = interaction.message
    if msg is None or not options:
        return
    try:
        view = build_view(menu, options) if menu["enabled"] else build_disabled_view(menu, options)
        await msg.edit(view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


def _error_text(code: str, menu: dict) -> str:
    if code == ERR_AT_CAP:
        n = menu["max_roles"]
        return (
            f"You can hold at most {n} role{'s' if n != 1 else ''} from this menu"
            " — remove one first."
        )
    if code == ERR_PERMANENT:
        return "Your choice here is permanent."
    if code == ERR_NO_CHANGE:
        if menu["mode"] == "verify":
            return "You already have that."
        if menu["mode"] == "drop":
            return "You don't have that role, so there's nothing to remove."
        return "No changes — your roles already match that selection."
    return MSG_BROKEN


async def _apply_outcome(
    interaction: discord.Interaction,
    ctx: "AppContext",
    guild: discord.Guild,
    member: discord.Member,
    menu: dict,
    options: list[dict],
    outcome: Outcome,
) -> None:
    bot_member = get_bot_member(guild)
    if bot_member is None or not bot_member.guild_permissions.manage_roles:
        await _reply(interaction, MSG_BROKEN)
        await _alert_mods_once(ctx, guild, menu, "I'm missing the Manage Roles permission")
        return

    add_roles: list[discord.Role] = []
    remove_roles: list[discord.Role] = []
    for rid, bucket in [(r, add_roles) for r in outcome.adds] + [
        (r, remove_roles) for r in outcome.removes
    ]:
        role = guild.get_role(rid)
        if role is None:
            await _reply(
                interaction,
                "Sorry — that choice isn't available anymore. The mods have been alerted.",
            )
            await _alert_mods_once(
                ctx, guild, menu, f"a configured role (id {rid}) no longer exists"
            )
            return
        if role >= bot_member.top_role:
            await _reply(interaction, MSG_BROKEN)
            await _alert_mods_once(
                ctx, guild, menu, f"**@{role.name}** is above my highest role"
            )
            return
        bucket.append(role)

    reason = f"Role menu: {menu['title'] or menu['id']}"
    try:
        if add_roles:
            await member.add_roles(*add_roles, reason=reason)
        if remove_roles:
            await member.remove_roles(*remove_roles, reason=reason)
    except discord.Forbidden:
        await _reply(interaction, MSG_BROKEN)
        await _alert_mods_once(ctx, guild, menu, "Discord refused the role change")
        return
    except discord.HTTPException as exc:
        log.warning("role menu %d apply failed for %s: %s", menu["id"], member.id, exc)
        await _reply(interaction, MSG_BROKEN)
        return

    changes = [(r.id, "grant") for r in add_roles] + [
        (r.id, "remove") for r in remove_roles
    ]
    menu_id = menu["id"]
    guild_id = guild.id
    member_id = member.id
    bind_role_id = outcome.bind_role_id

    def _persist() -> None:
        with ctx.open_db() as conn:
            menus_db.record_grants(
                conn, menu_id, guild_id, member_id, changes, time.time()
            )
            if bind_role_id:
                menus_db.set_binding(conn, menu_id, member_id, bind_role_id, time.time())
            if menu["alerted_at"]:
                # A successful click proves the menu works again.
                menus_db.set_menu_alerted(conn, menu_id, 0)

    await asyncio.to_thread(_persist)
    if add_roles:
        # role_pick quest trigger — a one-time setup kind (the bio_set
        # pattern): grants only, constant occurrence so it pays once ever.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            cast("Bot", interaction.client), guild_id, member_id,
            "role_pick", occurrence="set",
        )
    await _mod_log(ctx, guild, member, menu, add_roles, remove_roles)
    await _reply(interaction, _confirmation_text(menu, add_roles, remove_roles))


def _confirmation_text(
    menu: dict, add_roles: list[discord.Role], remove_roles: list[discord.Role]
) -> str:
    if menu["mode"] == "binding":
        picked = add_roles[0].name if add_roles else ""
        if picked:
            return f"✅ Your choice is locked in: **@{picked}**. This one's permanent."
        return "✅ Your choice is locked in. This one's permanent."
    if len(add_roles) == 1 and not remove_roles:
        return f"✅ You now have **@{add_roles[0].name}**."
    if len(remove_roles) == 1 and not add_roles:
        return f"✅ Removed **@{remove_roles[0].name}**."
    parts = [f"+{r.name}" for r in add_roles] + [f"−{r.name}" for r in remove_roles]
    return f"✅ Updated your roles: {', '.join(parts)}"


async def _reply(interaction: discord.Interaction, text: str) -> None:
    try:
        await interaction.followup.send(text, ephemeral=True)
    except discord.HTTPException:
        pass


# ── mod-facing plumbing ─────────────────────────────────────────────

def _mod_channel(ctx: "AppContext", guild: discord.Guild) -> discord.TextChannel | None:
    mod_channel_id = ctx.guild_config(guild.id).mod_channel_id
    if mod_channel_id <= 0:
        return None
    channel = guild.get_channel(mod_channel_id)
    return channel if isinstance(channel, discord.TextChannel) else None


async def _mod_log(
    ctx: "AppContext",
    guild: discord.Guild,
    member: discord.Member,
    menu: dict,
    add_roles: list[discord.Role],
    remove_roles: list[discord.Role],
) -> None:
    channel = _mod_channel(ctx, guild)
    if channel is None or (not add_roles and not remove_roles):
        return
    parts = [f"+{r.name}" for r in add_roles] + [f"−{r.name}" for r in remove_roles]
    label = menu["title"] or f"menu {menu['id']}"
    try:
        await channel.send(
            f"🎭 {member.mention} {' '.join(parts)} ({label})",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _alert_mods_once(
    ctx: "AppContext", guild: discord.Guild, menu: dict, why: str
) -> None:
    """Alert the mod channel about a broken menu — once, not once per click."""
    if menu["alerted_at"]:
        return
    menu_id = menu["id"]

    def _stamp() -> None:
        with ctx.open_db() as conn:
            menus_db.set_menu_alerted(conn, menu_id, time.time())

    await asyncio.to_thread(_stamp)
    menu["alerted_at"] = time.time()  # callers hold this dict for the request
    channel = _mod_channel(ctx, guild)
    if channel is None:
        return
    label = menu["title"] or f"menu {menu_id}"
    try:
        await channel.send(
            f"⚠️ The role menu **{label}** is misconfigured: {why}."
            " Members are getting a polite failure message; check the Role Menus"
            " page on the dashboard.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
