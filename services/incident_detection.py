"""Real-time anomaly detection for the community health dashboard.

The ``VelocityTracker`` maintains an in-memory sliding window of message
timestamps per guild.  On each message it checks whether the current velocity
exceeds the stored baseline by more than 3 standard deviations.  When it does,
an ``incident_events`` row is written.

Baseline computation and other batch-oriented anomaly checks are handled by
``update_baselines`` (called from the periodic health batch job).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections import defaultdict, deque

log = logging.getLogger("dungeonkeeper.incidents")


class VelocityTracker:
    """Per-guild sliding-window message velocity tracker.

    Designed to be called from the ``on_message`` handler with negligible
    overhead — it maintains a deque of timestamps and does a single DB read
    (baseline) that is cached in memory.
    """

    def __init__(self) -> None:
        # guild_id -> deque of message timestamps (last 10 min)
        self._windows: dict[int, deque[float]] = defaultdict(lambda: deque())
        # guild_id -> (hour_of_day, day_of_week) -> (mean, stddev)
        self._baselines: dict[int, dict[tuple[int, int], tuple[float, float]]] = {}
        self._baseline_loaded: dict[int, float] = {}  # guild_id -> last load time
        self._WINDOW_SECONDS = 600  # 10-minute sliding window
        self._BASELINE_CACHE_TTL = 900  # reload baselines every 15 min
        # Cooldown to avoid duplicate incidents
        self._last_incident: dict[int, float] = {}  # guild_id -> timestamp
        self._INCIDENT_COOLDOWN = 300  # 5 min between incidents of same type

    def record_message(
        self,
        conn: sqlite3.Connection,
        guild_id: int,
        channel_id: int,
        ts: float | None = None,
    ) -> dict | None:
        """Record a message timestamp and check for velocity anomalies.

        Returns an incident dict if an anomaly is detected, else ``None``.
        """
        ts = ts or time.time()
        window = self._windows[guild_id]
        window.append(ts)

        # Trim window
        cutoff = ts - self._WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()

        # Load baselines if stale
        if ts - self._baseline_loaded.get(guild_id, 0) > self._BASELINE_CACHE_TTL:
            self._load_baselines(conn, guild_id)

        # Current velocity (messages per minute)
        current_rate = len(window) / (self._WINDOW_SECONDS / 60)

        # Baseline for this hour/day
        t = time.gmtime(ts)
        key = (t.tm_hour, (t.tm_wday + 1) % 7)  # align to our dow convention
        baseline = self._baselines.get(guild_id, {}).get(key)
        if baseline is None:
            return None

        mean, stddev = baseline
        if stddev == 0:
            return None

        # Check for anomaly: current rate > mean + 3*stddev
        threshold = mean + 3 * stddev
        if current_rate > threshold and current_rate > 5:  # minimum 5 msg/min to trigger
            # Cooldown check
            last = self._last_incident.get(guild_id, 0)
            if ts - last < self._INCIDENT_COOLDOWN:
                return None

            self._last_incident[guild_id] = ts
            incident = {
                "guild_id": guild_id,
                "event_type": "velocity_spike",
                "severity": "warning" if current_rate < threshold * 1.5 else "critical",
                "channel_id": channel_id,
                "details_json": json.dumps({
                    "current_rate": round(current_rate, 1),
                    "baseline_mean": round(mean, 1),
                    "baseline_stddev": round(stddev, 1),
                    "threshold": round(threshold, 1),
                }),
                "detected_at": ts,
            }

            # Write to DB
            conn.execute(
                "INSERT INTO incident_events "
                "(guild_id, event_type, severity, channel_id, details_json, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (incident["guild_id"], incident["event_type"], incident["severity"],
                 incident["channel_id"], incident["details_json"], incident["detected_at"]),
            )
            conn.commit()

            log.warning(
                "Velocity spike detected in guild %s: %.1f msgs/min (threshold: %.1f)",
                guild_id, current_rate, threshold,
            )
            return incident

        return None

    def _load_baselines(self, conn: sqlite3.Connection, guild_id: int) -> None:
        rows = conn.execute(
            "SELECT hour_of_day, day_of_week, mean_rate, stddev_rate "
            "FROM message_velocity_baseline WHERE guild_id=?",
            (guild_id,),
        ).fetchall()
        baselines: dict[tuple[int, int], tuple[float, float]] = {}
        for r in rows:
            baselines[(r["hour_of_day"], r["day_of_week"])] = (r["mean_rate"], r["stddev_rate"])
        self._baselines[guild_id] = baselines
        self._baseline_loaded[guild_id] = time.time()


def check_join_raid(
    conn: sqlite3.Connection,
    guild_id: int,
    member_id: int,
    account_created_at: float,
    ts: float | None = None,
) -> dict | None:
    """Check for new-account clustering (raid detection).

    Fires when 3+ accounts under 7 days old join within 2 minutes.
    """
    ts = ts or time.time()
    account_age_days = (ts - account_created_at) / 86400
    if account_age_days > 7:
        return None

    # Count recent new-account joins (last 2 minutes)
    two_min_ago = ts - 120
    # We check invite_edges for recent joins
    recent_new = conn.execute(
        """SELECT COUNT(*) FROM invite_edges
           WHERE guild_id=? AND joined_at>=? AND joined_at<=?""",
        (guild_id, two_min_ago, ts),
    ).fetchone()[0]

    if recent_new >= 3:
        incident = {
            "guild_id": guild_id,
            "event_type": "raid_attempt",
            "severity": "critical",
            "channel_id": None,
            "details_json": json.dumps({
                "new_accounts_2min": recent_new,
                "trigger_member_id": str(member_id),
                "account_age_days": round(account_age_days, 1),
            }),
            "detected_at": ts,
        }
        conn.execute(
            "INSERT INTO incident_events "
            "(guild_id, event_type, severity, channel_id, details_json, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, "raid_attempt", "critical", None, incident["details_json"], ts),
        )
        conn.commit()
        log.warning("Possible raid detected in guild %s: %d new accounts in 2 min", guild_id, recent_new)
        return incident
    return None


def update_baselines(conn: sqlite3.Connection, guild_id: int) -> None:
    """Recompute message velocity baselines from the last 30 days of data.

    Groups messages into 5-minute windows, then computes per-hour/day-of-week
    mean and standard deviation of the message rate (msgs per minute).
    """
    thirty_days_ago = time.time() - 30 * 86400
    now = time.time()

    rows = conn.execute(
        """SELECT
             CAST(((ts % 604800) + 345600) / 86400 AS INTEGER) % 7 AS dow,
             (ts % 86400) / 3600 AS hod,
             CAST(ts / 300 AS INTEGER) AS window5,
             COUNT(*) AS cnt
           FROM messages
           WHERE guild_id=? AND ts>=?
           GROUP BY dow, hod, window5""",
        (guild_id, int(thirty_days_ago)),
    ).fetchall()

    # Group by (hod, dow) and collect per-5-min rates
    rates: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in rows:
        rate = r["cnt"] / 5.0  # msgs per minute
        rates[(r["hod"], r["dow"])].append(rate)

    for (hod, dow), rate_list in rates.items():
        if not rate_list:
            continue
        mean = sum(rate_list) / len(rate_list)
        if len(rate_list) >= 2:
            variance = sum((x - mean) ** 2 for x in rate_list) / (len(rate_list) - 1)
            stddev = math.sqrt(variance)
        else:
            stddev = mean * 0.5  # fallback for sparse data

        conn.execute(
            "INSERT OR REPLACE INTO message_velocity_baseline "
            "(guild_id, hour_of_day, day_of_week, mean_rate, stddev_rate, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, hod, dow, round(mean, 3), round(stddev, 3), now),
        )

    conn.commit()
    log.debug("Updated velocity baselines for guild %s (%d slots)", guild_id, len(rates))


# Singleton tracker shared across the bot process
velocity_tracker = VelocityTracker()
