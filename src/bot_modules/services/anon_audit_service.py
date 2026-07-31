"""Audit trail for anonymous member-facing surfaces.

Every feature that lets a member post under anonymity records who did what and
when here, so a moderator can trace an abusive anonymous submission back to its
author. See ``docs/anon_audit_spec.md`` and migration 145 for the privacy
reasoning; the short version is that this table stores **no content**. It keeps
a ``message_id`` pointer and the reader joins ``messages`` for the text, exactly
as the Confessions audit panel does — so content follows the guild's existing
message-storage level rather than being copied into a second place.

Confessions, Whisper and Guess are not routed through here. All three already
have DB-backed trails and admin panels of their own (``confession_threads``,
``whispers``, ``guess_audit_log`` — the last one covering ``/guess confess``
via ``guess_cog._do_audit``), and their tables are load-bearing for the
features themselves, so putting them under this retention purge would break
thread identity, whisper state and round history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import open_db

log = logging.getLogger("dungeonkeeper.anon_audit")

DEFAULT_RETENTION_DAYS = 90
RETENTION_FOREVER = 0
SECONDS_PER_DAY = 86400

# Feature slugs. These double as the dashboard filter values, and where a
# games-suite equivalent exists the slug matches ``games.constants`` game_type
# so GAME_ICONS lookups keep working.
FEATURE_AMA = "ama"
FEATURE_FFA = "ffa"
FEATURE_HOTTAKES = "hottakes"
FEATURE_FANTASIES = "fantasies"
FEATURE_CLAPBACK = "clapback"
FEATURE_WYR = "wyr"
FEATURE_COMPLIMENT = "compliment"

KNOWN_FEATURES = (
    FEATURE_AMA,
    FEATURE_FFA,
    FEATURE_HOTTAKES,
    FEATURE_FANTASIES,
    FEATURE_CLAPBACK,
    FEATURE_WYR,
    FEATURE_COMPLIMENT,
)


@dataclass(frozen=True)
class AnonAuditEvent:
    id: int
    guild_id: int
    feature: str
    event: str
    actor_id: int
    target_id: int | None
    game_id: str | None
    message_id: int | None
    channel_id: int | None
    extra: dict
    created_at: float


def _row_to_event(row: sqlite3.Row) -> AnonAuditEvent:
    try:
        extra = json.loads(row["extra"] or "{}")
    except (TypeError, ValueError):
        extra = {}
    return AnonAuditEvent(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        feature=row["feature"],
        event=row["event"],
        actor_id=int(row["actor_id"]),
        target_id=int(row["target_id"]) if row["target_id"] is not None else None,
        game_id=row["game_id"],
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        channel_id=int(row["channel_id"]) if row["channel_id"] is not None else None,
        extra=extra if isinstance(extra, dict) else {},
        created_at=float(row["created_at"]),
    )


# ── Writing ───────────────────────────────────────────────────────────────────


def insert_event(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    feature: str,
    event: str,
    actor_id: int,
    target_id: int | None = None,
    game_id: str | None = None,
    message_id: int | None = None,
    channel_id: int | None = None,
    extra: dict | None = None,
    created_at: float | None = None,
) -> int:
    """Insert one audit row on an existing connection. Returns its rowid."""
    cur = conn.execute(
        """
        INSERT INTO anon_audit_log (
            guild_id, feature, event, actor_id, target_id,
            game_id, message_id, channel_id, extra, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            feature,
            event,
            actor_id,
            target_id,
            game_id,
            message_id,
            channel_id,
            json.dumps(extra or {}),
            created_at if created_at is not None else time.time(),
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def record_event(
    db_path: Path,
    *,
    guild_id: int,
    feature: str,
    event: str,
    actor_id: int,
    target_id: int | None = None,
    game_id: str | None = None,
    message_id: int | None = None,
    channel_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """Best-effort audit write, opening its own connection.

    Swallows and logs every DB error: an audit failure must never take down the
    member-facing flow it is observing. A member should not lose the question
    they typed because the audit table was locked. This mirrors the existing
    ``guess_cog._write_audit`` contract.

    Call this from a thread (``asyncio.to_thread``) — it does blocking IO.
    """
    try:
        with open_db(db_path) as conn:
            insert_event(
                conn,
                guild_id=guild_id,
                feature=feature,
                event=event,
                actor_id=actor_id,
                target_id=target_id,
                game_id=game_id,
                message_id=message_id,
                channel_id=channel_id,
                extra=extra,
            )
    except Exception:
        log.exception(
            "anon audit write failed for feature=%s event=%s guild=%s",
            feature,
            event,
            guild_id,
        )


# ── Reading ───────────────────────────────────────────────────────────────────


def list_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    feature: str | None = None,
    actor_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AnonAuditEvent]:
    """Most recent events first, optionally narrowed by feature and/or actor."""
    clauses = ["guild_id = ?"]
    params: list = [guild_id]
    if feature:
        clauses.append("feature = ?")
        params.append(feature)
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT * FROM anon_audit_log
        WHERE {where}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def count_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    feature: str | None = None,
    actor_id: int | None = None,
) -> int:
    clauses = ["guild_id = ?"]
    params: list = [guild_id]
    if feature:
        clauses.append("feature = ?")
        params.append(feature)
    if actor_id is not None:
        clauses.append("actor_id = ?")
        params.append(actor_id)
    where = " AND ".join(clauses)
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM anon_audit_log WHERE {where}", params
        ).fetchone()[0]
    )


# ── Retention ─────────────────────────────────────────────────────────────────


def get_retention_days(conn: sqlite3.Connection, guild_id: int) -> int:
    row = conn.execute(
        "SELECT retention_days FROM anon_audit_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    if row is None:
        return DEFAULT_RETENTION_DAYS
    return int(row["retention_days"])


def set_retention_days(conn: sqlite3.Connection, guild_id: int, days: int) -> None:
    """Store the retention window. ``0`` disables purging for this guild."""
    if days < 0:
        raise ValueError("retention_days must be >= 0")
    conn.execute(
        """
        INSERT INTO anon_audit_config (guild_id, retention_days) VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET retention_days = excluded.retention_days
        """,
        (guild_id, days),
    )


def purge_expired(db_path: Path, *, now: float | None = None) -> int:
    """Delete rows past each guild's retention window. Returns rows removed.

    Guilds with no config row use :data:`DEFAULT_RETENTION_DAYS`, so a server
    that never opens the panel is still bounded. A guild set to
    :data:`RETENTION_FOREVER` is skipped entirely.

    Guilds are resolved from the log itself rather than from the config table,
    because the common case is a guild with rows and no config row.
    """
    now = time.time() if now is None else now
    removed = 0
    with open_db(db_path) as conn:
        guild_ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT guild_id FROM anon_audit_log"
            ).fetchall()
        ]
        for guild_id in guild_ids:
            days = get_retention_days(conn, guild_id)
            if days <= RETENTION_FOREVER:
                continue
            cutoff = now - (days * SECONDS_PER_DAY)
            cur = conn.execute(
                "DELETE FROM anon_audit_log WHERE guild_id = ? AND created_at < ?",
                (guild_id, cutoff),
            )
            removed += cur.rowcount or 0
    if removed:
        log.info("anon audit purge removed %d row(s)", removed)
    return removed
