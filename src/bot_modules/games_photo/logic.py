"""Fill in the daily photo card's history row after the fact, and recap the
day for the next card.

The card archives the moment it posts (there is no live game to keep open —
members just reply in the channel), so its ``games_game_history`` row said
nothing: ``player_count = 0``, ``round_count = 0`` on every card, and the one
number Billy asks for — how many people answered the prompt — lived in the
economy's payout anchor and the message ingest and never reached a dashboard
(photo-external-102).

The answers are already recorded: every message is ingested with a
``media_kind`` derived from its attachments at ingest time (no content is
read here), and the ``photo_post`` faucet pays on exactly that signal. So a
card's numbers are one query over ``messages``: distinct authors and image
posts in the channel during the 24 h after the card, the bot's own posts
(the card is an image too) excluded. Rows are filled at the next launch, once
their window has closed — counting at hour three would freeze a number still
climbing.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bot_modules.core.utils import jump_url

log = logging.getLogger(__name__)

# How long after a card its answers are counted. A daily schedule posts the
# next card at +24 h, so this is "until the next card"; a rarer schedule still
# counts a day — the prompt is a day's challenge, not a week's.
BACKFILL_WINDOW_SECONDS = 86400

# Cards older than this are left as they are: the count is meant to describe
# the card while anyone remembers it, not to reprocess the whole archive on
# every launch. A few launches catch up a channel that fell behind.
BACKFILL_MAX_AGE_SECONDS = 30 * 86400

# How many rows one launch fills. Bounds the work on the launch path.
BACKFILL_BATCH = 10

# The ingest classification a photo answer carries (``classify_media_kind``:
# 'media' is an image or video attachment, as opposed to 'gif' / 'other').
PHOTO_MEDIA_KIND = "media"


async def backfill_card_counts(
    db,
    *,
    channel_id: int,
    guild_id: int,
    exclude_author_ids: Sequence[int] = (),
    now: float | None = None,
) -> int:
    """Write ``player_count`` / ``round_count`` on every uncounted card in
    *channel_id* whose 24 h window has closed. Returns how many rows changed.

    A card is *uncounted* while its archived payload carries no ``counted``
    flag; the flag is written here alongside the numbers, so a zero means
    "nobody posted" and the row is not re-read next time. Rows archived
    before this shipped hold a literal 0 and no flag, so they are counted
    once on the way past like any other.

    ``guild_id`` also repairs a row still at 0 (cards archived before
    migration 204 stamped the guild), since the channel is the guild's own
    photo channel. ``exclude_author_ids`` is the bot itself: the card is an
    image post too.
    """
    import time

    now = time.time() if now is None else now
    excluded = [int(a) for a in exclude_author_ids] or [0]
    placeholders = ", ".join("?" for _ in excluded)

    rows = await db.fetchall(
        "SELECT history_id, started_at, strftime('%s', started_at) AS started_epoch "
        "FROM games_game_history "
        "WHERE game_type = 'photo' AND channel_id = ? "
        "AND (player_count IS NULL OR player_count = 0) "
        "AND json_extract(COALESCE(payload, '{}'), '$.counted') IS NULL "
        "AND strftime('%s', started_at) + ? <= ? "
        "AND CAST(strftime('%s', started_at) AS INTEGER) >= ? "
        "ORDER BY history_id DESC LIMIT ?",
        (
            int(channel_id),
            BACKFILL_WINDOW_SECONDS,
            int(now),
            int(now) - BACKFILL_MAX_AGE_SECONDS,
            BACKFILL_BATCH,
        ),
    )
    changed = 0
    for row in rows:
        try:
            start = int(row["started_epoch"])
        except (TypeError, ValueError):
            log.warning("photo backfill: unreadable started_at on history %s", row["history_id"])
            continue
        counts = await db.fetchone(
            "SELECT COUNT(*) AS photos, COUNT(DISTINCT author_id) AS posters "
            "FROM messages WHERE channel_id = ? AND media_kind = ? "
            f"AND author_id NOT IN ({placeholders}) AND ts >= ? AND ts < ?",
            (int(channel_id), PHOTO_MEDIA_KIND, *excluded, start, start + BACKFILL_WINDOW_SECONDS),
        )
        photos = int(counts["photos"]) if counts else 0
        posters = int(counts["posters"]) if counts else 0
        cur = await db.execute(
            "UPDATE games_game_history SET player_count = ?, round_count = ?, "
            "guild_id = CASE WHEN guild_id = 0 THEN ? ELSE guild_id END, "
            "payload = json_set(COALESCE(payload, '{}'), '$.counted', json('true')) "
            "WHERE history_id = ? AND json_extract(COALESCE(payload, '{}'), '$.counted') IS NULL",
            (posters, photos, int(guild_id), row["history_id"]),
        )
        if (cur.rowcount or 0) > 0:
            changed += 1
    return changed


# ── Yesterday's recap (photo-external-105) ───────────────────────────────────
#
# The card was a stream with no ending: members posted and never heard back.
# The next card now carries one line under it — how many photos from how many
# people in the previous 24 h, and the most-loved one as a jump link. Same
# ingest-time signals as the counts above (media_kind, the reaction tallies
# ``message_reactions`` keeps per emoji); nothing about a photo is read.


@dataclass(frozen=True)
class DayRecap:
    """One day's answers. ``most_loved`` is ``(message_id, author_id,
    reactions)`` for the photo with the most reactions, or None when nobody
    reacted to anything."""

    photos: int
    posters: int
    most_loved: tuple[int, int, int] | None


async def previous_day_recap(
    db,
    *,
    channel_id: int,
    exclude_author_ids: Sequence[int] = (),
    now: float | None = None,
    window_seconds: int = BACKFILL_WINDOW_SECONDS,
) -> DayRecap | None:
    """The recap for the ``window_seconds`` before ``now`` in one channel, or
    None when no photo was posted (nothing to say — the line is skipped)."""
    import time

    now = time.time() if now is None else now
    excluded = [int(a) for a in exclude_author_ids] or [0]
    placeholders = ", ".join("?" for _ in excluded)
    since, until = int(now) - window_seconds, int(now)
    counts = await db.fetchone(
        "SELECT COUNT(*) AS photos, COUNT(DISTINCT author_id) AS posters "
        "FROM messages WHERE channel_id = ? AND media_kind = ? AND deleted_at IS NULL "
        f"AND author_id NOT IN ({placeholders}) AND ts >= ? AND ts < ?",
        (int(channel_id), PHOTO_MEDIA_KIND, *excluded, since, until),
    )
    photos = int(counts["photos"]) if counts else 0
    if photos == 0:
        return None
    loved = await db.fetchone(
        "SELECT m.message_id, m.author_id, SUM(r.count) AS reactions "
        "FROM messages m JOIN message_reactions r ON r.message_id = m.message_id "
        "WHERE m.channel_id = ? AND m.media_kind = ? AND m.deleted_at IS NULL "
        f"AND m.author_id NOT IN ({placeholders}) AND m.ts >= ? AND m.ts < ? "
        "GROUP BY m.message_id HAVING SUM(r.count) > 0 "
        "ORDER BY reactions DESC, m.ts ASC LIMIT 1",
        (int(channel_id), PHOTO_MEDIA_KIND, *excluded, since, until),
    )
    most_loved = (
        (int(loved["message_id"]), int(loved["author_id"]), int(loved["reactions"]))
        if loved else None
    )
    return DayRecap(photos=photos, posters=int(counts["posters"]), most_loved=most_loved)


def recap_line(
    recap: DayRecap, *, guild_id: int, channel_id: int, name_fn: Callable[[int], str]
) -> str:
    """The one line posted under the next card. Names come through
    ``name_fn`` (never a ``<@id>`` — a mention the reader's client can't
    resolve renders as a bare number), the photo as a jump link."""
    photos = f"{recap.photos} photo{'' if recap.photos == 1 else 's'}"
    people = f"{recap.posters} {'person' if recap.posters == 1 else 'people'}"
    line = f"Yesterday: {photos} from {people}"
    if recap.most_loved is not None:
        message_id, author_id, _n = recap.most_loved
        link = jump_url(int(guild_id), int(channel_id), int(message_id))
        line += f" — most loved: {name_fn(author_id)}'s, {link}"
    return line
