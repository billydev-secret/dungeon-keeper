"""Inactive Report — one member list over last-activity data.

Replaces the four separate member-list reports (Inactive, Inactive Role,
List Role, Oldest SFW): a single scope (everyone / holders of a role /
members lacking a role) is filtered by an optional idle threshold and
sorted oldest-activity-first. The route gathers live guild members and
the activity map; everything here is plain filtering so it stays
testable without Discord objects.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from bot_modules.core.xp_system import MemberActivity
from bot_modules.services import xp_rollup_service

DEFAULT_LIMIT = 500


@dataclass(frozen=True)
class MemberScope:
    """The slice of a live guild member the report needs."""

    user_id: int
    display_name: str
    is_bot: bool = False
    role_ids: tuple[int, ...] = field(default_factory=tuple)


def scope_members(
    members: list[MemberScope],
    *,
    role_id: int | None = None,
    role_mode: str = "with",
) -> list[MemberScope]:
    """Drop bots, then scope to holders ("with") or non-holders ("without") of a role."""
    scoped = [m for m in members if not m.is_bot]
    if role_id:
        if role_mode == "without":
            scoped = [m for m in scoped if role_id not in m.role_ids]
        else:
            scoped = [m for m in scoped if role_id in m.role_ids]
    return scoped


def build_inactive_report(
    scoped: list[MemberScope],
    activities: dict[int, MemberActivity],
    *,
    now_ts: float,
    days: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    """Rows for scoped members idle at least *days* (0 = everyone), oldest first.

    A member with no tracked activity counts as inactive at any threshold —
    "never tracked" must not hide someone from an inactivity sweep.
    """
    cutoff = now_ts - days * 86400 if days > 0 else None
    rows = []
    for m in scoped:
        a = activities.get(m.user_id)
        last_ts = a.created_at if a else None
        if cutoff is not None and last_ts is not None and last_ts >= cutoff:
            continue
        rows.append(
            {
                "user_id": str(m.user_id),
                "display_name": m.display_name,
                "last_message_ts": last_ts,
                "last_message_channel_id": str(a.channel_id) if a else None,
                "days_since_last": (
                    round((now_ts - last_ts) / 86400.0, 1) if last_ts else None
                ),
            }
        )
    rows.sort(key=lambda r: r["last_message_ts"] or 0)
    return {
        "total_scoped": len(scoped),
        "tracking_coverage": sum(1 for m in scoped if m.user_id in activities),
        "total": len(rows),
        "members": rows[:limit],
    }


def channel_activity_map(
    conn: sqlite3.Connection,
    guild_id: int,
    member_ids: list[int],
    channel_id: int,
) -> dict[int, MemberActivity]:
    """Last activity per member measured inside one channel (xp_events)."""
    if not member_ids:
        return {}
    act_map: dict[int, MemberActivity] = {}
    batch_size = 800
    for i in range(0, len(member_ids), batch_size):
        batch = member_ids[i : i + batch_size]
        placeholders = ",".join("?" for _ in batch)
        # xp_events has no message_id column (the old /inactive route selected
        # one and broke whenever a channel filter was applied) — report 0.
        #
        # Unions xp_daily's last_at. This reader is the one most opposed to
        # retention: its whole job is reporting activity that happened long
        # ago, so reading raw alone would make a member last seen beyond the
        # boundary look like they were never here at all (see
        # docs/plans/xp-events-retention-and-rollup.md).
        boundary = xp_rollup_service.read_boundary(conn)
        if boundary is None:
            rows = conn.execute(
                f"""
                SELECT user_id, channel_id, MAX(created_at) AS created_at
                FROM xp_events
                WHERE guild_id = ? AND channel_id = ? AND user_id IN ({placeholders})
                GROUP BY user_id
                """,
                [guild_id, channel_id, *batch],
            ).fetchall()
        else:
            boundary_day, boundary_ts = boundary
            rows = conn.execute(
                f"""
                SELECT user_id, channel_id, MAX(created_at) AS created_at FROM (
                    SELECT user_id, channel_id, created_at
                      FROM xp_events
                     WHERE guild_id = ? AND channel_id = ? AND created_at >= ?
                       AND user_id IN ({placeholders})
                    UNION ALL
                    SELECT user_id, channel_id, last_at AS created_at
                      FROM xp_daily
                     WHERE guild_id = ? AND channel_id = ? AND day < ?
                       AND user_id IN ({placeholders})
                )
                GROUP BY user_id
                """,
                [guild_id, channel_id, boundary_ts, *batch,
                 guild_id, channel_id, boundary_day, *batch],
            ).fetchall()
        for row in rows:
            uid = int(row["user_id"])
            act_map[uid] = MemberActivity(
                user_id=uid,
                channel_id=int(row["channel_id"]),
                message_id=0,
                created_at=float(row["created_at"]),
            )
    return act_map
