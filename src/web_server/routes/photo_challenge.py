"""Photo Challenge — standalone dashboard feature (config + own schedule).

Photo Challenge left the shared games menu/scheduler: it has one dedicated
channel it posts in, its own recurring schedule, a ping role, and an enabled
toggle. Under the hood it reuses the game-type-agnostic scheduler runtime —
schedule rows live in ``games_scheduled`` with ``game_type='photo'`` and are
fired by ``scheduled_games_loop`` — but they're created here (channel forced
from config) and hidden from the shared scheduler UI. Config (channel_id,
ping_role_id, enabled) rides in ``games_game_config`` under game_type='photo',
which is also where the scheduler's enable-gate and the cog's launch() read it,
so no new table is needed.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.games.constants import GAME_ICONS, GAME_NAMES
from bot_modules.services.scheduled_games_service import (
    GIVEUP_GRACE_SECONDS,
    VALID_RECURRENCE,
    compute_next_run,
    create_scheduled,
    delete_scheduled,
    get_scheduled,
    list_scheduled,
    update_scheduled,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_game_host, run_query

log = logging.getLogger("dungeonkeeper.photo_challenge")

router = APIRouter()

GAME_TYPE = "photo"


# ── Pydantic models ──────────────────────────────────────────────────────────

class ConfigBody(BaseModel):
    channel_id: str = ""          # "" clears the dedicated channel
    ping_role_id: str = ""        # "" / "0" = no ping
    enabled: bool = True
    react_threshold: int = 5      # distinct human reactors a post needs to pay
    auto_react: str = ""          # emoji the bot seeds on each photo ("" = off)


class ScheduleBody(BaseModel):
    recurrence: str                          # once | daily | weekly
    time: str                                # "HH:MM" in guild-local time
    recur_days: Optional[list[int]] = None   # weekly: weekday ints (Mon=0..Sun=6)
    start_date: Optional[str] = None         # once: "YYYY-MM-DD" (guild-local)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_time_of_day(raw: str) -> int:
    """Parse 'HH:MM' into minutes since local midnight (0..1439)."""
    try:
        hh, mm = raw.split(":")
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="time must be 'HH:MM'")
    if not 0 <= minutes < 24 * 60:
        raise HTTPException(status_code=400, detail="time out of range")
    return minutes


def _validate_schedule(body: ScheduleBody) -> tuple[int, str | None]:
    """Validate schedule body; return (time_of_day_min, recur_days_json)."""
    if body.recurrence not in VALID_RECURRENCE:
        raise HTTPException(status_code=400, detail=f"Invalid recurrence: {body.recurrence}")
    tod = _parse_time_of_day(body.time)
    recur_days_json: str | None = None
    if body.recurrence == "weekly":
        days = sorted({int(d) for d in (body.recur_days or []) if 0 <= int(d) <= 6})
        if not days:
            raise HTTPException(status_code=400, detail="weekly needs at least one weekday")
        recur_days_json = json.dumps(days)
    if body.recurrence == "once" and not body.start_date:
        raise HTTPException(status_code=400, detail="once needs a start_date")
    return tod, recur_days_json


def _read_config(conn, guild_id: int) -> dict:
    row = conn.execute(
        "SELECT enabled, options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
        (guild_id, GAME_TYPE),
    ).fetchone()
    if not row:
        return {
            "enabled": True,
            "channel_id": "",
            "ping_role_id": "",
            "react_threshold": 5,
            "auto_react": "",
        }
    opts = json.loads(row[1] or "{}")
    return {
        "enabled": bool(row[0]),
        "channel_id": str(opts.get("channel_id") or ""),
        "ping_role_id": str(opts.get("ping_role_id") or ""),
        "react_threshold": int(opts.get("react_threshold") or 5),
        "auto_react": str(opts.get("auto_react") or ""),
    }


def _configured_channel_id(conn, guild_id: int) -> int:
    """The dedicated channel int, or 400 if none is set yet."""
    ch = _read_config(conn, guild_id)["channel_id"]
    if not ch.isdigit() or int(ch) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Set a Photo Challenge channel before creating a schedule.",
        )
    return int(ch)


# ── Config endpoints ─────────────────────────────────────────────────────────

@router.get("/config")
async def get_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return _read_config(conn, guild_id)

    return await run_query(_q)


@router.put("/config")
async def set_config(
    request: Request,
    body: ConfigBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    channel_raw = (body.channel_id or "").strip()
    if channel_raw and not channel_raw.isdigit():
        raise HTTPException(status_code=400, detail="channel_id must be numeric")
    role_raw = (body.ping_role_id or "").strip()
    if role_raw in ("", "0"):
        role_raw = ""
    elif not role_raw.isdigit():
        raise HTTPException(status_code=400, detail="ping_role_id must be numeric")
    # Reaction payout tuning (read by EconomyCog._on_photo_react): clamp the
    # threshold to a sane 1..100, cap the auto-react emoji length.
    threshold = max(1, min(100, int(body.react_threshold or 5)))
    auto_react = (body.auto_react or "").strip()[:64]

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT options FROM games_game_config WHERE guild_id = ? AND game_type = ?",
                (guild_id, GAME_TYPE),
            ).fetchone()
            opts = json.loads(row[0] or "{}") if row else {}
            opts["channel_id"] = channel_raw
            opts["ping_role_id"] = role_raw
            opts["react_threshold"] = threshold
            opts["auto_react"] = auto_react
            enabled = int(body.enabled)
            if row is not None:
                conn.execute(
                    "UPDATE games_game_config SET enabled = ?, options = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND game_type = ?",
                    (enabled, json.dumps(opts), guild_id, GAME_TYPE),
                )
            else:
                conn.execute(
                    "INSERT INTO games_game_config (guild_id, game_type, enabled, options) "
                    "VALUES (?, ?, ?, ?)",
                    (guild_id, GAME_TYPE, enabled, json.dumps(opts)),
                )
            # Keep existing photo schedules pointed at the (possibly new) channel.
            if channel_raw:
                conn.execute(
                    "UPDATE games_scheduled SET channel_id = ? "
                    "WHERE guild_id = ? AND game_type = ?",
                    (int(channel_raw), guild_id, GAME_TYPE),
                )
            conn.commit()
        return {"ok": True}

    return await run_query(_q)


# ── Schedule endpoints ───────────────────────────────────────────────────────

@router.get("/schedule")
async def list_schedule(
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = list_scheduled(conn, guild_id)
        out = []
        for r in rows:
            d = dict(r)
            if d["game_type"] != GAME_TYPE:
                continue
            d["game_name"] = GAME_NAMES.get(GAME_TYPE, GAME_TYPE)
            d["game_icon"] = GAME_ICONS.get(GAME_TYPE, "📸")
            d["recur_days"] = json.loads(d["recur_days"]) if d.get("recur_days") else None
            out.append(d)
        return out

    return await run_query(_q)


@router.post("/schedule")
async def create_schedule(
    request: Request,
    body: ScheduleBody,
    user: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tod, recur_days_json = _validate_schedule(body)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            channel_id = _configured_channel_id(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            next_run_at = compute_next_run(
                now_utc=now, offset_hours=offset, recurrence=body.recurrence,
                time_of_day_min=tod,
                recur_days=json.loads(recur_days_json) if recur_days_json else None,
                start_date=body.start_date,
            )
            if next_run_at is None:
                raise HTTPException(status_code=400, detail="Could not compute a run time")
            if body.recurrence == "once" and next_run_at < now:
                raise HTTPException(status_code=400, detail="That date/time is in the past")
            giveup_at = (next_run_at + GIVEUP_GRACE_SECONDS) if body.recurrence == "once" else None

            sched_id = create_scheduled(
                conn,
                guild_id=guild_id,
                channel_id=channel_id,
                game_type=GAME_TYPE,
                options="{}",
                created_by=int(user.user_id),
                created_at=now,
                time_of_day=tod,
                recurrence=body.recurrence,
                recur_days=recur_days_json,
                start_date=body.start_date,
                next_run_at=next_run_at,
                giveup_at=giveup_at,
                announce=0,
                announce_role_id=None,
            )
        return {"ok": True, "id": sched_id, "next_run_at": next_run_at}

    return await run_query(_q)


@router.put("/schedule/{sched_id}")
async def update_schedule(
    sched_id: int,
    request: Request,
    body: ScheduleBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tod, recur_days_json = _validate_schedule(body)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            existing = get_scheduled(conn, sched_id, guild_id)
            if existing is None or existing["game_type"] != GAME_TYPE:
                raise HTTPException(status_code=404, detail="Schedule not found")
            channel_id = _configured_channel_id(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            next_run_at = compute_next_run(
                now_utc=now, offset_hours=offset, recurrence=body.recurrence,
                time_of_day_min=tod,
                recur_days=json.loads(recur_days_json) if recur_days_json else None,
                start_date=body.start_date,
            )
            if next_run_at is None:
                raise HTTPException(status_code=400, detail="Could not compute a run time")
            if body.recurrence == "once" and next_run_at < now:
                raise HTTPException(status_code=400, detail="That date/time is in the past")
            giveup_at = (next_run_at + GIVEUP_GRACE_SECONDS) if body.recurrence == "once" else None

            update_scheduled(conn, sched_id, guild_id, {
                "channel_id": channel_id,
                "time_of_day": tod,
                "recurrence": body.recurrence,
                "recur_days": recur_days_json,
                "start_date": body.start_date,
                "next_run_at": next_run_at,
                "giveup_at": giveup_at,
                "status": "active",
            })
        return {"ok": True, "next_run_at": next_run_at}

    return await run_query(_q)


@router.delete("/schedule/{sched_id}")
async def delete_schedule(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = get_scheduled(conn, sched_id, guild_id)
            if row is None or row["game_type"] != GAME_TYPE:
                raise HTTPException(status_code=404, detail="Schedule not found")
            delete_scheduled(conn, sched_id, guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/schedule/{sched_id}/pause")
async def pause_schedule(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = get_scheduled(conn, sched_id, guild_id)
            if row is None or row["game_type"] != GAME_TYPE:
                raise HTTPException(status_code=404, detail="Schedule not found")
            update_scheduled(conn, sched_id, guild_id, {"status": "paused"})
        return {"ok": True}

    return await run_query(_q)


@router.post("/schedule/{sched_id}/resume")
async def resume_schedule(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            row = get_scheduled(conn, sched_id, guild_id)
            if row is None or row["game_type"] != GAME_TYPE:
                raise HTTPException(status_code=404, detail="Schedule not found")
            offset = get_tz_offset_hours(conn, guild_id)
            next_run_at = compute_next_run(
                now_utc=now, offset_hours=offset, recurrence=row["recurrence"],
                time_of_day_min=row["time_of_day"],
                recur_days=json.loads(row["recur_days"]) if row["recur_days"] else None,
                start_date=row["start_date"], after=now,
            )
            if next_run_at is None:
                raise HTTPException(status_code=400, detail="Nothing left to run; delete it instead")
            update_scheduled(conn, sched_id, guild_id,
                             {"status": "active", "next_run_at": next_run_at})
        return {"ok": True, "next_run_at": next_run_at}

    return await run_query(_q)


@router.post("/schedule/{sched_id}/run-now")
async def run_now(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    """Arm the schedule to fire on the next poll (reuses the busy/disabled guards)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            row = get_scheduled(conn, sched_id, guild_id)
            if row is None or row["game_type"] != GAME_TYPE:
                raise HTTPException(status_code=404, detail="Schedule not found")
            update_scheduled(conn, sched_id, guild_id,
                             {"status": "active", "next_run_at": now})
        return {"ok": True}

    return await run_query(_q)
