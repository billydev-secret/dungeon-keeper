"""Interaction graph — replies and mentions between users.

Stores pairwise interaction weights (``user_interactions``) and a timestamped
log (``user_interactions_log``). Reporting reads them through
``reports_data.get_interaction_graph_data``; rendering happens client-side.
"""

from __future__ import annotations

import logging
import sqlite3
import time as _time

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def init_interaction_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_interactions (
            guild_id     INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            weight       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, from_user_id, to_user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_interactions_log (
            guild_id     INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            to_user_id   INTEGER NOT NULL,
            ts           INTEGER NOT NULL,
            message_id   INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_interactions_log_guild_ts
        ON user_interactions_log (guild_id, ts)
        """
    )
    # Partial unique index: deduplicates rows that have a message_id so that
    # running /interaction_scan multiple times (or while the bot is live) does
    # not inflate the counts.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_interactions_log_dedup
        ON user_interactions_log (guild_id, message_id, from_user_id, to_user_id)
        WHERE message_id IS NOT NULL
        """
    )
    # Migration for existing databases that pre-date the message_id column.
    try:
        conn.execute("ALTER TABLE user_interactions_log ADD COLUMN message_id INTEGER")
    except Exception:
        log.exception("interaction_graph: message_id column may already exist")


def record_interactions(
    conn: sqlite3.Connection,
    guild_id: int,
    from_user_id: int,
    to_user_ids: list[int],
    amount: int = 1,
    ts: int | None = None,
    message_id: int | None = None,
) -> None:
    """Increment the interaction weight from *from_user_id* to each target.

    ts         – Unix timestamp of the interaction; defaults to now.
    message_id – Discord message ID.  When provided, the unique index on the
                 log table prevents the same message from being counted twice
                 (guards against scan + live-recording overlap, and repeated
                 scan runs).  The aggregate table is only updated when the log
                 insert is genuinely new.
    """
    ts = ts if ts is not None else int(_time.time())
    for to_user_id in to_user_ids:
        if to_user_id == from_user_id:
            continue
        result = conn.execute(
            """
            INSERT OR IGNORE INTO user_interactions_log
                (guild_id, from_user_id, to_user_id, ts, message_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, from_user_id, to_user_id, ts, message_id),
        )
        if result.rowcount == 0:
            # Duplicate message — already counted; skip aggregate update too.
            continue
        conn.execute(
            """
            INSERT INTO user_interactions (guild_id, from_user_id, to_user_id, weight)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, from_user_id, to_user_id)
            DO UPDATE SET weight = weight + excluded.weight
            """,
            (guild_id, from_user_id, to_user_id, amount),
        )


