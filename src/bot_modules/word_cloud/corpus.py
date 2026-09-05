"""Where a cloud's words come from: the message archive, or a live fetch.

Two paths, one shape. The archive path serves guilds whose
``message_storage_level`` is ``all`` and can reach back to 2023-12-16. Every
other guild keeps ids and metadata but no text, so it falls back to reading
recent history straight off Discord — capped at
:data:`logic.LIVE_FETCH_MAX`, and storing nothing.

Both return newest-first ``Doc`` lists, so the counting above them never has
to know which one ran.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from bot_modules.services.message_search_service import (
    DELETED_LIVE,
    MessageFilters,
    build_where,
)

from .logic import Doc


def fetch_archive(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_ids: Sequence[int],
    since_ts: int,
    cap: int,
    author_id: int | None = None,
) -> list[Doc]:
    """Read stored message content for a window, newest first.

    The predicate is built by ``message_search_service.build_where`` rather
    than by hand: it is the repo's one description of what a filtered read of
    ``messages`` means, and a second one here would let the two archive readers
    drift on questions like what "exclude bots" covers.

    Three of its filters carry this feature's rules:

    * ``deleted=DELETED_LIVE`` — the archive outlives Discord deletions by
      design, so a cloud that ignored the column would resurface words a member
      deliberately removed.
    * ``min_length=1`` — ``LENGTH(NULL)`` is NULL and ``NULL >= 1`` is not true,
      so this drops both the content-free rows and the empty ones in a single
      predicate.
    * ``author`` — naming one turns *off* ``build_where``'s bot exclusion,
      which is exactly the behaviour wanted: a moderator asking for one
      account's words gets them whether or not that account is a bot.

    ``channel_ids`` is the set the requesting moderator can actually read —
    read permission is the gate on this command, so the caller resolves it and
    passes the result rather than this layer guessing. An empty set means there
    is nothing they may see, which is not an error.
    """
    if not channel_ids or cap <= 0:
        return []

    filters = MessageFilters(
        channel=[str(c) for c in channel_ids],
        after=since_ts,
        min_length=1,
        deleted=DELETED_LIVE,
        author=[str(author_id)] if author_id is not None else None,
        sort="newest",
    )
    where = build_where(conn, guild_id, filters)
    if where.impossible:
        return []

    rows = conn.execute(
        f"SELECT m.content, m.sentiment FROM messages m WHERE {where.sql} "
        "ORDER BY m.ts DESC LIMIT ?",
        [*where.params, cap],
    ).fetchall()
    return [Doc(text=str(r[0]), sentiment=r[1]) for r in rows]


def archive_has_content(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True if this guild has any stored message text at all.

    Used to explain an empty cloud honestly: a guild that archives content but
    had a quiet week is a different story from one that never kept any, and
    the reply should not say the same thing for both.
    """
    row = conn.execute(
        "SELECT 1 FROM messages "
        "WHERE guild_id = ? AND content IS NOT NULL AND content <> '' LIMIT 1",
        (guild_id,),
    ).fetchone()
    return row is not None


def recent_channel_ids(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    channel_ids: Sequence[int],
    limit: int,
) -> list[int]:
    """Pick the ``limit`` most recently active of ``channel_ids``.

    Bounds the live-fetch fan-out when a moderator asks for every channel at
    once: without it, "everywhere" in a large guild is one Discord round trip
    per channel. Ranking uses the archive's timestamps, which every guild has
    — the content-free ones still record *that* a message happened, just not
    what it said — so this works precisely where the live path is needed.

    Falls back to the caller's own order for channels the archive has never
    seen, so a brand-new channel is not silently unreachable.

    Deliberately *not* built on ``build_where``: this ranks rooms by when they
    last saw traffic, so it must count every row — bot-authored, deleted and
    content-free alike. Those are exactly the rows a filtered read drops, and
    dropping them here would rank a busy room as dead.
    """
    if not channel_ids or limit <= 0:
        return []

    ph = ",".join("?" * len(channel_ids))
    rows = conn.execute(
        f"SELECT channel_id, MAX(ts) FROM messages "
        f"WHERE guild_id = ? AND channel_id IN ({ph}) "
        f"GROUP BY channel_id ORDER BY MAX(ts) DESC LIMIT ?",
        [guild_id, *channel_ids, limit],
    ).fetchall()
    ranked = [int(r[0]) for r in rows]

    if len(ranked) < limit:
        seen = set(ranked)
        for cid in channel_ids:
            if len(ranked) >= limit:
                break
            if cid not in seen:
                ranked.append(int(cid))
                seen.add(cid)
    return ranked
