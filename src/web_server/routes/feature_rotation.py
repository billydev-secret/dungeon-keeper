"""Daily feature-channel rotation — dashboard API.

The rotation's entire admin surface (CLAUDE.md: configuration lives on the web
dashboard, not in Discord). Settings, the pool table, and the two Discord-side
actions — applying today's plan now, and lifting the rotation off a channel
when it leaves the pool.

Removing a room **restores it first, then deletes the row**: dropping the row
of a hidden channel would strand it hidden with its saved overwrites gone,
which is the one unrecoverable mistake this feature can make.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.economy.quests import TRIGGER_KINDS
from bot_modules.feature_rotation.logic import Room, local_day
from bot_modules.feature_rotation.store import (
    RotationConfig,
    delete_room,
    get_config,
    list_pool,
    list_pool_state,
    rotation_day_for,
    save_config,
    upsert_room,
)
from bot_modules.services.feature_rotation_service import apply_day, show_room
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
_ADMIN = Depends(require_perms({"admin"}))


class ConfigBody(BaseModel):
    enabled: bool = False
    announce_channel_id: int = 0
    announce_hour: int = Field(9, ge=0, le=23)
    tz_offset_hours: int = Field(-7, ge=-12, le=14)
    rooms_per_day: int = Field(1, ge=1, le=10)


class RoomBody(BaseModel):
    position: int = Field(0, ge=0, le=999)
    label: str = Field("", max_length=100)
    blurb: str = Field("", max_length=300)
    in_rotation: bool = True
    hide_when_off: bool = True
    announce: bool = True
    quest_kinds: list[str] = Field(default_factory=list)
    blocked_kinds: list[str] = Field(default_factory=list)


def _clean_kinds(kinds: list[str]) -> tuple[str, ...]:
    """Keep only real trigger kinds, in order, without duplicates.

    Unknown strings are dropped rather than rejected: a kind retired from
    ``TRIGGER_KINDS`` between a page load and a save should cost the admin the
    stale entry, not the whole save.
    """
    seen: set[str] = set()
    out: list[str] = []
    for kind in kinds:
        k = str(kind).strip()
        if k and k in TRIGGER_KINDS and k not in seen:
            seen.add(k)
            out.append(k)
    return tuple(out)


def _room_payload(room: Room, hidden: bool, featured: bool) -> dict:
    return {
        "channel_id": str(room.channel_id),
        "position": room.position,
        "label": room.label,
        "blurb": room.blurb,
        "in_rotation": room.in_rotation,
        "hide_when_off": room.hide_when_off,
        "announce": room.announce,
        "quest_kinds": list(room.quest_kinds),
        "blocked_kinds": list(room.blocked_kinds),
        "hidden_now": hidden,
        "featured_today": featured,
    }


@router.get("/feature-rotation")
async def get_rotation(
    request: Request,
    user: AuthenticatedUser = _ADMIN,
) -> dict:
    """Settings, the pool, and what today and tomorrow look like."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q() -> dict:
        with ctx.open_db() as conn:
            cfg = get_config(conn, guild_id)
            rooms = list_pool(conn, guild_id)
            hidden = list_pool_state(conn, guild_id)
            today_str = local_day(now, cfg.tz_offset_hours)
            today = rotation_day_for(conn, guild_id, today_str)
            tomorrow_day = local_day(now + 86400, cfg.tz_offset_hours)
            tomorrow = rotation_day_for(conn, guild_id, tomorrow_day)
            featured = set(today.featured) if today else set()
            return {
                "config": {
                    "enabled": cfg.enabled,
                    "announce_channel_id": str(cfg.announce_channel_id),
                    "announce_hour": cfg.announce_hour,
                    "tz_offset_hours": cfg.tz_offset_hours,
                    "rooms_per_day": cfg.rooms_per_day,
                    "last_flip_date": cfg.last_flip_date,
                    "last_announce_date": cfg.last_announce_date,
                },
                "rooms": [
                    _room_payload(r, hidden.get(r.channel_id, False), r.channel_id in featured)
                    for r in rooms
                ],
                "today": {
                    "local_day": today_str,
                    "featured": [str(c) for c in (today.featured if today else ())],
                    "hidden": [str(c) for c in (today.plan.hide if today else ())],
                    "blocked_quest_kinds": sorted(
                        today.blocked_quest_kinds if today else ()
                    ),
                },
                "tomorrow": {
                    "local_day": tomorrow_day,
                    "featured": [str(c) for c in (tomorrow.featured if tomorrow else ())],
                },
                "trigger_kinds": [
                    {"kind": k, "label": v} for k, v in sorted(TRIGGER_KINDS.items())
                ],
            }

    return await run_query(_q)


@router.put("/feature-rotation/config")
async def put_config(
    body: ConfigBody,
    request: Request,
    user: AuthenticatedUser = _ADMIN,
) -> dict:
    """Save the settings. The claimed flip/announce dates are left alone."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q() -> None:
        with ctx.open_db() as conn:
            save_config(
                conn,
                RotationConfig(
                    guild_id=guild_id,
                    enabled=body.enabled,
                    announce_channel_id=body.announce_channel_id,
                    announce_hour=body.announce_hour,
                    tz_offset_hours=body.tz_offset_hours,
                    rooms_per_day=body.rooms_per_day,
                ),
            )

    await run_query(_q)
    return {"ok": True}


@router.put("/feature-rotation/rooms/{channel_id}")
async def put_room(
    channel_id: int,
    body: RoomBody,
    request: Request,
    user: AuthenticatedUser = _ADMIN,
) -> dict:
    """Add or update one pool row."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    room = Room(
        channel_id=channel_id,
        position=body.position,
        label=body.label.strip(),
        blurb=body.blurb.strip(),
        in_rotation=body.in_rotation,
        hide_when_off=body.hide_when_off,
        announce=body.announce,
        quest_kinds=_clean_kinds(body.quest_kinds),
        blocked_kinds=_clean_kinds(body.blocked_kinds),
    )

    def _q() -> None:
        with ctx.open_db() as conn:
            upsert_room(conn, guild_id, room)

    await run_query(_q)
    return {"ok": True}


@router.delete("/feature-rotation/rooms/{channel_id}")
async def remove_room(
    channel_id: int,
    request: Request,
    user: AuthenticatedUser = _ADMIN,
) -> dict:
    """Take a channel out of the pool, restoring its permissions first.

    Order matters and is not an optimisation: deleting the row first would
    discard the saved overwrites while the channel is still hidden, leaving no
    record of what its permissions used to be.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    restored = False
    if guild is not None:
        restored = await show_room(
            bot, guild, channel_id, reason="Removed from feature rotation"
        )

    def _q() -> bool:
        with ctx.open_db() as conn:
            return delete_room(conn, guild_id, channel_id)

    if not await run_query(_q):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Channel is not in the pool."
        )
    return {"ok": True, "restored": restored}


@router.post("/feature-rotation/apply")
async def apply_now(
    request: Request,
    user: AuthenticatedUser = _ADMIN,
) -> dict:
    """Bring Discord into line with today's plan without waiting for midnight.

    Useful straight after editing the pool, and the only way to un-hide rooms
    if the rotation is switched off mid-day. Does not claim the day, so the
    real flip still happens on schedule.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The bot isn't connected to this server right now.",
        )
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            cfg = get_config(conn, guild_id)
            return cfg, rotation_day_for(conn, guild_id, local_day(now, cfg.tz_offset_hours))

    _cfg, day = await run_query(_q)
    if day is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Turn the rotation on and add at least one channel first.",
        )
    shown, hidden = await apply_day(bot, guild, day, reason="Feature rotation (manual)")
    return {"ok": True, "shown": shown, "hidden": hidden}
