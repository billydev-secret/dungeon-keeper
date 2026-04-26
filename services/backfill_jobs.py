"""Async backfill jobs — replace the slash-command versions of /xp_backfill_history,
/interaction_scan, and /report backfill_roles. Each job takes the AppContext and
guild (already resolved by the caller) and returns a stats dict.

Designed to be invoked from FastAPI routes via BackgroundTasks. Each job runs in
the bot's event loop, so they can use discord.py channel iteration.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from services.interaction_graph import clear_interaction_data, record_interactions
from services.message_store import (
    set_reaction_count,
    store_message,
)
from services.xp_service import maybe_grant_level_role
from xp_system import (
    DEFAULT_XP_SETTINGS,
    PairState,
    MessageXpContext,
    XP_SOURCE_REPLY,
    XP_SOURCE_TEXT,
    apply_xp_award,
    calculate_message_xp,
    is_channel_xp_eligible,
    is_message_processed,
    log_role_event,
    mark_message_processed,
    normalize_message_content,
    record_member_activity,
    record_xp_event,
    update_pair_state,
)

log = logging.getLogger("dungeonkeeper.backfill")


# ── Channel discovery helpers ────────────────────────────────────────────


async def _collect_channels(
    guild: discord.Guild,
    me: discord.Member | None,
    *,
    selected_channel: discord.TextChannel | discord.Thread | None = None,
) -> list[discord.TextChannel | discord.Thread]:
    """All text channels + active threads + archived threads readable by *me*.

    If *selected_channel* is set, returns only that channel (plus its threads
    when it's a TextChannel).
    """
    channels: list[discord.TextChannel | discord.Thread] = []
    seen_ids: set[int] = set()

    if selected_channel is not None:
        if isinstance(selected_channel, discord.Thread):
            channels.append(selected_channel)
            seen_ids.add(selected_channel.id)
        else:
            channels.append(selected_channel)
            seen_ids.add(selected_channel.id)
            for thread in selected_channel.threads:
                if thread.id not in seen_ids:
                    channels.append(thread)
                    seen_ids.add(thread.id)
        return channels

    for channel in guild.text_channels:
        channels.append(channel)
        seen_ids.add(channel.id)

    for thread in guild.threads:
        if thread.id not in seen_ids:
            channels.append(thread)
            seen_ids.add(thread.id)

    for text_channel in guild.text_channels:
        if me and not text_channel.permissions_for(me).read_message_history:
            continue
        try:
            async for archived in text_channel.archived_threads(limit=None):
                if archived.id not in seen_ids:
                    channels.append(archived)
                    seen_ids.add(archived.id)
        except (discord.Forbidden, discord.HTTPException):
            pass

    return channels


def _embed_to_dict(e: discord.Embed) -> dict:
    return {
        "title": e.title,
        "description": e.description,
        "url": e.url,
        "color": e.color.value if e.color else None,
        "image": e.image.url if e.image else None,
        "thumbnail": e.thumbnail.url if e.thumbnail else None,
        "author": {"name": e.author.name} if e.author else None,
        "footer": {"text": e.footer.text} if e.footer else None,
    }


def _counts_as_member_activity(message: discord.Message) -> bool:
    """Filter messages that should not count toward activity / interactions."""
    if message.author.bot:
        return False
    if message.type not in (
        discord.MessageType.default,
        discord.MessageType.reply,
    ):
        return False
    return True


def _archived_message_content(message: discord.Message) -> str | None:
    return message.content or None


# ── Role events backfill ────────────────────────────────────────────────


def backfill_roles_sync(ctx: Any, guild: discord.Guild) -> dict[str, int]:
    """Sync the role_events log with current server membership.

    Adds a 'grant' event for any (user, role) pair currently held but not
    represented in the events log; adds a 'remove' event for any pair that
    appears net-granted in the log but isn't currently held.
    """
    grants_added = 0
    removes_added = 0
    now_ts = time.time()

    with ctx.open_db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, role_name,
                   SUM(CASE WHEN action = 'grant' THEN 1 ELSE -1 END) AS net
            FROM role_events
            WHERE guild_id = ?
            GROUP BY user_id, role_name
            """,
            (guild.id,),
        ).fetchall()
        db_state: dict[tuple[int, str], int] = {
            (int(r[0]), str(r[1])): int(r[2]) for r in rows
        }

        live_pairs: set[tuple[int, str]] = set()
        for role in guild.roles:
            if role.is_default():
                continue
            for m in role.members:
                live_pairs.add((m.id, role.name))

        for user_id, role_name in live_pairs:
            net = db_state.get((user_id, role_name), 0)
            if net <= 0:
                m = guild.get_member(user_id)
                ts = m.joined_at.timestamp() if m and m.joined_at else now_ts
                log_role_event(conn, guild.id, user_id, role_name, "grant", ts=ts)
                grants_added += 1

        for (user_id, role_name), net in db_state.items():
            if net > 0 and (user_id, role_name) not in live_pairs:
                log_role_event(
                    conn, guild.id, user_id, role_name, "remove", ts=now_ts
                )
                removes_added += 1

    return {"grants_added": grants_added, "removes_added": removes_added}


# ── XP history backfill ─────────────────────────────────────────────────


async def backfill_xp_async(
    ctx: Any, guild: discord.Guild, days: int = 0
) -> dict[str, Any]:
    """Scan past messages and award any XP that wasn't recorded yet."""
    now_dt = datetime.now(timezone.utc)
    after_dt = None if days == 0 else now_dt - timedelta(days=days)

    granted_members: dict[int, discord.Member] = {}
    backfill_user_state: dict[int, tuple[float, str]] = {}
    pair_states: dict[int, PairState] = {}
    stats: dict[str, Any] = {
        "channels_scanned": 0,
        "messages_seen": 0,
        "messages_processed": 0,
        "messages_skipped_processed": 0,
        "messages_awarded": 0,
        "xp_awarded": 0.0,
    }

    me = guild.get_member(ctx.bot.user.id) if ctx.bot and ctx.bot.user else None
    all_channels = await _collect_channels(guild, me)

    with ctx.open_db() as conn:
        for channel in all_channels:
            channel_id: int | None = getattr(channel, "id", None)
            parent_id = getattr(channel, "parent_id", None)
            if channel_id is None or not is_channel_xp_eligible(
                channel_id, parent_id, ctx.xp_excluded_channel_ids
            ):
                continue

            if me and not channel.permissions_for(me).read_message_history:
                continue

            stats["channels_scanned"] += 1
            channel_pair_state = pair_states.get(channel.id)

            try:
                async for message in channel.history(
                    limit=None, after=after_dt, oldest_first=True
                ):
                    stats["messages_seen"] += 1

                    if not message.guild or message.author.bot:
                        continue

                    if is_message_processed(conn, guild.id, message.id):
                        stats["messages_skipped_processed"] += 1
                        continue

                    resolved_ref = (
                        message.reference.resolved
                        if message.reference
                        and isinstance(message.reference.resolved, discord.Message)
                        else None
                    )
                    is_reply_to_human = bool(
                        resolved_ref
                        and not resolved_ref.author.bot
                        and resolved_ref.author.id != message.author.id
                    )

                    now_ts = (
                        message.created_at.timestamp()
                        if message.created_at
                        else time.time()
                    )
                    normalized_content = normalize_message_content(message.content)
                    channel_pair_state, pair_streak = update_pair_state(
                        channel_pair_state, message.author.id
                    )
                    pair_states[channel.id] = channel_pair_state

                    prior_ts = None
                    prior_norm = None
                    if message.author.id in backfill_user_state:
                        prior_ts, prior_norm = backfill_user_state[
                            message.author.id
                        ]

                    breakdown = calculate_message_xp(
                        MessageXpContext(
                            content=message.content,
                            seconds_since_last_message=(
                                None if prior_ts is None else now_ts - prior_ts
                            ),
                            is_duplicate=bool(normalized_content)
                            and normalized_content == prior_norm,
                            is_reply_to_human=is_reply_to_human,
                            pair_streak=pair_streak,
                        ),
                        ctx.xp_settings,
                    )

                    award = apply_xp_award(
                        conn,
                        guild.id,
                        message.author.id,
                        breakdown.awarded_xp,
                        settings=ctx.xp_settings,
                    )

                    reply_award = 0.0
                    if breakdown.reply_bonus_xp > 0:
                        reply_award = round(
                            breakdown.reply_bonus_xp
                            * breakdown.cooldown_multiplier
                            * breakdown.duplicate_multiplier
                            * breakdown.pair_multiplier,
                            2,
                        )
                    text_award = round(max(0.0, award.awarded_xp - reply_award), 2)
                    record_xp_event(
                        conn,
                        guild.id,
                        message.author.id,
                        XP_SOURCE_TEXT,
                        text_award,
                        now_ts,
                        channel_id=message.channel.id,
                    )
                    record_xp_event(
                        conn,
                        guild.id,
                        message.author.id,
                        XP_SOURCE_REPLY,
                        reply_award,
                        now_ts,
                        channel_id=message.channel.id,
                    )
                    mark_message_processed(
                        conn,
                        guild.id,
                        message.id,
                        message.channel.id,
                        message.author.id,
                        now_ts,
                    )
                    record_member_activity(
                        conn,
                        guild.id,
                        message.author.id,
                        message.channel.id,
                        message.id,
                        now_ts,
                    )

                    backfill_user_state[message.author.id] = (
                        now_ts,
                        normalized_content,
                    )
                    stats["messages_processed"] += 1
                    if award.awarded_xp > 0:
                        stats["messages_awarded"] += 1
                        stats["xp_awarded"] += award.awarded_xp
                        m = (
                            message.author
                            if isinstance(message.author, discord.Member)
                            else guild.get_member(message.author.id)
                        )
                        if (
                            m
                            and award.new_level
                            >= DEFAULT_XP_SETTINGS.role_grant_level
                        ):
                            granted_members[m.id] = m
            except discord.Forbidden:
                continue

    for m in granted_members.values():
        await maybe_grant_level_role(
            m, DEFAULT_XP_SETTINGS.role_grant_level, ctx.level_5_role_id
        )

    stats["xp_awarded"] = round(stats["xp_awarded"], 2)
    return stats


# ── Interaction graph backfill ──────────────────────────────────────────


async def backfill_interactions_async(
    ctx: Any,
    guild: discord.Guild,
    *,
    days: int = 0,
    reset: bool = False,
    channel_id: int | None = None,
) -> dict[str, Any]:
    """Backfill the interaction graph (replies + mentions) from message history."""
    now_dt = datetime.now(timezone.utc)
    after_dt = None if days == 0 else now_dt - timedelta(days=days)

    me = guild.get_member(ctx.bot.user.id) if ctx.bot and ctx.bot.user else None

    selected_channel: discord.TextChannel | discord.Thread | None = None
    if channel_id is not None:
        ch = guild.get_channel_or_thread(channel_id)
        if isinstance(ch, (discord.TextChannel, discord.Thread)):
            selected_channel = ch

    channels = await _collect_channels(guild, me, selected_channel=selected_channel)
    stats: dict[str, int] = {"channels": 0, "messages": 0, "interactions": 0}

    with ctx.open_db() as conn:
        if reset:
            clear_interaction_data(conn, guild.id)

        for ch in channels:
            if me and not ch.permissions_for(me).read_message_history:
                continue
            stats["channels"] += 1
            try:
                async for message in ch.history(
                    limit=None, after=after_dt, oldest_first=True
                ):
                    if message.author.bot or not message.guild:
                        continue
                    stats["messages"] += 1
                    msg_ts = int(message.created_at.timestamp())

                    reply_to_id: int | None = (
                        message.reference.message_id
                        if message.reference and message.reference.message_id
                        else None
                    )
                    mention_ids = [
                        u.id
                        for u in message.mentions
                        if not u.bot and u.id != message.author.id
                    ]

                    store_message(
                        conn,
                        message_id=message.id,
                        guild_id=guild.id,
                        channel_id=ch.id,
                        author_id=message.author.id,
                        content=_archived_message_content(message),
                        reply_to_id=reply_to_id,
                        ts=msg_ts,
                        attachment_urls=[a.url for a in message.attachments],
                        mention_ids=mention_ids,
                        embeds=[_embed_to_dict(e) for e in message.embeds],
                    )

                    for reaction in message.reactions:
                        set_reaction_count(
                            conn, message.id, str(reaction.emoji), reaction.count
                        )

                    targets: list[int] = list(mention_ids)
                    if message.reference and isinstance(
                        message.reference.resolved, discord.Message
                    ):
                        ref = message.reference.resolved
                        if (
                            not ref.author.bot
                            and ref.author.id != message.author.id
                            and ref.author.id not in targets
                        ):
                            targets.insert(0, ref.author.id)

                    if targets and _counts_as_member_activity(message):
                        record_interactions(
                            conn,
                            guild.id,
                            message.author.id,
                            targets,
                            ts=msg_ts,
                            message_id=message.id,
                        )
                        stats["interactions"] += len(targets)

            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Could not scan channel #%s: %s", ch.name, exc)

    return stats
