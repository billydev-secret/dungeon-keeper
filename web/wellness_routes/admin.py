"""Wellness admin JSON API.

All routes here require the `manage_server` permission resolved from the
Discord MANAGE_GUILD bit (see web/auth.py::resolve_discord_perms).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from services.wellness_service import (
    ENFORCEMENT_LEVELS,
    add_exempt_channel,
    get_wellness_config,
    list_active_users,
    list_exempt_channels,
    pause_user,
    remove_exempt_channel,
    resume_user,
    upsert_wellness_config,
)
from web.auth import AuthenticatedUser
from web.wellness_routes.deps import get_ctx, require_manage_server

log = logging.getLogger("dungeonkeeper.wellness.web.admin")

router = APIRouter()


def _ok(**extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **extra})


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


# ── Read endpoints ─────────────────────────────────────────────────────

@router.get("/dashboard")
async def admin_dashboard_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        cfg = get_wellness_config(conn, ctx.guild_id)
        active = list_active_users(conn, ctx.guild_id)
        exempt = list_exempt_channels(conn, ctx.guild_id)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _channel_name(cid: int, label: str) -> str:
        if guild:
            ch = guild.get_channel(cid)
            if ch is not None:
                return getattr(ch, "name", label) or label
        return label or f"#{cid}"

    return {
        "active_count": len(active),
        "exempt_channels": [
            {"id": cid, "name": _channel_name(cid, lbl)} for cid, lbl in exempt
        ],
        "config": {
            "default_enforcement": cfg.default_enforcement if cfg else "gradual",
            "crisis_resource_url": cfg.crisis_resource_url if cfg else "",
        } if cfg else None,
    }


@router.get("/defaults")
async def admin_defaults_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        cfg = get_wellness_config(conn, ctx.guild_id)
    return {
        "config": {
            "default_enforcement": cfg.default_enforcement if cfg else "gradual",
            "crisis_resource_url": cfg.crisis_resource_url if cfg else "",
        } if cfg else None,
        "enforcement_levels": ENFORCEMENT_LEVELS,
    }


@router.post("/defaults")
async def admin_defaults_save(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    default_enforcement = payload.get("default_enforcement")
    crisis_resource_url = payload.get("crisis_resource_url")
    if default_enforcement is not None and default_enforcement not in ENFORCEMENT_LEVELS:
        return _err("invalid enforcement level")
    with ctx.open_db() as conn:
        upsert_wellness_config(
            conn, ctx.guild_id,
            default_enforcement=default_enforcement,
            crisis_resource_url=str(crisis_resource_url) if crisis_resource_url is not None else None,
        )
    return _ok()


@router.get("/users")
async def admin_users_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        active = list_active_users(conn, ctx.guild_id)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    def _name(uid: int) -> str:
        if guild:
            m = guild.get_member(uid)
            if m:
                return m.display_name
        return f"User {uid}"

    rows = [
        {
            "user_id": u.user_id,
            "name": _name(u.user_id),
            "timezone": u.timezone,
            "enforcement_level": u.enforcement_level,
            "is_paused": u.is_paused,
            "public_commitment": u.public_commitment,
        }
        for u in active
    ]
    return {"users": rows}


@router.post("/users/{user_id}/pause")
async def admin_pause_user(
    user_id: int,
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    import time
    try:
        minutes = int(payload.get("minutes", 0))
    except (TypeError, ValueError):
        return _err("minutes must be an integer")
    if minutes < 1 or minutes > 7 * 24 * 60:
        return _err("minutes must be between 1 and 10080")
    with ctx.open_db() as conn:
        pause_user(conn, ctx.guild_id, user_id, time.time() + minutes * 60)
    return _ok()


@router.post("/users/{user_id}/resume")
async def admin_resume_user(
    user_id: int,
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    with ctx.open_db() as conn:
        resume_user(conn, ctx.guild_id, user_id)
    return _ok()


# ── Exempt channels ─────────────────────────────────────────────────────

@router.get("/exempt")
async def admin_exempt_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        exempt = list_exempt_channels(conn, ctx.guild_id)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    rows = []
    for cid, label in exempt:
        name = label
        if guild:
            ch = guild.get_channel(cid)
            if ch is not None:
                name = getattr(ch, "name", label) or label
        rows.append({"id": cid, "label": label, "name": name})

    channel_options = []
    if guild:
        for ch in guild.text_channels:
            channel_options.append({"id": ch.id, "name": ch.name})
        channel_options.sort(key=lambda c: c["name"])

    return {"exempt": rows, "channel_options": channel_options}


@router.post("/exempt")
async def admin_exempt_add(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    try:
        channel_id = int(payload.get("channel_id", 0))
    except (TypeError, ValueError):
        return _err("channel_id must be an integer")
    if channel_id <= 0:
        return _err("channel_id is required")
    label = str(payload.get("label", "")).strip() or f"#{channel_id}"
    with ctx.open_db() as conn:
        add_exempt_channel(conn, ctx.guild_id, channel_id, label)
    return _ok()


@router.delete("/exempt/{channel_id}")
async def admin_exempt_remove(
    channel_id: int,
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    with ctx.open_db() as conn:
        ok = remove_exempt_channel(conn, ctx.guild_id, channel_id)
    if not ok:
        return _err("channel was not exempt", status=404)
    return _ok()
