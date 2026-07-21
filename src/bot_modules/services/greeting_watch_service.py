"""Greeting Watch — flag "good morning" / "hello" messages that go unanswered.

Detection (:func:`is_greeting`) runs live in ``on_message`` because the default
``"none"`` storage level drops message text before it ever reaches the DB. The
DB helpers here persist a lightweight watch row and let the background loop
decide, once the window has closed, whether anyone replied to or @mentioned the
greeter — reusing the interaction-log edges the ingest path already records.
"""

from __future__ import annotations

import re
import sqlite3
from typing import NamedTuple

# Matched against the start of a stripped message. A greeting is a short message
# that OPENS with a hello-ish token — "good morning everyone", "gm", "hey all",
# "hiya 👋". The trailing ``\b`` stops prefixes like "history" / "gaming" /
# "morningstar" from matching. This is a heuristic dial, not a classifier: widen
# the vocabulary as real misses surface rather than treating it as exhaustive.
_GREETING_RE = re.compile(
    r"^\W*(?:"
    r"g(?:ood)?\s*mornin[g']?"
    r"|g(?:ood)?\s*afternoon"
    r"|g(?:ood)?\s*evening"
    r"|mornin[g']?"
    r"|gm"
    r"|hey+"
    r"|hi+"
    r"|hello+"
    r"|heya+"
    r"|hiya+"
    r"|howdy"
    r"|greetings"
    r"|yo+"
    r"|hola"
    r"|salut(?:ations)?"
    r"|what'?s\s*up"
    r"|wass?up"
    r"|sup"
    r")\b",
    re.IGNORECASE,
)

# A greeting to the room is short; a longer message is a conversation that
# merely opens with "hey", and we don't want to babysit those.
_MAX_GREETING_WORDS = 8


def is_greeting(content: str) -> bool:
    """True if *content* reads as a greeting addressed to the channel."""
    if not content:
        return False
    text = content.strip()
    if not text or len(text.split()) > _MAX_GREETING_WORDS:
        return False
    return _GREETING_RE.match(text) is not None


class PendingGreeting(NamedTuple):
    message_id: int
    channel_id: int
    author_id: int
    created_ts: int


def has_pending_greeting(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, author_id: int
) -> bool:
    """True if this author already has an unresolved greeting in the channel."""
    row = conn.execute(
        """
        SELECT 1 FROM greeting_watch
        WHERE guild_id = ? AND channel_id = ? AND author_id = ?
          AND resolved_at IS NULL
        LIMIT 1
        """,
        (guild_id, channel_id, author_id),
    ).fetchone()
    return row is not None


def record_greeting(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    channel_id: int,
    author_id: int,
    created_ts: int,
) -> bool:
    """Persist a greeting to watch.

    Returns ``False`` (a no-op) when this author already has an unresolved
    greeting in the channel — one open watch per person per channel stops a
    "gm 🙂 … hey all" double-post from queuing two alerts for the same silence.
    """
    if has_pending_greeting(conn, guild_id, channel_id, author_id):
        return False
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO greeting_watch
            (guild_id, message_id, channel_id, author_id, created_ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, message_id, channel_id, author_id, created_ts),
    )
    return cur.rowcount > 0


def guilds_with_pending(conn: sqlite3.Connection) -> list[int]:
    """Distinct guilds that have at least one unresolved greeting."""
    rows = conn.execute(
        "SELECT DISTINCT guild_id FROM greeting_watch WHERE resolved_at IS NULL"
    ).fetchall()
    return [int(r[0]) for r in rows]


def list_due_greetings(
    conn: sqlite3.Connection, guild_id: int, cutoff_ts: int
) -> list[PendingGreeting]:
    """Unresolved greetings whose window has closed (created at/before cutoff)."""
    rows = conn.execute(
        """
        SELECT message_id, channel_id, author_id, created_ts
        FROM greeting_watch
        WHERE guild_id = ? AND resolved_at IS NULL AND created_ts <= ?
        ORDER BY created_ts
        """,
        (guild_id, cutoff_ts),
    ).fetchall()
    return [
        PendingGreeting(int(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in rows
    ]


def was_acknowledged(
    conn: sqlite3.Connection,
    guild_id: int,
    author_id: int,
    since_ts: int,
    until_ts: int,
) -> bool:
    """True if someone other than the greeter replied to or @mentioned them.

    Reads ``user_interactions_log`` — the ingest path writes one edge there for
    every reply target and @mention (``record_interactions``), so a directed
    acknowledgment of the greeter shows up as a ``to_user_id`` row inside the
    window. The greeter's own reply/mention edges are ``from_user_id`` rows and
    are excluded, so greeting someone by name never counts as being answered.
    """
    row = conn.execute(
        """
        SELECT 1 FROM user_interactions_log
        WHERE guild_id = ? AND to_user_id = ? AND from_user_id != ?
          AND ts >= ? AND ts <= ?
        LIMIT 1
        """,
        (guild_id, author_id, author_id, since_ts, until_ts),
    ).fetchone()
    return row is not None


def pending_greetings_for(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_ids: tuple[int, ...],
    target_ids: tuple[int, ...],
) -> list[tuple[int, int]]:
    """Unresolved greetings by any of ``target_ids`` in these channels.

    The `greeting_answered` quest detector: a reply/mention landing on a
    member with a pending greeting in the same channel is "answering the
    hello". Pending ≈ within the window — the background loop resolves rows
    shortly after their window closes, so anything still open is answerable.
    Returns ``(message_id, author_id)`` pairs (the message id keys the quest
    occurrence, so each greeting credits an answerer at most once).
    """
    if not channel_ids or not target_ids:
        return []
    ch = ",".join("?" * len(channel_ids))
    tg = ",".join("?" * len(target_ids))
    rows = conn.execute(
        f"""
        SELECT message_id, author_id FROM greeting_watch
        WHERE guild_id = ? AND channel_id IN ({ch}) AND author_id IN ({tg})
          AND resolved_at IS NULL
        """,
        (guild_id, *channel_ids, *target_ids),
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def mark_resolved(
    conn: sqlite3.Connection,
    guild_id: int,
    message_id: int,
    outcome: str,
    now_ts: int,
) -> None:
    """Close a greeting with a verdict; a no-op if it was already resolved."""
    conn.execute(
        """
        UPDATE greeting_watch
        SET resolved_at = ?, outcome = ?
        WHERE guild_id = ? AND message_id = ? AND resolved_at IS NULL
        """,
        (now_ts, outcome, guild_id, message_id),
    )
