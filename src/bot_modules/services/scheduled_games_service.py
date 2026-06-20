"""Scheduled games: auto-launch party games at set times.

Three layers live here:
  * Pure time math — ``compute_next_run`` (unit-tested, no I/O).
  * Sync CRUD over a sqlite3 connection — used by the web route via ``run_query``.
  * The async polling loop ``scheduled_games_loop`` — registered as a bot startup task.

Wall-clock fields (``time_of_day`` minutes, ``recur_days``, ``start_date``) are the
source of truth; ``next_run_at`` is a derived UTC-epoch cache the loop polls. The
guild's fixed ``tz_offset_hours`` defines local time (no DST — matches the rest of
the bot, see ``db_utils.get_tz_offset_hours``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta

from bot_modules.games.constants import GAME_NAMES, SCHEDULABLE_GAME_TYPES
from bot_modules.games.utils.game_manager import (
    check_game_enabled,
    get_active_game,
    resolve_name,
)

log = logging.getLogger(__name__)

# How long after a one-time schedule's slot we keep retrying past a busy channel
# before giving up.
GIVEUP_GRACE_SECONDS = 2 * 3600

VALID_RECURRENCE = ("once", "daily", "weekly")

_EPOCH = datetime(1970, 1, 1)


# ── Pure time math ──────────────────────────────────────────────────────────

def _local_to_epoch(local_naive: datetime, offset_hours: float) -> float:
    """Convert a guild-local naive datetime to a UTC epoch (local = UTC + offset)."""
    utc_naive = local_naive - timedelta(hours=offset_hours)
    return (utc_naive - _EPOCH).total_seconds()


def _epoch_to_local(epoch: float, offset_hours: float) -> datetime:
    """Convert a UTC epoch to a guild-local naive datetime."""
    return _EPOCH + timedelta(seconds=epoch) + timedelta(hours=offset_hours)


def compute_next_run(
    *,
    now_utc: float,
    offset_hours: float,
    recurrence: str,
    time_of_day_min: int,
    recur_days: list[int] | None = None,
    start_date: str | None = None,
    after: float | None = None,
) -> float | None:
    """Return the next fire time as a UTC epoch.

    For 'daily'/'weekly' the result is strictly greater than ``max(now_utc, after)``,
    so a row that fired (or was skipped) advances past many missed occurrences to the
    next future slot — at-most-once on recovery. For 'once' it returns the single
    slot's epoch (which may be in the past, i.e. fire-late); the loop never advances a
    one-time row. Returns None for weekly with no selected days, or a malformed once.
    """
    thr = now_utc if after is None else max(now_utc, after)
    tod = timedelta(minutes=int(time_of_day_min))

    if recurrence == "once":
        if not start_date:
            return None
        d = datetime.strptime(start_date, "%Y-%m-%d")
        slot = datetime(d.year, d.month, d.day) + tod
        return _local_to_epoch(slot, offset_hours)

    if recurrence == "daily":
        local_thr = _epoch_to_local(thr, offset_hours)
        base = local_thr.date()
        slot = datetime(base.year, base.month, base.day) + tod
        if _local_to_epoch(slot, offset_hours) <= thr:
            slot = slot + timedelta(days=1)
        return _local_to_epoch(slot, offset_hours)

    if recurrence == "weekly":
        if not recur_days:
            return None
        days = {int(x) for x in recur_days}
        base = _epoch_to_local(thr, offset_hours).date()
        for i in range(8):
            d = base + timedelta(days=i)
            if d.weekday() in days:
                slot = datetime(d.year, d.month, d.day) + tod
                epoch = _local_to_epoch(slot, offset_hours)
                if epoch > thr:
                    return epoch
        return None

    return None


# ── Offset readers ──────────────────────────────────────────────────────────

def _parse_offset(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


async def get_offset_hours_async(games_db, guild_id: int) -> float:
    """Async mirror of db_utils.get_tz_offset_hours, over the GamesDb wrapper."""
    row = await games_db.fetchone(
        "SELECT value FROM config WHERE guild_id = ? AND key = 'tz_offset_hours'",
        (guild_id,),
    )
    if row is None and guild_id != 0:
        row = await games_db.fetchone(
            "SELECT value FROM config WHERE guild_id = 0 AND key = 'tz_offset_hours'"
        )
    if row is None:
        return 0.0
    return _parse_offset(row[0])


# ── Sync CRUD (web route, via run_query) ────────────────────────────────────

_INSERT_COLS = (
    "guild_id", "channel_id", "game_type", "options", "created_by", "created_at",
    "time_of_day", "recurrence", "recur_days", "start_date", "next_run_at",
    "giveup_at", "announce", "announce_role_id",
)

_UPDATABLE_COLS = {
    "channel_id", "game_type", "options", "time_of_day", "recurrence", "recur_days",
    "start_date", "next_run_at", "giveup_at", "announce", "announce_role_id", "status",
}


def create_scheduled(conn: sqlite3.Connection, **fields) -> int:
    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    cols = ", ".join(_INSERT_COLS)
    cur = conn.execute(
        f"INSERT INTO games_scheduled ({cols}, status) VALUES ({placeholders}, 'active')",
        tuple(fields[c] for c in _INSERT_COLS),
    )
    return int(cur.lastrowid or 0)


def list_scheduled(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM games_scheduled WHERE guild_id = ? "
        "ORDER BY (status='active') DESC, next_run_at ASC",
        (guild_id,),
    ).fetchall()


def get_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM games_scheduled WHERE id = ? AND guild_id = ?",
        (sched_id, guild_id),
    ).fetchone()


def update_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int, fields: dict) -> None:
    cols = [c for c in fields if c in _UPDATABLE_COLS]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    params = [fields[c] for c in cols] + [sched_id, guild_id]
    conn.execute(
        f"UPDATE games_scheduled SET {assignments} WHERE id = ? AND guild_id = ?",
        params,
    )


def delete_scheduled(conn: sqlite3.Connection, sched_id: int, guild_id: int) -> None:
    conn.execute(
        "DELETE FROM games_scheduled WHERE id = ? AND guild_id = ?", (sched_id, guild_id)
    )


def set_status(conn: sqlite3.Connection, sched_id: int, guild_id: int, status: str) -> None:
    conn.execute(
        "UPDATE games_scheduled SET status = ? WHERE id = ? AND guild_id = ?",
        (status, sched_id, guild_id),
    )


# ── Async polling loop ──────────────────────────────────────────────────────

async def _advance_or_finish(games_db, row, now: float, last_status: str,
                             offset: float, recur_days) -> None:
    """Recurring → roll next_run_at past now; once → mark done. Record last_run/status."""
    if row["recurrence"] == "once":
        await games_db.execute(
            "UPDATE games_scheduled SET status='done', next_run_at=NULL, "
            "last_run_at=?, last_status=? WHERE id=?",
            (now, last_status, row["id"]),
        )
        return
    nxt = compute_next_run(
        now_utc=now, offset_hours=offset, recurrence=row["recurrence"],
        time_of_day_min=row["time_of_day"], recur_days=recur_days,
        start_date=row["start_date"], after=now,
    )
    if nxt is None:
        await games_db.execute(
            "UPDATE games_scheduled SET status='done', next_run_at=NULL, "
            "last_run_at=?, last_status=? WHERE id=?",
            (now, last_status, row["id"]),
        )
    else:
        await games_db.execute(
            "UPDATE games_scheduled SET next_run_at=?, last_run_at=?, last_status=? WHERE id=?",
            (nxt, now, last_status, row["id"]),
        )


async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


async def _process_due(bot, games_db, row, now: float) -> None:
    sched_id = row["id"]
    guild_id = row["guild_id"]
    channel_id = row["channel_id"]
    game_type = row["game_type"]
    offset = await get_offset_hours_async(games_db, guild_id)
    recur_days = json.loads(row["recur_days"]) if row["recur_days"] else None

    # 1. Resolve the target channel.
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        log.warning("Scheduled game %s: channel %s unreachable", sched_id, channel_id)
        await _advance_or_finish(games_db, row, now, "error", offset, recur_days)
        return

    # 2. Re-check the game is still enabled (scheduler bypasses the slash guards).
    if not await check_game_enabled(games_db, game_type, guild_id):
        await _advance_or_finish(games_db, row, now, "skipped_disabled", offset, recur_days)
        return

    # 3. Skip if the channel already has an active game. Most games register in the
    #    games_active_games table; some (e.g. risky_roll) track rounds in-memory and
    #    register an optional busy-check so we can see them too — otherwise we'd ping
    #    "starting now!" and then fail to launch a duplicate. Let the running game ride.
    busy = await get_active_game(games_db, channel_id) is not None
    if not busy:
        busy_check = getattr(bot, "game_busy_checks", {}).get(game_type)
        if busy_check is not None:
            try:
                busy = bool(await busy_check(channel_id))
            except Exception:
                log.exception("Scheduled game %s: busy-check for %s raised", sched_id, game_type)
    if busy:
        if row["recurrence"] == "once":
            if now >= (row["giveup_at"] or now):
                await games_db.execute(
                    "UPDATE games_scheduled SET status='done', next_run_at=NULL, "
                    "last_run_at=?, last_status='skipped_giveup' WHERE id=?",
                    (now, sched_id),
                )
            else:
                # Stay due — the 60s poll is the retry until giveup_at.
                await games_db.execute(
                    "UPDATE games_scheduled SET last_status='skipped_active' WHERE id=?",
                    (sched_id,),
                )
            return
        await _advance_or_finish(games_db, row, now, "skipped_active", offset, recur_days)
        return

    launcher = bot.game_launchers.get(game_type) if hasattr(bot, "game_launchers") else None
    if launcher is None:
        log.error("Scheduled game %s: no launcher for game_type=%s", sched_id, game_type)
        await _advance_or_finish(games_db, row, now, "error", offset, recur_days)
        return

    # 4. Claim before launch: advance/finish state BEFORE awaiting the launch so a crash
    #    mid-launch can't double-fire on restart.
    await _advance_or_finish(games_db, row, now, "launching", offset, recur_days)

    guild = bot.get_guild(guild_id)
    host_name = resolve_name(guild, row["created_by"]) if guild else "Scheduled Game"
    try:
        options = json.loads(row["options"] or "{}")
    except Exception:
        options = {}

    if row["announce"]:
        game_label = GAME_NAMES.get(game_type, game_type)
        prefix = f"<@&{row['announce_role_id']}> " if row["announce_role_id"] else ""
        try:
            await channel.send(f"{prefix}🎮 **{game_label}** is starting now!")
        except Exception:
            log.warning("Scheduled game %s: announce failed in channel %s", sched_id, channel_id)

    try:
        gid = await launcher(
            channel=channel,
            host_id=row["created_by"],
            host_name=host_name,
            guild_id=guild_id,
            options=options,
        )
    except Exception:
        log.exception("Scheduled game %s: launcher %s raised", sched_id, game_type)
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='error' WHERE id=?", (sched_id,)
        )
        return

    # Launchers return None on failure (e.g. missing send perms, caught internally),
    # so a falsy result is a real failure — don't mislabel it as launched.
    if gid:
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='launched' WHERE id=?", (sched_id,)
        )
        log.info("Scheduled game %s launched: %s in channel %s", sched_id, game_type, channel_id)
    else:
        await games_db.execute(
            "UPDATE games_scheduled SET last_status='error' WHERE id=?", (sched_id,)
        )
        log.warning("Scheduled game %s: launcher %s returned no game (no perms or channel busy)", sched_id, game_type)


async def scheduled_games_loop(bot) -> None:
    """Poll every 60s and fire due schedules. Registered as a bot startup task."""
    await bot.wait_until_ready()
    games_db = bot.games_db

    # Coverage check: every schedulable game must have a registered launcher.
    launchers = getattr(bot, "game_launchers", {})
    missing = [g for g in SCHEDULABLE_GAME_TYPES if g not in launchers]
    if missing:
        log.error(
            "Scheduled games: %d schedulable game(s) have no launcher and will fail "
            "to auto-launch: %s", len(missing), ", ".join(missing),
        )


    while not bot.is_closed():
        try:
            now = time.time()
            rows = await games_db.fetchall(
                "SELECT * FROM games_scheduled "
                "WHERE status='active' AND next_run_at IS NOT NULL AND next_run_at <= ?",
                (now,),
            )
            for row in rows:
                try:
                    await _process_due(bot, games_db, row, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("scheduled game %s failed to process", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled_games_loop iteration error")
        await asyncio.sleep(60)
