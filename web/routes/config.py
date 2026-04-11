"""Config endpoints — read and update bot configuration from the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from db_utils import (
    get_config_id_set,
    get_config_value,
    get_grant_roles,
)
from services.inactivity_prune_service import get_prune_rule as _get_prune_rule
from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query

router = APIRouter()


# ── Read helpers ───────────────────────────────────────────────────────

def _id_set_list(conn, bucket: str) -> list[int]:
    return sorted(get_config_id_set(conn, bucket))


def _int_val(conn, key: str, default: int = 0) -> int:
    raw = get_config_value(conn, key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _str_val(conn, key: str, default: str = "") -> str:
    return get_config_value(conn, key, default)


def _float_val(conn, key: str, default: float = 0.0) -> float:
    raw = get_config_value(conn, key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# ── GET: full config snapshot ──────────────────────────────────────────

@router.get("/config")
async def get_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            from services.welcome_service import DEFAULT_LEAVE_MESSAGE, DEFAULT_WELCOME_MESSAGE

            prune_rule = _get_prune_rule(ctx.db_path, ctx.guild_id)
            grant_roles = get_grant_roles(conn, ctx.guild_id)

            return {
                "global": {
                    "guild_id": _int_val(conn, "guild_id"),
                    "tz_offset_hours": _float_val(conn, "tz_offset_hours"),
                    "mod_channel_id": str(_int_val(conn, "mod_channel_id")),
                    "bypass_role_ids": [str(i) for i in _id_set_list(conn, "bypass_role_ids")],
                    "booster_swatch_dir": _str_val(conn, "booster_swatch_dir"),
                },
                "welcome": {
                    "welcome_channel_id": str(_int_val(conn, "welcome_channel_id")),
                    "welcome_message": _str_val(conn, "welcome_message", DEFAULT_WELCOME_MESSAGE),
                    "welcome_ping_role_id": str(_int_val(conn, "welcome_ping_role_id")),
                    "leave_channel_id": str(_int_val(conn, "leave_channel_id")),
                    "leave_message": _str_val(conn, "leave_message", DEFAULT_LEAVE_MESSAGE),
                    "greeter_role_id": str(_int_val(conn, "greeter_role_id")),
                    "greeter_chat_channel_id": str(_int_val(conn, "greeter_chat_channel_id")),
                },
                "xp": {
                    "level_5_role_id": str(_int_val(conn, "xp_level_5_role_id")),
                    "level_5_log_channel_id": str(_int_val(conn, "xp_level_5_log_channel_id")),
                    "level_up_log_channel_id": str(_int_val(conn, "xp_level_up_log_channel_id")),
                    "xp_grant_allowed_user_ids": [str(i) for i in _id_set_list(conn, "xp_grant_allowed_user_ids")],
                    "xp_excluded_channel_ids": [str(i) for i in _id_set_list(conn, "xp_excluded_channel_ids")],
                },
                "prune": {
                    "role_id": str(prune_rule["role_id"]) if prune_rule else "0",
                    "inactivity_days": prune_rule["inactivity_days"] if prune_rule else 0,
                },
                "spoiler": {
                    "spoiler_required_channels": [str(i) for i in _id_set_list(conn, "spoiler_required_channels")],
                },
                "moderation": {
                    "jailed_role_id": str(_int_val(conn, "jailed_role_id")),
                    "jail_category_id": str(_int_val(conn, "jail_category_id")),
                    "ticket_category_id": str(_int_val(conn, "ticket_category_id")),
                    "log_channel_id": str(_int_val(conn, "log_channel_id")),
                    "transcript_channel_id": str(_int_val(conn, "transcript_channel_id")),
                    "mod_role_ids": _str_val(conn, "mod_role_ids"),
                    "admin_role_ids": _str_val(conn, "admin_role_ids"),
                    "ticket_notify_on_create": _str_val(conn, "ticket_notify_on_create", "1"),
                    "warning_threshold": _int_val(conn, "warning_threshold", 3),
                },
                "roles": {
                    name: {
                        "label": cfg["label"],
                        "role_id": str(cfg["role_id"]),
                        "log_channel_id": str(cfg["log_channel_id"]),
                        "announce_channel_id": str(cfg["announce_channel_id"]),
                        "grant_message": cfg["grant_message"],
                    }
                    for name, cfg in grant_roles.items()
                },
            }

    return await run_query(_q)


# ── PUT: update a config section ───────────────────────────────────────

class GlobalConfigUpdate(BaseModel):
    tz_offset_hours: float | None = None
    mod_channel_id: str | None = None
    bypass_role_ids: list[str] | None = None
    booster_swatch_dir: str | None = None


@router.put("/config/global")
async def update_global(
    request: Request,
    body: GlobalConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if body.tz_offset_hours is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("tz_offset_hours", str(body.tz_offset_hours)),
                )
                ctx.tz_offset_hours = body.tz_offset_hours
            if body.mod_channel_id is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("mod_channel_id", body.mod_channel_id),
                )
                ctx.mod_channel_id = int(body.mod_channel_id)
            if body.bypass_role_ids is not None:
                conn.execute("DELETE FROM config_ids WHERE bucket = ?", ("bypass_role_ids",))
                for rid in body.bypass_role_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)",
                        ("bypass_role_ids", int(rid)),
                    )
                ctx.bypass_role_ids = {int(r) for r in body.bypass_role_ids}
            if body.booster_swatch_dir is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("booster_swatch_dir", body.booster_swatch_dir),
                )
        return {"ok": True}

    return await run_query(_q)


class WelcomeConfigUpdate(BaseModel):
    welcome_channel_id: str | None = None
    welcome_message: str | None = None
    welcome_ping_role_id: str | None = None
    leave_channel_id: str | None = None
    leave_message: str | None = None
    greeter_role_id: str | None = None
    greeter_chat_channel_id: str | None = None


@router.put("/config/welcome")
async def update_welcome(
    request: Request,
    body: WelcomeConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    _FIELDS = {
        "welcome_channel_id": "welcome_channel_id",
        "welcome_message": "welcome_message",
        "welcome_ping_role_id": "welcome_ping_role_id",
        "leave_channel_id": "leave_channel_id",
        "leave_message": "leave_message",
        "greeter_role_id": "greeter_role_id",
        "greeter_chat_channel_id": "greeter_chat_channel_id",
    }

    def _q():
        with ctx.open_db() as conn:
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    conn.execute(
                        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (config_key, val),
                    )
                    # Update live context for int fields
                    if hasattr(ctx, config_key):
                        try:
                            setattr(ctx, config_key, int(val))
                        except ValueError:
                            setattr(ctx, config_key, val)
        return {"ok": True}

    return await run_query(_q)


class XpConfigUpdate(BaseModel):
    level_5_role_id: str | None = None
    level_5_log_channel_id: str | None = None
    level_up_log_channel_id: str | None = None
    xp_grant_allowed_user_ids: list[str] | None = None
    xp_excluded_channel_ids: list[str] | None = None


@router.put("/config/xp")
async def update_xp(
    request: Request,
    body: XpConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if body.level_5_role_id is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("xp_level_5_role_id", body.level_5_role_id),
                )
                ctx.level_5_role_id = int(body.level_5_role_id)
            if body.level_5_log_channel_id is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("xp_level_5_log_channel_id", body.level_5_log_channel_id),
                )
                ctx.level_5_log_channel_id = int(body.level_5_log_channel_id)
            if body.level_up_log_channel_id is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("xp_level_up_log_channel_id", body.level_up_log_channel_id),
                )
                ctx.level_up_log_channel_id = int(body.level_up_log_channel_id)
            if body.xp_grant_allowed_user_ids is not None:
                conn.execute("DELETE FROM config_ids WHERE bucket = ?", ("xp_grant_allowed_user_ids",))
                for uid in body.xp_grant_allowed_user_ids:
                    conn.execute("INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)",
                                 ("xp_grant_allowed_user_ids", int(uid)))
                ctx.xp_grant_allowed_user_ids = {int(u) for u in body.xp_grant_allowed_user_ids}
            if body.xp_excluded_channel_ids is not None:
                conn.execute("DELETE FROM config_ids WHERE bucket = ?", ("xp_excluded_channel_ids",))
                for cid in body.xp_excluded_channel_ids:
                    conn.execute("INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)",
                                 ("xp_excluded_channel_ids", int(cid)))
                ctx.xp_excluded_channel_ids = {int(c) for c in body.xp_excluded_channel_ids}
        return {"ok": True}

    return await run_query(_q)


class PruneConfigUpdate(BaseModel):
    role_id: str | None = None
    inactivity_days: int | None = None


@router.put("/config/prune")
async def update_prune(
    request: Request,
    body: PruneConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.inactivity_prune_service import remove_prune_rule, upsert_prune_rule
        if body.role_id and body.inactivity_days and body.inactivity_days > 0 and body.role_id != "0":
            upsert_prune_rule(ctx.db_path, ctx.guild_id, int(body.role_id), body.inactivity_days)
        elif body.role_id == "0" or (body.inactivity_days is not None and body.inactivity_days <= 0):
            remove_prune_rule(ctx.db_path, ctx.guild_id)
        return {"ok": True}

    return await run_query(_q)


class ModerationConfigUpdate(BaseModel):
    jailed_role_id: str | None = None
    jail_category_id: str | None = None
    ticket_category_id: str | None = None
    log_channel_id: str | None = None
    transcript_channel_id: str | None = None
    mod_role_ids: str | None = None
    admin_role_ids: str | None = None
    ticket_notify_on_create: str | None = None
    warning_threshold: int | None = None


@router.put("/config/moderation")
async def update_moderation(
    request: Request,
    body: ModerationConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    _FIELDS = {
        "jailed_role_id": "jailed_role_id",
        "jail_category_id": "jail_category_id",
        "ticket_category_id": "ticket_category_id",
        "log_channel_id": "log_channel_id",
        "transcript_channel_id": "transcript_channel_id",
        "mod_role_ids": "mod_role_ids",
        "admin_role_ids": "admin_role_ids",
        "ticket_notify_on_create": "ticket_notify_on_create",
    }

    def _q():
        with ctx.open_db() as conn:
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    conn.execute(
                        "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (config_key, val),
                    )
            if body.warning_threshold is not None:
                conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    ("warning_threshold", str(body.warning_threshold)),
                )
        return {"ok": True}

    return await run_query(_q)


class RoleGrantUpdate(BaseModel):
    role_id: str | None = None
    log_channel_id: str | None = None
    announce_channel_id: str | None = None
    grant_message: str | None = None


@router.put("/config/roles/{grant_name}")
async def update_role_grant(
    grant_name: str,
    request: Request,
    body: RoleGrantUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from db_utils import upsert_grant_role
        with ctx.open_db() as conn:
            # Read existing values so partial updates work
            existing = get_grant_roles(conn, ctx.guild_id)
            if grant_name not in existing:
                return {"ok": False, "detail": f"Unknown grant role: {grant_name}"}
            cur = existing[grant_name]
            upsert_grant_role(
                conn, ctx.guild_id, grant_name,
                label=cur["label"],
                role_id=int(body.role_id) if body.role_id is not None else cur["role_id"],
                log_channel_id=int(body.log_channel_id) if body.log_channel_id is not None else cur["log_channel_id"],
                announce_channel_id=int(body.announce_channel_id) if body.announce_channel_id is not None else cur["announce_channel_id"],
                grant_message=body.grant_message if body.grant_message is not None else cur["grant_message"],
            )
        return {"ok": True}

    return await run_query(_q)


class SpoilerConfigUpdate(BaseModel):
    spoiler_required_channels: list[str] | None = None


@router.put("/config/spoiler")
async def update_spoiler(
    request: Request,
    body: SpoilerConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            if body.spoiler_required_channels is not None:
                conn.execute("DELETE FROM config_ids WHERE bucket = ?", ("spoiler_required_channels",))
                for cid in body.spoiler_required_channels:
                    conn.execute("INSERT OR IGNORE INTO config_ids (bucket, value) VALUES (?, ?)",
                                 ("spoiler_required_channels", int(cid)))
                ctx.spoiler_required_channels = {int(c) for c in body.spoiler_required_channels}
        return {"ok": True}

    return await run_query(_q)
