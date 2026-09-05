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

from bot_modules.core.bot_exclusion import bot_filter_clause

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

    ``channel_ids`` is the set the requesting moderator can actually read —
    read permission is the gate on this command, so the caller resolves it and
    passes the result rather than this layer guessing. An empty set means
    there is nothing they may see, which is not an error: it returns no docs
    and the caller renders "nothing to show".

    Bot authors are excluded by default, because they are ~21% of stored
    volume and their flattened embed text swamps everything a member said.
    Naming an ``author_id`` overrides that: a moderator who explicitly asks for
    one account's words gets them even if the account is a bot.
    """
    if not channel_ids or cap <= 0:
        return []

    channel_ph = ",".join("?" * len(channel_ids))
    sql = [
        "SELECT content, sentiment FROM messages",
        f"WHERE guild_id = ? AND channel_id IN ({channel_ph})",
        "AND ts >= ?",
        "AND content IS NOT NULL AND content <> ''",
        # The archive outlives Discord deletions on purpose, so a cloud that
        # ignored this column would resurface words a member deliberately
        # removed. 40k rows in the home guild carry it.
        "AND deleted_at IS NULL",
    ]
    params: list[object] = [guild_id, *channel_ids, since_ts]

    if author_id is not None:
        sql.append("AND author_id = ?")
        params.append(author_id)

    # Splice the bot clause and its params together — the fragment is
    # positional, so appending one without the other silently mis-binds.
    clause, clause_params = bot_filter_clause(
        guild_id, include_bots=author_id is not None
    )
    if clause:
        sql.append(clause.strip())
        params.extend(clause_params)

    sql.append("ORDER BY ts DESC LIMIT ?")
    params.append(cap)

    rows = conn.execute(" ".join(sql), params).fetchall()
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
