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

import asyncio
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
PURGE_INTERVAL_SECONDS = 6 * 3600

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

# Event names. Constants rather than bare strings at the call sites, because
# MOD_EVENTS below assigns them meaning — renaming one silently reclassifies
# rows, and the dashboard must not have to keep its own copy of these strings.
EVENT_QUESTION_ASKED = "question_asked"
EVENT_QUESTION_APPROVED = "question_approved"
EVENT_QUESTION_REJECTED = "question_rejected"
EVENT_QUESTION_ANSWERED = "question_answered"
EVENT_QUESTION_PASSED = "question_passed"
EVENT_QUESTION_POSED = "question_posed"
EVENT_HOT_SEAT_SKIPPED = "hot_seat_skipped"
EVENT_HOT_SEAT_CHANGED = "hot_seat_changed"
EVENT_REPLY_POSTED = "reply_posted"
EVENT_TAKE_SUBMITTED = "take_submitted"
EVENT_ENTRY_SUBMITTED = "entry_submitted"
EVENT_ANSWER_SUBMITTED = "answer_submitted"
EVENT_VOTE = "vote"
EVENT_VOTERS_REVEALED = "voters_revealed"
EVENT_PAIRINGS_GENERATED = "pairings_generated"

# Events where the actor is a moderator acting on someone else's anonymous
# post, rather than a member posting anonymously. This is the most
# accountability-relevant distinction in the table, so it is derived here and
# shipped to the dashboard as a flag — the panel must not re-derive it from
# event strings it would then have to keep in sync.
MOD_EVENTS = frozenset({
    EVENT_QUESTION_APPROVED,
    EVENT_QUESTION_REJECTED,
    EVENT_HOT_SEAT_SKIPPED,
    EVENT_VOTERS_REVEALED,
})


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
    # Joined from `messages` when the caller asks for it; this table never
    # stores content of its own (migration 145).
    content: str | None = None

    @property
    def is_mod_action(self) -> bool:
        return self.event in MOD_EVENTS


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
        content=row["content"] if "content" in row.keys() else None,
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


def _filter(
    guild_id: int, feature: str | None, actor_id: int | None, *, prefix: str = ""
) -> tuple[str, list]:
    """Build the shared WHERE clause for the listing and its count.

    ``prefix`` qualifies the column names ("a." when the caller joins), so the
    listing, the count and the dashboard all narrow rows identically instead of
    each maintaining its own copy of this logic.
    """
    clauses = [f"{prefix}guild_id = ?"]
    params: list = [guild_id]
    if feature:
        clauses.append(f"{prefix}feature = ?")
        params.append(feature)
    if actor_id is not None:
        clauses.append(f"{prefix}actor_id = ?")
        params.append(actor_id)
    return " AND ".join(clauses), params


def list_events(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    feature: str | None = None,
    actor_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    with_content: bool = False,
) -> list[AnonAuditEvent]:
    """Most recent events first, optionally narrowed by feature and/or actor.

    ``with_content`` LEFT JOINs ``messages`` to recover each row's text. This
    table stores none of its own (migration 145), so content is present only
    where the event produced a guild message *and* the guild retains message
    content. The join is by rowid primary key, so it costs one seek per row.
    """
    where, params = _filter(guild_id, feature, actor_id, prefix="a.")
    content_col = ", m.content" if with_content else ", NULL AS content"
    join = (
        "LEFT JOIN messages m ON m.message_id = a.message_id"
        if with_content
        else ""
    )
    rows = conn.execute(
        f"""
        SELECT a.*{content_col}
        FROM anon_audit_log a
        {join}
        WHERE {where}
        ORDER BY a.created_at DESC, a.id DESC
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
    where, params = _filter(guild_id, feature, actor_id)
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

    One statement, correlating each row against its own guild's window. A
    per-guild loop needed a ``SELECT DISTINCT guild_id`` over the whole table
    first, and SQLite has no loose index scan — that walked every index entry
    four times a day just to rediscover a handful of ids, on a table WYR votes
    make large.
    """
    now = time.time() if now is None else now
    with open_db(db_path) as conn:
        cur = conn.execute(
            """
            DELETE FROM anon_audit_log
            WHERE created_at < ? - (? * COALESCE(
                (SELECT c.retention_days FROM anon_audit_config c
                  WHERE c.guild_id = anon_audit_log.guild_id), ?))
              AND COALESCE(
                (SELECT c.retention_days FROM anon_audit_config c
                  WHERE c.guild_id = anon_audit_log.guild_id), ?) > ?
            """,
            (
                now,
                SECONDS_PER_DAY,
                DEFAULT_RETENTION_DAYS,
                DEFAULT_RETENTION_DAYS,
                RETENTION_FOREVER,
            ),
        )
        removed = cur.rowcount or 0
    if removed:
        log.info("anon audit purge removed %d row(s)", removed)
    return removed


async def anon_audit_purge_loop(bot, db_path: Path) -> None:
    """Apply each guild's retention window, every ``PURGE_INTERVAL_SECONDS``.

    Registered as a ``startup_task_factory`` rather than living in a cog: this
    table spans seven features, so no feature cog owns it, and the repo already
    routes owner-less background work this way (see ``__main__``).

    Six-hourly, not hourly — retention is measured in days, so landing within a
    quarter-day of the cutoff is exact enough.
    """
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
        try:
            removed = await asyncio.to_thread(purge_expired, db_path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("anon audit retention sweep failed")
            continue
        if removed:
            log.info("anon audit retention sweep removed %d row(s)", removed)
