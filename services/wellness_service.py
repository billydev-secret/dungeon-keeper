"""Core wellness guardian service — schema + low-level helpers.

This module owns the wellness_* tables and exposes pure-DB helpers (no Discord
imports). Higher-level orchestration lives in services/wellness_enforcement.py
and services/wellness_scheduler.py. Slash commands import from here for CRUD.

All timezone-sensitive math goes through user_now() / window_start_for() so
windows are derived lazily on every message — never from a loop reset.
"""
from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENFORCEMENT_LEVELS: tuple[str, ...] = ("gentle", "cooldown", "slow_mode", "gradual")
NOTIFICATION_PREFS: tuple[str, ...] = ("ephemeral", "dm", "both")
CAP_SCOPES: tuple[str, ...] = ("global", "channel", "category", "voice")
CAP_WINDOWS: tuple[str, ...] = ("hourly", "daily", "weekly")
PARTNER_STATUSES: tuple[str, ...] = ("pending", "accepted")

DEFAULT_ENFORCEMENT = "gradual"
DEFAULT_NOTIFICATIONS = "both"
DEFAULT_SLOW_MODE_RATE_SECONDS = 120
DEFAULT_TIMEZONE = "UTC"

NUDGE_SUPPRESSION_SECONDS = 300  # spec §4.1: 5 minutes
COOLDOWN_DURATION_SECONDS = 300  # spec §4.2: 5-minute breather
AWAY_RATE_LIMIT_SECONDS = 1800   # spec §4.6: once per channel per 30 min
AWAY_MESSAGE_MAX_LEN = 500       # spec §4.6: editor character limit
SETTINGS_RETENTION_SECONDS = 30 * 86400  # spec §3: 30 days post-optout

# Milestone badges (earned days → badge emoji). Order matters for upgrades.
MILESTONES: tuple[tuple[int, str], ...] = (
    (0, "🌱"),
    (7, "🌟"),
    (30, "🔥"),
    (100, "💪"),
    (365, "👑"),
)

# Day-of-week bitmask: Mon=1, Tue=2, ..., Sun=64
DAY_BIT = {0: 1, 1: 2, 2: 4, 3: 8, 4: 16, 5: 32, 6: 64}
ALL_DAYS_MASK = 127  # 1+2+4+8+16+32+64
WEEKDAY_MASK = 1 + 2 + 4 + 8 + 16  # Mon..Fri
WEEKEND_MASK = 32 + 64  # Sat+Sun


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_wellness_tables(conn: sqlite3.Connection) -> None:
    """Create all wellness_* tables. Idempotent — safe to call on every startup."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_users (
            guild_id               INTEGER NOT NULL,
            user_id                INTEGER NOT NULL,
            timezone               TEXT NOT NULL DEFAULT 'UTC',
            enforcement_level      TEXT NOT NULL DEFAULT 'gradual',
            notifications_pref     TEXT NOT NULL DEFAULT 'both',
            slow_mode_rate_seconds INTEGER NOT NULL DEFAULT 120,
            public_commitment      INTEGER NOT NULL DEFAULT 1,
            away_enabled           INTEGER NOT NULL DEFAULT 0,
            away_message           TEXT NOT NULL DEFAULT '',
            daily_reset_hour       INTEGER NOT NULL DEFAULT 0,
            opted_in_at            REAL,
            opted_out_at           REAL,
            paused_until           REAL,
            cooldown_until         REAL,
            last_nudge_at          REAL,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_caps (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            label           TEXT NOT NULL,
            scope           TEXT NOT NULL,
            scope_target_id INTEGER NOT NULL DEFAULT 0,
            window          TEXT NOT NULL,
            cap_limit       INTEGER NOT NULL,
            exclude_exempt  INTEGER NOT NULL DEFAULT 1,
            created_at      REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellness_caps_user ON wellness_caps (guild_id, user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_cap_counters (
            cap_id             INTEGER NOT NULL,
            window_start_epoch INTEGER NOT NULL,
            count              INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (cap_id, window_start_epoch)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_cap_overages (
            cap_id             INTEGER NOT NULL,
            window_start_epoch INTEGER NOT NULL,
            overage_count      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (cap_id, window_start_epoch)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_blackouts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            name         TEXT NOT NULL,
            start_minute INTEGER NOT NULL,
            end_minute   INTEGER NOT NULL,
            days_mask    INTEGER NOT NULL,
            enabled      INTEGER NOT NULL DEFAULT 1,
            created_at   REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellness_blackouts_user ON wellness_blackouts (guild_id, user_id)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_blackout_active (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            blackout_id INTEGER NOT NULL,
            started_at  REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, blackout_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_slow_mode (
            guild_id               INTEGER NOT NULL,
            user_id                INTEGER NOT NULL,
            triggered_by_cap_id    INTEGER NOT NULL DEFAULT 0,
            triggered_window_start INTEGER NOT NULL DEFAULT 0,
            last_message_ts        REAL NOT NULL DEFAULT 0,
            active_until_ts        REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_streaks (
            guild_id            INTEGER NOT NULL,
            user_id             INTEGER NOT NULL,
            current_days        INTEGER NOT NULL DEFAULT 0,
            personal_best       INTEGER NOT NULL DEFAULT 0,
            streak_start_date   TEXT,
            last_violation_date TEXT,
            current_badge       TEXT NOT NULL DEFAULT '',
            celebrated_badge    TEXT NOT NULL DEFAULT '',
            updated_at          REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    # Idempotent migration for pre-Phase-E schemas
    try:
        conn.execute("ALTER TABLE wellness_streaks ADD COLUMN celebrated_badge TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # already added

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_streak_history (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            day         TEXT NOT NULL,
            streak_days INTEGER NOT NULL,
            PRIMARY KEY (guild_id, user_id, day)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_partners (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id     INTEGER NOT NULL,
            user_a       INTEGER NOT NULL,
            user_b       INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   REAL NOT NULL,
            accepted_at  REAL,
            UNIQUE (guild_id, user_a, user_b)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellness_partners_a ON wellness_partners (guild_id, user_a)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_wellness_partners_b ON wellness_partners (guild_id, user_b)"
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_away_rate_limit (
            guild_id     INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            channel_id   INTEGER NOT NULL,
            last_sent_at REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, channel_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_exempt_channels (
            guild_id   INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            label      TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            PRIMARY KEY (guild_id, channel_id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_weekly_reports (
            guild_id    INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            iso_year    INTEGER NOT NULL,
            iso_week    INTEGER NOT NULL,
            week_start  TEXT NOT NULL,
            report_json TEXT NOT NULL,
            ai_text     TEXT NOT NULL DEFAULT '',
            sent_at     REAL NOT NULL,
            PRIMARY KEY (guild_id, user_id, iso_year, iso_week)
        )
        """
    )

    # Migrate old schema (category + 3 channels) → single channel
    cols = {r[1] for r in conn.execute("PRAGMA table_info(wellness_config)").fetchall()}
    if "category_id" in cols and "channel_id" not in cols:
        conn.execute("DROP TABLE wellness_config")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wellness_config (
            guild_id               INTEGER PRIMARY KEY,
            role_id                INTEGER NOT NULL DEFAULT 0,
            channel_id             INTEGER NOT NULL DEFAULT 0,
            active_list_message_id INTEGER NOT NULL DEFAULT 0,
            crisis_resource_url    TEXT NOT NULL DEFAULT '',
            default_enforcement    TEXT NOT NULL DEFAULT 'gradual'
        )
        """
    )


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------

def safe_zone(tz_name: str | None) -> ZoneInfo:
    """Resolve a tz string to a ZoneInfo, falling back to UTC on bad input."""
    if not tz_name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def user_now(tz_name: str | None) -> datetime:
    """Current local time in the user's timezone."""
    return datetime.now(safe_zone(tz_name))


def window_start_for(window: str, now_local: datetime, daily_reset_hour: int = 0) -> datetime:
    """Return the start of the current window in the user's local time.

    - hourly: top of the current hour
    - daily : at `daily_reset_hour` today (or yesterday if before reset hour)
    - weekly: Monday at `daily_reset_hour` of the current ISO week
    """
    if window == "hourly":
        return now_local.replace(minute=0, second=0, microsecond=0)
    if window == "daily":
        anchor = now_local.replace(hour=daily_reset_hour, minute=0, second=0, microsecond=0)
        if now_local < anchor:
            anchor -= timedelta(days=1)
        return anchor
    if window == "weekly":
        anchor = now_local.replace(hour=daily_reset_hour, minute=0, second=0, microsecond=0)
        if now_local < anchor:
            anchor -= timedelta(days=1)
        days_since_monday = anchor.weekday()  # Mon=0
        return anchor - timedelta(days=days_since_monday)
    raise ValueError(f"Unknown window: {window!r}")


def window_start_epoch(window: str, now_local: datetime, daily_reset_hour: int = 0) -> int:
    """Same as window_start_for but returns an epoch second integer for storage."""
    return int(window_start_for(window, now_local, daily_reset_hour).timestamp())


# ---------------------------------------------------------------------------
# wellness_users CRUD
# ---------------------------------------------------------------------------

@dataclass
class WellnessUser:
    guild_id: int
    user_id: int
    timezone: str
    enforcement_level: str
    notifications_pref: str
    slow_mode_rate_seconds: int
    public_commitment: bool
    away_enabled: bool
    away_message: str
    daily_reset_hour: int
    opted_in_at: float | None
    opted_out_at: float | None
    paused_until: float | None
    cooldown_until: float | None
    last_nudge_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessUser":
        return cls(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            timezone=str(row["timezone"]),
            enforcement_level=str(row["enforcement_level"]),
            notifications_pref=str(row["notifications_pref"]),
            slow_mode_rate_seconds=int(row["slow_mode_rate_seconds"]),
            public_commitment=bool(row["public_commitment"]),
            away_enabled=bool(row["away_enabled"]),
            away_message=str(row["away_message"]),
            daily_reset_hour=int(row["daily_reset_hour"]),
            opted_in_at=row["opted_in_at"],
            opted_out_at=row["opted_out_at"],
            paused_until=row["paused_until"],
            cooldown_until=row["cooldown_until"],
            last_nudge_at=row["last_nudge_at"],
        )

    @property
    def is_active(self) -> bool:
        """A user is active if they've opted in and not opted out."""
        return self.opted_in_at is not None and self.opted_out_at is None

    @property
    def is_paused(self) -> bool:
        return self.paused_until is not None and self.paused_until > time.time()


def get_wellness_user(
    conn: sqlite3.Connection, guild_id: int, user_id: int,
) -> WellnessUser | None:
    row = conn.execute(
        "SELECT * FROM wellness_users WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return WellnessUser.from_row(row) if row else None


def opt_in_user(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    timezone: str,
    enforcement_level: str = DEFAULT_ENFORCEMENT,
    notifications_pref: str = DEFAULT_NOTIFICATIONS,
) -> WellnessUser:
    """Insert or re-activate a wellness user. Resets opt_out timestamp."""
    if enforcement_level not in ENFORCEMENT_LEVELS:
        enforcement_level = DEFAULT_ENFORCEMENT
    if notifications_pref not in NOTIFICATION_PREFS:
        notifications_pref = DEFAULT_NOTIFICATIONS

    now = time.time()
    conn.execute(
        """
        INSERT INTO wellness_users (
            guild_id, user_id, timezone, enforcement_level, notifications_pref,
            slow_mode_rate_seconds, public_commitment, away_enabled, away_message,
            daily_reset_hour, opted_in_at, opted_out_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, '', 0, ?, NULL)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            timezone           = excluded.timezone,
            enforcement_level  = excluded.enforcement_level,
            notifications_pref = excluded.notifications_pref,
            opted_in_at        = excluded.opted_in_at,
            opted_out_at       = NULL
        """,
        (
            guild_id, user_id, timezone, enforcement_level, notifications_pref,
            DEFAULT_SLOW_MODE_RATE_SECONDS, now,
        ),
    )
    user = get_wellness_user(conn, guild_id, user_id)
    assert user is not None
    return user


def opt_out_user(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Mark user as opted out. Settings are kept for SETTINGS_RETENTION_SECONDS."""
    conn.execute(
        """
        UPDATE wellness_users
           SET opted_out_at = ?,
               paused_until = NULL,
               cooldown_until = NULL
         WHERE guild_id = ? AND user_id = ?
        """,
        (time.time(), guild_id, user_id),
    )
    # Also lift any active slow mode immediately
    conn.execute(
        "DELETE FROM wellness_slow_mode WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    # Drop active blackout markers
    conn.execute(
        "DELETE FROM wellness_blackout_active WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def update_user_settings(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    timezone: str | None = None,
    enforcement_level: str | None = None,
    notifications_pref: str | None = None,
    slow_mode_rate_seconds: int | None = None,
    public_commitment: bool | None = None,
    daily_reset_hour: int | None = None,
) -> None:
    fields: list[str] = []
    values: list = []
    if timezone is not None:
        fields.append("timezone = ?")
        values.append(timezone)
    if enforcement_level is not None and enforcement_level in ENFORCEMENT_LEVELS:
        fields.append("enforcement_level = ?")
        values.append(enforcement_level)
    if notifications_pref is not None and notifications_pref in NOTIFICATION_PREFS:
        fields.append("notifications_pref = ?")
        values.append(notifications_pref)
    if slow_mode_rate_seconds is not None and slow_mode_rate_seconds > 0:
        fields.append("slow_mode_rate_seconds = ?")
        values.append(slow_mode_rate_seconds)
    if public_commitment is not None:
        fields.append("public_commitment = ?")
        values.append(1 if public_commitment else 0)
    if daily_reset_hour is not None and 0 <= daily_reset_hour < 24:
        fields.append("daily_reset_hour = ?")
        values.append(daily_reset_hour)
    if not fields:
        return
    values.extend([guild_id, user_id])
    conn.execute(
        f"UPDATE wellness_users SET {', '.join(fields)} WHERE guild_id = ? AND user_id = ?",
        values,
    )


def pause_user(
    conn: sqlite3.Connection, guild_id: int, user_id: int, until: float,
) -> None:
    conn.execute(
        "UPDATE wellness_users SET paused_until = ? WHERE guild_id = ? AND user_id = ?",
        (until, guild_id, user_id),
    )
    conn.execute(
        "DELETE FROM wellness_slow_mode WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def resume_user(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE wellness_users SET paused_until = NULL WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def set_cooldown(
    conn: sqlite3.Connection, guild_id: int, user_id: int, until: float,
) -> None:
    conn.execute(
        "UPDATE wellness_users SET cooldown_until = ? WHERE guild_id = ? AND user_id = ?",
        (until, guild_id, user_id),
    )


def clear_cooldown(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "UPDATE wellness_users SET cooldown_until = NULL WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def list_active_users(conn: sqlite3.Connection, guild_id: int) -> list[WellnessUser]:
    rows = conn.execute(
        "SELECT * FROM wellness_users WHERE guild_id = ? AND opted_in_at IS NOT NULL AND opted_out_at IS NULL",
        (guild_id,),
    ).fetchall()
    return [WellnessUser.from_row(r) for r in rows]


def gc_opted_out_users(conn: sqlite3.Connection, retention_seconds: int = SETTINGS_RETENTION_SECONDS) -> int:
    """Delete users opted out longer than retention. Returns number deleted."""
    cutoff = time.time() - retention_seconds
    rows = conn.execute(
        "SELECT guild_id, user_id FROM wellness_users WHERE opted_out_at IS NOT NULL AND opted_out_at < ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    for row in rows:
        gid, uid = int(row["guild_id"]), int(row["user_id"])
        conn.execute("DELETE FROM wellness_users WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_caps WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_blackouts WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_blackout_active WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_slow_mode WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_streaks WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_streak_history WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute(
            "DELETE FROM wellness_partners WHERE guild_id = ? AND (user_a = ? OR user_b = ?)",
            (gid, uid, uid),
        )
        conn.execute("DELETE FROM wellness_away_rate_limit WHERE guild_id = ? AND user_id = ?", (gid, uid))
        conn.execute("DELETE FROM wellness_weekly_reports WHERE guild_id = ? AND user_id = ?", (gid, uid))
    return len(rows)


# ---------------------------------------------------------------------------
# wellness_config (per-guild singleton)
# ---------------------------------------------------------------------------

@dataclass
class WellnessConfig:
    guild_id: int
    role_id: int
    channel_id: int
    active_list_message_id: int
    crisis_resource_url: str
    default_enforcement: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessConfig":
        return cls(
            guild_id=int(row["guild_id"]),
            role_id=int(row["role_id"]),
            channel_id=int(row["channel_id"]),
            active_list_message_id=int(row["active_list_message_id"]),
            crisis_resource_url=str(row["crisis_resource_url"]),
            default_enforcement=str(row["default_enforcement"]),
        )


def get_wellness_config(conn: sqlite3.Connection, guild_id: int) -> WellnessConfig | None:
    row = conn.execute(
        "SELECT * FROM wellness_config WHERE guild_id = ?", (guild_id,),
    ).fetchone()
    return WellnessConfig.from_row(row) if row else None


def upsert_wellness_config(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    role_id: int | None = None,
    channel_id: int | None = None,
    active_list_message_id: int | None = None,
    crisis_resource_url: str | None = None,
    default_enforcement: str | None = None,
) -> WellnessConfig:
    """Upsert the per-guild wellness config row, only setting fields supplied."""
    existing = get_wellness_config(conn, guild_id)
    if existing is None:
        conn.execute(
            "INSERT INTO wellness_config (guild_id) VALUES (?)",
            (guild_id,),
        )
        existing = get_wellness_config(conn, guild_id)
        assert existing is not None

    fields: list[str] = []
    values: list = []
    if role_id is not None:
        fields.append("role_id = ?")
        values.append(int(role_id))
    if channel_id is not None:
        fields.append("channel_id = ?")
        values.append(int(channel_id))
    if active_list_message_id is not None:
        fields.append("active_list_message_id = ?")
        values.append(int(active_list_message_id))
    if crisis_resource_url is not None:
        fields.append("crisis_resource_url = ?")
        values.append(str(crisis_resource_url))
    if default_enforcement is not None and default_enforcement in ENFORCEMENT_LEVELS:
        fields.append("default_enforcement = ?")
        values.append(default_enforcement)

    if fields:
        values.append(guild_id)
        conn.execute(
            f"UPDATE wellness_config SET {', '.join(fields)} WHERE guild_id = ?",
            values,
        )
    result = get_wellness_config(conn, guild_id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

@dataclass
class WellnessCap:
    id: int
    guild_id: int
    user_id: int
    label: str
    scope: str
    scope_target_id: int
    window: str
    cap_limit: int
    exclude_exempt: bool
    created_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessCap":
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            label=str(row["label"]),
            scope=str(row["scope"]),
            scope_target_id=int(row["scope_target_id"]),
            window=str(row["window"]),
            cap_limit=int(row["cap_limit"]),
            exclude_exempt=bool(row["exclude_exempt"]),
            created_at=float(row["created_at"]),
        )


def add_cap(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    label: str,
    scope: str,
    scope_target_id: int,
    window: str,
    cap_limit: int,
    exclude_exempt: bool = True,
) -> int:
    if scope not in CAP_SCOPES:
        raise ValueError(f"invalid scope: {scope}")
    if window not in CAP_WINDOWS:
        raise ValueError(f"invalid window: {window}")
    if cap_limit < 1:
        raise ValueError("cap_limit must be >= 1")
    cur = conn.execute(
        """
        INSERT INTO wellness_caps
            (guild_id, user_id, label, scope, scope_target_id, window, cap_limit, exclude_exempt, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, label, scope, scope_target_id, window, cap_limit, 1 if exclude_exempt else 0, time.time()),
    )
    return int(cur.lastrowid or 0)


def list_caps(conn: sqlite3.Connection, guild_id: int, user_id: int) -> list[WellnessCap]:
    rows = conn.execute(
        "SELECT * FROM wellness_caps WHERE guild_id = ? AND user_id = ? ORDER BY id",
        (guild_id, user_id),
    ).fetchall()
    return [WellnessCap.from_row(r) for r in rows]


def get_cap(conn: sqlite3.Connection, cap_id: int) -> WellnessCap | None:
    row = conn.execute("SELECT * FROM wellness_caps WHERE id = ?", (cap_id,)).fetchone()
    return WellnessCap.from_row(row) if row else None


def find_cap_by_label(
    conn: sqlite3.Connection, guild_id: int, user_id: int, label: str,
) -> WellnessCap | None:
    row = conn.execute(
        "SELECT * FROM wellness_caps WHERE guild_id = ? AND user_id = ? AND label = ? LIMIT 1",
        (guild_id, user_id, label),
    ).fetchone()
    return WellnessCap.from_row(row) if row else None


def update_cap_limit(conn: sqlite3.Connection, cap_id: int, new_limit: int) -> bool:
    if new_limit < 1:
        return False
    cur = conn.execute(
        "UPDATE wellness_caps SET cap_limit = ? WHERE id = ?", (new_limit, cap_id),
    )
    return (cur.rowcount or 0) > 0


def remove_cap(conn: sqlite3.Connection, cap_id: int) -> bool:
    cur = conn.execute("DELETE FROM wellness_caps WHERE id = ?", (cap_id,))
    conn.execute("DELETE FROM wellness_cap_counters WHERE cap_id = ?", (cap_id,))
    conn.execute("DELETE FROM wellness_cap_overages WHERE cap_id = ?", (cap_id,))
    return (cur.rowcount or 0) > 0


def increment_cap_counter(
    conn: sqlite3.Connection, cap_id: int, window_start_epoch_value: int,
) -> int:
    """Atomically bump (cap_id, window_start) and return the new count."""
    conn.execute(
        """
        INSERT INTO wellness_cap_counters (cap_id, window_start_epoch, count)
        VALUES (?, ?, 1)
        ON CONFLICT(cap_id, window_start_epoch) DO UPDATE SET count = count + 1
        """,
        (cap_id, window_start_epoch_value),
    )
    row = conn.execute(
        "SELECT count FROM wellness_cap_counters WHERE cap_id = ? AND window_start_epoch = ?",
        (cap_id, window_start_epoch_value),
    ).fetchone()
    return int(row["count"]) if row else 0


def get_cap_counter(
    conn: sqlite3.Connection, cap_id: int, window_start_epoch_value: int,
) -> int:
    row = conn.execute(
        "SELECT count FROM wellness_cap_counters WHERE cap_id = ? AND window_start_epoch = ?",
        (cap_id, window_start_epoch_value),
    ).fetchone()
    return int(row["count"]) if row else 0


def increment_cap_overage(
    conn: sqlite3.Connection, cap_id: int, window_start_epoch_value: int,
) -> int:
    """Bump and return the new overage count for (cap, window)."""
    conn.execute(
        """
        INSERT INTO wellness_cap_overages (cap_id, window_start_epoch, overage_count)
        VALUES (?, ?, 1)
        ON CONFLICT(cap_id, window_start_epoch) DO UPDATE SET overage_count = overage_count + 1
        """,
        (cap_id, window_start_epoch_value),
    )
    row = conn.execute(
        "SELECT overage_count FROM wellness_cap_overages WHERE cap_id = ? AND window_start_epoch = ?",
        (cap_id, window_start_epoch_value),
    ).fetchone()
    return int(row["overage_count"]) if row else 0


def gc_old_cap_data(conn: sqlite3.Connection, older_than_seconds: int = 14 * 86400) -> int:
    """Delete counter/overage rows older than the cutoff. Returns rows deleted."""
    cutoff = int(time.time() - older_than_seconds)
    cur1 = conn.execute("DELETE FROM wellness_cap_counters WHERE window_start_epoch < ?", (cutoff,))
    cur2 = conn.execute("DELETE FROM wellness_cap_overages WHERE window_start_epoch < ?", (cutoff,))
    return (cur1.rowcount or 0) + (cur2.rowcount or 0)


# ---------------------------------------------------------------------------
# Blackouts
# ---------------------------------------------------------------------------

@dataclass
class WellnessBlackout:
    id: int
    guild_id: int
    user_id: int
    name: str
    start_minute: int
    end_minute: int
    days_mask: int
    enabled: bool
    created_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessBlackout":
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            name=str(row["name"]),
            start_minute=int(row["start_minute"]),
            end_minute=int(row["end_minute"]),
            days_mask=int(row["days_mask"]),
            enabled=bool(row["enabled"]),
            created_at=float(row["created_at"]),
        )

    def includes_day(self, weekday_mon0: int) -> bool:
        return bool(self.days_mask & DAY_BIT[weekday_mon0])

    def is_active_at(self, local_dt: datetime) -> bool:
        """True if `local_dt` (user local time) falls inside this blackout."""
        if not self.enabled:
            return False
        minute_of_day = local_dt.hour * 60 + local_dt.minute
        weekday = local_dt.weekday()
        if self.start_minute <= self.end_minute:
            # Same-day window (e.g. 09:00-17:00)
            if not self.includes_day(weekday):
                return False
            return self.start_minute <= minute_of_day < self.end_minute
        # Wrap-around window (e.g. 23:00-07:00)
        if self.includes_day(weekday) and minute_of_day >= self.start_minute:
            return True
        prev_day = (weekday - 1) % 7
        if self._mask_includes(prev_day) and minute_of_day < self.end_minute:
            return True
        return False

    def _mask_includes(self, weekday_mon0: int) -> bool:
        return bool(self.days_mask & DAY_BIT[weekday_mon0])


def add_blackout(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    name: str,
    start_minute: int,
    end_minute: int,
    days_mask: int,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO wellness_blackouts
            (guild_id, user_id, name, start_minute, end_minute, days_mask, enabled, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (guild_id, user_id, name, start_minute, end_minute, days_mask, time.time()),
    )
    return int(cur.lastrowid or 0)


def list_blackouts(
    conn: sqlite3.Connection, guild_id: int, user_id: int,
) -> list[WellnessBlackout]:
    rows = conn.execute(
        "SELECT * FROM wellness_blackouts WHERE guild_id = ? AND user_id = ? ORDER BY id",
        (guild_id, user_id),
    ).fetchall()
    return [WellnessBlackout.from_row(r) for r in rows]


def find_blackout_by_name(
    conn: sqlite3.Connection, guild_id: int, user_id: int, name: str,
) -> WellnessBlackout | None:
    row = conn.execute(
        "SELECT * FROM wellness_blackouts WHERE guild_id = ? AND user_id = ? AND name = ? LIMIT 1",
        (guild_id, user_id, name),
    ).fetchone()
    return WellnessBlackout.from_row(row) if row else None


def toggle_blackout(conn: sqlite3.Connection, blackout_id: int, enabled: bool) -> bool:
    cur = conn.execute(
        "UPDATE wellness_blackouts SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, blackout_id),
    )
    return (cur.rowcount or 0) > 0


def remove_blackout(conn: sqlite3.Connection, blackout_id: int) -> bool:
    cur = conn.execute("DELETE FROM wellness_blackouts WHERE id = ?", (blackout_id,))
    conn.execute("DELETE FROM wellness_blackout_active WHERE blackout_id = ?", (blackout_id,))
    return (cur.rowcount or 0) > 0


def mark_blackout_active(
    conn: sqlite3.Connection, guild_id: int, user_id: int, blackout_id: int,
) -> bool:
    """Returns True if newly inserted (i.e. blackout just started)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO wellness_blackout_active (guild_id, user_id, blackout_id, started_at) VALUES (?, ?, ?, ?)",
        (guild_id, user_id, blackout_id, time.time()),
    )
    return (cur.rowcount or 0) > 0


def clear_blackout_active(
    conn: sqlite3.Connection, guild_id: int, user_id: int, blackout_id: int,
) -> None:
    conn.execute(
        "DELETE FROM wellness_blackout_active WHERE guild_id = ? AND user_id = ? AND blackout_id = ?",
        (guild_id, user_id, blackout_id),
    )


def list_active_blackout_markers(
    conn: sqlite3.Connection, guild_id: int, user_id: int,
) -> list[int]:
    rows = conn.execute(
        "SELECT blackout_id FROM wellness_blackout_active WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchall()
    return [int(r["blackout_id"]) for r in rows]


# Blackout templates per spec §9
BLACKOUT_TEMPLATES: dict[str, dict] = {
    "night_owl": {
        "name": "Night Owl",
        "start_minute": 23 * 60,
        "end_minute": 7 * 60,
        "days_mask": ALL_DAYS_MASK,
    },
    "work_hours": {
        "name": "Work Hours",
        "start_minute": 9 * 60,
        "end_minute": 17 * 60,
        "days_mask": WEEKDAY_MASK,
    },
    "school_hours": {
        "name": "School Hours",
        "start_minute": 8 * 60,
        "end_minute": 15 * 60,
        "days_mask": WEEKDAY_MASK,
    },
    "weekend_detox": {
        "name": "Weekend Detox",
        "start_minute": 0,
        "end_minute": 23 * 60 + 59,
        "days_mask": WEEKEND_MASK,
    },
}


# ---------------------------------------------------------------------------
# Slow mode (per-user global friction state)
# ---------------------------------------------------------------------------

@dataclass
class WellnessSlowMode:
    guild_id: int
    user_id: int
    triggered_by_cap_id: int
    triggered_window_start: int
    last_message_ts: float
    active_until_ts: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessSlowMode":
        return cls(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            triggered_by_cap_id=int(row["triggered_by_cap_id"]),
            triggered_window_start=int(row["triggered_window_start"]),
            last_message_ts=float(row["last_message_ts"]),
            active_until_ts=float(row["active_until_ts"]),
        )


def get_slow_mode(
    conn: sqlite3.Connection, guild_id: int, user_id: int,
) -> WellnessSlowMode | None:
    row = conn.execute(
        "SELECT * FROM wellness_slow_mode WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return WellnessSlowMode.from_row(row) if row else None


def arm_slow_mode(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    triggered_by_cap_id: int,
    triggered_window_start: int,
    active_until_ts: float,
) -> None:
    conn.execute(
        """
        INSERT INTO wellness_slow_mode
            (guild_id, user_id, triggered_by_cap_id, triggered_window_start, last_message_ts, active_until_ts)
        VALUES (?, ?, ?, ?, 0, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            triggered_by_cap_id    = excluded.triggered_by_cap_id,
            triggered_window_start = excluded.triggered_window_start,
            active_until_ts        = MAX(wellness_slow_mode.active_until_ts, excluded.active_until_ts)
        """,
        (guild_id, user_id, triggered_by_cap_id, triggered_window_start, active_until_ts),
    )


def update_slow_mode_last_message(
    conn: sqlite3.Connection, guild_id: int, user_id: int, ts: float,
) -> None:
    conn.execute(
        "UPDATE wellness_slow_mode SET last_message_ts = ? WHERE guild_id = ? AND user_id = ?",
        (ts, guild_id, user_id),
    )


def lift_slow_mode(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    conn.execute(
        "DELETE FROM wellness_slow_mode WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def list_expired_slow_mode(conn: sqlite3.Connection, now: float) -> list[WellnessSlowMode]:
    rows = conn.execute(
        "SELECT * FROM wellness_slow_mode WHERE active_until_ts > 0 AND active_until_ts <= ?",
        (now,),
    ).fetchall()
    return [WellnessSlowMode.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Streaks
# ---------------------------------------------------------------------------

@dataclass
class WellnessStreak:
    guild_id: int
    user_id: int
    current_days: int
    personal_best: int
    streak_start_date: str | None
    last_violation_date: str | None
    current_badge: str
    celebrated_badge: str
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessStreak":
        return cls(
            guild_id=int(row["guild_id"]),
            user_id=int(row["user_id"]),
            current_days=int(row["current_days"]),
            personal_best=int(row["personal_best"]),
            streak_start_date=row["streak_start_date"],
            last_violation_date=row["last_violation_date"],
            current_badge=str(row["current_badge"]),
            celebrated_badge=str(row["celebrated_badge"] if "celebrated_badge" in row.keys() else ""),
            updated_at=float(row["updated_at"]),
        )


def get_streak(conn: sqlite3.Connection, guild_id: int, user_id: int) -> WellnessStreak | None:
    row = conn.execute(
        "SELECT * FROM wellness_streaks WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    return WellnessStreak.from_row(row) if row else None


def ensure_streak(
    conn: sqlite3.Connection, guild_id: int, user_id: int, today_iso: str,
) -> WellnessStreak:
    existing = get_streak(conn, guild_id, user_id)
    if existing is not None:
        return existing
    conn.execute(
        """
        INSERT INTO wellness_streaks
            (guild_id, user_id, current_days, personal_best, streak_start_date, current_badge, updated_at)
        VALUES (?, ?, 0, 0, ?, '🌱', ?)
        """,
        (guild_id, user_id, today_iso, time.time()),
    )
    streak = get_streak(conn, guild_id, user_id)
    assert streak is not None
    return streak


def badge_for_days(days: int) -> str:
    badge = "🌱"
    for threshold, emoji in MILESTONES:
        if days >= threshold:
            badge = emoji
        else:
            break
    return badge


def decay_streak(streak_days: int) -> int:
    """Apply spec §5 decay rule: lose 10% rounded up, floor at 1."""
    if streak_days <= 1:
        return max(1, streak_days)
    loss = max(1, math.ceil(streak_days * 0.10))
    return max(1, streak_days - loss)


def apply_streak_violation(
    conn: sqlite3.Connection, guild_id: int, user_id: int, today_iso: str,
) -> tuple[int, int]:
    """Apply a violation to the user's streak. Returns (old_days, new_days).

    Same-day violations no-op after the first.
    """
    streak = ensure_streak(conn, guild_id, user_id, today_iso)
    if streak.last_violation_date == today_iso:
        return streak.current_days, streak.current_days
    new_days = decay_streak(streak.current_days)
    new_badge = badge_for_days(new_days)
    conn.execute(
        """
        UPDATE wellness_streaks
           SET current_days = ?,
               last_violation_date = ?,
               current_badge = ?,
               updated_at = ?
         WHERE guild_id = ? AND user_id = ?
        """,
        (new_days, today_iso, new_badge, time.time(), guild_id, user_id),
    )
    return streak.current_days, new_days


def increment_streak_day(
    conn: sqlite3.Connection, guild_id: int, user_id: int, today_iso: str,
) -> tuple[int, str, bool]:
    """Mark today as a clean day for the user. Returns (new_days, new_badge, badge_upgraded)."""
    streak = ensure_streak(conn, guild_id, user_id, today_iso)
    new_days = streak.current_days + 1
    new_badge = badge_for_days(new_days)
    badge_upgraded = new_badge != streak.current_badge
    new_pb = max(streak.personal_best, new_days)
    conn.execute(
        """
        UPDATE wellness_streaks
           SET current_days = ?,
               personal_best = ?,
               current_badge = ?,
               updated_at = ?
         WHERE guild_id = ? AND user_id = ?
        """,
        (new_days, new_pb, new_badge, time.time(), guild_id, user_id),
    )
    conn.execute(
        """
        INSERT INTO wellness_streak_history (guild_id, user_id, day, streak_days)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id, day) DO UPDATE SET streak_days = excluded.streak_days
        """,
        (guild_id, user_id, today_iso, new_days),
    )
    return new_days, new_badge, badge_upgraded


def next_milestone(days: int) -> tuple[int, str] | None:
    """Return (threshold, badge) for the next milestone above `days`, or None at max."""
    for threshold, emoji in MILESTONES:
        if threshold > days:
            return threshold, emoji
    return None


def list_committed_users_with_streaks(
    conn: sqlite3.Connection, guild_id: int,
) -> list[tuple[int, WellnessStreak]]:
    """Return (user_id, streak) pairs for opted-in users with public_commitment=True.

    Sorted by current streak descending, then user_id for stability.
    """
    rows = conn.execute(
        """
        SELECT s.*
          FROM wellness_streaks s
          JOIN wellness_users u
            ON u.guild_id = s.guild_id AND u.user_id = s.user_id
         WHERE s.guild_id = ?
           AND u.public_commitment = 1
           AND u.opted_in_at IS NOT NULL
           AND u.opted_out_at IS NULL
         ORDER BY s.current_days DESC, s.user_id ASC
        """,
        (guild_id,),
    ).fetchall()
    return [(int(r["user_id"]), WellnessStreak.from_row(r)) for r in rows]


def list_uncelebrated_milestones(
    conn: sqlite3.Connection, guild_id: int,
) -> list[tuple[int, WellnessStreak]]:
    """Return users whose current_badge differs from celebrated_badge (i.e. a
    badge change has not yet been announced). Only wellness users who are
    opted-in and public_commitment=True."""
    rows = conn.execute(
        """
        SELECT s.*
          FROM wellness_streaks s
          JOIN wellness_users u
            ON u.guild_id = s.guild_id AND u.user_id = s.user_id
         WHERE s.guild_id = ?
           AND u.public_commitment = 1
           AND u.opted_in_at IS NOT NULL
           AND u.opted_out_at IS NULL
           AND s.current_badge <> s.celebrated_badge
        """,
        (guild_id,),
    ).fetchall()
    return [(int(r["user_id"]), WellnessStreak.from_row(r)) for r in rows]


def mark_badge_celebrated(
    conn: sqlite3.Connection, guild_id: int, user_id: int, badge: str,
) -> None:
    conn.execute(
        "UPDATE wellness_streaks SET celebrated_badge = ? WHERE guild_id = ? AND user_id = ?",
        (badge, guild_id, user_id),
    )


def has_clean_day_credit(
    conn: sqlite3.Connection, guild_id: int, user_id: int, day_iso: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM wellness_streak_history WHERE guild_id = ? AND user_id = ? AND day = ?",
        (guild_id, user_id, day_iso),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Exempt channels
# ---------------------------------------------------------------------------

def add_exempt_channel(
    conn: sqlite3.Connection, guild_id: int, channel_id: int, label: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO wellness_exempt_channels (guild_id, channel_id, label, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET label = excluded.label
        """,
        (guild_id, channel_id, label, time.time()),
    )


def remove_exempt_channel(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM wellness_exempt_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return (cur.rowcount or 0) > 0


def list_exempt_channels(conn: sqlite3.Connection, guild_id: int) -> list[tuple[int, str]]:
    rows = conn.execute(
        "SELECT channel_id, label FROM wellness_exempt_channels WHERE guild_id = ? ORDER BY channel_id",
        (guild_id,),
    ).fetchall()
    return [(int(r["channel_id"]), str(r["label"])) for r in rows]


def is_channel_exempt(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM wellness_exempt_channels WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Partners
# ---------------------------------------------------------------------------

def _ordered_pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a < b else (b, a)


@dataclass
class WellnessPartner:
    id: int
    guild_id: int
    user_a: int
    user_b: int
    requester_id: int
    status: str
    created_at: float
    accepted_at: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "WellnessPartner":
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            user_a=int(row["user_a"]),
            user_b=int(row["user_b"]),
            requester_id=int(row["requester_id"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            accepted_at=row["accepted_at"],
        )

    def other(self, user_id: int) -> int:
        return self.user_b if user_id == self.user_a else self.user_a


def create_partner_request(
    conn: sqlite3.Connection, guild_id: int, requester_id: int, target_id: int,
) -> WellnessPartner | None:
    """Create a pending partner request. Returns None if a request already exists."""
    if requester_id == target_id:
        return None
    a, b = _ordered_pair(requester_id, target_id)
    existing = conn.execute(
        "SELECT * FROM wellness_partners WHERE guild_id = ? AND user_a = ? AND user_b = ?",
        (guild_id, a, b),
    ).fetchone()
    if existing:
        return None
    conn.execute(
        """
        INSERT INTO wellness_partners (guild_id, user_a, user_b, requester_id, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (guild_id, a, b, requester_id, time.time()),
    )
    row = conn.execute(
        "SELECT * FROM wellness_partners WHERE guild_id = ? AND user_a = ? AND user_b = ?",
        (guild_id, a, b),
    ).fetchone()
    return WellnessPartner.from_row(row) if row else None


def accept_partner_request(conn: sqlite3.Connection, partner_id: int) -> bool:
    cur = conn.execute(
        "UPDATE wellness_partners SET status = 'accepted', accepted_at = ? WHERE id = ? AND status = 'pending'",
        (time.time(), partner_id),
    )
    return (cur.rowcount or 0) > 0


def dissolve_partnership(conn: sqlite3.Connection, partner_id: int) -> bool:
    cur = conn.execute("DELETE FROM wellness_partners WHERE id = ?", (partner_id,))
    return (cur.rowcount or 0) > 0


def get_partnership(conn: sqlite3.Connection, partner_id: int) -> WellnessPartner | None:
    row = conn.execute("SELECT * FROM wellness_partners WHERE id = ?", (partner_id,)).fetchone()
    return WellnessPartner.from_row(row) if row else None


def list_partnerships(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, accepted_only: bool = True,
) -> list[WellnessPartner]:
    query = (
        "SELECT * FROM wellness_partners WHERE guild_id = ? AND (user_a = ? OR user_b = ?)"
    )
    params: list = [guild_id, user_id, user_id]
    if accepted_only:
        query += " AND status = 'accepted'"
    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()
    return [WellnessPartner.from_row(r) for r in rows]


def remove_user_partnerships(conn: sqlite3.Connection, guild_id: int, user_id: int) -> int:
    cur = conn.execute(
        "DELETE FROM wellness_partners WHERE guild_id = ? AND (user_a = ? OR user_b = ?)",
        (guild_id, user_id, user_id),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# Away rate limit
# ---------------------------------------------------------------------------

def can_send_away(
    conn: sqlite3.Connection, guild_id: int, user_id: int, channel_id: int, now: float,
) -> bool:
    row = conn.execute(
        "SELECT last_sent_at FROM wellness_away_rate_limit WHERE guild_id = ? AND user_id = ? AND channel_id = ?",
        (guild_id, user_id, channel_id),
    ).fetchone()
    if not row:
        return True
    return (now - float(row["last_sent_at"])) >= AWAY_RATE_LIMIT_SECONDS


def record_away_sent(
    conn: sqlite3.Connection, guild_id: int, user_id: int, channel_id: int, now: float,
) -> None:
    conn.execute(
        """
        INSERT INTO wellness_away_rate_limit (guild_id, user_id, channel_id, last_sent_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id, channel_id) DO UPDATE SET last_sent_at = excluded.last_sent_at
        """,
        (guild_id, user_id, channel_id, now),
    )


def update_away_message(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, enabled: bool, message: str | None = None,
) -> None:
    if message is None:
        conn.execute(
            "UPDATE wellness_users SET away_enabled = ? WHERE guild_id = ? AND user_id = ?",
            (1 if enabled else 0, guild_id, user_id),
        )
    else:
        conn.execute(
            "UPDATE wellness_users SET away_enabled = ?, away_message = ? WHERE guild_id = ? AND user_id = ?",
            (1 if enabled else 0, message, guild_id, user_id),
        )


# ---------------------------------------------------------------------------
# Weekly reports
# ---------------------------------------------------------------------------

def has_weekly_report(
    conn: sqlite3.Connection, guild_id: int, user_id: int, iso_year: int, iso_week: int,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM wellness_weekly_reports WHERE guild_id = ? AND user_id = ? AND iso_year = ? AND iso_week = ?",
        (guild_id, user_id, iso_year, iso_week),
    ).fetchone()
    return row is not None


def compute_weekly_summary(
    conn: sqlite3.Connection, guild_id: int, user_id: int, week_start: date,
) -> dict:
    """Build a structured summary of the user's last 7 days of wellness state.

    Returns a dict with:
        clean_days     — count of days in [week_start, week_start+6] with a streak_history row
        violation_days — 1 if last_violation_date falls in the week, else 0 (proxy)
        compliance_pct — clean_days/7 * 100, rounded
        current_days   — end-of-week streak
        personal_best  — all-time PB
        is_personal_best — whether user is currently at their PB (and PB ≥ 1)
        badge          — current badge
    """
    end = week_start + timedelta(days=6)
    week_iso = [str((week_start + timedelta(days=i))) for i in range(7)]
    placeholders = ",".join("?" * len(week_iso))
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n
          FROM wellness_streak_history
         WHERE guild_id = ? AND user_id = ? AND day IN ({placeholders})
        """,
        (guild_id, user_id, *week_iso),
    ).fetchone()
    clean_days = int(row["n"] if row else 0)

    streak_row = conn.execute(
        "SELECT current_days, personal_best, current_badge, last_violation_date FROM wellness_streaks WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    current_days = int(streak_row["current_days"]) if streak_row else 0
    personal_best = int(streak_row["personal_best"]) if streak_row else 0
    badge = str(streak_row["current_badge"]) if streak_row else "🌱"
    last_violation = streak_row["last_violation_date"] if streak_row else None

    violation_days = 0
    if last_violation and last_violation in week_iso:
        violation_days = 1

    compliance_pct = round((clean_days / 7) * 100)
    is_pb = current_days >= personal_best and personal_best >= 1

    return {
        "clean_days": clean_days,
        "violation_days": violation_days,
        "compliance_pct": compliance_pct,
        "current_days": current_days,
        "personal_best": personal_best,
        "is_personal_best": is_pb,
        "badge": badge,
        "week_start": week_start.isoformat(),
        "week_end": end.isoformat(),
    }


def insert_weekly_report(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    iso_year: int,
    iso_week: int,
    week_start: str,
    report_json: str,
    ai_text: str,
) -> bool:
    """Returns True if newly inserted (False on dup conflict)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO wellness_weekly_reports
            (guild_id, user_id, iso_year, iso_week, week_start, report_json, ai_text, sent_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (guild_id, user_id, iso_year, iso_week, week_start, report_json, ai_text, time.time()),
    )
    return (cur.rowcount or 0) > 0


def list_weekly_reports(
    conn: sqlite3.Connection, guild_id: int, user_id: int, limit: int = 12,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT iso_year, iso_week, week_start, report_json, ai_text, sent_at
          FROM wellness_weekly_reports
         WHERE guild_id = ? AND user_id = ?
         ORDER BY iso_year DESC, iso_week DESC
         LIMIT ?
        """,
        (guild_id, user_id, limit),
    ).fetchall()
