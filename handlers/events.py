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
from services.interaction_graph import record_interactions
from services.invite_tracker import detect_inviter, record_invite, refresh_invite_cache
from services.message_store import (
    adjust_reaction_count,
    delete_message,
    delete_messages_bulk,
    store_message,
)
from services.message_xp_service import award_image_reaction_xp, award_message_xp
from services.welcome_service import build_leave_embed, build_welcome_embed
from services.xp_service import handle_level_progress
from xp_system import DEFAULT_XP_SETTINGS, count_xp_events, log_role_event, record_member_activity

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
        _guild = bot.get_guild(ctx.guild_id) if ctx.guild_id else None
        _guild_name = _guild.name if _guild else ctx.guild_id

        def _ch(cid: int) -> str:
            c = _guild.get_channel(cid) if _guild else None
            return f"#{c.name}" if c else str(cid)

        def _ro(rid: int) -> str:
            r = _guild.get_role(rid) if _guild else None
            return f"@{r.name}" if r else str(rid)

        log.info(
            "In Guild %s (ID: %s, guarding: %s)",
            _guild_name, ctx.guild_id,
            [_ch(c) for c in ctx.spoiler_required_channels],
        )
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            DEFAULT_XP_SETTINGS.role_grant_level,
            _ro(ctx.level_5_role_id),
            _ch(ctx.level_up_log_channel_id),
            DEFAULT_XP_SETTINGS.role_grant_level,
            _ch(ctx.level_5_log_channel_id),
        )
        log.debug("XP excluded channels: %s", sorted(ctx.xp_excluded_channel_ids))
        if _guild is not None:
            await refresh_invite_cache(_guild)
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

        # Collect reply / mention data once (used by both store_message and record_interactions)
        reply_to_id: int | None = None
        if message.reference and message.reference.message_id:
            reply_to_id = message.reference.message_id

        mention_ids = [u.id for u in message.mentions if not u.bot and u.id != message.author.id]
        attachment_urls = [a.url for a in message.attachments]

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

            store_message(
                conn,
                message_id=message.id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                content=message.content or None,
                reply_to_id=reply_to_id,
                ts=int(message_ts),
                attachment_urls=attachment_urls,
                mention_ids=mention_ids,
            )

            # Record reply and mention interactions for the connection web
            interaction_targets = [uid for uid in mention_ids]
            if reply_to_id and message.reference and isinstance(message.reference.resolved, discord.Message):
                ref = message.reference.resolved
                if not ref.author.bot and ref.author.id != message.author.id and ref.author.id not in interaction_targets:
                    interaction_targets.insert(0, ref.author.id)
            if interaction_targets:
                record_interactions(
                    conn, message.guild.id, message.author.id, interaction_targets,
                    ts=int(message_ts),
                    message_id=message.id,
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
                db_path=ctx.db_path,
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
                db_path=ctx.db_path,
            )

        if payload.guild_id:
            with ctx.open_db() as conn:
                adjust_reaction_count(conn, payload.message_id, str(payload.emoji), +1)

    @bot.event
    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        if payload.guild_id:
            with ctx.open_db() as conn:
                adjust_reaction_count(conn, payload.message_id, str(payload.emoji), -1)

    @bot.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        from services.auto_delete_service import remove_tracked_auto_delete_message
        remove_tracked_auto_delete_message(
            ctx.db_path, payload.guild_id, payload.channel_id, payload.message_id
        )
        with ctx.open_db() as conn:
            delete_message(conn, payload.message_id)

    async def _dm_admin_permission_warning(guild: discord.Guild, message: str) -> None:
        owner = guild.owner
        if owner is None:
            return
        try:
            await owner.send(f"⚠️ **{guild.name}** — {message}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    @bot.event
    async def on_member_update(before: discord.Member, after: discord.Member) -> None:
        if before.guild.id != ctx.guild_id:
            return
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if before_ids == after_ids:
            return
        now = time.time()
        with ctx.open_db() as conn:
            for role in after.roles:
                if role.id not in before_ids:
                    log_role_event(conn, after.guild.id, after.id, role.name, "grant", ts=now)
            for role in before.roles:
                if role.id not in after_ids:
                    log_role_event(conn, after.guild.id, after.id, role.name, "remove", ts=now)

    @bot.event
    async def on_member_join(member: discord.Member) -> None:
        # Invite tracking — detect who invited this member
        inviter_id, invite_code = await detect_inviter(member.guild)
        if inviter_id is not None:
            with ctx.open_db() as conn:
                record_invite(conn, member.guild.id, inviter_id, member.id, invite_code)
            log.info("Invite tracked: %s invited by %s (code: %s)", member, inviter_id, invite_code)

        # Welcome message
        if ctx.welcome_channel_id > 0:
            channel = member.guild.get_channel(ctx.welcome_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    ping = f"<@&{ctx.welcome_ping_role_id}>" if ctx.welcome_ping_role_id > 0 else None
                    await channel.send(content=ping, embed=build_welcome_embed(member, ctx.welcome_message))
                except discord.Forbidden:
                    log.warning("Missing permission to send welcome message in #%s.", channel.name)
                    await _dm_admin_permission_warning(
                        member.guild,
                        f"Missing permission to send welcome messages in <#{ctx.welcome_channel_id}>.",
                    )
                except discord.HTTPException as exc:
                    log.error("Failed to send welcome message: %s", exc)

        # Ping greeter chat channel if configured
        if ctx.greeter_chat_channel_id > 0:
            greeter_channel = member.guild.get_channel(ctx.greeter_chat_channel_id)
            if isinstance(greeter_channel, discord.TextChannel):
                try:
                    await greeter_channel.send(f"@here - {member.mention} has arrived")
                except discord.Forbidden:
                    log.warning("Missing permission to send greeter ping in #%s.", greeter_channel.name)
                except discord.HTTPException as exc:
                    log.error("Failed to send greeter chat ping: %s", exc)

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
            log.warning("Missing permission to send leave message in #%s.", channel.name)
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
        with ctx.open_db() as conn:
            delete_messages_bulk(conn, payload.message_ids)

    @bot.event
    async def on_interaction(interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.application_command and interaction.data:
            data: dict = interaction.data  # type: ignore[assignment]
            cmd = data.get("name", "?")
            opts: list[dict] = data.get("options") or []
            parts: list[str] = [str(cmd)]
            for opt in opts:
                # Subcommand groups / subcommands nest one level deeper
                if opt.get("type") in (1, 2) and opt.get("options"):
                    parts.append(str(opt["name"]))
                    for sub in opt["options"]:
                        parts.append(f"{sub['name']}={sub.get('value', '')}")
                else:
                    parts.append(f"{opt['name']}={opt.get('value', '')}")
            guild_name = interaction.guild.name if interaction.guild else "DM"
            channel = getattr(interaction.channel, "name", interaction.channel_id)
            log.info(
                "Command /%s by %s (%s) in #%s [%s]",
                " ".join(parts),
                interaction.user.display_name,
                interaction.user.id,
                channel,
                guild_name,
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
                interaction.guild.name if interaction.guild else interaction.guild_id,
                interaction.user,
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
