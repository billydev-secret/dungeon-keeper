"""Event handlers for the Discord bot."""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from openai import AsyncOpenAI

from post_monitoring import enforce_spoiler_requirement
from services.ai_moderation_service import ai_check_watched_message
from services.auto_delete_service import auto_delete_rule_exists, track_auto_delete_message
from services.message_xp_service import award_image_reaction_xp, award_message_xp
from services.welcome_service import build_leave_embed, build_welcome_embed
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

        message_ts = message.created_at.timestamp() if message.created_at else time.time()
        spoiler_deleted = await enforce_spoiler_requirement(
            message,
            spoiler_required_channels=ctx.spoiler_required_channels,
            bypass_role_ids=ctx.bypass_role_ids,
            log=log,
        )

        with ctx.open_db() as conn:
            record_member_activity(
                conn,
                message.guild.id,
                message.author.id,
                message.channel.id,
                message.id,
                message_ts,
            )
            if not spoiler_deleted and auto_delete_rule_exists(conn, message.guild.id, message.channel.id):
                track_auto_delete_message(
                    conn,
                    message.guild.id,
                    message.channel.id,
                    message.id,
                    message_ts,
                )

        if spoiler_deleted:
            return

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

        if message.author.id in ctx.watched_users:
            await _dm_watchers(message)

    async def _dm_watchers(message: discord.Message) -> None:
        watchers = list(ctx.watched_users.get(message.author.id, set()))
        if not watchers:
            return

        # Only notify watchers when the AI detects a rule violation.
        # If OPENAI_API_KEY is not set, fall back to notifying on every message.
        reason = ""
        api_key = os.getenv("OPENAI_API_KEY")
        if api_key:
            client = AsyncOpenAI(api_key=api_key)
            try:
                is_violation, reason = await ai_check_watched_message(client, message)
            except Exception as exc:
                log.warning(
                    "AI watch check failed for %s: %s — notifying anyway.",
                    message.author.display_name, exc,
                )
                is_violation = True  # fail open: DM watchers if the AI check errors
            if not is_violation:
                return

        channel_name = getattr(message.channel, "name", str(message.channel.id))
        guild_name = message.guild.name if message.guild else "Unknown Server"

        body = message.content or "*[no text content]*"
        attachment_lines = "\n".join(a.url for a in message.attachments)
        rule_line = f"\n⚠️ **Rule concern:** {reason}" if reason else ""
        footer = (
            f"{attachment_lines}\n" if attachment_lines else ""
        ) + (
            f"— **{message.author.display_name}** (@{message.author.name}) "
            f"in **{guild_name}** / #{channel_name}\n"
            f"{message.jump_url}"
        )
        dm_text = f"{body}{rule_line}\n\n{footer}"

        for watcher_id in watchers:
            try:
                watcher = bot.get_user(watcher_id) or await bot.fetch_user(watcher_id)
            except discord.HTTPException as exc:
                log.warning(
                    "Could not fetch watcher (id=%s) while relaying post from %s: %s",
                    watcher_id, message.author.display_name, exc,
                )
                continue
            try:
                await watcher.send(dm_text)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not DM watcher %s for watched user %s: %s",
                    watcher.display_name, message.author.display_name, exc,
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

    async def _dm_admin_permission_warning(guild: discord.Guild, message: str) -> None:
        owner = guild.owner
        if owner is None:
            return
        try:
            await owner.send(f"⚠️ **{guild.name}** — {message}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        if ctx.welcome_channel_id <= 0:
            return
        channel = member.guild.get_channel(ctx.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=build_welcome_embed(member, ctx.welcome_message))
        except discord.Forbidden:
            log.warning("Missing permission to send welcome message in channel %s.", ctx.welcome_channel_id)
            await _dm_admin_permission_warning(
                member.guild,
                f"Missing permission to send welcome messages in <#{ctx.welcome_channel_id}>.",
            )
        except discord.HTTPException as exc:
            log.error("Failed to send welcome message: %s", exc)

    @bot.event
    async def on_member_remove(member: discord.Member) -> None:
        if ctx.leave_channel_id <= 0:
            return
        channel = member.guild.get_channel(ctx.leave_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(embed=build_leave_embed(member, ctx.leave_message))
        except discord.Forbidden:
            log.warning("Missing permission to send leave message in channel %s.", ctx.leave_channel_id)
            await _dm_admin_permission_warning(
                member.guild,
                f"Missing permission to send leave messages in <#{ctx.leave_channel_id}>.",
            )
        except discord.HTTPException as exc:
            log.error("Failed to send leave message: %s", exc)

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
