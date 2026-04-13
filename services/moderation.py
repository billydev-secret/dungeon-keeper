"""Jail, Ticket, Warning, Audit, and Transcript services.

Implements the moderation system described in dungeon_keeper_jail_ticket_spec.md.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

log = logging.getLogger("dungeonkeeper.moderation")

# ---------------------------------------------------------------------------
# Duration parsing  (30m, 2h, 1d, 7d, 1w, etc.)
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"(?:(\d+)\s*w)?\s*(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?",
    re.IGNORECASE,
)


def parse_duration(text: str) -> int | None:
    """Parse a human duration string into seconds.  Returns None if unparseable."""
    text = text.strip()
    if not text:
        return None
    m = _DURATION_RE.fullmatch(text)
    if not m or not any(m.groups()):
        return None
    weeks = int(m.group(1) or 0)
    days = int(m.group(2) or 0)
    hours = int(m.group(3) or 0)
    minutes = int(m.group(4) or 0)
    total = weeks * 604800 + days * 86400 + hours * 3600 + minutes * 60
    return total if total > 0 else None


def fmt_duration(seconds: int) -> str:
    """Format seconds into a human-readable string like '2d 6h'."""
    parts: list[str] = []
    if seconds >= 604800:
        w = seconds // 604800
        seconds %= 604800
        parts.append(f"{w}w")
    if seconds >= 86400:
        d = seconds // 86400
        seconds %= 86400
        parts.append(f"{d}d")
    if seconds >= 3600:
        h = seconds // 3600
        seconds %= 3600
        parts.append(f"{h}h")
    if seconds >= 60:
        m = seconds // 60
        parts.append(f"{m}m")
    return " ".join(parts) if parts else "<1m"


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------


def init_moderation_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jails (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            moderator_id    INTEGER NOT NULL,
            reason          TEXT NOT NULL DEFAULT '',
            stored_roles    TEXT NOT NULL DEFAULT '[]',
            channel_id      INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL,
            expires_at      REAL,
            released_at     REAL,
            release_reason  TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jails_active "
        "ON jails (guild_id, status) WHERE status = 'active'"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL DEFAULT 0,
            description     TEXT NOT NULL DEFAULT '',
            source_message_url TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'open',
            claimer_id      INTEGER,
            escalated       INTEGER NOT NULL DEFAULT 0,
            created_at      REAL NOT NULL,
            closed_at       REAL,
            closed_by       INTEGER,
            close_reason    TEXT NOT NULL DEFAULT '',
            deleted_at      REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tickets_active ON tickets (guild_id, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_participants (
            ticket_id   INTEGER NOT NULL REFERENCES tickets(id),
            user_id     INTEGER NOT NULL,
            added_by    INTEGER NOT NULL,
            added_at    REAL NOT NULL,
            removed_at  REAL,
            PRIMARY KEY (ticket_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS warnings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            moderator_id    INTEGER NOT NULL,
            reason          TEXT NOT NULL DEFAULT '',
            created_at      REAL NOT NULL,
            revoked         INTEGER NOT NULL DEFAULT 0,
            revoked_at      REAL,
            revoked_by      INTEGER,
            revoke_reason   TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_warnings_user ON warnings (guild_id, user_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            action      TEXT NOT NULL,
            actor_id    INTEGER NOT NULL,
            target_id   INTEGER,
            extra       TEXT NOT NULL DEFAULT '{}',
            created_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_guild "
        "ON audit_log (guild_id, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id    INTEGER NOT NULL,
            record_type TEXT NOT NULL,
            record_id   INTEGER NOT NULL,
            content     TEXT NOT NULL,
            created_at  REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transcripts_record "
        "ON transcripts (record_type, record_id)"
    )

    # Policy tickets & voting
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_tickets (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            creator_id      INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL DEFAULT 0,
            title           TEXT NOT NULL DEFAULT '',
            description     TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'open',
            vote_text       TEXT NOT NULL DEFAULT '',
            created_at      REAL NOT NULL,
            vote_started_at REAL,
            vote_ended_at   REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_tickets_guild "
        "ON policy_tickets (guild_id, status)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_votes (
            policy_id   INTEGER NOT NULL REFERENCES policy_tickets(id),
            user_id     INTEGER NOT NULL,
            vote        TEXT NOT NULL,
            voted_at    REAL NOT NULL,
            PRIMARY KEY (policy_id, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS policies (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id          INTEGER NOT NULL,
            policy_ticket_id  INTEGER NOT NULL,
            title             TEXT NOT NULL DEFAULT '',
            description       TEXT NOT NULL DEFAULT '',
            passed_at         REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_policies_guild ON policies (guild_id)")


# ---------------------------------------------------------------------------
# Typed row helpers
# ---------------------------------------------------------------------------


class JailRow(TypedDict):
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    stored_roles: str
    channel_id: int
    created_at: float
    expires_at: float | None
    released_at: float | None
    release_reason: str
    status: str


class TicketRow(TypedDict):
    id: int
    guild_id: int
    user_id: int
    channel_id: int
    description: str
    source_message_url: str
    status: str
    claimer_id: int | None
    escalated: int
    created_at: float
    closed_at: float | None
    closed_by: int | None
    close_reason: str
    deleted_at: float | None


class WarningRow(TypedDict):
    id: int
    guild_id: int
    user_id: int
    moderator_id: int
    reason: str
    created_at: float
    revoked: int
    revoked_at: float | None
    revoked_by: int | None
    revoke_reason: str


class PolicyTicketRow(TypedDict):
    id: int
    guild_id: int
    creator_id: int
    channel_id: int
    title: str
    description: str
    status: str
    vote_text: str
    created_at: float
    vote_started_at: float | None
    vote_ended_at: float | None


class PolicyVoteRow(TypedDict):
    policy_id: int
    user_id: int
    vote: str
    voted_at: float


class PolicyRow(TypedDict):
    id: int
    guild_id: int
    policy_ticket_id: int
    title: str
    description: str
    passed_at: float


# ---------------------------------------------------------------------------
# Jail DB operations
# ---------------------------------------------------------------------------


def create_jail(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
    stored_roles: list[int],
    channel_id: int,
    duration_seconds: int | None,
) -> int:
    now = time.time()
    expires_at = now + duration_seconds if duration_seconds else None
    cur = conn.execute(
        """
        INSERT INTO jails (guild_id, user_id, moderator_id, reason, stored_roles,
                           channel_id, created_at, expires_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            guild_id,
            user_id,
            moderator_id,
            reason,
            json.dumps(stored_roles),
            channel_id,
            now,
            expires_at,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_active_jail(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> JailRow | None:
    row = conn.execute(
        "SELECT * FROM jails WHERE guild_id = ? AND user_id = ? AND status = 'active'",
        (guild_id, user_id),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def get_jail_by_channel(
    conn: sqlite3.Connection,
    channel_id: int,
) -> JailRow | None:
    row = conn.execute(
        "SELECT * FROM jails WHERE channel_id = ? AND status = 'active'",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def get_expired_jails(conn: sqlite3.Connection, guild_id: int) -> list[JailRow]:
    now = time.time()
    rows = conn.execute(
        "SELECT * FROM jails WHERE guild_id = ? AND status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?",
        (guild_id, now),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def release_jail(
    conn: sqlite3.Connection,
    jail_id: int,
    *,
    reason: str,
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE jails SET status = 'released', released_at = ?, release_reason = ? WHERE id = ?",
        (now, reason, jail_id),
    )


def get_jail_history(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> list[JailRow]:
    rows = conn.execute(
        "SELECT * FROM jails WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
        (guild_id, user_id),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Ticket DB operations
# ---------------------------------------------------------------------------


def create_ticket(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    description: str,
    source_message_url: str = "",
) -> int:
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO tickets (guild_id, user_id, channel_id, description,
                             source_message_url, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (guild_id, user_id, channel_id, description, source_message_url, now),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_ticket_by_channel(
    conn: sqlite3.Connection,
    channel_id: int,
) -> TicketRow | None:
    row = conn.execute(
        "SELECT * FROM tickets WHERE channel_id = ? AND status IN ('open', 'closed')",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def close_ticket(
    conn: sqlite3.Connection,
    ticket_id: int,
    *,
    closed_by: int,
    reason: str,
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, close_reason = ? WHERE id = ?",
        (now, closed_by, reason, ticket_id),
    )


def reopen_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute(
        "UPDATE tickets SET status = 'open', closed_at = NULL, closed_by = NULL, close_reason = '' WHERE id = ?",
        (ticket_id,),
    )


def delete_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    now = time.time()
    conn.execute(
        "UPDATE tickets SET status = 'deleted', deleted_at = ? WHERE id = ?",
        (now, ticket_id),
    )


def claim_ticket(conn: sqlite3.Connection, ticket_id: int, claimer_id: int) -> None:
    conn.execute(
        "UPDATE tickets SET claimer_id = ? WHERE id = ?",
        (claimer_id, ticket_id),
    )


def escalate_ticket(conn: sqlite3.Connection, ticket_id: int) -> None:
    conn.execute(
        "UPDATE tickets SET escalated = 1 WHERE id = ?",
        (ticket_id,),
    )


def get_ticket_history(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> list[TicketRow]:
    rows = conn.execute(
        "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
        (guild_id, user_id),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def add_ticket_participant(
    conn: sqlite3.Connection,
    ticket_id: int,
    user_id: int,
    added_by: int,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO ticket_participants (ticket_id, user_id, added_by, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ticket_id, user_id) DO UPDATE SET removed_at = NULL, added_by = excluded.added_by, added_at = excluded.added_at
        """,
        (ticket_id, user_id, added_by, now),
    )


def remove_ticket_participant(
    conn: sqlite3.Connection,
    ticket_id: int,
    user_id: int,
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE ticket_participants SET removed_at = ? WHERE ticket_id = ? AND user_id = ? AND removed_at IS NULL",
        (now, ticket_id, user_id),
    )


# ---------------------------------------------------------------------------
# Warning DB operations
# ---------------------------------------------------------------------------


def create_warning(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    user_id: int,
    moderator_id: int,
    reason: str,
) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, user_id, moderator_id, reason, now),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_active_warning_count(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM warnings WHERE guild_id = ? AND user_id = ? AND revoked = 0",
        (guild_id, user_id),
    ).fetchone()
    return row["cnt"]


def get_warnings(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
) -> list[WarningRow]:
    rows = conn.execute(
        "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC",
        (guild_id, user_id),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def revoke_warning(
    conn: sqlite3.Connection,
    warning_id: int,
    *,
    revoked_by: int,
    reason: str,
) -> bool:
    now = time.time()
    cur = conn.execute(
        "UPDATE warnings SET revoked = 1, revoked_at = ?, revoked_by = ?, revoke_reason = ? WHERE id = ? AND revoked = 0",
        (now, revoked_by, reason, warning_id),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def write_audit(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    action: str,
    actor_id: int,
    target_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO audit_log (guild_id, action, actor_id, target_id, extra, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, action, actor_id, target_id, json.dumps(extra or {}), now),
    )
    return cur.lastrowid  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def store_transcript(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    record_type: str,
    record_id: int,
    content: dict[str, Any],
) -> int:
    now = time.time()
    cur = conn.execute(
        "INSERT INTO transcripts (guild_id, record_type, record_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (guild_id, record_type, record_id, json.dumps(content), now),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_transcript(
    conn: sqlite3.Connection,
    record_type: str,
    record_id: int,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT content FROM transcripts WHERE record_type = ? AND record_id = ? ORDER BY created_at DESC LIMIT 1",
        (record_type, record_id),
    ).fetchone()
    return json.loads(row["content"]) if row else None


# ---------------------------------------------------------------------------
# Transcript generation from Discord channel
# ---------------------------------------------------------------------------


async def generate_transcript(
    channel,  # discord.TextChannel
    *,
    record_type: str,
    record_id: int,
    participants: list[dict[str, Any]] | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect all messages from a channel and build a transcript dict."""
    messages: list[dict[str, Any]] = []
    async for msg in channel.history(limit=None, oldest_first=True):
        entry: dict[str, Any] = {
            "author_id": msg.author.id,
            "author_name": str(msg.author),
            "content": msg.content,
            "timestamp": msg.created_at.isoformat(),
        }
        if msg.embeds:
            entry["embeds"] = [e.to_dict() for e in msg.embeds]
        if msg.attachments:
            entry["attachments"] = [
                {"filename": a.filename, "url": a.url} for a in msg.attachments
            ]
        messages.append(entry)

    transcript: dict[str, Any] = {
        "type": record_type,
        "record_id": record_id,
        "channel_name": channel.name,
        "guild_id": channel.guild.id,
        "message_count": len(messages),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "messages": messages,
    }
    if participants:
        transcript["participants"] = participants
    if extra_meta:
        transcript.update(extra_meta)
    return transcript


# ---------------------------------------------------------------------------
# Policy ticket DB operations
# ---------------------------------------------------------------------------


def create_policy_ticket(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    creator_id: int,
    channel_id: int,
    title: str,
    description: str,
) -> int:
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO policy_tickets (guild_id, creator_id, channel_id, title,
                                     description, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?)
        """,
        (guild_id, creator_id, channel_id, title, description, now),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_policy_ticket_by_channel(
    conn: sqlite3.Connection,
    channel_id: int,
) -> PolicyTicketRow | None:
    row = conn.execute(
        "SELECT * FROM policy_tickets WHERE channel_id = ? AND status IN ('open', 'voting')",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def get_policy_ticket(
    conn: sqlite3.Connection,
    policy_id: int,
) -> PolicyTicketRow | None:
    row = conn.execute(
        "SELECT * FROM policy_tickets WHERE id = ?",
        (policy_id,),
    ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def start_policy_vote(
    conn: sqlite3.Connection, policy_id: int, *, vote_text: str
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE policy_tickets SET status = 'voting', vote_text = ?, vote_started_at = ? WHERE id = ?",
        (vote_text, now, policy_id),
    )


def cast_policy_vote(
    conn: sqlite3.Connection,
    *,
    policy_id: int,
    user_id: int,
    vote: str,
) -> None:
    now = time.time()
    conn.execute(
        """
        INSERT INTO policy_votes (policy_id, user_id, vote, voted_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (policy_id, user_id)
        DO UPDATE SET vote = excluded.vote, voted_at = excluded.voted_at
        """,
        (policy_id, user_id, vote, now),
    )


def get_policy_votes(
    conn: sqlite3.Connection,
    policy_id: int,
) -> list[PolicyVoteRow]:
    rows = conn.execute(
        "SELECT * FROM policy_votes WHERE policy_id = ?",
        (policy_id,),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def resolve_policy_vote(
    conn: sqlite3.Connection,
    policy_id: int,
    *,
    status: str,
) -> None:
    now = time.time()
    conn.execute(
        "UPDATE policy_tickets SET status = ?, vote_ended_at = ? WHERE id = ?",
        (status, now, policy_id),
    )


def close_policy_ticket(
    conn: sqlite3.Connection,
    policy_id: int,
) -> None:
    conn.execute(
        "UPDATE policy_tickets SET status = 'closed' WHERE id = ?",
        (policy_id,),
    )


def add_policy(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    policy_ticket_id: int,
    title: str,
    description: str,
) -> int:
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO policies (guild_id, policy_ticket_id, title, description, passed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild_id, policy_ticket_id, title, description, now),
    )
    return cur.lastrowid  # type: ignore[return-value]


def get_policies(
    conn: sqlite3.Connection,
    guild_id: int,
) -> list[PolicyRow]:
    rows = conn.execute(
        "SELECT * FROM policies WHERE guild_id = ? ORDER BY passed_at DESC",
        (guild_id,),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]


def get_policies_by_ticket_id(
    conn: sqlite3.Connection,
    policy_ticket_id: int,
) -> list[PolicyRow]:
    rows = conn.execute(
        "SELECT * FROM policies WHERE policy_ticket_id = ? ORDER BY passed_at DESC",
        (policy_ticket_id,),
    ).fetchall()
    return [dict(r) for r in rows]  # type: ignore[misc]
