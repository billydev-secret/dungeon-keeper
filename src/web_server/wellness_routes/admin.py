"""Wellness admin JSON API.

All routes here require the `manage_server` permission resolved from the
Discord MANAGE_GUILD bit (see web/auth.py::resolve_discord_perms).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from bot_modules.core.role_provision import (
    ensure_feature_role,
    provenance_recorder,
)
from bot_modules.services import feature_roles as fr
from bot_modules.services.wellness_service import (
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
from web_server.auth import AuthenticatedUser
from web_server.deps import run_query
from web_server.wellness_routes.deps import get_ctx, get_guild_id, require_manage_server

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
    guild_id: int = Depends(get_guild_id),
):
    def _q():
        with ctx.open_db() as conn:
            cfg = get_wellness_config(conn, guild_id)
            active = list_active_users(conn, guild_id)
            exempt = list_exempt_channels(conn, guild_id)
            return cfg, active, exempt

    cfg, active, exempt = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _channel_name(cid: int, label: str) -> str:
        if guild:
            ch = guild.get_channel(cid)
            if ch is not None:
                return getattr(ch, "name", label) or label
        return label or f"#{cid}"

    return {
        "active_count": len(active),
        "exempt_channels": [
            {"id": str(cid), "name": _channel_name(cid, lbl)} for cid, lbl in exempt
        ],
        "config": {
            "default_enforcement": cfg.default_enforcement if cfg else "gradual",
        }
        if cfg
        else None,
    }


@router.get("/defaults")
async def admin_defaults_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
):
    def _q():
        with ctx.open_db() as conn:
            return get_wellness_config(conn, guild_id)

    cfg = await run_query(_q)
    return {
        # Snowflakes as strings: a bare JSON number over 2**53 loses its low
        # digits in the browser and would be written back pointing at nothing.
        "config": {
            "default_enforcement": cfg.default_enforcement if cfg else "gradual",
            "role_id": str(cfg.role_id) if cfg else "0",
            "channel_id": str(cfg.channel_id) if cfg else "0",
        }
        if cfg
        else None,
        "enforcement_levels": ENFORCEMENT_LEVELS,
    }


def _optional_snowflake(payload: dict, key: str) -> int | None | str:
    """Read an optional id from the body. Returns the int, None when absent,
    or the error message when it isn't a number."""
    raw = payload.get(key)
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"{key} must be a number"
    if value < 0:
        return f"{key} must be a number"
    return value


@router.post("/defaults")
async def admin_defaults_save(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    default_enforcement = payload.get("default_enforcement")
    if (
        default_enforcement is not None
        and default_enforcement not in ENFORCEMENT_LEVELS
    ):
        return _err("invalid enforcement level")

    # The two fields that switch the programme on at all: `/wellness setup`
    # refuses without the opt-in role, and the scheduler posts neither the
    # active list nor a milestone without the channel. 0 is a real value here
    # — it is what "not set" looks like to both gates.
    role_id = _optional_snowflake(payload, "role_id")
    if isinstance(role_id, str):
        return _err(role_id)
    channel_id = _optional_snowflake(payload, "channel_id")
    if isinstance(channel_id, str):
        return _err(channel_id)

    def _write():
        with ctx.open_db() as conn:
            upsert_wellness_config(
                conn,
                guild_id,
                role_id=role_id,
                channel_id=channel_id,
                default_enforcement=default_enforcement,
            )

    await run_query(_write)
    return _ok()


@router.get("/users")
async def admin_users_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
):
    def _q():
        with ctx.open_db() as conn:
            return list_active_users(conn, guild_id)

    active = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _name(uid: int) -> str:
        if guild:
            m = guild.get_member(uid)
            if m:
                return m.display_name
        return f"User {uid}"

    rows = [
        {
            "user_id": str(u.user_id),
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
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    try:
        minutes = int(payload.get("minutes", 0))
    except (TypeError, ValueError):
        return _err("minutes must be an integer")
    if minutes < 1 or minutes > 7 * 24 * 60:
        return _err("minutes must be between 1 and 10080")
    until = time.time() + minutes * 60

    def _write():
        with ctx.open_db() as conn:
            return pause_user(conn, guild_id, user_id, until)

    ok = await run_query(_write)
    if not ok:
        return _err("user is not opted in", status=404)
    return _ok()


@router.post("/users/{user_id}/resume")
async def admin_resume_user(
    user_id: int,
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    def _write():
        with ctx.open_db() as conn:
            return resume_user(conn, guild_id, user_id)

    ok = await run_query(_write)
    if not ok:
        return _err("user is not opted in", status=404)
    return _ok()


# ── Provisioning (the Activate Wellness card) ──────────────────────────
#
# `role_id` / `channel_id` gate the whole feature — opt-in refuses without a
# role, the active list and milestone posts refuse without a channel — and
# until these routes existed nothing in src/ ever wrote them (the retired
# `/wellness-admin setup` command was their only writer). See
# docs/plans/wellness-relaunch.md Stage D.

#: The wellness role is Class A in the role-autocreate audit: the bot creates
#: it, the bot hands it out on opt-in, and it means nothing without the
#: feature — which is what makes auto-create safe here. Adopt-by-name means a
#: guild that already has a "Wellness Guardian" role keeps it.
#:
#: The spec itself lives in the shared registry (round 2), so the Bot-Managed
#: Roles page can list this role beside the other fifteen; a spec built here
#: was one nothing could enumerate.
WELLNESS_ROLE = fr.WELLNESS_ROLE
WELLNESS_ROLE_SPEC = WELLNESS_ROLE.spec


@router.get("/provision")
async def admin_provision_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
):
    def _q():
        with ctx.open_db() as conn:
            return get_wellness_config(conn, guild_id)

    cfg = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    role_id = cfg.role_id if cfg else 0
    channel_id = cfg.channel_id if cfg else 0
    role = guild.get_role(role_id) if guild and role_id else None
    channel = guild.get_channel(channel_id) if guild and channel_id else None

    role_options: list[dict[str, str]] = []
    channel_options: list[dict[str, str]] = []
    if guild:
        for r in guild.roles:
            # @everyone can't be handed out, and a managed role (bot,
            # integration, booster) refuses add_roles outright.
            if r.id == guild.id or getattr(r, "managed", False):
                continue
            role_options.append({"id": str(r.id), "name": r.name})
        role_options.sort(key=lambda r: r["name"])
        for ch in guild.text_channels:
            channel_options.append({"id": str(ch.id), "name": ch.name})
        channel_options.sort(key=lambda c: c["name"])

    return {
        "role_id": str(role_id),
        "role_name": getattr(role, "name", None),
        "channel_id": str(channel_id),
        "channel_name": getattr(channel, "name", None),
        "role_options": role_options,
        "channel_options": channel_options,
        "bot_connected": guild is not None,
        "auto_role_name": WELLNESS_ROLE_SPEC.name,
    }


@router.post("/provision/role")
async def admin_provision_role(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return _err("the bot isn't connected to this server right now", status=503)

    async def _store(rid: int) -> None:
        def _w():
            with ctx.open_db() as conn:
                upsert_wellness_config(conn, guild_id, role_id=int(rid))

        await run_query(_w)

    if payload.get("auto_create"):

        def _load():
            with ctx.open_db() as conn:
                cfg = get_wellness_config(conn, guild_id)
                return cfg.role_id if cfg else 0

        stored_id = await run_query(_load)
        # No mod-log announce even on a recreate: the admin is on the panel
        # doing this deliberately and sees the outcome directly.
        role = await ensure_feature_role(
            guild,
            WELLNESS_ROLE_SPEC,
            load=lambda: stored_id,
            store=_store,
            # The bot grants this on opt-in, so a same-named role above its own
            # top role must not be adopted — it could never be handed out.
            assigns=WELLNESS_ROLE.assigns,
            on_provision=provenance_recorder(ctx, guild_id, WELLNESS_ROLE.key),
            feature="Wellness",
        )
        if role is None:
            return _err(
                "couldn't create the role — check the bot has Manage Roles",
                status=502,
            )
        return _ok(role_id=str(role.id), role_name=role.name)

    try:
        role_id = int(payload.get("role_id", 0))
    except (TypeError, ValueError):
        return _err("role_id must be an integer")
    if role_id <= 0:
        return _err("role_id is required")
    role = guild.get_role(role_id)
    if role is None:
        return _err("that role doesn't exist on this server", status=404)
    if role.id == guild.id or getattr(role, "managed", False):
        return _err("that role can't be handed out by the bot")
    await _store(role.id)
    return _ok(role_id=str(role.id), role_name=role.name)


@router.post("/provision/channel")
async def admin_provision_channel(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return _err("the bot isn't connected to this server right now", status=503)

    try:
        channel_id = int(payload.get("channel_id", 0))
    except (TypeError, ValueError):
        return _err("channel_id must be an integer")
    if channel_id <= 0:
        return _err("channel_id is required")
    channel = guild.get_channel(channel_id)
    if channel is None or channel not in guild.text_channels:
        return _err("pick a text channel on this server", status=404)

    def _write():
        with ctx.open_db() as conn:
            upsert_wellness_config(conn, guild_id, channel_id=channel_id)

    await run_query(_write)
    return _ok(channel_id=str(channel_id), channel_name=channel.name)


# ── Exempt channels ─────────────────────────────────────────────────────


@router.get("/exempt")
async def admin_exempt_data(
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
):
    def _q():
        with ctx.open_db() as conn:
            return list_exempt_channels(conn, guild_id)

    exempt = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    rows = []
    for cid, label in exempt:
        name = label
        if guild:
            ch = guild.get_channel(cid)
            if ch is not None:
                name = getattr(ch, "name", label) or label
        rows.append({"id": str(cid), "label": label, "name": name})

    channel_options = []
    if guild:
        for ch in guild.text_channels:
            channel_options.append({"id": str(ch.id), "name": ch.name})
        channel_options.sort(key=lambda c: c["name"])

    return {"exempt": rows, "channel_options": channel_options}


@router.post("/exempt")
async def admin_exempt_add(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    try:
        channel_id = int(payload.get("channel_id", 0))
    except (TypeError, ValueError):
        return _err("channel_id must be an integer")
    if channel_id <= 0:
        return _err("channel_id is required")
    label = str(payload.get("label", "")).strip() or f"#{channel_id}"

    def _write():
        with ctx.open_db() as conn:
            add_exempt_channel(conn, guild_id, channel_id, label)

    await run_query(_write)
    return _ok()


@router.delete("/exempt/{channel_id}")
async def admin_exempt_remove(
    channel_id: int,
    user: AuthenticatedUser = Depends(require_manage_server),
    ctx=Depends(get_ctx),
    guild_id: int = Depends(get_guild_id),
) -> JSONResponse:
    def _write():
        with ctx.open_db() as conn:
            return remove_exempt_channel(conn, guild_id, channel_id)

    ok = await run_query(_write)
    if not ok:
        return _err("channel was not exempt", status=404)
    return _ok()
