"""One-shot /setup — creates bot channels, then walks through role/category config.

This is the single entry point for first-run bot setup. It runs in two phases:

1. **Channel creation (automatic):** ensures the Bot Logs category and all
   private log/ticket/DM-perms channels exist. Re-running this phase only
   creates what's missing — existing channels are reused.
2. **Config wizard (interactive):** a 6-step flow to pick mod/admin roles
   and the jail/ticket categories. Reuses ``_setup_view`` from jail_commands.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import (
    CLR_TICKET,
    TicketPanelButton,
    _add_ticket_panel,
    _guild_has_any_ticket_panel,
    _setup_view,
)
from bot_modules.core.db_utils import get_config_value
from bot_modules.services.dm_perms_service import set_panel_settings, set_request_channel
from bot_modules.services.embeds import MOD_SUCCESS

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot


_BOT_LOGS_CAT = "Bot Logs"
_SUPPORT_CHANNEL = "support"
_DM_PANEL_CHANNEL = "dm-requests"

# (config_key, channel_name, ctx_attr_or_None, topic)
# ctx_attr is None for channels read directly from the DB at use-time
# (e.g. log_channel_id / transcript_channel_id are looked up on each
# /jail or /ticket invocation, so there's no in-memory ctx mirror).
_PRIVATE_CHANNELS: list[tuple[str, str, str | None, str]] = [
    ("mod_channel_id", "mod-log", "mod_channel_id", "Moderator activity log"),
    ("join_leave_log_channel_id", "join-leave-log", "join_leave_log_channel_id", "Member join and leave events"),
    ("xp_level_up_log_channel_id", "xp-level-up-log", "level_up_log_channel_id", "XP level-up notifications"),
    ("xp_level_5_log_channel_id", "xp-level-5-log", "level_5_log_channel_id", "XP level-5 milestones"),
    ("greeter_chat_channel_id", "greeter-chat", "greeter_chat_channel_id", "Greeter prompts for new members"),
    ("log_channel_id", "ticket-log", None, "Ticket and jail audit log"),
    ("transcript_channel_id", "ticket-transcripts", None, "Ticket transcripts"),
]


def _private_ow(
    guild: discord.Guild, ctx: AppContext
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    # Defensively deny both view_channel AND read_message_history so a
    # misconfigured parent category can't accidentally leak history.
    ow: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, read_message_history=False
        ),
    }
    if guild.me:
        ow[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    cfg = ctx.guild_config(guild.id)
    for rid in cfg.mod_role_ids | cfg.admin_role_ids:
        role = guild.get_role(rid)
        if role:
            ow[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    return ow


def _readonly_ow(
    guild: discord.Guild,
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    ow: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False),
    }
    if guild.me:
        ow[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    return ow


def _stored_id(ctx: AppContext, config_key: str, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, config_key, "0", guild_id)
    return int(raw) if raw.strip().isdigit() else 0


class SetupCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="setup",
        description="First-time setup — creates bot channels, then walks through role/category config.",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        guild = interaction.guild
        assert guild is not None

        if not ctx.is_admin(interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Preflight: full set of perms the bot needs across both phases —
        # channel creation, role management, and posting embeds in panels.
        if guild.me:
            bp = guild.me.guild_permissions
            required = {
                "Manage Channels": bp.manage_channels,
                "Manage Roles": bp.manage_roles,
                "Send Messages": bp.send_messages,
                "Read Message History": bp.read_message_history,
                "Embed Links": bp.embed_links,
                "Attach Files": bp.attach_files,
                "Manage Messages": bp.manage_messages,
            }
            missing = [name for name, ok in required.items() if not ok]
            if missing:
                await interaction.followup.send(
                    "Bot is missing required permissions:\n"
                    + "\n".join(f"• **{p}**" for p in missing)
                    + "\n\nGrant them in Server Settings → Integrations and run "
                    "`/setup` again.",
                    ephemeral=True,
                )
                return

        lines: list[str] = []

        # ── Bot Logs category ─────────────────────────────────────────────────
        priv = _private_ow(guild, ctx)
        logs_cat = discord.utils.get(guild.categories, name=_BOT_LOGS_CAT)
        if logs_cat is None:
            logs_cat = await guild.create_category(_BOT_LOGS_CAT, overwrites=priv)  # type: ignore[arg-type]
            lines.append(f"🆕 **{_BOT_LOGS_CAT}** category created")
        else:
            lines.append(f"✅ **{_BOT_LOGS_CAT}** category exists")

        # ── Private log channels ──────────────────────────────────────────────
        is_home = guild.id == ctx.guild_id
        for config_key, ch_name, ctx_attr, topic in _PRIVATE_CHANNELS:
            existing_id = _stored_id(ctx, config_key, guild.id)
            ch = guild.get_channel(existing_id) if existing_id else None
            if isinstance(ch, discord.TextChannel):
                # Resync the home in-memory ctx mirror in case it drifted from
                # DB (e.g. after a partial reload). Non-home guilds read via
                # guild_config, refreshed by the invalidation below.
                if ctx_attr and is_home:
                    setattr(ctx, ctx_attr, ch.id)
                lines.append(f"✅ {ch.mention} already set")
            else:
                ch = await guild.create_text_channel(
                    ch_name, category=logs_cat, overwrites=priv, topic=topic  # type: ignore[arg-type]
                )
                ctx.set_config_value(config_key, str(ch.id), guild.id)
                if ctx_attr and is_home:
                    setattr(ctx, ctx_attr, ch.id)
                lines.append(f"🆕 {ch.mention} created")

        # ── Ticket panel channel ──────────────────────────────────────────────
        ro = _readonly_ow(guild)
        if not _guild_has_any_ticket_panel(ctx, guild.id):
            ticket_ch = await guild.create_text_channel(
                _SUPPORT_CHANNEL,
                overwrites=ro,  # type: ignore[arg-type]
                topic="Open a support ticket with the mod team",
            )
            panel_embed = discord.Embed(
                title="📩 Support Tickets",
                description=(
                    "Need help from the mod team? Click the button below to open a private ticket.\n\n"
                    "A moderator will respond as soon as possible."
                ),
                color=CLR_TICKET,
            )
            panel_view = discord.ui.View(timeout=None)
            panel_view.add_item(TicketPanelButton())
            msg = await ticket_ch.send(embed=panel_embed, view=panel_view)
            _add_ticket_panel(ctx, guild.id, ticket_ch.id, msg.id)
            lines.append(f"🆕 {ticket_ch.mention} created with ticket panel")
        else:
            lines.append("✅ Ticket panel already exists (skipping)")

        # ── DM perms panel channel ────────────────────────────────────────────
        dm_cog = self.bot.get_cog("DmPermsCog")
        if dm_cog is not None:
            existing_dm = dm_cog.panel_settings.get(guild.id, {})  # type: ignore[union-attr]
            dm_ch_id = existing_dm.get("panel_channel_id") or 0
            dm_ch = guild.get_channel(dm_ch_id) if dm_ch_id else None
            if not isinstance(dm_ch, discord.TextChannel):
                dm_ch = await guild.create_text_channel(
                    _DM_PANEL_CHANNEL,
                    overwrites=ro,  # type: ignore[arg-type]
                    topic="Request or manage DM permissions",
                )
                dm_cog.panel_settings[guild.id] = {  # type: ignore[union-attr]
                    "panel_channel_id": dm_ch.id,
                    "panel_message_id": None,
                }
                await dm_cog._ensure_panel(guild, dm_ch.id, force_repost=True)  # type: ignore[union-attr]
                set_panel_settings(ctx.db_path, guild.id, dm_ch.id, None)
                # Route DM requests to the mod log if no request channel is set yet
                mod_channel_id = ctx.guild_config(guild.id).mod_channel_id
                if mod_channel_id and not dm_cog.request_channels.get(guild.id):  # type: ignore[union-attr]
                    set_request_channel(ctx.db_path, guild.id, mod_channel_id)
                    dm_cog.request_channels[guild.id] = mod_channel_id  # type: ignore[union-attr]
                lines.append(f"🆕 {dm_ch.mention} created with DM panel")
            else:
                lines.append(f"✅ {dm_ch.mention} DM panel already set")
        else:
            lines.append("⚠️ DmPermsCog not loaded — DM panel skipped")

        embed = discord.Embed(
            title="Phase 1 of 2 — Channels Ready",
            description="\n".join(lines)
            + "\n\nNext: pick roles and categories below.",
            color=MOD_SUCCESS,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

        # Phase 2 — interactive wizard (mod roles, admin roles, jail/ticket
        # categories, log/transcript channels). The wizard returns its own
        # embed + view; we send it as a follow-up so the user can step through
        # without losing the channel-creation summary above.
        wizard_embed, wizard_view = _setup_view(ctx, 1)
        await interaction.followup.send(
            embed=wizard_embed, view=wizard_view, ephemeral=True
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(SetupCog(bot, bot.ctx))
