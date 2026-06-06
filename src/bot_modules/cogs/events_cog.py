"""Discord event listeners (Cog)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import check_jail_rejoin
from bot_modules.core.post_monitoring import enforce_spoiler_requirement
from bot_modules.services.auto_delete_service import (
    auto_delete_rule_exists,
    remove_tracked_auto_delete_message,
    remove_tracked_auto_delete_messages,
    track_auto_delete_message,
)
from bot_modules.services.discord_scan import collect_messageable_channels
from bot_modules.services.incident_detection import check_join_raid, velocity_tracker
from bot_modules.services.interaction_graph import record_interactions
from bot_modules.services.invite_tracker import detect_inviter, record_invite, refresh_invite_cache
from bot_modules.services.message_store import (
    adjust_reaction_count,
    mark_member_left,
    record_member_event,
    record_reaction,
    set_reaction_count,
    store_message,
    upsert_known_channel,
    upsert_known_user,
)
from bot_modules.services.message_xp_service import award_image_reaction_xp, award_message_xp
from bot_modules.services.sentiment_service import score_text
from bot_modules.services.welcome_service import build_leave_embed, build_welcome_embed
from bot_modules.services.wellness_enforcement import wellness_on_message
from bot_modules.services.xp_service import handle_level_progress
from bot_modules.core.utils import format_guild_for_log
from bot_modules.core.xp_system import count_xp_events, log_role_event, record_member_activity

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot, GuildConfig

log = logging.getLogger("dungeonkeeper.events")


def _discord_embed_to_dict(e: discord.Embed) -> dict:
    return {
        "title": e.title,
        "description": e.description,
        "url": e.url,
        "author": e.author.name if e.author else None,
        "footer": e.footer.text if e.footer else None,
        "fields": [
            {"name": f.name, "value": f.value, "inline": f.inline}
            for f in e.fields
        ],
    }


def _message_mention_ids(
    recorded_bot_user_ids: frozenset[int] | set[int], message: discord.Message
) -> list[int]:
    return [
        user.id
        for user in message.mentions
        if (not user.bot or user.id in recorded_bot_user_ids)
        and user.id != message.author.id
    ]


def _archived_message_content(message: discord.Message) -> str | None:
    if message.content:
        return message.content
    system_content = (getattr(message, "system_content", "") or "").strip()
    return system_content or None


def _counts_as_member_activity(message: discord.Message) -> bool:
    return message.type in {
        discord.MessageType.default,
        discord.MessageType.reply,
    }


_collect_backfill_channels = collect_messageable_channels


async def _backfill_messages(bot: Bot, ctx: AppContext) -> None:
    for g in bot.guilds:
        me = g.me
        guild_count = 0
        for channel in await _collect_backfill_channels(g, me):
            with ctx.open_db() as conn:
                row = conn.execute(
                    "SELECT MAX(message_id) FROM messages WHERE guild_id = ? AND channel_id = ?",
                    (g.id, channel.id),
                ).fetchone()
                channel_has_auto_delete_rule = auto_delete_rule_exists(
                    conn, g.id, channel.id
                )
            max_id = row[0] if row and row[0] else None
            history_kwargs: dict = {"limit": None, "oldest_first": True}
            if max_id:
                history_kwargs["after"] = discord.Object(id=max_id)

            channel_count = 0
            batch: list[discord.Message] = []
            BATCH_SIZE = 200

            def _flush(msgs: list[discord.Message]) -> None:
                if not msgs:
                    return
                with ctx.open_db() as conn:
                    for msg in msgs:
                        msg_ts = msg.created_at.timestamp() if msg.created_at else time.time()
                        sentiment, emotion = score_text(msg.content)
                        mention_ids = (
                            []
                            if msg.author.bot
                            else _message_mention_ids(
                                ctx.guild_config(g.id).recorded_bot_user_ids, msg
                            )
                        )
                        reply_to_id = (
                            msg.reference.message_id
                            if msg.reference and msg.reference.message_id
                            else None
                        )
                        if channel_has_auto_delete_rule:
                            track_auto_delete_message(
                                conn,
                                g.id,
                                channel.id,
                                msg.id,
                                msg_ts,
                            )
                        store_message(
                            conn,
                            message_id=msg.id,
                            guild_id=g.id,
                            channel_id=channel.id,
                            author_id=msg.author.id,
                            content=_archived_message_content(msg),
                            reply_to_id=reply_to_id,
                            ts=int(msg_ts),
                            attachment_urls=[a.url for a in msg.attachments],
                            mention_ids=mention_ids,
                            sentiment=sentiment,
                            emotion=emotion,
                            embeds=[_discord_embed_to_dict(e) for e in msg.embeds],
                        )
                        for reaction in msg.reactions:
                            set_reaction_count(
                                conn, msg.id, str(reaction.emoji), reaction.count
                            )
                        if sentiment is not None:
                            conn.execute(
                                "INSERT OR IGNORE INTO message_sentiment "
                                "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
                                "VALUES (?, ?, ?, ?, ?, ?)",
                                (msg.id, g.id, channel.id, sentiment, emotion, msg_ts),
                            )
                        upsert_known_user(
                            conn,
                            guild_id=g.id,
                            user_id=msg.author.id,
                            username=str(msg.author),
                            display_name=msg.author.display_name,
                            ts=msg_ts,
                            is_bot=msg.author.bot,
                        )
                        upsert_known_channel(
                            conn,
                            guild_id=g.id,
                            channel_id=channel.id,
                            channel_name=getattr(channel, "name", str(channel.id)),
                            ts=msg_ts,
                        )

            try:
                async for msg in channel.history(**history_kwargs):
                    batch.append(msg)
                    channel_count += 1
                    if len(batch) >= BATCH_SIZE:
                        _flush(batch)
                        batch = []
                _flush(batch)
            except discord.Forbidden:
                _flush(batch)
                continue
            except Exception:
                _flush(batch)
                log.exception(
                    "Backfill failed in guild %s channel #%s",
                    format_guild_for_log(g),
                    getattr(channel, "name", channel.id),
                )
                continue
            if channel_count:
                log.info(
                    "Backfilled %d messages in guild %s channel #%s.",
                    channel_count,
                    format_guild_for_log(g),
                    getattr(channel, "name", channel.id),
                )
                guild_count += channel_count
        if guild_count:
            log.info(
                "Backfill complete for guild %s: %d messages.",
                format_guild_for_log(g),
                guild_count,
            )


async def _on_tree_error(
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
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "That command is out of date on this server. Please try again in a moment.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            pass
        return

    log.exception("Unhandled app command error: %s", error)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Command failed. Please try again.", ephemeral=True
            )
    except discord.HTTPException:
        pass


class EventsCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self._message_backfill_task: asyncio.Task[None] | None = None
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.tree.error(_on_tree_error)

    def _log_background_task_result(self, task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("Background message backfill crashed.")

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self.bot.user is None:
            log.warning("Bot user was not available during on_ready.")
            return

        log.info("Logged in as %s (ID: %s)", self.bot.user, self.bot.user.id)
        _primary_guild = (
            self.bot.get_guild(self.ctx.guild_id) if self.ctx.guild_id else None
        )

        def _ch(cid: int) -> str:
            c = _primary_guild.get_channel(cid) if _primary_guild else None
            return f"#{c.name}" if c else str(cid)

        def _ro(rid: int) -> str:
            r = _primary_guild.get_role(rid) if _primary_guild else None
            return f"@{r.name}" if r else str(rid)

        log.info(
            "Primary guild %s (ID: %s, guarding: %s)",
            _primary_guild.name if _primary_guild else self.ctx.guild_id,
            self.ctx.guild_id,
            [_ch(c) for c in self.ctx.spoiler_required_channels],
        )
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            self.ctx.xp_settings.role_grant_level,
            _ro(self.ctx.level_5_role_id),
            _ch(self.ctx.level_up_log_channel_id),
            self.ctx.xp_settings.role_grant_level,
            _ch(self.ctx.level_5_log_channel_id),
        )
        log.debug("XP excluded channels: %s", sorted(self.ctx.xp_excluded_channel_ids))

        now_ts = time.time()
        for g in self.bot.guilds:
            await refresh_invite_cache(g)
            with self.ctx.open_db() as conn:
                for m in g.members:
                    upsert_known_user(
                        conn,
                        guild_id=g.id,
                        user_id=m.id,
                        username=str(m),
                        display_name=m.display_name,
                        ts=now_ts,
                        is_bot=m.bot,
                        current_member=True,
                    )
                for ch in g.channels:
                    if hasattr(ch, "name"):
                        upsert_known_channel(
                            conn,
                            guild_id=g.id,
                            channel_id=ch.id,
                            channel_name=ch.name,
                            ts=now_ts,
                        )
                log.debug(
                    "XP event rows for guild %s: %s",
                    format_guild_for_log(g),
                    count_xp_events(conn, g.id),
                )
            log.info(
                "Backfilled guild %s: %d known users, %d known channels.",
                g.name,
                len(g.members),
                len(g.channels),
            )

        if self._message_backfill_task is None or self._message_backfill_task.done():
            self._message_backfill_task = asyncio.create_task(
                _backfill_messages(self.bot, self.ctx)
            )
            self._message_backfill_task.add_done_callback(
                self._log_background_task_result
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        cfg = self.ctx.guild_config(message.guild.id)
        is_bot_author = message.author.bot

        message_ts = (
            message.created_at.timestamp() if message.created_at else time.time()
        )

        reply_to_id: int | None = None
        if message.reference and message.reference.message_id:
            reply_to_id = message.reference.message_id
        attachment_urls = [a.url for a in message.attachments]

        if is_bot_author:
            sentiment, emotion = await asyncio.to_thread(score_text, message.content)
            with self.ctx.open_db() as conn:
                if auto_delete_rule_exists(conn, message.guild.id, message.channel.id):
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
                    content=_archived_message_content(message),
                    reply_to_id=reply_to_id,
                    ts=int(message_ts),
                    attachment_urls=attachment_urls,
                    mention_ids=[],
                    sentiment=sentiment,
                    emotion=emotion,
                    embeds=[_discord_embed_to_dict(e) for e in message.embeds],
                )
                if sentiment is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO message_sentiment "
                        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            message.id,
                            message.guild.id,
                            message.channel.id,
                            sentiment,
                            emotion,
                            message_ts,
                        ),
                    )
                upsert_known_user(
                    conn,
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    username=str(message.author),
                    display_name=message.author.display_name,
                    ts=message_ts,
                    is_bot=message.author.bot,
                )
                upsert_known_channel(
                    conn,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    channel_name=getattr(message.channel, "name", str(message.channel.id)),
                    ts=message_ts,
                )
                velocity_tracker.record_message(
                    conn, message.guild.id, message.channel.id, ts=message_ts
                )
            return

        archive_content = _archived_message_content(message)
        if not _counts_as_member_activity(message):
            mention_ids = _message_mention_ids(cfg.recorded_bot_user_ids, message)
            with self.ctx.open_db() as conn:
                if auto_delete_rule_exists(conn, message.guild.id, message.channel.id):
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
                    content=archive_content,
                    reply_to_id=reply_to_id,
                    ts=int(message_ts),
                    attachment_urls=attachment_urls,
                    mention_ids=mention_ids,
                    embeds=[_discord_embed_to_dict(e) for e in message.embeds],
                )
                upsert_known_user(
                    conn,
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    username=str(message.author),
                    display_name=message.author.display_name,
                    ts=message_ts,
                )
                upsert_known_channel(
                    conn,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    channel_name=getattr(
                        message.channel, "name", str(message.channel.id)
                    ),
                    ts=message_ts,
                )
            return

        spoiler_deleted = await enforce_spoiler_requirement(
            message,
            spoiler_required_channels=cfg.spoiler_required_channels,
            bypass_role_ids=cfg.bypass_role_ids,
            log=log,
        )

        mention_ids = _message_mention_ids(cfg.recorded_bot_user_ids, message)

        if spoiler_deleted:
            return

        if await wellness_on_message(self.ctx, message):
            return

        sentiment, emotion = await asyncio.to_thread(score_text, message.content)

        with self.ctx.open_db() as conn:
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

            store_message(
                conn,
                message_id=message.id,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                author_id=message.author.id,
                content=archive_content,
                reply_to_id=reply_to_id,
                ts=int(message_ts),
                attachment_urls=attachment_urls,
                mention_ids=mention_ids,
                sentiment=sentiment,
                emotion=emotion,
                embeds=[_discord_embed_to_dict(e) for e in message.embeds],
            )

            if sentiment is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO message_sentiment "
                    "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        message.id,
                        message.guild.id,
                        message.channel.id,
                        sentiment,
                        emotion,
                        message_ts,
                    ),
                )

            upsert_known_user(
                conn,
                guild_id=message.guild.id,
                user_id=message.author.id,
                username=str(message.author),
                display_name=message.author.display_name,
                ts=message_ts,
                is_bot=message.author.bot,
            )

            upsert_known_channel(
                conn,
                guild_id=message.guild.id,
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", str(message.channel.id)),
                ts=message_ts,
            )

            interaction_targets = list(mention_ids)
            if (
                reply_to_id
                and message.reference
                and isinstance(message.reference.resolved, discord.Message)
            ):
                ref = message.reference.resolved
                if (
                    (not ref.author.bot or ref.author.id in cfg.recorded_bot_user_ids)
                    and ref.author.id != message.author.id
                    and ref.author.id not in interaction_targets
                ):
                    interaction_targets.insert(0, ref.author.id)
            if interaction_targets:
                record_interactions(
                    conn,
                    message.guild.id,
                    message.author.id,
                    interaction_targets,
                    ts=int(message_ts),
                    message_id=message.id,
                )

            velocity_tracker.record_message(
                conn, message.guild.id, message.channel.id, ts=message_ts
            )

        result = await award_message_xp(
            message,
            bot=self.bot,
            db_path=self.ctx.db_path,
            xp_pair_states=self.ctx.xp_pair_states,
            excluded_channel_ids=cfg.xp_excluded_channel_ids,
            settings=cfg.xp_settings,
        )
        if result is not None and isinstance(message.author, discord.Member):
            await handle_level_progress(
                message.author,
                result,
                "text_message",
                level_5_role_id=cfg.level_5_role_id,
                level_up_log_channel_id=cfg.level_up_log_channel_id,
                level_5_log_channel_id=cfg.level_5_log_channel_id,
                settings=cfg.xp_settings,
                db_path=self.ctx.db_path,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return  # DM reactions earn no XP and have no reaction-count tracking
        cfg = self.ctx.guild_config(payload.guild_id)
        delay = 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            try:
                result = await award_image_reaction_xp(
                    payload,
                    bot=self.bot,
                    db_path=self.ctx.db_path,
                    excluded_channel_ids=cfg.xp_excluded_channel_ids,
                    settings=cfg.xp_settings,
                )
                break
            except discord.HTTPException as exc:
                if (
                    exc.status < 500
                    or loop.time() + delay > deadline
                ):
                    raise
                log.warning(
                    "award_image_reaction_xp got %s, retrying in %ss", exc.status, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 16)
        if result is not None:
            member, award = result
            await handle_level_progress(
                member,
                award,
                "image_reaction",
                level_5_role_id=cfg.level_5_role_id,
                level_up_log_channel_id=cfg.level_up_log_channel_id,
                level_5_log_channel_id=cfg.level_5_log_channel_id,
                settings=cfg.xp_settings,
                db_path=self.ctx.db_path,
            )

        if payload.guild_id:
            with self.ctx.open_db() as conn:
                adjust_reaction_count(conn, payload.message_id, str(payload.emoji), +1)
                row = conn.execute(
                    "SELECT author_id, channel_id FROM messages WHERE message_id = ?",
                    (payload.message_id,),
                ).fetchone()
                if row and payload.user_id != int(row["author_id"]):
                    record_reaction(
                        conn,
                        guild_id=payload.guild_id,
                        reactor_id=payload.user_id,
                        author_id=int(row["author_id"]),
                        channel_id=int(row["channel_id"]),
                        message_id=payload.message_id,
                        ts=int(discord.utils.utcnow().timestamp()),
                    )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.guild_id:
            with self.ctx.open_db() as conn:
                adjust_reaction_count(conn, payload.message_id, str(payload.emoji), -1)

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        # Auto-delete tracking is per-message bookkeeping; clear it so we
        # don't try to re-delete a Discord message that's already gone.
        remove_tracked_auto_delete_message(
            self.ctx.db_path, payload.guild_id, payload.channel_id, payload.message_id
        )
        # The messages table itself is a permanent local archive — we never
        # remove rows when Discord deletes a message, so historical content
        # (sentiment, XP audits, mod review) survives.

    async def _dm_admin_permission_warning(
        self, guild: discord.Guild, message: str
    ) -> None:
        owner = guild.owner
        if owner is None:
            return
        try:
            await owner.send(f"⚠️ **{guild.name}** — {message}")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _send_welcome(
        self, member: discord.Member, cfg: "GuildConfig"
    ) -> None:
        from bot_modules.bios.resurrect import resolve_member_bio_link
        from bot_modules.bios.trigger import resolve_bio_placeholders
        from bot_modules.core.db_utils import get_config_value
        from bot_modules.services.welcome_service import (
            server_guide_mention_for,
        )

        channel = member.guild.get_channel(cfg.welcome_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        with self.ctx.open_db() as conn:
            bio_link, bios_channel_mention = resolve_bio_placeholders(
                conn, member.guild.id
            )
            try:
                server_guide_channel_id = int(
                    get_config_value(
                        conn, "server_guide_channel_id", "0", member.guild.id
                    )
                )
            except (TypeError, ValueError):
                server_guide_channel_id = 0
        try:
            member_bio_link = await resolve_member_bio_link(self.ctx, member)
        except Exception:
            log.exception("Failed to resolve member bio link for %d", member.id)
            member_bio_link = ""
        ping = (
            f"<@&{cfg.welcome_ping_role_id}>"
            if cfg.welcome_ping_role_id > 0
            else None
        )
        try:
            await channel.send(
                content=ping,
                embed=build_welcome_embed(
                    member,
                    cfg.welcome_message,
                    bio_link=bio_link,
                    bios_channel_mention=bios_channel_mention,
                    member_bio_link=member_bio_link,
                    server_guide_mention=server_guide_mention_for(
                        server_guide_channel_id
                    ),
                ),
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to send welcome message in #%s.", channel.name
            )
            await self._dm_admin_permission_warning(
                member.guild,
                f"Missing permission to send welcome messages in <#{cfg.welcome_channel_id}>.",
            )
        except discord.HTTPException as exc:
            log.error("Failed to send welcome message: %s", exc)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        before_ids = {r.id for r in before.roles}
        after_ids = {r.id for r in after.roles}
        if before_ids == after_ids:
            return
        now = time.time()
        with self.ctx.open_db() as conn:
            for role in after.roles:
                if role.id not in before_ids:
                    log_role_event(
                        conn, after.guild.id, after.id, role.name, "grant", ts=now
                    )
            for role in before.roles:
                if role.id not in after_ids:
                    log_role_event(
                        conn, after.guild.id, after.id, role.name, "remove", ts=now
                    )

        cfg = self.ctx.guild_config(after.guild.id)
        if (
            cfg.welcome_trigger == "verified"
            and cfg.unverified_role_id > 0
            and cfg.unverified_role_id in (before_ids - after_ids)
            and cfg.welcome_channel_id > 0
        ):
            from bot_modules.bios import db as bios_db

            with self.ctx.open_db() as conn:
                has_bio = bios_db.get_user_bio(conn, after.guild.id, after.id) is not None
            if has_bio:
                await self._send_welcome(after, cfg)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        is_jailed = await check_jail_rejoin(self.ctx, member)

        with self.ctx.open_db() as conn:
            now = time.time()
            upsert_known_user(
                conn,
                guild_id=member.guild.id,
                user_id=member.id,
                username=str(member),
                display_name=member.display_name,
                ts=now,
                is_bot=member.bot,
                current_member=True,
            )
            record_member_event(conn, member.guild.id, member.id, "join", now)
            check_join_raid(
                conn, member.guild.id, member.id, member.created_at.timestamp(), now
            )

        try:
            inviter_id, invite_code = await detect_inviter(member.guild)
        except Exception:
            log.exception("detect_inviter failed for %s in guild %s", member, member.guild.id)
            inviter_id, invite_code = None, None
        if inviter_id is not None:
            with self.ctx.open_db() as conn:
                record_invite(conn, member.guild.id, inviter_id, member.id, invite_code)
            log.info(
                "Invite tracked: %s invited by %s (code: %s)",
                member,
                inviter_id,
                invite_code,
            )

        cfg = self.ctx.guild_config(member.guild.id)

        if cfg.welcome_channel_id > 0 and cfg.welcome_trigger == "join":
            await self._send_welcome(member, cfg)

        if cfg.greeter_chat_channel_id > 0:
            greeter_channel = member.guild.get_channel(cfg.greeter_chat_channel_id)
            if isinstance(greeter_channel, discord.TextChannel):
                try:
                    await greeter_channel.send(f"@here - {member.mention} has arrived")
                except discord.Forbidden:
                    log.warning(
                        "Missing permission to send greeter ping in #%s.",
                        greeter_channel.name,
                    )
                except discord.HTTPException as exc:
                    log.error("Failed to send greeter chat ping: %s", exc)

        if cfg.auto_role_ids and not member.bot and not is_jailed:
            me = member.guild.me
            assignable = [
                r
                for rid in cfg.auto_role_ids
                if (r := member.guild.get_role(rid)) is not None
                and not r.managed
                and r < me.top_role
            ]
            skipped = cfg.auto_role_ids - {r.id for r in assignable}
            if skipped:
                log.warning(
                    "auto_role: skipping unassignable role ids %s for %s in guild %s",
                    skipped,
                    member,
                    member.guild.id,
                )
            if assignable:
                try:
                    await member.add_roles(*assignable, reason="auto-role on join")
                except discord.Forbidden:
                    log.warning(
                        "auto_role: missing permission to assign roles to %s in guild %s",
                        member,
                        member.guild.id,
                    )
                except discord.HTTPException as exc:
                    log.error("auto_role: failed to assign roles to %s: %s", member, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        with self.ctx.open_db() as conn:
            now = time.time()
            mark_member_left(conn, member.guild.id, member.id)
            record_member_event(conn, member.guild.id, member.id, "leave", now)

        cfg = self.ctx.guild_config(member.guild.id)
        if cfg.leave_channel_id <= 0:
            return
        channel = member.guild.get_channel(cfg.leave_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        from bot_modules.bios import db as bios_db
        from bot_modules.bios.trigger import resolve_bio_placeholders
        from bot_modules.core.db_utils import get_config_value
        from bot_modules.services.welcome_service import server_guide_mention_for

        with self.ctx.open_db() as conn:
            bio_link, bios_channel_mention = resolve_bio_placeholders(
                conn, member.guild.id
            )
            stored = bios_db.get_user_bio(conn, member.guild.id, member.id)
            try:
                server_guide_channel_id = int(
                    get_config_value(
                        conn, "server_guide_channel_id", "0", member.guild.id
                    )
                )
            except (TypeError, ValueError):
                server_guide_channel_id = 0

        # If the member has a still-live bio embed (BiosCog may or may
        # not have archived it yet — listener order is undefined), the
        # snapshotted (channel_id, message_id) gives a working jump URL.
        if stored is not None and stored.message_id != 0 and stored.channel_id != 0:
            member_bio_link = (
                f"https://discord.com/channels/{member.guild.id}/"
                f"{stored.channel_id}/{stored.message_id}"
            )
        else:
            member_bio_link = ""

        try:
            await channel.send(
                embed=build_leave_embed(
                    member,
                    cfg.leave_message,
                    bio_link=bio_link,
                    bios_channel_mention=bios_channel_mention,
                    member_bio_link=member_bio_link,
                    server_guide_mention=server_guide_mention_for(
                        server_guide_channel_id
                    ),
                )
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to send leave message in #%s.", channel.name
            )
            await self._dm_admin_permission_warning(
                member.guild,
                f"Missing permission to send leave messages in <#{cfg.leave_channel_id}>.",
            )
        except discord.HTTPException as exc:
            log.error("Failed to send leave message: %s", exc)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        if payload.guild_id is None:
            return
        # Auto-delete tracking only — the messages table is a permanent
        # archive (see on_raw_message_delete for the rationale).
        remove_tracked_auto_delete_messages(
            self.ctx.db_path, payload.guild_id, payload.channel_id, payload.message_ids
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if (
            interaction.type == discord.InteractionType.application_command
            and interaction.data
        ):
            data: dict = interaction.data  # type: ignore[assignment]
            cmd = data.get("name", "?")
            opts: list[dict] = data.get("options") or []
            parts: list[str] = [str(cmd)]
            for opt in opts:
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

    @commands.Cog.listener()
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        cmd = interaction.command.qualified_name if interaction.command else "?"
        log.exception(
            "Command /%s failed for %s (%s)",
            cmd,
            interaction.user,
            interaction.user.id,
            exc_info=error,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(EventsCog(bot, bot.ctx))
