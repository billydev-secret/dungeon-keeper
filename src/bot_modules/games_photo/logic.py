"""Fill in the daily photo card's history row after the fact.

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
from collections.abc import Sequence

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
        "AND strftime('%s', started_at) >= ? "
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
