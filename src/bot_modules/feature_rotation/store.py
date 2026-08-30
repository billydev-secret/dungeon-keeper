"""Database access for the daily feature-channel rotation.

Split from ``services/feature_rotation_service`` so the economy's quest board
can ask which rooms are open today without importing ``discord`` — the board
runs inside a synchronous DB read on a request thread, and dragging the
Discord client into that import graph for two integer lookups would be a poor
trade. Same split, and the same reason, as ``hidden_channels/store.py``.

Everything here is synchronous ``sqlite3``; async callers wrap it in
``asyncio.to_thread``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from bot_modules.feature_rotation.logic import (
    Room,
    RotationDay,
    format_kinds,
    local_day,
    parse_kinds,
    resolve_day,
)

log = logging.getLogger("dungeonkeeper.feature_rotation")


@dataclass(frozen=True)
class RotationConfig:
    guild_id: int
    enabled: bool = False
    announce_channel_id: int = 0
    announce_hour: int = 9
    tz_offset_hours: int = -7
    rooms_per_day: int = 1
    last_flip_date: str = ""
    last_announce_date: str = ""


# ── config ───────────────────────────────────────────────────────────────────


def get_config(conn: sqlite3.Connection, guild_id: int) -> RotationConfig:
    """The guild's rotation settings, defaulted when it has no row yet."""
    row = conn.execute(
        "SELECT * FROM feature_rotation_config WHERE guild_id = ?", (guild_id,)
    ).fetchone()
    if row is None:
        return RotationConfig(guild_id=guild_id)
    return RotationConfig(
        guild_id=guild_id,
        enabled=bool(row["enabled"]),
        announce_channel_id=int(row["announce_channel_id"]),
        announce_hour=int(row["announce_hour"]),
        tz_offset_hours=int(row["tz_offset_hours"]),
        rooms_per_day=int(row["rooms_per_day"]),
        last_flip_date=str(row["last_flip_date"]),
        last_announce_date=str(row["last_announce_date"]),
    )


def save_config(conn: sqlite3.Connection, cfg: RotationConfig) -> None:
    """Upsert the settings, leaving the two claimed dates untouched.

    The dates are the loop's exactly-once guards; an admin pressing Save must
    not be able to re-trigger today's flip or re-post today's announcement.
    """
    conn.execute(
        """
        INSERT INTO feature_rotation_config
            (guild_id, enabled, announce_channel_id, announce_hour,
             tz_offset_hours, rooms_per_day)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            enabled = excluded.enabled,
            announce_channel_id = excluded.announce_channel_id,
            announce_hour = excluded.announce_hour,
            tz_offset_hours = excluded.tz_offset_hours,
            rooms_per_day = excluded.rooms_per_day
        """,
        (
            cfg.guild_id,
            int(cfg.enabled),
            cfg.announce_channel_id,
            cfg.announce_hour,
            cfg.tz_offset_hours,
            max(1, cfg.rooms_per_day),
        ),
    )


# ── pool ─────────────────────────────────────────────────────────────────────


def _row_to_room(row: sqlite3.Row) -> Room:
    return Room(
        channel_id=int(row["channel_id"]),
        position=int(row["position"]),
        label=str(row["label"]),
        blurb=str(row["blurb"]),
        in_rotation=bool(row["in_rotation"]),
        hide_when_off=bool(row["hide_when_off"]),
        announce=bool(row["announce"]),
        quest_kinds=parse_kinds(str(row["quest_kinds"])),
        blocked_kinds=parse_kinds(str(row["blocked_kinds"])),
    )


def list_pool(conn: sqlite3.Connection, guild_id: int) -> list[Room]:
    """Every pool row for the guild, including ones not in rotation."""
    return [
        _row_to_room(r)
        for r in conn.execute(
            "SELECT * FROM feature_rotation_pool WHERE guild_id = ? "
            "ORDER BY position, channel_id",
            (guild_id,),
        )
    ]


def list_pool_state(conn: sqlite3.Connection, guild_id: int) -> dict[int, bool]:
    """``channel_id -> currently hidden by the rotation``."""
    return {
        int(r["channel_id"]): r["hidden_at"] is not None
        for r in conn.execute(
            "SELECT channel_id, hidden_at FROM feature_rotation_pool WHERE guild_id = ?",
            (guild_id,),
        )
    }


def upsert_room(conn: sqlite3.Connection, guild_id: int, room: Room) -> None:
    """Add or update one pool row, never touching its hidden-state columns."""
    conn.execute(
        """
        INSERT INTO feature_rotation_pool
            (guild_id, channel_id, position, label, blurb, in_rotation,
             hide_when_off, announce, quest_kinds, blocked_kinds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id) DO UPDATE SET
            position = excluded.position,
            label = excluded.label,
            blurb = excluded.blurb,
            in_rotation = excluded.in_rotation,
            hide_when_off = excluded.hide_when_off,
            announce = excluded.announce,
            quest_kinds = excluded.quest_kinds,
            blocked_kinds = excluded.blocked_kinds
        """,
        (
            guild_id,
            room.channel_id,
            room.position,
            room.label,
            room.blurb,
            int(room.in_rotation),
            int(room.hide_when_off),
            int(room.announce),
            format_kinds(room.quest_kinds),
            format_kinds(room.blocked_kinds),
        ),
    )


def get_room_snapshot(
    conn: sqlite3.Connection, guild_id: int, channel_id: int
) -> tuple[list[dict], bool]:
    """``(stored overwrites, is currently hidden)`` for one pool row."""
    row = conn.execute(
        "SELECT stored_overwrites, hidden_at FROM feature_rotation_pool "
        "WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    ).fetchone()
    if row is None:
        return [], False
    raw = str(row["stored_overwrites"] or "")
    try:
        stored = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        log.warning("Rotation: unreadable overwrite snapshot for channel %s", channel_id)
        stored = []
    return stored, row["hidden_at"] is not None


def mark_hidden(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    stored: list[dict],
    now: float,
) -> None:
    """Record the pre-hide overwrites. Written BEFORE the Discord edit.

    Losing this snapshot means losing the channel's real permissions, so it is
    persisted first and the edit rolled back if Discord refuses — the same
    ordering ``hidden_channels_cog.hide`` uses, for the same reason.
    """
    conn.execute(
        "UPDATE feature_rotation_pool SET stored_overwrites = ?, hidden_at = ? "
        "WHERE guild_id = ? AND channel_id = ?",
        (json.dumps(stored), now, guild_id, channel_id),
    )


def mark_visible(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> None:
    """Clear the hidden state after a successful restore."""
    conn.execute(
        "UPDATE feature_rotation_pool SET stored_overwrites = '', hidden_at = NULL "
        "WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )


def delete_room(conn: sqlite3.Connection, guild_id: int, channel_id: int) -> bool:
    """Remove a channel from the pool. Callers restore visibility first."""
    cur = conn.execute(
        "DELETE FROM feature_rotation_pool WHERE guild_id = ? AND channel_id = ?",
        (guild_id, channel_id),
    )
    return cur.rowcount > 0


# ── exactly-once day claims ──────────────────────────────────────────────────


def _claim(
    conn: sqlite3.Connection, guild_id: int, column: str, day: str
) -> bool:
    """Atomically claim ``day`` for one action; False means someone else won.

    ``column`` is a literal chosen by this module, never caller input. The
    conditional UPDATE is the whole guard: two loop passes (or two processes)
    racing the same midnight can't both flip, and a restart mid-day can't
    re-run an action already claimed.
    """
    conn.execute(
        "INSERT OR IGNORE INTO feature_rotation_config (guild_id) VALUES (?)",
        (guild_id,),
    )
    cur = conn.execute(
        f"UPDATE feature_rotation_config SET {column} = ? "  # noqa: S608 - literal
        f"WHERE guild_id = ? AND {column} < ?",
        (day, guild_id, day),
    )
    return cur.rowcount > 0


def claim_flip(conn: sqlite3.Connection, guild_id: int, day: str) -> bool:
    return _claim(conn, guild_id, "last_flip_date", day)


def claim_announce(conn: sqlite3.Connection, guild_id: int, day: str) -> bool:
    return _claim(conn, guild_id, "last_announce_date", day)


# ── what other features ask us ───────────────────────────────────────────────


def rotation_day_for(
    conn: sqlite3.Connection, guild_id: int, local_day_str: str
) -> RotationDay | None:
    """The derived rotation for one named local day, or ``None`` when it's off.

    Takes the day as a string rather than a clock reading so the quest board
    can ask about *its* day. The board's period and the rotation's day have to
    be the same string or the featured room and the featured quest describe
    different days; passing it in makes that impossible to get wrong.

    ``None`` must mean "behave exactly as before", so a guild that never
    enables the rotation sees byte-identical boards.
    """
    cfg = get_config(conn, guild_id)
    if not cfg.enabled:
        return None
    rooms = list_pool(conn, guild_id)
    if not rooms:
        return None
    protected = {cfg.announce_channel_id} if cfg.announce_channel_id else set()
    return resolve_day(
        rooms,
        local_day_str=local_day_str,
        rooms_per_day=cfg.rooms_per_day,
        protected=protected,
    )


def rotation_day(
    conn: sqlite3.Connection, guild_id: int, now: float
) -> RotationDay | None:
    """Today's derived rotation for a guild, from a clock reading."""
    cfg = get_config(conn, guild_id)
    if not cfg.enabled:
        return None
    return rotation_day_for(conn, guild_id, local_day(now, cfg.tz_offset_hours))


def blocked_quest_kinds_on(
    conn: sqlite3.Connection, guild_id: int, local_day_str: str
) -> frozenset[str]:
    """Trigger kinds whose room is hidden AND whose entry point is in-channel."""
    day = rotation_day_for(conn, guild_id, local_day_str)
    return day.blocked_quest_kinds if day else frozenset()


def featured_quest_kinds_on(
    conn: sqlite3.Connection, guild_id: int, local_day_str: str
) -> frozenset[str]:
    """Trigger kinds belonging to whichever room is open on that day."""
    day = rotation_day_for(conn, guild_id, local_day_str)
    return day.featured_quest_kinds if day else frozenset()
