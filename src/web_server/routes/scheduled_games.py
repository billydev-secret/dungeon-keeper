"""Scheduled games endpoints — create/list/edit schedules that auto-launch games."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.games.constants import (
    GAME_ICONS,
    GAME_NAMES,
    SCHEDULABLE_GAME_TYPES,
    SCHEDULE_OPTION_SCHEMA,
)
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

log = logging.getLogger("dungeonkeeper.games.schedule")

router = APIRouter()


# ── Pydantic models ──────────────────────────────────────────────────────────

class ScheduleBody(BaseModel):
    channel_id: str
    game_type: str
    options: dict = {}
    recurrence: str                       # once | daily | weekly
    time: str                             # "HH:MM" in guild-local time
    recur_days: Optional[list[int]] = None  # weekly: weekday ints (Mon=0..Sun=6)
    start_date: Optional[str] = None      # once: "YYYY-MM-DD" (guild-local)
    announce: bool = False
    announce_role_id: Optional[str] = None


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


def _validate(body: ScheduleBody) -> tuple[int, int, str | None]:
    """Validate body shape; return (channel_id, time_of_day_min, recur_days_json)."""
    if body.game_type not in SCHEDULABLE_GAME_TYPES:
        raise HTTPException(status_code=400, detail=f"Not schedulable: {body.game_type}")
    if body.recurrence not in VALID_RECURRENCE:
        raise HTTPException(status_code=400, detail=f"Invalid recurrence: {body.recurrence}")
    try:
        channel_id = int(body.channel_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="channel_id must be numeric")

    tod = _parse_time_of_day(body.time)

    recur_days_json: str | None = None
    if body.recurrence == "weekly":
        days = sorted({int(d) for d in (body.recur_days or []) if 0 <= int(d) <= 6})
        if not days:
            raise HTTPException(status_code=400, detail="weekly needs at least one weekday")
        recur_days_json = json.dumps(days)
    if body.recurrence == "once" and not body.start_date:
        raise HTTPException(status_code=400, detail="once needs a start_date")

    return channel_id, tod, recur_days_json


def _channel_in_guild(ctx, guild_id: int, channel_id: int) -> bool:
    """True if the live bot can see this channel in the active guild (best-effort)."""
    bot = getattr(ctx, "bot", None)
    if bot is None:
        return True  # bot not attached (e.g. tests) — skip the guard
    guild = bot.get_guild(guild_id)
    if guild is None:
        return True
    return guild.get_channel(channel_id) is not None


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/options")
async def schedule_options(
    _: AuthenticatedUser = Depends(require_game_host),
):
    """Schedulable game types + per-game option schema for the UI."""
    return {
        "games": [
            {
                "type": g,
                "name": GAME_NAMES.get(g, g),
                "icon": GAME_ICONS.get(g, "🎮"),
                "fields": SCHEDULE_OPTION_SCHEMA.get(g, []),
            }
            for g in SCHEDULABLE_GAME_TYPES
        ],
    }


@router.get("")
async def list_schedules(
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
            # Stringify snowflakes so JS keeps full 64-bit precision (a bare
            # number > 2^53 rounds, breaking the role-picker match on edit and
            # silently nulling announce_role_id via the full-column PUT).
            for k in ("guild_id", "channel_id", "announce_role_id"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            # Skip rows for game types that have left the shared games menu
            # (e.g. 'photo' — now the standalone Photo Challenge feature, which
            # owns its own schedule UI). Their rows still run on the shared loop.
            if d["game_type"] not in SCHEDULABLE_GAME_TYPES:
                continue
            d["game_name"] = GAME_NAMES.get(d["game_type"], d["game_type"])
            d["game_icon"] = GAME_ICONS.get(d["game_type"], "🎮")
            d["recur_days"] = json.loads(d["recur_days"]) if d.get("recur_days") else None
            try:
                d["options"] = json.loads(d.get("options") or "{}")
            except (ValueError, TypeError):
                d["options"] = {}
            out.append(d)
        return out

    return await run_query(_q)


@router.post("")
async def create_schedule(
    request: Request,
    body: ScheduleBody,
    user: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    channel_id, tod, recur_days_json = _validate(body)

    if not _channel_in_guild(ctx, guild_id, channel_id):
        raise HTTPException(status_code=400, detail="Channel is not in this server")

    now = time.time()

    def _q():
        with ctx.open_db() as conn:
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
                game_type=body.game_type,
                options=json.dumps(body.options or {}),
                created_by=int(user.user_id),
                created_at=now,
                time_of_day=tod,
                recurrence=body.recurrence,
                recur_days=recur_days_json,
                start_date=body.start_date,
                next_run_at=next_run_at,
                giveup_at=giveup_at,
                announce=1 if body.announce else 0,
                announce_role_id=int(body.announce_role_id) if body.announce_role_id else None,
            )
        return {"ok": True, "id": sched_id, "next_run_at": next_run_at}

    return await run_query(_q)


@router.put("/{sched_id}")
async def update_schedule(
    sched_id: int,
    request: Request,
    body: ScheduleBody,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    channel_id, tod, recur_days_json = _validate(body)

    if not _channel_in_guild(ctx, guild_id, channel_id):
        raise HTTPException(status_code=400, detail="Channel is not in this server")

    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            if get_scheduled(conn, sched_id, guild_id) is None:
                raise HTTPException(status_code=404, detail="Schedule not found")
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
                "game_type": body.game_type,
                "options": json.dumps(body.options or {}),
                "time_of_day": tod,
                "recurrence": body.recurrence,
                "recur_days": recur_days_json,
                "start_date": body.start_date,
                "next_run_at": next_run_at,
                "giveup_at": giveup_at,
                "announce": 1 if body.announce else 0,
                "announce_role_id": int(body.announce_role_id) if body.announce_role_id else None,
                "status": "active",
            })
        return {"ok": True, "next_run_at": next_run_at}

    return await run_query(_q)


@router.delete("/{sched_id}")
async def delete_schedule(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_scheduled(conn, sched_id, guild_id) is None:
                raise HTTPException(status_code=404, detail="Schedule not found")
            delete_scheduled(conn, sched_id, guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/{sched_id}/pause")
async def pause_schedule(
    sched_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_scheduled(conn, sched_id, guild_id) is None:
                raise HTTPException(status_code=404, detail="Schedule not found")
            update_scheduled(conn, sched_id, guild_id, {"status": "paused"})
        return {"ok": True}

    return await run_query(_q)


@router.post("/{sched_id}/resume")
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
            if row is None:
                raise HTTPException(status_code=404, detail="Schedule not found")
            offset = get_tz_offset_hours(conn, guild_id)
            next_run_at = compute_next_run(
                now_utc=now, offset_hours=offset, recurrence=row["recurrence"],
                time_of_day_min=row["time_of_day"],
                recur_days=json.loads(row["recur_days"]) if row["recur_days"] else None,
                start_date=row["start_date"], after=now,
            )
            if next_run_at is None:
                # A one-time schedule whose slot has passed can't be resumed.
                raise HTTPException(status_code=400, detail="Nothing left to run; delete it instead")
            update_scheduled(conn, sched_id, guild_id,
                             {"status": "active", "next_run_at": next_run_at})
        return {"ok": True, "next_run_at": next_run_at}

    return await run_query(_q)


@router.post("/{sched_id}/run-now")
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
            if get_scheduled(conn, sched_id, guild_id) is None:
                raise HTTPException(status_code=404, detail="Schedule not found")
            update_scheduled(conn, sched_id, guild_id,
                             {"status": "active", "next_run_at": now})
        return {"ok": True}

    return await run_query(_q)
