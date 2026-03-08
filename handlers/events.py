"""Event handlers for the Discord bot."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from post_monitoring import enforce_spoiler_requirement
from services.auto_delete_service import auto_delete_rule_exists, track_auto_delete_message
from services.message_xp_service import award_image_reaction_xp, award_message_xp
from services.xp_service import handle_level_progress
from xp_system import DEFAULT_XP_SETTINGS, count_xp_events, record_member_activity

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.events")


def register_events(bot: Bot, ctx: AppContext) -> None:
    @bot.event
    async def on_ready():
        if bot.user is None:
            log.warning("Bot user was not available during on_ready.")
            return

        log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
        log.info("In Guild %s (Guarding: %s)", ctx.guild_id, ctx.spoiler_required_channels)
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            DEFAULT_XP_SETTINGS.role_grant_level,
            ctx.level_5_role_id,
            ctx.level_up_log_channel_id,
            DEFAULT_XP_SETTINGS.role_grant_level,
            ctx.level_5_log_channel_id,
        )
        log.debug("XP excluded channels: %s", sorted(ctx.xp_excluded_channel_ids))
        if ctx.guild_id:
            with ctx.open_db() as conn:
                log.debug(
                    "XP event rows for guild %s: %s",
                    ctx.guild_id,
                    count_xp_events(conn, ctx.guild_id),
                )

    @bot.event
    async def on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if await enforce_spoiler_requirement(
            message,
            spoiler_required_channels=ctx.spoiler_required_channels,
            bypass_role_ids=ctx.bypass_role_ids,
            log=log,
        ):
            return

        message_ts = message.created_at.timestamp() if message.created_at else time.time()
        with ctx.open_db() as conn:
            record_member_activity(
                conn,
                message.guild.id,
                message.author.id,
                message.channel.id,
                message.id,
                message_ts,
            )
            if auto_delete_rule_exists(conn, message.guild.id, message.channel.id):
                track_auto_delete_message(
                    conn,
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    message_ts,
                )

        result = await award_message_xp(
            message,
            bot=bot,
            db_path=ctx.db_path,
            xp_pair_states=ctx.xp_pair_states,
            excluded_channel_ids=ctx.xp_excluded_channel_ids,
        )
        if result is not None and isinstance(message.author, discord.Member):
            await handle_level_progress(
                message.author,
                result,
                "text_message",
                level_5_role_id=ctx.level_5_role_id,
                level_up_log_channel_id=ctx.level_up_log_channel_id,
                level_5_log_channel_id=ctx.level_5_log_channel_id,
            )

    @bot.event
    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        result = await award_image_reaction_xp(
            payload,
            bot=bot,
            db_path=ctx.db_path,
            excluded_channel_ids=ctx.xp_excluded_channel_ids,
        )
        if result is not None:
            member, award = result
            await handle_level_progress(
                member,
                award,
                "image_reaction",
                level_5_role_id=ctx.level_5_role_id,
                level_up_log_channel_id=ctx.level_up_log_channel_id,
                level_5_log_channel_id=ctx.level_5_log_channel_id,
            )

    @bot.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        from services.auto_delete_service import remove_tracked_auto_delete_message
        remove_tracked_auto_delete_message(
            ctx.db_path, payload.guild_id, payload.channel_id, payload.message_id
        )

    @bot.event
    async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None:
            return
        from services.auto_delete_service import remove_tracked_auto_delete_messages
        remove_tracked_auto_delete_messages(
            ctx.db_path, payload.guild_id, payload.channel_id, payload.message_ids
        )

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            missing_name = getattr(error, "name", "unknown")
            log.warning(
                "Received unknown slash command '%s' in guild %s (user %s). "
                "This is usually stale command registration.",
                missing_name,
                interaction.guild_id,
                interaction.user.id,
            )
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That command is out of date on this server. Please try again in a moment.",
                    ephemeral=True,
                )
            return

        log.exception("Unhandled app command error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Command failed. Please try again.", ephemeral=True)
