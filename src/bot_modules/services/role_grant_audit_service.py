"""Grant-audit service — durable prune ledger + bucketing for the dashboard panel.

The inactivity-prune loop removes a configured role from long-inactive
members with no hold row anywhere; ``role_prune_events`` is its durable "why"
record. The Grant Audit panel reads that ledger and splits members missing a
grant role into three buckets:

- **waiting for first grant** — leveled up, never granted, never pruned;
- **stripped but came back** — pruned, active again, never re-granted;
- **recent inactive stripped** — pruned and still inactive (newest first).

``restored_at`` is set the moment a mod re-grants (a discrete fact worth
storing); "is this member active again" stays a live computation against
``get_member_last_activity_map`` since it's inherently a moving target.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import discord


class _HasCreatedAt(Protocol):
    @property
    def created_at(self) -> float: ...


# ---------------------------------------------------------------------------
# Ledger writes / reads
# ---------------------------------------------------------------------------


def record_prune_events(
    conn: sqlite3.Connection,
    guild_id: int,
    user_ids: Iterable[int],
    role_id: int,
    pruned_at: float,
    source: str = "inactivity_prune",
) -> int:
    """Record one open prune event per user; returns rows inserted."""
    rows = [(guild_id, uid, role_id, source, pruned_at) for uid in user_ids]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO role_prune_events (guild_id, user_id, role_id, source, pruned_at) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def mark_restored(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    role_id: int,
    restored_at: float,
) -> int:
    """Close any open prune events for (user, role); returns rows closed."""
    cursor = conn.execute(
        "UPDATE role_prune_events SET restored_at = ? "
        "WHERE guild_id = ? AND user_id = ? AND role_id = ? AND restored_at IS NULL",
        (restored_at, guild_id, user_id, role_id),
    )
    return cursor.rowcount


def get_open_prune_events(
    conn: sqlite3.Connection, guild_id: int, role_id: int
) -> list[sqlite3.Row]:
    """Open (unrestored) prune events, one row per user with the latest pruned_at."""
    return conn.execute(
        """
        SELECT user_id, MAX(pruned_at) AS pruned_at
        FROM role_prune_events
        WHERE guild_id = ? AND role_id = ? AND restored_at IS NULL
        GROUP BY user_id
        """,
        (guild_id, role_id),
    ).fetchall()


def get_ever_pruned_ids(
    conn: sqlite3.Connection, guild_id: int, role_id: int
) -> set[int]:
    """Every user with any prune event for this role, open or restored."""
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM role_prune_events WHERE guild_id = ? AND role_id = ?",
        (guild_id, role_id),
    ).fetchall()
    return {int(r["user_id"]) for r in rows}


def get_hold_excluded_ids(
    conn: sqlite3.Connection, guild_id: int
) -> tuple[set[int], set[int]]:
    """DB-side hold exclusions + configured hold role ids for the live check.

    Members on an active inactive-channel hold or in jail had every role
    stripped on purpose — they must never appear in any audit bucket. The
    returned hold role ids let the caller also exclude members who hold the
    Inactive/Jailed role live in Discord without a matching DB row (a mod who
    stripped roles by hand).
    """
    from bot_modules.core.db_utils import get_config_value
    from bot_modules.inactive.store import active_inactive_user_ids
    from bot_modules.services.moderation import active_jailed_user_ids

    held_ids = active_inactive_user_ids(conn, guild_id) | active_jailed_user_ids(
        conn, guild_id
    )
    hold_role_ids = {
        rid
        for rid in (
            int(get_config_value(conn, "inactive_role_id", "0", guild_id) or "0"),
            int(get_config_value(conn, "jailed_role_id", "0", guild_id) or "0"),
        )
        if rid > 0
    }
    return held_ids, hold_role_ids


# ---------------------------------------------------------------------------
# Bucketing (pure)
# ---------------------------------------------------------------------------


def compute_waiting_for_first_grant(
    levels: dict[int, int],
    granted_ids: set[int],
    ever_pruned_ids: set[int],
) -> list[tuple[int, int]]:
    """``(user_id, level)`` pairs at/above the level bar with no grant and no
    prune history at all — the role was plain never given. Highest level first."""
    out = [
        (uid, lvl)
        for uid, lvl in levels.items()
        if uid not in granted_ids and uid not in ever_pruned_ids
    ]
    out.sort(key=lambda p: -p[1])
    return out


def _open_event_pairs(open_events: Iterable) -> list[tuple[int, float]]:
    return [(int(ev["user_id"]), float(ev["pruned_at"])) for ev in open_events]


def compute_stripped_returned(
    open_events: Iterable,
    granted_ids: set[int],
    activity_map: Mapping[int, _HasCreatedAt],
    cutoff_ts: float,
) -> list[dict]:
    """Open prune event, still not re-granted, but active again (at/after the
    cutoff) — pruned fairly, came back, and nobody closed the loop."""
    out = [
        {"user_id": uid, "pruned_at": pruned_at}
        for uid, pruned_at in _open_event_pairs(open_events)
        if uid not in granted_ids
        and (a := activity_map.get(uid)) is not None
        and a.created_at >= cutoff_ts
    ]
    out.sort(key=lambda r: -r["pruned_at"])
    return out


def compute_recent_inactive(
    open_events: Iterable,
    granted_ids: set[int],
    activity_map: Mapping[int, _HasCreatedAt],
    cutoff_ts: float,
    limit: int = 10,
) -> list[dict]:
    """Open prune event and still inactive (no activity, or all before the
    cutoff) — the prune is working as intended. Newest prunes first, capped."""
    out = [
        {"user_id": uid, "pruned_at": pruned_at}
        for uid, pruned_at in _open_event_pairs(open_events)
        if uid not in granted_ids
        and ((a := activity_map.get(uid)) is None or a.created_at < cutoff_ts)
    ]
    out.sort(key=lambda r: -r["pruned_at"])
    return out[:limit]


# ---------------------------------------------------------------------------
# One-off backfill from role_events history
# ---------------------------------------------------------------------------


def backfill_prune_events_from_role_events(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    role: discord.Role,
    inactivity_days: int,
    *,
    now: float | None = None,
) -> int:
    """Seed ``role_prune_events`` from historical ``role_events`` removals.

    Idempotent: users who already have any prune event for this role are
    skipped, so running it twice inserts nothing new. A removal only counts
    as a prune if the member's last activity doesn't disprove it — activity
    inside the prune window at removal time, or no activity record at all,
    means the prune loop can't have done it (it never strips without an
    activity record older than the window). Members who hold the role again
    are inserted already-restored so they don't reopen the audit.

    Needs live Discord state (current role membership), so it's called once
    from a REPL/manage path, not a migration step.
    """
    from bot_modules.core.xp_system import get_member_last_activity_map

    now_ts = now if now is not None else time.time()
    guild_id = guild.id
    rows = conn.execute(
        "SELECT user_id, MAX(granted_at) AS removed_at FROM role_events "
        "WHERE guild_id = ? AND role_name = ? AND action = 'remove' GROUP BY user_id",
        (guild_id, role.name),
    ).fetchall()
    if not rows:
        return 0

    already_recorded = {
        int(r["user_id"])
        for r in conn.execute(
            "SELECT DISTINCT user_id FROM role_prune_events "
            "WHERE guild_id = ? AND role_id = ?",
            (guild_id, role.id),
        ).fetchall()
    }
    candidates = [
        (int(r["user_id"]), float(r["removed_at"]))
        for r in rows
        if int(r["user_id"]) not in already_recorded
    ]
    if not candidates:
        return 0

    current_holder_ids = {m.id for m in role.members}
    activity_map = get_member_last_activity_map(
        conn, guild_id, [uid for uid, _ in candidates]
    )
    window_secs = inactivity_days * 86400
    inserted = 0
    for uid, removed_at in candidates:
        activity = activity_map.get(uid)
        if activity is None:
            continue
        if (
            activity.created_at < removed_at
            and removed_at - activity.created_at < window_secs
        ):
            # Active inside the window when removed — a mod removal, not a prune.
            continue
        restored_at = now_ts if uid in current_holder_ids else None
        conn.execute(
            "INSERT INTO role_prune_events "
            "(guild_id, user_id, role_id, source, pruned_at, restored_at) "
            "VALUES (?, ?, ?, 'inactivity_prune', ?, ?)",
            (guild_id, uid, role.id, removed_at, restored_at),
        )
        inserted += 1
    return inserted
