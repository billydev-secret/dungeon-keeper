"""Age-based retention for the archive and the behavioural stores.

Built by the 2026-09-05 retention & disclosure review. That review's finding
was not that anything overruns a stated period — every bounded claim in
``docs/data_register.md`` is enforced and honoured — but that 44 of the
register's 78 rows said "indefinite", and almost none of those recorded a
decision. They recorded a default nobody had revisited.

**This ships disabled.** ``retention_enabled`` is false until a guild turns it
on, and no guild has. That is not the original intent — the first draft shipped
it enabled, on the argument that a rule nobody switches on is not a rule — but
the period below rests on a premise that turned out to be wrong, and a sweep
that deletes on a wrong number is worse than one that waits:

    The first draft said "no report reads further back than 90 days"
    (``contributors_service.WINDOW_DAYS``; ``attention_report`` uses 30).
    That is the longest *aggregate* consumer, not the longest consumer. The
    Connection Graph's replay (``reports_data.get_interaction_series``) reads
    ``user_interactions_log`` over **30 weeks / 210 days** by default, and
    ``connection-graph.js`` hard-codes ``weeks: 30``. At 180 days the first
    pass would permanently blank the replay's earliest weeks.

``BEHAVIOURAL_RETENTION_DAYS`` is therefore **provisional**. It is the owner's
to re-decide — raise the period past 210, or narrow the replay — and the dial
stays off until that happens. Do not enable it in prod on the strength of the
number as it stands.

* **Message content — 365 days.** The row survives; only the text goes. That
  split is deliberate and matches the archive's own design: CLAUDE.md keeps
  message content off by default precisely because the *derivations* (XP,
  sentiment, interaction edges, member activity) are the durable artefact and
  the text is the sensitive part. Deleting whole rows would also destroy the
  oldest activity history of seven guilds that never stored a byte of text, to
  solve a problem only the eighth guild has. See
  ``message_store.redact_message_content_older_than``.
  This period is *not* in doubt: nothing reads message text on a window at all.

* **Behavioural stores — 180 days, provisional.** ``reaction_log``,
  ``user_interactions_log``, ``voice_follow_log`` and ``ping_events``: who
  reacted to, replied to, followed and pinged whom. The profiling-flavoured
  half of the archive, and the half with no rollup to fall back on, so a
  deletion here is a real loss.

``member_events`` is deliberately **not** in that list. Tenure is computed as
``MIN(ts) FROM member_events`` with no window at all
(``rules_watch/service.py:compute_tenure_days``) — it wants a member's
first-ever join. Any period on that table shortens tenure for anyone who
joined before it, and ``rules_watch/scorer.py`` up-weights ``tenure_days < 7``,
so a three-year member who rejoined would be scored as a newcomer. The table is
1,497 rows. It stays.
"""

from __future__ import annotations

import logging
import sqlite3
import time

log = logging.getLogger(__name__)

#: Message *content* older than this is nulled; the row and every derivation
#: stay. A year comfortably clears the 360-day month-resolution activity graph
#: and gives the mod desk a full year to reach back over when investigating a
#: pattern, which is the use that genuinely wants depth.
MESSAGE_CONTENT_RETENTION_DAYS = 365

#: Behavioural rows older than this are deleted outright. Chosen as double the
#: longest *aggregate* consumer (90 days) — but see the module warning: the
#: Connection Graph replay reads 210 days by default and up to 420, so this
#: number does **not** currently leave every surface full headroom.
BEHAVIOURAL_RETENTION_DAYS = 180

#: Per-guild switch. Absent means **off**, matching ``xp_retention_enabled``:
#: an admin turns retention on deliberately, per guild, after deciding the
#: period is right for that guild. An earlier draft inverted this so that
#: absence meant *on*; that only made sense while the periods were settled,
#: and one of them is not.
RETENTION_CONFIG_KEY = "data_retention_enabled"

#: ``(table, timestamp column)``. Every one of these carries ``guild_id``, so
#: the sweep is per-guild and a guild that opts out is genuinely untouched.
#: Adding a row here needs a matching ``docs/data_register.md`` update and a
#: line in ``manual.html`` §Your Data & Privacy — the register row is the
#: record of processing, and an undisclosed sweep is as much a defect as an
#: undisclosed store.
BEHAVIOURAL_TABLES: tuple[tuple[str, str], ...] = (
    ("reaction_log", "ts"),
    ("user_interactions_log", "ts"),
    ("voice_follow_log", "ts"),
    ("ping_events", "ts"),
)

#: Rows deleted per statement. The first pass on the busy guild clears ~26k
#: rows from ``user_interactions_log``; nibbling keeps the write lock short
#: enough that message ingest does not stall behind it. That only holds
#: because each chunk is committed — see :func:`sweep_behavioural`; chunking
#: inside one open transaction would split the statements but not the lock.
DELETE_CHUNK = 20_000


def retention_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True only where a guild has explicitly switched retention on.

    Absent means off. Nothing is deleted anywhere until an admin ticks the box,
    which is the correct posture while ``BEHAVIOURAL_RETENTION_DAYS`` is still
    provisional — see the module docstring.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE key = ? AND guild_id = ?",
        (RETENTION_CONFIG_KEY, guild_id),
    ).fetchone()
    return bool(row) and str(row[0]).strip() in ("1", "true", "True", "on", "yes")


def behavioural_cutoff(*, older_than_days: int, now: float | None = None) -> float:
    """Unix timestamp before which behavioural rows are past their period."""
    return (time.time() if now is None else now) - older_than_days * 86400


def prunable_behavioural_count(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    older_than_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """How many rows each behavioural table would lose, without deleting any.

    A read-only counterpart to :func:`sweep_behavioural`, so the size of a
    pass can be measured — from a console or an ops script — without waiting
    for the sweep's own log line. Nothing on the dashboard calls it yet.
    """
    cutoff = behavioural_cutoff(older_than_days=older_than_days, now=now)
    counts: dict[str, int] = {}
    for table, ts_col in BEHAVIOURAL_TABLES:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE guild_id = ? AND {ts_col} < ?",
            (guild_id, cutoff),
        ).fetchone()
        counts[table] = int(row[0]) if row else 0
    return counts


def sweep_behavioural(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    older_than_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """Delete behavioural rows past their period. Returns per-table counts.

    Honours the guild's off switch — a disabled guild returns all-zero rather
    than raising, because "nothing to do" is the ordinary case here, not an
    error worth a caller's attention.

    **Commits between chunks.** SQLite's write lock is held for a whole
    transaction, so ``DELETE_CHUNK`` only bounds the stall if each chunk is
    released; without the commit the caller's transaction would pin the lock
    for the entire drain. The trade is that a mid-sweep failure leaves the
    earlier chunks deleted — which is the right way round for retention: the
    work is idempotent and the next pass simply resumes.
    """
    if not retention_enabled(conn, guild_id):
        return {table: 0 for table, _ in BEHAVIOURAL_TABLES}

    cutoff = behavioural_cutoff(older_than_days=older_than_days, now=now)
    deleted: dict[str, int] = {}
    for table, ts_col in BEHAVIOURAL_TABLES:
        total = 0
        while True:
            cur = conn.execute(
                f"DELETE FROM {table} WHERE rowid IN ("
                f"  SELECT rowid FROM {table}"
                f"  WHERE guild_id = ? AND {ts_col} < ? LIMIT {DELETE_CHUNK}"
                f")",
                (guild_id, cutoff),
            )
            n = max(cur.rowcount, 0)
            total += n
            # Release the write lock between chunks — see the docstring.
            conn.commit()
            if n < DELETE_CHUNK:
                break
        deleted[table] = total
    return deleted


def run_retention(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    message_days: int = MESSAGE_CONTENT_RETENTION_DAYS,
    behavioural_days: int = BEHAVIOURAL_RETENTION_DAYS,
    now: float | None = None,
) -> dict[str, int]:
    """Run both arms for one guild. Returns ``{"messages_redacted": n, ...}``.

    Imported lazily to keep ``message_store`` off this module's import path at
    definition time — the two are peers and a top-level import would make the
    retention service part of every message write's import graph.
    """
    from bot_modules.services import message_store

    if not retention_enabled(conn, guild_id):
        # Same keys as the enabled path: a caller that reads ``result[table]``
        # must not KeyError only for the guilds that opted out.
        return {
            "messages_redacted": 0,
            **{table: 0 for table, _ in BEHAVIOURAL_TABLES},
        }

    result = {
        "messages_redacted": message_store.redact_message_content_older_than(
            conn, guild_id, older_than_days=message_days, now=now
        )
    }
    result.update(
        sweep_behavioural(
            conn, guild_id, older_than_days=behavioural_days, now=now
        )
    )
    return result
