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
    remove_tracked_auto_delete_message,
    remove_tracked_auto_delete_messages,
    should_track_auto_delete_message,
    track_auto_delete_message,
)
from bot_modules.services.discord_scan import collect_messageable_channels
from bot_modules.services.greeting_watch_service import is_greeting, record_greeting
from bot_modules.services.interaction_graph import record_interactions
from bot_modules.services.invite_tracker import detect_inviter, record_invite, refresh_invite_cache
from bot_modules.services.message_store import (
    adjust_reaction_count,
    classify_media_kind,
    mark_member_left,
    record_member_event,
    record_reaction,
    set_reaction_count,
    store_message,
    upsert_known_channel,
    upsert_known_user,
)
from bot_modules.services.message_xp_service import (
    award_image_reaction_xp,
    award_message_xp,
    award_reaction_given_xp,
)
from bot_modules.services.sentiment_service import score_text
from bot_modules.services.welcome_service import build_leave_embed, build_welcome_embed
from bot_modules.services.wellness_enforcement import wellness_on_message
from bot_modules.services.xp_service import handle_level_progress, nsfw_grant_role_id
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.utils import format_guild_for_log, get_guild_channel_or_thread
from bot_modules.core.xp_system import count_xp_events, log_role_event, record_member_activity
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_quests_service import (
    fire_trigger_inline,
    fire_trigger_quests,
)
from bot_modules.services.economy_service import (
    EconSettings,
    LoginOutcome,
    load_econ_settings,
    notify_member,
    open_qotd_for,
    process_login,
    try_award_qotd,
)

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
            _g_id = g.id
            _ch_id = channel.id

            def _do_channel_query():
                with ctx.open_db() as conn:
                    _row = conn.execute(
                        "SELECT MAX(message_id) FROM messages WHERE guild_id = ? AND channel_id = ?",
                        (_g_id, _ch_id),
                    ).fetchone()
                return _row

            row = await asyncio.to_thread(_do_channel_query)
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
                    gcfg = ctx.guild_config(g.id)
                    retain = gcfg.retains_content
                    for msg in msgs:
                        msg_ts = msg.created_at.timestamp() if msg.created_at else time.time()
                        sentiment, emotion = score_text(msg.content)
                        mention_ids = (
                            []
                            if msg.author.bot
                            else _message_mention_ids(gcfg.recorded_bot_user_ids, msg)
                        )
                        reply_to_id = (
                            msg.reference.message_id
                            if msg.reference and msg.reference.message_id
                            else None
                        )
                        if should_track_auto_delete_message(
                            conn, g.id, channel.id, has_media=bool(msg.attachments)
                        ):
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
                            attachment_urls=[a.url for a in msg.attachments]
                            if retain
                            else [],
                            mention_ids=mention_ids,
                            sentiment=sentiment,
                            emotion=emotion,
                            embeds=[_discord_embed_to_dict(e) for e in msg.embeds]
                            if retain
                            else (),
                            retain_content=retain,
                            media_kind=classify_media_kind(
                                [a.filename for a in msg.attachments]
                            ),
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

        cfg = self.ctx.guild_config(self.ctx.guild_id)
        log.info(
            "Primary guild %s (ID: %s, guarding: %s)",
            _primary_guild.name if _primary_guild else self.ctx.guild_id,
            self.ctx.guild_id,
            [_ch(c) for c in cfg.spoiler_required_channels],
        )
        log.info(
            "XP config loaded: level-%s role=%s level-up-log=%s level-%s-log=%s.",
            cfg.xp_settings.role_grant_level,
            _ro(cfg.level_5_role_id),
            _ch(cfg.level_up_log_channel_id),
            cfg.xp_settings.role_grant_level,
            _ch(cfg.level_5_log_channel_id),
        )
        log.debug("XP excluded channels: %s", sorted(cfg.xp_excluded_channel_ids))

        now_ts = time.time()
        for g in self.bot.guilds:
            await refresh_invite_cache(g)
            _guild_members = [(m.id, str(m), m.display_name, m.bot) for m in g.members]
            _guild_channels = [(ch.id, ch.name) for ch in g.channels if hasattr(ch, "name")]
            _guild_id = g.id
            _guild_log = format_guild_for_log(g)

            def _do_upserts():
                with self.ctx.open_db() as conn:
                    for uid, uname, dname, is_bot in _guild_members:
                        upsert_known_user(
                            conn,
                            guild_id=_guild_id,
                            user_id=uid,
                            username=uname,
                            display_name=dname,
                            ts=now_ts,
                            is_bot=is_bot,
                            current_member=True,
                        )
                    for ch_id, ch_name in _guild_channels:
                        upsert_known_channel(
                            conn,
                            guild_id=_guild_id,
                            channel_id=ch_id,
                            channel_name=ch_name,
                            ts=now_ts,
                        )
                    log.debug(
                        "XP event rows for guild %s: %s",
                        _guild_log,
                        count_xp_events(conn, _guild_id),
                    )

            await asyncio.to_thread(_do_upserts)
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
        # Bound to a local so the narrowing survives into the _persist_*
        # closures below (pyright can't narrow `message.guild` across a
        # nested-function boundary).
        guild_id = message.guild.id
        cfg = self.ctx.guild_config(guild_id)
        is_bot_author = message.author.bot
        # At storage level "none" (the default) content/media are dropped, so
        # skip building the attachment/embed payloads that store_message would
        # discard. Derivations (sentiment/mentions/XP) are still computed below.
        retain = cfg.retains_content

        message_ts = (
            message.created_at.timestamp() if message.created_at else time.time()
        )

        reply_to_id: int | None = None
        if message.reference and message.reference.message_id:
            reply_to_id = message.reference.message_id
        attachment_urls = [a.url for a in message.attachments] if retain else []
        # media_kind is metadata (an attachment classification, not a URL), so it
        # is recorded regardless of storage level to keep media metrics working.
        media_kind = classify_media_kind([a.filename for a in message.attachments])

        if is_bot_author:
            sentiment, emotion = await asyncio.to_thread(score_text, message.content)

            def _persist_bot_message():
                with self.ctx.open_db() as conn:
                    if should_track_auto_delete_message(
                        conn,
                        guild_id,
                        message.channel.id,
                        has_media=bool(message.attachments),
                    ):
                        track_auto_delete_message(
                            conn,
                            guild_id,
                            message.channel.id,
                            message.id,
                            message_ts,
                        )
                    store_message(
                        conn,
                        message_id=message.id,
                        guild_id=guild_id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        content=_archived_message_content(message),
                        reply_to_id=reply_to_id,
                        ts=int(message_ts),
                        attachment_urls=attachment_urls,
                        mention_ids=[],
                        sentiment=sentiment,
                        emotion=emotion,
                        embeds=[_discord_embed_to_dict(e) for e in message.embeds]
                        if retain
                        else (),
                        retain_content=retain,
                        media_kind=media_kind,
                    )
                    if sentiment is not None:
                        conn.execute(
                            "INSERT OR IGNORE INTO message_sentiment "
                            "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                message.id,
                                guild_id,
                                message.channel.id,
                                sentiment,
                                emotion,
                                message_ts,
                            ),
                        )
                    upsert_known_user(
                        conn,
                        guild_id=guild_id,
                        user_id=message.author.id,
                        username=str(message.author),
                        display_name=message.author.display_name,
                        ts=message_ts,
                        is_bot=message.author.bot,
                    )
                    upsert_known_channel(
                        conn,
                        guild_id=guild_id,
                        channel_id=message.channel.id,
                        channel_name=getattr(message.channel, "name", str(message.channel.id)),
                        ts=message_ts,
                    )

            await asyncio.to_thread(_persist_bot_message)
            return

        archive_content = _archived_message_content(message)
        if not _counts_as_member_activity(message):
            mention_ids = _message_mention_ids(cfg.recorded_bot_user_ids, message)

            def _persist_nonmember_message():
                with self.ctx.open_db() as conn:
                    if should_track_auto_delete_message(
                        conn,
                        guild_id,
                        message.channel.id,
                        has_media=bool(message.attachments),
                    ):
                        track_auto_delete_message(
                            conn,
                            guild_id,
                            message.channel.id,
                            message.id,
                            message_ts,
                        )
                    store_message(
                        conn,
                        message_id=message.id,
                        guild_id=guild_id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        content=archive_content,
                        reply_to_id=reply_to_id,
                        ts=int(message_ts),
                        attachment_urls=attachment_urls,
                        mention_ids=mention_ids,
                        embeds=[_discord_embed_to_dict(e) for e in message.embeds]
                        if retain
                        else (),
                        retain_content=retain,
                        media_kind=media_kind,
                    )
                    upsert_known_user(
                        conn,
                        guild_id=guild_id,
                        user_id=message.author.id,
                        username=str(message.author),
                        display_name=message.author.display_name,
                        ts=message_ts,
                    )
                    upsert_known_channel(
                        conn,
                        guild_id=guild_id,
                        channel_id=message.channel.id,
                        channel_name=getattr(
                            message.channel, "name", str(message.channel.id)
                        ),
                        ts=message_ts,
                    )

            await asyncio.to_thread(_persist_nonmember_message)
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

        def _persist_member_message():
            with self.ctx.open_db() as conn:
                record_member_activity(
                    conn,
                    guild_id,
                    message.author.id,
                    message.channel.id,
                    message.id,
                    message_ts,
                )
                if should_track_auto_delete_message(
                    conn,
                    guild_id,
                    message.channel.id,
                    has_media=bool(message.attachments),
                ):
                    track_auto_delete_message(
                        conn,
                        guild_id,
                        message.channel.id,
                        message.id,
                        message_ts,
                    )

                store_message(
                    conn,
                    message_id=message.id,
                    guild_id=guild_id,
                    channel_id=message.channel.id,
                    author_id=message.author.id,
                    content=archive_content,
                    reply_to_id=reply_to_id,
                    ts=int(message_ts),
                    attachment_urls=attachment_urls,
                    mention_ids=mention_ids,
                    sentiment=sentiment,
                    emotion=emotion,
                    embeds=[_discord_embed_to_dict(e) for e in message.embeds]
                    if retain
                    else (),
                    retain_content=retain,
                    media_kind=media_kind,
                )

                if sentiment is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO message_sentiment "
                        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            message.id,
                            guild_id,
                            message.channel.id,
                            sentiment,
                            emotion,
                            message_ts,
                        ),
                    )

                upsert_known_user(
                    conn,
                    guild_id=guild_id,
                    user_id=message.author.id,
                    username=str(message.author),
                    display_name=message.author.display_name,
                    ts=message_ts,
                    is_bot=message.author.bot,
                )

                upsert_known_channel(
                    conn,
                    guild_id=guild_id,
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
                        guild_id,
                        message.author.id,
                        interaction_targets,
                        ts=int(message_ts),
                        message_id=message.id,
                    )

        await asyncio.to_thread(_persist_member_message)

        # Greeting watch: if this is a "good morning"/"hello" in a watched
        # channel, stamp it so the background loop can DM the notify user when
        # nobody replies to or mentions the greeter in time. Content is judged
        # here in-memory — the default "none" storage level drops it before it
        # reaches the DB, so it can't be matched after the fact.
        if (
            cfg.greeting_watch_enabled
            and message.channel.id in cfg.greeting_watch_channel_ids
            and is_greeting(message.content)
        ):

            def _record_greeting():
                with self.ctx.open_db() as conn:
                    record_greeting(
                        conn,
                        guild_id=guild_id,
                        message_id=message.id,
                        channel_id=message.channel.id,
                        author_id=message.author.id,
                        created_ts=int(message_ts),
                    )

            await asyncio.to_thread(_record_greeting)

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
                nsfw_role_id=nsfw_grant_role_id(cfg.grant_roles),
            )

        # Economy faucets — daily text login + QOTD reward. Optional and fully
        # fail-safe: an economy error must never break message/XP processing.
        if isinstance(message.author, discord.Member):
            try:
                await self._process_economy_message(message)
            except Exception:
                log.exception("economy on_message hook failed")

    async def _process_economy_message(self, message: discord.Message) -> None:
        """Pay the daily text login and any open QOTD reward for a member message.

        The DB work (settings load, streak read, login, QOTD award) runs in one
        off-loop transaction; the streak read *before* ``process_login`` captures
        the pre-login streak so a trivial 1→short reset stays silent (spec §10).
        """
        assert message.guild is not None
        guild_id = message.guild.id
        user_id = message.author.id
        channel_id = message.channel.id
        booster = (
            isinstance(message.author, discord.Member)
            and message.author.premium_since is not None
        )
        message_id = message.id
        parent_id = getattr(message.channel, "parent_id", None)
        channel_ids = tuple(c for c in (channel_id, parent_id) if c is not None)
        # A reply counts only against someone ELSE's message. When the
        # reference isn't resolved in cache we can't verify the author, so it
        # counts — the dedup and target still bound the payout.
        ref = message.reference
        resolved = ref.resolved if ref is not None else None
        is_reply = ref is not None and not (
            isinstance(resolved, discord.Message) and resolved.author.id == user_id
        )

        def _econ_work() -> tuple[EconSettings, LoginOutcome, int] | None:
            with self.ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    return None
                offset = get_tz_offset_hours(conn, guild_id)
                today = local_day_for(time.time(), offset)
                prior = conn.execute(
                    "SELECT current_streak FROM econ_streaks "
                    "WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                prior_streak = int(prior["current_streak"]) if prior else 0
                outcome = process_login(
                    conn,
                    settings,
                    guild_id,
                    user_id,
                    local_day=today,
                    source="text",
                    booster=booster,
                )
                qotd = open_qotd_for(conn, guild_id, channel_id, today)
                if qotd is not None:
                    newly_awarded = try_award_qotd(
                        conn,
                        settings,
                        int(qotd["id"]),
                        guild_id,
                        user_id,
                        booster=booster,
                    )
                    if newly_awarded:
                        # First qualifying reply → the qotd_reply quest
                        # trigger (silent; wallet/quests carry the news).
                        fire_trigger_quests(
                            conn, settings, guild_id, "qotd_reply", user_id,
                            local_day=today,
                            occurrence=str(int(qotd["id"])),
                            booster=booster,
                        )
                # Message/reply quest triggers (usually counted quests —
                # occurrence = the message, so nothing double-counts).
                fire_trigger_quests(
                    conn, settings, guild_id, "message_sent", user_id,
                    local_day=today, occurrence=str(message_id),
                    booster=booster, channel_ids=channel_ids,
                )
                if is_reply:
                    fire_trigger_quests(
                        conn, settings, guild_id, "reply_sent", user_id,
                        local_day=today, occurrence=str(message_id),
                        booster=booster, channel_ids=channel_ids,
                    )
                if outcome is None:
                    return None
                return settings, outcome, prior_streak

        result = await asyncio.to_thread(_econ_work)
        if result is None:
            return
        settings, outcome, prior_streak = result

        # The login payout itself is silent; only milestones, a used grace day,
        # or a *meaningful* streak reset (a real streak, not a 1→1 blip) DM.
        notify_reset = outcome.reset and prior_streak >= 3
        if not (outcome.milestone > 0 or outcome.grace_consumed or notify_reset):
            return

        accent = await resolve_accent_color(self.ctx.db_path, message.guild)
        embed = self._econ_login_embed(settings, outcome, prior_streak, accent)
        # Streak/milestone/grace notices are recurring engagement — only DM
        # players who took the opt-in economy role. Payout stays silent for
        # everyone else (matches the quest-card path and the game_role_id
        # design intent).
        await notify_member(
            self.bot,
            self.ctx.db_path,
            guild_id,
            user_id,
            embed=embed,
            require_game_role=True,
        )

    @staticmethod
    def _econ_login_embed(
        settings: EconSettings,
        outcome: LoginOutcome,
        prior_streak: int,
        accent: discord.Color,
    ) -> discord.Embed:
        """Branded streak-update embed covering every triggered login event."""
        embed = discord.Embed(
            title=f"{settings.currency_emoji} Daily streak",
            color=accent,
        )
        if outcome.milestone > 0:
            unit = settings.currency_name if outcome.milestone == 1 else settings.currency_plural
            embed.add_field(
                name=f"🏆 Day {outcome.streak} milestone!",
                value=f"Bonus **{outcome.milestone:,}** {unit}",
                inline=False,
            )
        if outcome.grace_consumed:
            embed.add_field(
                name="🛟 Streak saved",
                value=(
                    f"We covered a missed day — your streak lives on at "
                    f"day **{outcome.streak}**."
                ),
                inline=False,
            )
        if outcome.reset and prior_streak >= 3:
            embed.add_field(
                name="🔁 Streak reset",
                value=(
                    f"Your **{prior_streak}**-day streak ended. Starting fresh "
                    f"at day **{outcome.streak}**."
                ),
                inline=False,
            )
        return embed

    async def _fetch_reaction_message(
        self, payload: discord.RawReactionActionEvent
    ) -> discord.Message | None:
        """Fetch the reacted-to message once, shared by both reaction XP awards.

        Retries transient 5xx with capped backoff (matching the prior inline
        loop); returns None on a permanent failure so the handler stays robust.
        """
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return None
        channel = get_guild_channel_or_thread(guild, payload.channel_id)
        if channel is None:
            return None
        delay = 1
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        while True:
            try:
                return await channel.fetch_message(payload.message_id)
            except (discord.Forbidden, discord.NotFound):
                return None
            except discord.HTTPException as exc:
                if exc.status < 500 or loop.time() + delay > deadline:
                    log.warning(
                        "reaction message fetch got %s; giving up", exc.status
                    )
                    return None
                log.warning(
                    "reaction message fetch got %s, retrying in %ss", exc.status, delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 16)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return  # DM reactions earn no XP and have no reaction-count tracking
        cfg = self.ctx.guild_config(payload.guild_id)

        # One fetch feeds both awards (author's image XP + reactor's given XP).
        message = await self._fetch_reaction_message(payload)

        # Reaction-given XP pays the reactor; keep it fail-safe so a hiccup here
        # never blocks the image-react award below.
        try:
            given = await award_reaction_given_xp(
                payload,
                bot=self.bot,
                db_path=self.ctx.db_path,
                excluded_channel_ids=cfg.xp_excluded_channel_ids,
                settings=cfg.xp_settings,
                message=message,
            )
        except Exception:
            log.exception("award_reaction_given_xp failed")
            given = None
        if given is not None:
            reactor, given_award = given
            await handle_level_progress(
                reactor,
                given_award,
                "reaction_given",
                level_5_role_id=cfg.level_5_role_id,
                level_up_log_channel_id=cfg.level_up_log_channel_id,
                level_5_log_channel_id=cfg.level_5_log_channel_id,
                settings=cfg.xp_settings,
                db_path=self.ctx.db_path,
                nsfw_role_id=nsfw_grant_role_id(cfg.grant_roles),
            )
            # Reaction quest trigger — `given` is non-None only when the XP
            # dedup admitted a NEW (message, reactor) pair, so the quest
            # inherits the farm guard (no self-reacts, no repeats, no bots).
            _guild_id = payload.guild_id
            _channel = getattr(message, "channel", None)
            _parent_id = getattr(_channel, "parent_id", None)
            _channel_ids = tuple(
                c for c in (payload.channel_id, _parent_id) if c is not None
            )
            _booster = (
                isinstance(reactor, discord.Member)
                and reactor.premium_since is not None
            )

            def _fire_reaction_quests():
                with self.ctx.open_db() as conn:
                    fire_trigger_inline(
                        conn,
                        _guild_id,
                        "reaction_given",
                        reactor.id,
                        occurrence=str(payload.message_id),
                        booster=_booster,
                        channel_ids=_channel_ids,
                    )

            await asyncio.to_thread(_fire_reaction_quests)

        try:
            result = await award_image_reaction_xp(
                payload,
                bot=self.bot,
                db_path=self.ctx.db_path,
                excluded_channel_ids=cfg.xp_excluded_channel_ids,
                settings=cfg.xp_settings,
                message=message,
            )
        except discord.HTTPException as exc:
            log.warning("award_image_reaction_xp failed: %s", exc)
            result = None
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
                nsfw_role_id=nsfw_grant_role_id(cfg.grant_roles),
            )

        if payload.guild_id:
            gid = payload.guild_id  # bind so narrowing survives into the closure

            def _record_reaction_add():
                with self.ctx.open_db() as conn:
                    adjust_reaction_count(conn, payload.message_id, str(payload.emoji), +1)
                    row = conn.execute(
                        "SELECT author_id, channel_id FROM messages WHERE message_id = ?",
                        (payload.message_id,),
                    ).fetchone()
                    if row and payload.user_id != int(row["author_id"]):
                        record_reaction(
                            conn,
                            guild_id=gid,
                            reactor_id=payload.user_id,
                            author_id=int(row["author_id"]),
                            channel_id=int(row["channel_id"]),
                            message_id=payload.message_id,
                            ts=int(discord.utils.utcnow().timestamp()),
                        )

            await asyncio.to_thread(_record_reaction_add)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(
        self, payload: discord.RawReactionActionEvent
    ) -> None:
        if payload.guild_id:
            def _record_reaction_remove():
                with self.ctx.open_db() as conn:
                    adjust_reaction_count(conn, payload.message_id, str(payload.emoji), -1)

            await asyncio.to_thread(_record_reaction_remove)

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
        _guild_id = member.guild.id

        def _do_welcome_db():
            with self.ctx.open_db() as conn:
                _bio_link, _bios_ch_mention = resolve_bio_placeholders(conn, _guild_id)
                try:
                    _sg_channel_id = int(
                        get_config_value(conn, "server_guide_channel_id", "0", _guild_id)
                    )
                except (TypeError, ValueError):
                    _sg_channel_id = 0
            return _bio_link, _bios_ch_mention, _sg_channel_id

        bio_link, bios_channel_mention, server_guide_channel_id = await asyncio.to_thread(_do_welcome_db)
        try:
            member_bio_link = await resolve_member_bio_link(self.ctx, member)
        except Exception:
            log.exception("Failed to resolve member bio link for %d", member.id)
            member_bio_link = ""
        # Mentions inside an embed don't notify anyone, so the user/role ping
        # has to ride in the message content to actually fire a notification.
        ping_parts: list[str] = []
        if cfg.welcome_ping_role_id > 0:
            ping_parts.append(f"<@&{cfg.welcome_ping_role_id}>")
        if cfg.welcome_ping_member:
            ping_parts.append(member.mention)
        ping = " ".join(ping_parts) or None
        try:
            await channel.send(
                content=ping,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
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
        _guild_id = after.guild.id
        _member_id = after.id
        _granted = [r.name for r in after.roles if r.id not in before_ids]
        _removed = [r.name for r in before.roles if r.id not in after_ids]

        def _do_log_role_events():
            with self.ctx.open_db() as conn:
                for name in _granted:
                    log_role_event(conn, _guild_id, _member_id, name, "grant", ts=now)
                for name in _removed:
                    log_role_event(conn, _guild_id, _member_id, name, "remove", ts=now)

        await asyncio.to_thread(_do_log_role_events)
        cfg = self.ctx.guild_config(after.guild.id)
        # Welcome fires the moment the unverified role is stripped (e.g. once
        # DoubleCounter finishes its alt scan and lifts the gate). No bio is
        # required — {member_bio_link} simply resolves to "" when absent.
        if (
            cfg.welcome_trigger == "verified"
            and cfg.unverified_role_id > 0
            and cfg.unverified_role_id in (before_ids - after_ids)
            and cfg.welcome_channel_id > 0
        ):
            await self._send_welcome(after, cfg)
            if cfg.greeter_chat_channel_id > 0:
                greeter_channel = after.guild.get_channel(cfg.greeter_chat_channel_id)
                if isinstance(greeter_channel, discord.TextChannel):
                    try:
                        await greeter_channel.send(f"@here - {after.mention} has arrived")
                    except discord.Forbidden:
                        log.warning(
                            "Missing permission to send greeter ping in #%s.",
                            greeter_channel.name,
                        )
                    except discord.HTTPException as exc:
                        log.error("Failed to send greeter chat ping: %s", exc)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        is_jailed = await check_jail_rejoin(self.ctx, member)

        _guild_id = member.guild.id
        _member_id = member.id
        _username = str(member)
        _display_name = member.display_name
        _is_bot = member.bot

        def _do_member_join():
            _now = time.time()
            with self.ctx.open_db() as conn:
                upsert_known_user(
                    conn,
                    guild_id=_guild_id,
                    user_id=_member_id,
                    username=_username,
                    display_name=_display_name,
                    ts=_now,
                    is_bot=_is_bot,
                    current_member=True,
                )
                record_member_event(conn, _guild_id, _member_id, "join", _now)

        await asyncio.to_thread(_do_member_join)

        try:
            inviter_id, invite_code = await detect_inviter(member.guild)
        except Exception:
            log.exception("detect_inviter failed for %s in guild %s", member, member.guild.id)
            inviter_id, invite_code = None, None
        if inviter_id is not None:
            _inv_id: int = inviter_id
            _inv_code = invite_code
            _inviter = member.guild.get_member(_inv_id)
            _inv_booster = _inviter is not None and _inviter.premium_since is not None

            def _do_record_invite():
                with self.ctx.open_db() as conn:
                    record_invite(conn, _guild_id, _inv_id, _member_id, _inv_code)
                    # Invite quest trigger for the inviter. Occurrence = the
                    # invitee, so a rejoin (or a re-fire off the OR IGNOREd
                    # edge) never double-pays the same recruit.
                    fire_trigger_inline(
                        conn,
                        _guild_id,
                        "invite",
                        _inv_id,
                        occurrence=str(_member_id),
                        booster=_inv_booster,
                    )

            await asyncio.to_thread(_do_record_invite)
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
        _guild_id = member.guild.id
        _member_id = member.id

        def _do_member_leave():
            _now = time.time()
            with self.ctx.open_db() as conn:
                mark_member_left(conn, _guild_id, _member_id)
                record_member_event(conn, _guild_id, _member_id, "leave", _now)

        await asyncio.to_thread(_do_member_leave)

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

        def _do_leave_db():
            with self.ctx.open_db() as conn:
                _bio_link, _bios_ch_mention = resolve_bio_placeholders(conn, _guild_id)
                _stored = bios_db.get_user_bio(conn, _guild_id, _member_id)
                try:
                    _sg_channel_id = int(
                        get_config_value(conn, "server_guide_channel_id", "0", _guild_id)
                    )
                except (TypeError, ValueError):
                    _sg_channel_id = 0
            return _bio_link, _bios_ch_mention, _stored, _sg_channel_id

        bio_link, bios_channel_mention, stored, server_guide_channel_id = await asyncio.to_thread(_do_leave_db)

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


async def setup(bot: Bot) -> None:
    await bot.add_cog(EventsCog(bot, bot.ctx))
