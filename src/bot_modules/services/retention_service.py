"""Age-based retention for the archive and the behavioural stores.

Built by the 2026-09-05 retention & disclosure review. That review's finding
was not that anything overruns a stated period — every bounded claim in
``docs/data_register.md`` is enforced and honoured — but that 44 of the
register's 79 rows said "indefinite", and almost none of those recorded a
decision. They recorded a default nobody had revisited.

Two periods were chosen by the owner against a measured fact: the longest
*aggregate* consumer reads 90 days back (``contributors_service``'s
``WINDOW_DAYS``; ``attention_report`` uses 30).

.. warning::

   That is not the longest consumer. The Connection Graph's replay
   (``reports_data.get_interaction_series``) reads ``user_interactions_log``
   and ``member_events`` over **30 weeks / 210 days** by default — the panel
   hard-codes ``weeks: 30`` — and the route accepts up to **60 weeks / 420
   days**. ``BEHAVIOURAL_RETENTION_DAYS`` below is *shorter* than that, so the
   first sweep permanently blanks the replay's earliest weeks. Flagged by the
   code review of 2026-09-05; the number is the owner's to re-decide (raise
   the period, or narrow the replay) before this runs in prod.

* **Message content — 365 days.** The row survives; only the text goes. That
  split is deliberate and matches the archive's own design: CLAUDE.md keeps
  message content off by default precisely because the *derivations* (XP,
  sentiment, interaction edges, member activity) are the durable artefact and
  the text is the sensitive part. Deleting whole rows would also destroy the
  oldest activity history of seven guilds that never stored a byte of text, to
  solve a problem only the eighth guild has. See
  ``message_store.redact_message_content_older_than``.

* **Behavioural stores — 180 days.** ``reaction_log``,
  ``user_interactions_log``, ``voice_follow_log``, ``member_events`` and
  ``ping_events``: who reacted to, replied to, followed and pinged whom. This
  is the profiling-flavoured half of the archive and the half with no rollup to
  fall back on, so a deletion here is a real loss and 180 days is double the
  longest consumer rather than merely equal to it.

Unlike ``xp_rollup_service``'s dial this ships **enabled**, with a per-guild
switch to turn it off. The reason is empirical: on the day it was written the
first pass redacted 0 message rows (the oldest text was 211 days old) and
deleted rows from exactly one behavioural table. A rule that costs nothing on
the day it lands and then simply holds the line is worth more than one that
ships dark — ``xp_retention_enabled`` shipped dark on 2026-08-26 and was still
off 210 days of events later, which is how a retention policy quietly becomes
no policy at all.
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

#: Per-guild off switch. Absent means **on** — the inverse of
#: ``xp_retention_enabled``, and the inversion is the point: a missing key here
#: means the policy applies, so a new guild is covered from its first day
#: rather than from the day someone remembers to tick a box.
RETENTION_CONFIG_KEY = "data_retention_disabled"

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
    ("member_events", "ts"),
    ("ping_events", "ts"),
)

#: Rows deleted per statement. The first pass on the busy guild clears ~26k
#: rows from ``user_interactions_log``; nibbling keeps the write lock short
#: enough that message ingest does not stall behind it. That only holds
#: because each chunk is committed — see :func:`sweep_behavioural`; chunking
#: inside one open transaction would split the statements but not the lock.
DELETE_CHUNK = 20_000


def retention_enabled(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True unless this guild has explicitly switched retention off.

    Note the default: an absent row means enabled. ``xp_retention_enabled``
    reads the other way round because turning *it* on deletes 651k rows at
    once; this sweep's first pass is near-empty, so the safe default and the
    privacy-preserving default are the same one for once.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE key = ? AND guild_id = ?",
        (RETENTION_CONFIG_KEY, guild_id),
    ).fetchone()
    if not row:
        return True
    return str(row[0]).strip() not in ("1", "true", "True", "on", "yes")


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
