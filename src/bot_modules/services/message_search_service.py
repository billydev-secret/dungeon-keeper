"""Shared query machinery behind the Message Search surfaces.

Three dashboard endpoints read the message archive with overlapping needs:
``/messages/search`` (paged and filtered), ``/messages/search/export`` (the same
filters, one capped page, JSON download) and ``/messages/context`` (a channel
window around a single hit, with no filters at all). Filter-clause assembly is
identical between the first two, and row hydration — resolving author and
channel ids to names, attaching attachment URLs, naming reply targets — is
identical between all three.

Kept deliberately free of FastAPI. The caller owns HTTP concerns: regex
validation and its 400 stay in the route, because that is where the rails on a
moderator-supplied pattern belong. Everything here takes a connection and
returns plain data, so it is tested directly rather than through a client.

The ``guild`` argument threaded through several functions is a live
``discord.Guild`` or None. It is only ever consulted as a *first* lookup for
names, with the ``known_users``/``known_channels`` tables as the fallback, so
every function here works with ``guild=None`` on a cold or offline bot.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from bot_modules.core.bot_exclusion import bot_ids_subquery
from bot_modules.core.utils import jump_url
from bot_modules.services.message_store import (
    DELETE_SOURCES,
    get_known_channels_bulk,
    get_known_users_bulk,
)

VALID_EMOTIONS = frozenset({"joy", "playful", "anger", "frustration", "neutral"})

# Column list every message-reading endpoint selects. Everything downstream
# reads rows by name (every connection sets ``row_factory = sqlite3.Row``), so
# this is a plain list rather than a positional contract — the one exception is
# ``scan_regex_rows``, which takes an explicit content accessor precisely so it
# doesn't impose one.
BASE_COLUMNS = (
    "m.message_id, m.channel_id, m.author_id, m.content, "
    "m.reply_to_id, m.ts, m.sentiment, m.emotion, "
    "m.deleted_at, m.deleted_source"
)

# ``deleted`` filter values. The archive keeps deleted messages and the panel
# shows them by default, badged — this narrows to one side or one source.
DELETED_ANY = "any"
DELETED_ONLY = "only"
DELETED_LIVE = "live"
# The source names come from message_store, which owns the vocabulary — a third
# source added there must not need a second edit here.
DELETED_FILTERS = frozenset({DELETED_ANY, DELETED_ONLY, DELETED_LIVE}) | DELETE_SOURCES

SORT_ORDERS = {
    "newest": "m.ts DESC",
    "oldest": "m.ts ASC",
    "most_reacted": "COALESCE(mr.total_reactions, 0) DESC, m.ts DESC",
    "longest": "LENGTH(m.content) DESC, m.ts DESC",
    "most_positive": "m.sentiment DESC, m.ts DESC",
    "most_negative": "m.sentiment ASC, m.ts DESC",
}

# Only ``most_reacted`` needs the aggregate, and it is expensive enough to be
# worth omitting from the other five plans.
_REACTION_JOIN = """
                    LEFT JOIN (
                        SELECT message_id, SUM(count) AS total_reactions
                        FROM message_reactions GROUP BY message_id
                    ) mr ON mr.message_id = m.message_id
"""


@dataclass
class MessageFilters:
    """The filter surface shared by search and export.

    ``author``, ``mentions`` and ``reply_to`` arrive as free text — either a
    numeric id or a partial name — and are resolved against the guild cache and
    ``known_users`` by :func:`resolve_user`.
    """

    author: list[str] | None = None
    mentions: str | None = None
    reply_to: str | None = None
    channel: list[str] | None = None
    before: int | None = None
    after: int | None = None
    sentiment_min: float | None = None
    sentiment_max: float | None = None
    emotion: str | None = None
    has_attachments: bool | None = None
    has_reactions: bool | None = None
    min_length: int | None = None
    max_length: int | None = None
    include_bots: bool = False
    sort: str = "newest"
    # "any" (default — deleted messages appear, badged), "only", "live", or a
    # specific source name. Anything unrecognized is treated as "any" rather
    # than erroring, matching how an unknown emotion is ignored.
    deleted: str = DELETED_ANY


@dataclass
class WhereClause:
    """Assembled SQL predicate plus its positional parameters.

    ``impossible`` is the "this can never match" signal: a name filter that
    resolved to zero users makes the whole query pointless, and the caller
    should short-circuit to an empty result rather than run SQL that would
    return everything (an empty ``IN ()`` list is a syntax error, and dropping
    the clause silently would be worse).
    """

    sql: str = ""
    params: list[Any] = field(default_factory=list)
    impossible: bool = False


def resolve_user(
    conn: sqlite3.Connection,
    value: str,
    guild_id: int,
    guild: Any | None = None,
) -> list[int]:
    """Resolve a user id or partial name to the ids it could mean.

    A numeric string is taken at face value — a moderator pasting an id must
    reach a user who has left, or who was never in the name cache. Otherwise the
    live guild member list is searched first (it has current nicknames), falling
    back to ``known_users``, which retains people who have since left.
    """
    try:
        return [int(value)]
    except ValueError:
        pass

    if guild is not None:
        needle = value.lower()
        matches = [
            m.id
            for m in guild.members
            if needle in m.display_name.lower() or needle in m.name.lower()
        ]
        if matches:
            return matches

    rows = conn.execute(
        "SELECT user_id FROM known_users WHERE guild_id = ? "
        "AND (username LIKE ? OR display_name LIKE ?)",
        (guild_id, f"%{value}%", f"%{value}%"),
    ).fetchall()
    return [r[0] for r in rows] if rows else []


def _id_list_clause(column: str, ids: list[int]) -> tuple[str, list[Any]]:
    """``col = ?`` for one id, ``col IN (?, ?)`` for several."""
    if len(ids) == 1:
        return f"{column} = ?", [ids[0]]
    placeholders = ",".join("?" * len(ids))
    return f"{column} IN ({placeholders})", list(ids)


def build_where(
    conn: sqlite3.Connection,
    guild_id: int,
    filters: MessageFilters,
    guild: Any | None = None,
) -> WhereClause:
    """Turn a :class:`MessageFilters` into a WHERE predicate over ``messages m``.

    Always guild-scoped first, so no caller can accidentally build a query that
    reads across guilds.
    """
    clauses = ["m.guild_id = ?"]
    params: list[Any] = [guild_id]

    if filters.author:
        author_ids: list[int] = []
        for a in filters.author:
            author_ids.extend(resolve_user(conn, a, guild_id, guild))
        author_ids = list(dict.fromkeys(author_ids))  # dedupe, keep order
        if not author_ids:
            return WhereClause(impossible=True)
        sql, bind = _id_list_clause("m.author_id", author_ids)
        clauses.append(sql)
        params.extend(bind)

    if filters.channel:
        channel_ids = [int(c) for c in filters.channel]
        sql, bind = _id_list_clause("m.channel_id", channel_ids)
        clauses.append(sql)
        params.extend(bind)

    if filters.reply_to:
        reply_to_ids = resolve_user(conn, filters.reply_to, guild_id, guild)
        if not reply_to_ids:
            return WhereClause(impossible=True)
        placeholders = ",".join("?" * len(reply_to_ids))
        clauses.append(f"""
                    m.reply_to_id IN (
                        SELECT message_id FROM messages
                        WHERE author_id IN ({placeholders}) AND guild_id = ?
                    )
                """)
        params.extend([*reply_to_ids, guild_id])

    if filters.mentions:
        mention_ids = resolve_user(conn, filters.mentions, guild_id, guild)
        if not mention_ids:
            return WhereClause(impossible=True)
        placeholders = ",".join("?" * len(mention_ids))
        clauses.append(f"""
                    m.message_id IN (
                        SELECT message_id FROM message_mentions
                        WHERE user_id IN ({placeholders})
                    )
                """)
        params.extend(mention_ids)

    if filters.before:
        clauses.append("m.ts <= ?")
        params.append(filters.before)
    if filters.after:
        clauses.append("m.ts >= ?")
        params.append(filters.after)
    if filters.sentiment_min is not None:
        clauses.append("m.sentiment >= ?")
        params.append(filters.sentiment_min)
    if filters.sentiment_max is not None:
        clauses.append("m.sentiment <= ?")
        params.append(filters.sentiment_max)

    if filters.emotion:
        emotions = [
            e.strip() for e in filters.emotion.split(",") if e.strip() in VALID_EMOTIONS
        ]
        if emotions:
            placeholders = ",".join("?" * len(emotions))
            clauses.append(f"m.emotion IN ({placeholders})")
            params.extend(emotions)

    if filters.has_attachments is not None:
        exists = "EXISTS" if filters.has_attachments else "NOT EXISTS"
        clauses.append(
            f"{exists} (SELECT 1 FROM message_attachments a "
            "WHERE a.message_id = m.message_id)"
        )
    if filters.has_reactions is not None:
        exists = "EXISTS" if filters.has_reactions else "NOT EXISTS"
        clauses.append(
            f"{exists} (SELECT 1 FROM message_reactions r "
            "WHERE r.message_id = m.message_id)"
        )

    if filters.min_length is not None:
        clauses.append("LENGTH(m.content) >= ?")
        params.append(filters.min_length)
    if filters.max_length is not None:
        clauses.append("LENGTH(m.content) <= ?")
        params.append(filters.max_length)

    # Deleted messages are included by default and badged in the panel; this
    # narrows to one side of the line, or to a single source.
    if filters.deleted == DELETED_ONLY:
        clauses.append("m.deleted_at IS NOT NULL")
    elif filters.deleted == DELETED_LIVE:
        clauses.append("m.deleted_at IS NULL")
    elif filters.deleted in DELETED_FILTERS and filters.deleted != DELETED_ANY:
        # The IS NOT NULL conjunct is redundant against the data but not against
        # the planner: SQLite only uses a partial index when the query's WHERE
        # *implies* the index's WHERE, so without it a source filter scans the
        # whole guild instead of the deleted minority.
        clauses.append("m.deleted_at IS NOT NULL AND m.deleted_source = ?")
        params.append(filters.deleted)

    # Bots are excluded from the browser by default, matching every other
    # message-volume surface. An explicit ``author`` filter is an override:
    # searching *for* a bot must still return its messages.
    if not filters.include_bots and not filters.author:
        clauses.append(f"m.author_id NOT IN ({bot_ids_subquery()})")
        params.append(guild_id)

    return WhereClause(sql=" AND ".join(clauses), params=params)


def reaction_join(sort: str) -> str:
    """The aggregate join ``most_reacted`` needs, empty for every other sort."""
    return _REACTION_JOIN if sort == "most_reacted" else ""


def reaction_select(sort: str) -> str:
    """Extra SELECT column pairing with :func:`reaction_join`."""
    return ", COALESCE(mr.total_reactions, 0) AS total_reactions" if sort == "most_reacted" else ""


def _anchor_of(
    conn: sqlite3.Connection, guild_id: int, message_id: int
) -> tuple[int, int] | None:
    """``(channel_id, ts)`` for a message, or None when it isn't archived."""
    row = conn.execute(
        "SELECT channel_id, ts FROM messages WHERE message_id = ? AND guild_id = ?",
        (message_id, guild_id),
    ).fetchone()
    return (row["channel_id"], row["ts"]) if row else None


def _context_side(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    anchor: tuple[int, int],
    *,
    comparison: str,
    order: str,
    limit: int,
) -> list[Any]:
    """One direction of a context read, walking away from ``anchor``.

    Ordering is by ``(ts, message_id)`` rather than ``ts`` alone. Stored
    timestamps are whole seconds, so a burst of messages inside one second would
    otherwise come back in an arbitrary order — snowflake ids break the tie in
    true post order. ``comparison`` and ``order`` are call-site literals, never
    user input.
    """
    anchor_ts, anchor_id = anchor
    return conn.execute(
        f"""
        SELECT {BASE_COLUMNS} FROM messages m
         WHERE m.guild_id = ? AND m.channel_id = ?
           AND (m.ts, m.message_id) {comparison} (?, ?)
         ORDER BY m.ts {order}, m.message_id {order}
         LIMIT ?
        """,
        (guild_id, channel_id, anchor_ts, anchor_id, limit),
    ).fetchall()


def fetch_context_window(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    *,
    before: int = 25,
    after: int = 25,
) -> dict[str, Any] | None:
    """Read the stored messages surrounding one hit, in its own channel.

    Returns ``None`` when the message isn't in the archive at all — the caller
    turns that into a 404.

    Bots are deliberately **not** excluded here even though search hides them by
    default. This is conversation reconstruction, not a result set: dropping the
    bot message someone was replying to would misrepresent the exchange.
    Deleted messages are included for the same reason, badged by the panel.
    """
    found = _anchor_of(conn, guild_id, message_id)
    if found is None:
        return None
    channel_id, anchor_ts = found
    anchor = (anchor_ts, message_id)

    older = _context_side(
        conn, guild_id, channel_id, anchor, comparison="<", order="DESC", limit=before
    )
    # ``>=`` so the anchor itself leads the newer side.
    newer = _context_side(
        conn, guild_id, channel_id, anchor, comparison=">=", order="ASC", limit=after + 1
    )

    return {
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        # Whether there is more to load in either direction, so the panel can
        # hide a load-more button that would return nothing.
        "has_older": len(older) == before,
        "has_newer": len(newer) == after + 1,
        "rows": list(reversed(older)) + list(newer),
    }


def fetch_context_page(
    conn: sqlite3.Connection,
    guild_id: int,
    from_message_id: int,
    direction: str,
    limit: int = 25,
) -> dict[str, Any] | None:
    """Read the next page of context above or below a row already on screen.

    ``from_message_id`` is the oldest (for ``older``) or newest (for ``newer``)
    row the panel currently holds; the page returned is strictly beyond it, so
    repeated clicks walk outward without overlapping.
    """
    found = _anchor_of(conn, guild_id, from_message_id)
    if found is None:
        return None
    channel_id, anchor_ts = found

    older = direction == "older"
    rows = _context_side(
        conn,
        guild_id,
        channel_id,
        (anchor_ts, from_message_id),
        comparison="<" if older else ">",
        order="DESC" if older else "ASC",
        limit=limit,
    )
    return {
        "channel_id": str(channel_id),
        "has_more": len(rows) == limit,
        # Always hand back oldest-first — the panel renders one continuous column.
        "rows": list(reversed(rows)) if older else list(rows),
    }


def resolve_names(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: set[int],
    channel_ids: set[int],
    guild: Any | None = None,
) -> tuple[dict[int, str], dict[int, str]]:
    """Resolve user and channel ids to display names.

    Live guild objects win (current nicknames and channel names); the
    ``known_*`` tables cover everyone and everything that has since left or been
    deleted, which is most of what a search over an archive turns up.
    """
    user_names: dict[int, str] = {}
    channel_names: dict[int, str] = {}

    if guild is not None:
        for uid in user_ids:
            member = guild.get_member(uid)
            if member:
                user_names[uid] = member.display_name
        for cid in channel_ids:
            ch = guild.get_channel(cid)
            if ch:
                channel_names[cid] = ch.name

    still_needed = user_ids - set(user_names)
    if still_needed:
        user_names.update(get_known_users_bulk(conn, guild_id, list(still_needed)))

    still_needed_ch = channel_ids - set(channel_names)
    if still_needed_ch:
        channel_names.update(
            get_known_channels_bulk(conn, guild_id, list(still_needed_ch))
        )

    return user_names, channel_names


def hydrate_rows(
    conn: sqlite3.Connection,
    guild_id: int,
    rows: list[Any],
    guild: Any | None = None,
) -> list[dict[str, Any]]:
    """Turn raw :data:`BASE_COLUMNS` rows into the dashboard's message dicts.

    Every id is stringified on the way out. Discord snowflakes exceed 2^53 and
    would lose precision as JSON numbers — the dashboard's snowflake sweep
    enforces this repo-wide.
    """
    user_ids: set[int] = set()
    channel_ids: set[int] = set()
    # A set, not a list: 50 results replying to the same message would
    # otherwise bind that id 50 times into the IN clause (5,000 on export).
    reply_msg_ids: set[int] = set()

    for r in rows:
        user_ids.add(r["author_id"])
        channel_ids.add(r["channel_id"])
        if r["reply_to_id"]:
            reply_msg_ids.add(r["reply_to_id"])

    # Resolve reply targets to their authors, so a result can say who was being
    # replied to even when the parent message isn't in the result set.
    reply_authors: dict[int, int] = {}
    if reply_msg_ids:
        reply_ids = list(reply_msg_ids)
        placeholders = ",".join("?" * len(reply_ids))
        for rr in conn.execute(
            f"SELECT message_id, author_id FROM messages "
            f"WHERE message_id IN ({placeholders})",
            reply_ids,
        ).fetchall():
            reply_authors[rr[0]] = rr[1]
            user_ids.add(rr[1])

    user_names, channel_names = resolve_names(
        conn, guild_id, user_ids, channel_ids, guild
    )

    msg_ids = [r["message_id"] for r in rows]
    attachments: dict[int, list[str]] = {}
    if msg_ids:
        placeholders = ",".join("?" * len(msg_ids))
        for ar in conn.execute(
            f"SELECT message_id, url FROM message_attachments "
            f"WHERE message_id IN ({placeholders})",
            msg_ids,
        ).fetchall():
            attachments.setdefault(ar[0], []).append(ar[1])

    results: list[dict[str, Any]] = []
    for r in rows:
        msg_id, ch_id, auth_id = r["message_id"], r["channel_id"], r["author_id"]
        content, reply_id, ts = r["content"], r["reply_to_id"], r["ts"]
        reply_author_id = reply_authors.get(reply_id) if reply_id else None
        deleted_at = r["deleted_at"]
        results.append(
            {
                "message_id": str(msg_id),
                "channel_id": str(ch_id),
                "channel_name": channel_names.get(ch_id) or f"channel {ch_id}",
                "author_id": str(auth_id),
                "author_name": user_names.get(auth_id) or f"User {auth_id}",
                "content": content or "",
                "reply_to_id": str(reply_id) if reply_id else None,
                "reply_to_author_id": str(reply_author_id) if reply_author_id else None,
                "reply_to_author_name": (
                    user_names.get(reply_author_id) or f"User {reply_author_id}"
                )
                if reply_author_id
                else None,
                "attachments": attachments.get(msg_id, []),
                "ts": ts,
                "sentiment": r["sentiment"],
                "emotion": r["emotion"],
                "deleted_at": deleted_at,
                "deleted_source": r["deleted_source"],
                # Built here rather than in the panel so the suppression rule
                # has one home: a deep link to a deleted message renders fine
                # and lands on nothing, so it is simply not offered.
                "discord_url": None
                if deleted_at is not None
                else jump_url(guild_id, ch_id, msg_id),
            }
        )
    return results
