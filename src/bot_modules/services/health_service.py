"""Health dashboard table initialization and metrics cache helpers.

The cache stores pre-computed JSON payloads keyed by ``(guild_id, metric_key)``
with a configurable TTL.  API endpoints read from cache first and fall back to
live computation on a miss.  A periodic batch job refreshes the cache every
15 minutes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

log = logging.getLogger("dungeonkeeper.health")


# ---------------------------------------------------------------------------
# Table initialization
# ---------------------------------------------------------------------------


def init_health_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS health_metrics_cache (
            guild_id     INTEGER NOT NULL,
            metric_key   TEXT    NOT NULL,
            payload_json TEXT    NOT NULL,
            computed_at  REAL    NOT NULL,
            ttl_seconds  INTEGER NOT NULL DEFAULT 900,
            PRIMARY KEY (guild_id, metric_key)
        );

        CREATE TABLE IF NOT EXISTS message_sentiment (
            message_id  INTEGER PRIMARY KEY,
            guild_id    INTEGER NOT NULL,
            channel_id  INTEGER NOT NULL,
            sentiment   REAL    NOT NULL,
            emotion     TEXT,
            computed_at REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sentiment_guild_ts
            ON message_sentiment (guild_id, computed_at);
        CREATE INDEX IF NOT EXISTS idx_sentiment_guild_channel
            ON message_sentiment (guild_id, channel_id);

        CREATE TABLE IF NOT EXISTS incident_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            event_type   TEXT    NOT NULL,
            severity     TEXT    NOT NULL,
            channel_id   INTEGER,
            details_json TEXT    NOT NULL DEFAULT '{}',
            detected_at  REAL    NOT NULL,
            resolved_at  REAL,
            resolved_by  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_incidents_guild_ts
            ON incident_events (guild_id, detected_at);

        CREATE TABLE IF NOT EXISTS message_velocity_baseline (
            guild_id    INTEGER NOT NULL,
            hour_of_day INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            mean_rate   REAL    NOT NULL,
            stddev_rate REAL    NOT NULL,
            updated_at  REAL    NOT NULL,
            PRIMARY KEY (guild_id, hour_of_day, day_of_week)
        );
    """)


# ---------------------------------------------------------------------------
# Metrics cache helpers
# ---------------------------------------------------------------------------

DEFAULT_TTL = 900  # 15 minutes


def get_cached(conn: sqlite3.Connection, guild_id: int, key: str) -> dict | None:
    """Return cached payload if fresh, else ``None``."""
    row = conn.execute(
        "SELECT payload_json, computed_at, ttl_seconds FROM health_metrics_cache "
        "WHERE guild_id = ? AND metric_key = ?",
        (guild_id, key),
    ).fetchone()
    if row is None:
        return None
    age = time.time() - row["computed_at"]
    if age > row["ttl_seconds"]:
        return None
    try:
        return json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def set_cached(
    conn: sqlite3.Connection,
    guild_id: int,
    key: str,
    payload: dict,
    ttl: int = DEFAULT_TTL,
) -> None:
    """Write *payload* into the cache."""
    conn.execute(
        "INSERT OR REPLACE INTO health_metrics_cache "
        "(guild_id, metric_key, payload_json, computed_at, ttl_seconds) "
        "VALUES (?, ?, ?, ?, ?)",
        (guild_id, key, json.dumps(payload), time.time(), ttl),
    )
    conn.commit()


def clear_cache(conn: sqlite3.Connection, guild_id: int, key: str | None = None) -> int:
    """Remove cached entries.  Returns rows deleted."""
    if key:
        cur = conn.execute(
            "DELETE FROM health_metrics_cache WHERE guild_id = ? AND metric_key = ?",
            (guild_id, key),
        )
    else:
        cur = conn.execute(
            "DELETE FROM health_metrics_cache WHERE guild_id = ?",
            (guild_id,),
        )
    conn.commit()
    return cur.rowcount
