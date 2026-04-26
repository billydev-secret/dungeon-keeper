"""One-shot /setup — creates or verifies all bot channels and categories."""
from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.jail_commands import CLR_TICKET, TicketPanelButton
from db_utils import get_config_value
from services.dm_perms_service import set_panel_settings, set_request_channel
from services.embeds import MOD_SUCCESS

if TYPE_CHECKING:
    from app_context import AppContext, Bot


_BOT_LOGS_CAT = "Bot Logs"
_SUPPORT_CHANNEL = "support"
_DM_PANEL_CHANNEL = "dm-requests"

# (config_key, channel_name, ctx_attr_or_None, topic)
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
    ow: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
    }
    if guild.me:
        ow[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_messages=True,
        )
    for rid in ctx.mod_role_ids | ctx.admin_role_ids:
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


def _stored_id(ctx: AppContext, config_key: str) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, config_key, "0", ctx.guild_id)
    return int(raw) if raw.strip().isdigit() else 0


class SetupCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="init",
        description="Initialize all bot channels and categories, creating any that are missing.",
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

        # Preflight: bot needs Manage Channels + Manage Roles to create
        # channels with permission overwrites.
        if guild.me:
            bp = guild.me.guild_permissions
            missing = [p for p, ok in [("manage_channels", bp.manage_channels), ("manage_roles", bp.manage_roles)] if not ok]
            if missing:
                await interaction.followup.send(
                    f"Bot is missing required permissions: **{', '.join(missing)}**.\n"
                    "Grant them in Server Settings → Integrations and try again.",
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
        for config_key, ch_name, ctx_attr, topic in _PRIVATE_CHANNELS:
            existing_id = _stored_id(ctx, config_key)
            ch = guild.get_channel(existing_id) if existing_id else None
            if isinstance(ch, discord.TextChannel):
                lines.append(f"✅ {ch.mention} already set")
            else:
                ch = await guild.create_text_channel(
                    ch_name, category=logs_cat, overwrites=priv, topic=topic  # type: ignore[arg-type]
                )
                ctx.set_config_value(config_key, str(ch.id))
                if ctx_attr:
                    setattr(ctx, ctx_attr, ch.id)
                lines.append(f"🆕 {ch.mention} created")

        # ── Ticket panel channel ──────────────────────────────────────────────
        ro = _readonly_ow(guild)
        ticket_ch_id = _stored_id(ctx, "ticket_panel_channel_id")
        ticket_ch = guild.get_channel(ticket_ch_id) if ticket_ch_id else None
        if not isinstance(ticket_ch, discord.TextChannel):
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
            ctx.set_config_value("ticket_panel_channel_id", str(ticket_ch.id))
            ctx.set_config_value("ticket_panel_message_id", str(msg.id))
            lines.append(f"🆕 {ticket_ch.mention} created with ticket panel")
        else:
            lines.append(f"✅ {ticket_ch.mention} ticket panel already set")

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
                if ctx.mod_channel_id and not dm_cog.request_channels.get(guild.id):  # type: ignore[union-attr]
                    set_request_channel(ctx.db_path, guild.id, ctx.mod_channel_id)
                    dm_cog.request_channels[guild.id] = ctx.mod_channel_id  # type: ignore[union-attr]
                lines.append(f"🆕 {dm_ch.mention} created with DM panel")
            else:
                lines.append(f"✅ {dm_ch.mention} DM panel already set")
        else:
            lines.append("⚠️ DmPermsCog not loaded — DM panel skipped")

        embed = discord.Embed(
            title="Setup Complete",
            description="\n".join(lines),
            color=MOD_SUCCESS,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(SetupCog(bot, bot.ctx))
