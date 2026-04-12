"""Wellness JSON API — read + write endpoints for the dashboard SPA.

Each endpoint reads the shared `dk_session` cookie via `require_user`,
mutates/reads state via `services.wellness_service`, and returns JSON.
Write endpoints return `{ok: true, ...}` or `{ok: false, error: "..."}`.
Read endpoints return the data directly.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from services.wellness_service import (
    AWAY_MESSAGE_MAX_LEN,
    BLACKOUT_TEMPLATES,
    CAP_SCOPES,
    CAP_WINDOWS,
    ENFORCEMENT_LEVELS,
    NOTIFICATION_PREFS,
    add_blackout,
    add_cap,
    create_partner_request,
    dissolve_partnership,
    ensure_streak,
    find_blackout_by_name,
    find_cap_by_label,
    get_cap,
    get_partnership,
    get_wellness_config,
    get_wellness_user,
    list_blackouts,
    list_caps,
    list_partnerships,
    list_weekly_reports,
    next_milestone,
    pause_user,
    remove_blackout,
    remove_cap,
    resume_user,
    toggle_blackout,
    update_away_message,
    update_cap_limit,
    update_user_settings,
    user_now,
)
from web.auth import AuthenticatedUser
from web.wellness_routes.deps import get_ctx, require_user

log = logging.getLogger("dungeonkeeper.wellness.web.api")

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────

_DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"]


def _blackout_to_dict(b) -> dict:
    days_str = "".join(
        _DAY_LETTERS[i] for i in range(7) if b.days_mask & (1 << i)
    )
    return {
        "id": b.id,
        "name": b.name,
        "enabled": b.enabled,
        "start_str": f"{b.start_minute // 60:02d}:{b.start_minute % 60:02d}",
        "end_str": f"{b.end_minute // 60:02d}:{b.end_minute % 60:02d}",
        "start_minute": b.start_minute,
        "end_minute": b.end_minute,
        "days_mask": b.days_mask,
        "days_str": days_str or "—",
    }


def _cap_to_dict(c) -> dict:
    return {
        "id": c.id,
        "label": c.label,
        "scope": c.scope,
        "scope_target_id": c.scope_target_id,
        "window": c.window,
        "limit": c.cap_limit,
        "exclude_exempt": c.exclude_exempt,
    }


# ── Read (GET) endpoints ──────────────────────────────────────────────

@router.get("/me")
async def wellness_me(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    """Current user's wellness overview — streak, counts, settings."""
    with ctx.open_db() as conn:
        wuser = get_wellness_user(conn, ctx.guild_id, user.user_id)
        if wuser is None or not wuser.is_active:
            return {"opted_in": False}

        now_local = user_now(wuser.timezone)
        today_iso = now_local.date().isoformat()
        streak = ensure_streak(conn, ctx.guild_id, user.user_id, today_iso)
        caps = list_caps(conn, ctx.guild_id, user.user_id)
        blackouts = list_blackouts(conn, ctx.guild_id, user.user_id)
        partnerships = list_partnerships(conn, ctx.guild_id, user.user_id, accepted_only=False)
        cfg = get_wellness_config(conn, ctx.guild_id)

    nxt = next_milestone(streak.current_days)
    next_text = f"{nxt[1]} {nxt[0]} days ({nxt[0] - streak.current_days} to go)" if nxt else "Top tier"

    return {
        "opted_in": True,
        "timezone": wuser.timezone,
        "enforcement_level": wuser.enforcement_level,
        "notifications_pref": wuser.notifications_pref,
        "public_commitment": wuser.public_commitment,
        "daily_reset_hour": wuser.daily_reset_hour,
        "slow_mode_rate_seconds": wuser.slow_mode_rate_seconds,
        "away_enabled": wuser.away_enabled,
        "away_message": wuser.away_message,
        "paused_until": wuser.paused_until,
        "streak": {
            "current_days": streak.current_days,
            "personal_best": streak.personal_best,
            "badge": streak.current_badge,
            "start_date": streak.streak_start_date,
        },
        "next_milestone_text": next_text,
        "caps_count": len(caps),
        "blackouts_count": len([b for b in blackouts if b.enabled]),
        "partners_count": len([p for p in partnerships if p.status == "accepted"]),
        "pending_partners_count": len([p for p in partnerships if p.status == "pending"]),
        "crisis_resource_url": cfg.crisis_resource_url if cfg else "",
        "enforcement_levels": ENFORCEMENT_LEVELS,
        "notification_prefs": NOTIFICATION_PREFS,
    }


@router.get("/caps")
async def get_caps(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        caps = list_caps(conn, ctx.guild_id, user.user_id)
    return {
        "caps": [_cap_to_dict(c) for c in caps],
        "scopes": CAP_SCOPES,
        "windows": CAP_WINDOWS,
    }


@router.get("/blackouts")
async def get_blackouts(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        blackouts = list_blackouts(conn, ctx.guild_id, user.user_id)
    return {
        "blackouts": [_blackout_to_dict(b) for b in blackouts],
        "templates": list(BLACKOUT_TEMPLATES.keys()),
    }


@router.get("/away")
async def get_away(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        wuser = get_wellness_user(conn, ctx.guild_id, user.user_id)
    if wuser is None or not wuser.is_active:
        return {"opted_in": False}
    return {
        "opted_in": True,
        "enabled": wuser.away_enabled,
        "message": wuser.away_message,
        "max_len": AWAY_MESSAGE_MAX_LEN,
    }


@router.get("/partners")
async def get_partners(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        partnerships = list_partnerships(conn, ctx.guild_id, user.user_id, accepted_only=False)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(ctx.guild_id) if bot else None

    rows = []
    for p in partnerships:
        other_id = p.other(user.user_id)
        name = f"User {other_id}"
        if guild:
            m = guild.get_member(other_id)
            if m:
                name = m.display_name
        rows.append({
            "id": p.id,
            "other_id": other_id,
            "other_name": name,
            "status": p.status,
            "is_requester": p.requester_id == user.user_id,
        })
    return {"partnerships": rows}


@router.get("/history")
async def get_history(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
):
    with ctx.open_db() as conn:
        rows = list_weekly_reports(conn, ctx.guild_id, user.user_id, limit=12)

    reports = []
    for r in rows:
        try:
            report_data = json.loads(r["report_json"])
        except (ValueError, TypeError):
            report_data = {}
        reports.append({
            "iso_year": int(r["iso_year"]),
            "iso_week": int(r["iso_week"]),
            "week_start": str(r["week_start"]),
            "ai_text": str(r["ai_text"]),
            "summary": report_data,
        })
    return {"reports": reports}


def _ok(**extra: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **extra})


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _require_active(ctx, user_id: int):
    """Returns the WellnessUser if active, otherwise raises HTTPException 403."""
    with ctx.open_db() as conn:
        wuser = get_wellness_user(conn, ctx.guild_id, user_id)
    if wuser is None or not wuser.is_active:
        raise HTTPException(status_code=403, detail="You must opt in to wellness first.")
    return wuser


# ── Settings ────────────────────────────────────────────────────────────

@router.post("/settings")
async def update_settings(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)

    timezone = payload.get("timezone")
    enforcement_level = payload.get("enforcement_level")
    notifications_pref = payload.get("notifications_pref")
    public_commitment = payload.get("public_commitment")
    daily_reset_hour = payload.get("daily_reset_hour")
    slow_mode_rate_seconds = payload.get("slow_mode_rate_seconds")

    if enforcement_level is not None and enforcement_level not in ENFORCEMENT_LEVELS:
        return _err("Invalid enforcement_level")
    if notifications_pref is not None and notifications_pref not in NOTIFICATION_PREFS:
        return _err("Invalid notifications_pref")
    if daily_reset_hour is not None:
        try:
            daily_reset_hour = int(daily_reset_hour)
        except (TypeError, ValueError):
            return _err("daily_reset_hour must be an integer 0-23")
        if not (0 <= daily_reset_hour < 24):
            return _err("daily_reset_hour must be between 0 and 23")
    if slow_mode_rate_seconds is not None:
        try:
            slow_mode_rate_seconds = int(slow_mode_rate_seconds)
        except (TypeError, ValueError):
            return _err("slow_mode_rate_seconds must be an integer")
        if slow_mode_rate_seconds < 1:
            return _err("slow_mode_rate_seconds must be ≥ 1")

    with ctx.open_db() as conn:
        update_user_settings(
            conn, ctx.guild_id, user.user_id,
            timezone=timezone if isinstance(timezone, str) and timezone else None,
            enforcement_level=enforcement_level,
            notifications_pref=notifications_pref,
            public_commitment=bool(public_commitment) if public_commitment is not None else None,
            daily_reset_hour=daily_reset_hour,
            slow_mode_rate_seconds=slow_mode_rate_seconds,
        )
    return _ok()


@router.post("/pause")
async def pause(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    try:
        minutes = int(payload.get("minutes", 0))
    except (TypeError, ValueError):
        return _err("minutes must be an integer")
    if minutes < 1 or minutes > 7 * 24 * 60:
        return _err("minutes must be between 1 and 10080 (7 days)")
    until = time.time() + minutes * 60
    with ctx.open_db() as conn:
        pause_user(conn, ctx.guild_id, user.user_id, until)
    return _ok(paused_until=until)


@router.post("/resume")
async def resume(
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    with ctx.open_db() as conn:
        resume_user(conn, ctx.guild_id, user.user_id)
    return _ok()


# ── Caps ────────────────────────────────────────────────────────────────

@router.post("/caps")
async def create_cap(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)

    label = str(payload.get("label", "")).strip()
    scope = str(payload.get("scope", "")).strip()
    window = str(payload.get("window", "")).strip()
    try:
        cap_limit = int(payload.get("limit", 0))
    except (TypeError, ValueError):
        return _err("limit must be an integer")
    try:
        scope_target_id = int(payload.get("scope_target_id", 0))
    except (TypeError, ValueError):
        scope_target_id = 0
    exclude_exempt = bool(payload.get("exclude_exempt", True))

    if not label:
        return _err("label is required")
    if scope not in CAP_SCOPES:
        return _err(f"scope must be one of {','.join(CAP_SCOPES)}")
    if window not in CAP_WINDOWS:
        return _err(f"window must be one of {','.join(CAP_WINDOWS)}")
    if cap_limit < 1:
        return _err("limit must be ≥ 1")
    if scope == "voice":
        return _err("voice scope is coming soon")

    with ctx.open_db() as conn:
        if find_cap_by_label(conn, ctx.guild_id, user.user_id, label):
            return _err(f"a cap named '{label}' already exists")
        cap_id = add_cap(
            conn, ctx.guild_id, user.user_id,
            label=label, scope=scope, scope_target_id=scope_target_id,
            window=window, cap_limit=cap_limit, exclude_exempt=exclude_exempt,
        )
    return _ok(id=cap_id)


@router.put("/caps/{cap_id}")
async def edit_cap(
    cap_id: int,
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    try:
        new_limit = int(payload.get("limit", 0))
    except (TypeError, ValueError):
        return _err("limit must be an integer")
    if new_limit < 1:
        return _err("limit must be ≥ 1")
    with ctx.open_db() as conn:
        cap = get_cap(conn, cap_id)
        if cap is None or cap.user_id != user.user_id or cap.guild_id != ctx.guild_id:
            return _err("cap not found", status=404)
        update_cap_limit(conn, cap_id, new_limit)
    return _ok()


@router.delete("/caps/{cap_id}")
async def delete_cap(
    cap_id: int,
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    with ctx.open_db() as conn:
        cap = get_cap(conn, cap_id)
        if cap is None or cap.user_id != user.user_id or cap.guild_id != ctx.guild_id:
            return _err("cap not found", status=404)
        remove_cap(conn, cap_id)
    return _ok()


# ── Blackouts ───────────────────────────────────────────────────────────

_DAY_NAME_TO_BIT = {
    "mon": 1, "tue": 2, "wed": 4, "thu": 8, "fri": 16, "sat": 32, "sun": 64,
}


def _parse_days_mask(value: Any) -> int:
    """Accept either an int days_mask, or a list of day names like ['mon', 'tue']."""
    if isinstance(value, int):
        return value & 0x7F
    if isinstance(value, list):
        mask = 0
        for d in value:
            mask |= _DAY_NAME_TO_BIT.get(str(d).lower()[:3], 0)
        return mask
    return 0


def _parse_minute(value: Any) -> int | None:
    """Accept either an int (minutes from midnight) or 'HH:MM' string."""
    if isinstance(value, int):
        return value if 0 <= value < 24 * 60 else None
    if isinstance(value, str) and ":" in value:
        try:
            h, m = value.split(":", 1)
            mins = int(h) * 60 + int(m)
            return mins if 0 <= mins < 24 * 60 else None
        except (ValueError, TypeError):
            return None
    return None


@router.post("/blackouts")
async def create_blackout(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)

    template_key = str(payload.get("template", "")).strip()
    start_minute: int | None
    end_minute: int | None
    if template_key:
        tpl = BLACKOUT_TEMPLATES.get(template_key)
        if not tpl:
            return _err(f"unknown template '{template_key}'")
        name = str(tpl["name"])
        start_minute = int(tpl["start_minute"])
        end_minute = int(tpl["end_minute"])
        days_mask = int(tpl["days_mask"])
    else:
        name = str(payload.get("name", "")).strip()
        if not name:
            return _err("name is required")
        start_minute = _parse_minute(payload.get("start"))
        end_minute = _parse_minute(payload.get("end"))
        if start_minute is None or end_minute is None:
            return _err("start/end must be 'HH:MM' or minutes-since-midnight")
        if start_minute == end_minute:
            return _err("start and end cannot be identical")
        days_mask = _parse_days_mask(payload.get("days"))
        if days_mask == 0:
            return _err("at least one day must be selected")

    with ctx.open_db() as conn:
        if find_blackout_by_name(conn, ctx.guild_id, user.user_id, name):
            return _err(f"a blackout named '{name}' already exists")
        blackout_id = add_blackout(
            conn, ctx.guild_id, user.user_id,
            name=name, start_minute=start_minute, end_minute=end_minute, days_mask=days_mask,
        )
    return _ok(id=blackout_id)


@router.put("/blackouts/{blackout_id}/toggle")
async def toggle_blackout_endpoint(
    blackout_id: int,
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    enabled = bool(payload.get("enabled", False))
    with ctx.open_db() as conn:
        # ownership check
        row = conn.execute(
            "SELECT user_id, guild_id FROM wellness_blackouts WHERE id = ?",
            (blackout_id,),
        ).fetchone()
        if row is None or int(row["user_id"]) != user.user_id or int(row["guild_id"]) != ctx.guild_id:
            return _err("blackout not found", status=404)
        toggle_blackout(conn, blackout_id, enabled)
    return _ok(enabled=enabled)


@router.delete("/blackouts/{blackout_id}")
async def delete_blackout(
    blackout_id: int,
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    with ctx.open_db() as conn:
        row = conn.execute(
            "SELECT user_id, guild_id FROM wellness_blackouts WHERE id = ?",
            (blackout_id,),
        ).fetchone()
        if row is None or int(row["user_id"]) != user.user_id or int(row["guild_id"]) != ctx.guild_id:
            return _err("blackout not found", status=404)
        remove_blackout(conn, blackout_id)
    return _ok()


# ── Away message ────────────────────────────────────────────────────────

@router.post("/away")
async def update_away(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    enabled = bool(payload.get("enabled", False))
    message = payload.get("message")
    if message is not None:
        message = str(message)
        if len(message) > AWAY_MESSAGE_MAX_LEN:
            return _err(f"message must be ≤ {AWAY_MESSAGE_MAX_LEN} characters")
    with ctx.open_db() as conn:
        update_away_message(
            conn, ctx.guild_id, user.user_id,
            enabled=enabled, message=message,
        )
    return _ok()


# ── Partners ────────────────────────────────────────────────────────────

@router.post("/partners/request")
async def request_partner(
    payload: dict = Body(...),
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    try:
        target_id = int(payload.get("user_id", 0))
    except (TypeError, ValueError):
        return _err("user_id must be a Discord user ID")
    if target_id <= 0:
        return _err("user_id is required")
    if target_id == user.user_id:
        return _err("you cannot partner with yourself")

    # Confirm target is also opted in
    with ctx.open_db() as conn:
        target = get_wellness_user(conn, ctx.guild_id, target_id)
        if target is None or not target.is_active:
            return _err("that user has not opted in to wellness")
        partner = create_partner_request(conn, ctx.guild_id, user.user_id, target_id)
    if partner is None:
        return _err("a partnership request already exists with that user")

    # Try to DM the target with the persistent buttons. The web flow
    # mirrors the slash command in commands/wellness_commands.py.
    bot = getattr(ctx, "bot", None)
    if bot:
        try:
            from services.wellness_partners import make_partner_request_view
            target_user = bot.get_user(target_id) or await bot.fetch_user(target_id)
            view = make_partner_request_view(partner.id)
            try:
                requester_name = user.username
            except AttributeError:
                requester_name = f"User {user.user_id}"
            await target_user.send(
                content=(
                    f"**{requester_name}** wants to be your wellness accountability partner. "
                    "If you accept, you'll both see each other's streaks and be able to "
                    "send each other supportive nudges. You can dissolve this anytime."
                ),
                view=view,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Could not DM partner request to %d: %s", target_id, e)
            with ctx.open_db() as conn:
                dissolve_partnership(conn, partner.id)
            return _err("could not DM that user — they may have DMs disabled")

    return _ok(id=partner.id)


@router.delete("/partners/{partner_id}")
async def delete_partnership(
    partner_id: int,
    user: AuthenticatedUser = Depends(require_user),
    ctx=Depends(get_ctx),
) -> JSONResponse:
    _require_active(ctx, user.user_id)
    with ctx.open_db() as conn:
        p = get_partnership(conn, partner_id)
        if p is None or p.guild_id != ctx.guild_id:
            return _err("partnership not found", status=404)
        if user.user_id not in (p.user_a, p.user_b):
            return _err("not your partnership", status=403)
        dissolve_partnership(conn, partner_id)
    return _ok()
