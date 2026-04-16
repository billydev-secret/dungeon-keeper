"""Config endpoints — read and update bot configuration from the dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from db_utils import (
    add_config_id,
    add_grant_permission,
    clear_config_id_bucket,
    delete_grant_role,
    get_config_id_set,
    get_config_value,
    get_grant_permissions,
    get_grant_roles,
    remove_grant_permission,
    set_config_value,
    upsert_grant_role,
)
from services.auto_delete_service import (
    format_duration_seconds as _fmt_dur,
)
from services.auto_delete_service import (
    list_auto_delete_rules_for_guild,
    remove_auto_delete_rule,
    upsert_auto_delete_rule,
)
from services.booster_roles import (
    delete_booster_role,
    get_booster_roles,
    upsert_booster_role,
)
from services.inactivity_prune_service import (
    add_prune_exception,
    get_prune_exception_ids,
    get_prune_rule as _get_prune_rule,
    remove_prune_exception,
)
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from xp_system import _XP_COEFF_PREFIX, DEFAULT_XP_SETTINGS

router = APIRouter()


def _require_primary_guild(request: Request) -> None:
    """Raise 403 if the active guild is not the primary (config-owning) guild."""
    ctx = request.app.state.ctx
    if get_active_guild_id(request) != ctx.guild_id:
        raise HTTPException(403, "Config editing is only available for the primary guild")


# ── Read helpers ───────────────────────────────────────────────────────


def _id_set_list(conn, bucket: str, guild_id: int) -> list[int]:
    return sorted(get_config_id_set(conn, bucket, guild_id))


def _int_val(conn, key: str, default: int = 0, guild_id: int = 0) -> int:
    raw = get_config_value(conn, key, str(default), guild_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _str_val(conn, key: str, default: str = "", guild_id: int = 0) -> str:
    return get_config_value(conn, key, default, guild_id)


def _float_val(conn, key: str, default: float = 0.0, guild_id: int = 0) -> float:
    raw = get_config_value(conn, key, str(default), guild_id)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _xp_coefficients(conn, guild_id: int = 0) -> dict:
    """Read XP algorithm coefficients from the config table, with defaults.

    Guild-scoped rows take precedence; falls back to ``guild_id=0`` legacy rows
    via ``get_config_value``.
    """
    from db_utils import get_config_value

    d = DEFAULT_XP_SETTINGS
    p = _XP_COEFF_PREFIX

    def _f(key: str, default: float) -> float:
        raw = get_config_value(conn, f"{p}{key}", str(default), guild_id)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _i(key: str, default: int) -> int:
        raw = get_config_value(conn, f"{p}{key}", str(default), guild_id)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def _s(key: str, default: str) -> str:
        return get_config_value(conn, f"{p}{key}", default, guild_id)

    return {
        "message_word_xp": _f("message_word_xp", d.message_word_xp),
        "reply_bonus_xp": _f("reply_bonus_xp", d.reply_bonus_xp),
        "image_reaction_received_xp": _f(
            "image_reaction_received_xp", d.image_reaction_received_xp
        ),
        "cooldown_thresholds_seconds": _s(
            "cooldown_thresholds_seconds",
            ",".join(str(v) for v in d.cooldown_thresholds_seconds),
        ),
        "cooldown_multipliers": _s(
            "cooldown_multipliers",
            ",".join(str(v) for v in d.cooldown_multipliers),
        ),
        "duplicate_multiplier": _f("duplicate_multiplier", d.duplicate_multiplier),
        "pair_streak_threshold": _i("pair_streak_threshold", d.pair_streak_threshold),
        "pair_streak_multiplier": _f(
            "pair_streak_multiplier", d.pair_streak_multiplier
        ),
        "voice_award_xp": _f("voice_award_xp", d.voice_award_xp),
        "voice_interval_seconds": _i(
            "voice_interval_seconds", d.voice_interval_seconds
        ),
        "voice_min_humans": _i("voice_min_humans", d.voice_min_humans),
        "manual_grant_xp": _f("manual_grant_xp", d.manual_grant_xp),
        "level_curve_factor": _f("level_curve_factor", d.level_curve_factor),
    }


# ── GET: full config snapshot ──────────────────────────────────────────


@router.get("/config")
async def get_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            from services.welcome_service import (
                DEFAULT_LEAVE_MESSAGE,
                DEFAULT_WELCOME_MESSAGE,
            )

            prune_rule = _get_prune_rule(ctx.db_path, guild_id)
            prune_exempt_ids = get_prune_exception_ids(ctx.db_path, guild_id)
            grant_roles = get_grant_roles(conn, guild_id)

            bot = getattr(ctx, "bot", None)
            prune_guild = bot.get_guild(guild_id) if bot is not None else None

            def _member_name(uid: int) -> str:
                if prune_guild is not None:
                    m = prune_guild.get_member(uid)
                    if m is not None:
                        return m.display_name
                row = conn.execute(
                    "SELECT display_name, username FROM known_users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, uid),
                ).fetchone()
                if row is not None:
                    return row["display_name"] or row["username"] or str(uid)
                return str(uid)

            exempt_users = [
                {"id": str(uid), "name": _member_name(uid)}
                for uid in sorted(prune_exempt_ids)
            ]

            return {
                "global": {
                    "guild_id": _int_val(conn, "guild_id", guild_id=guild_id),
                    "tz_offset_hours": _float_val(
                        conn, "tz_offset_hours", guild_id=guild_id
                    ),
                    "mod_channel_id": str(
                        _int_val(conn, "mod_channel_id", guild_id=guild_id)
                    ),
                    "bypass_role_ids": [
                        str(i) for i in _id_set_list(conn, "bypass_role_ids", guild_id)
                    ],
                    "recorded_bot_user_ids": [
                        str(i)
                        for i in _id_set_list(
                            conn, "recorded_bot_user_ids", guild_id
                        )
                    ],
                    "booster_swatch_dir": _str_val(
                        conn, "booster_swatch_dir", guild_id=guild_id
                    ),
                },
                "welcome": {
                    "welcome_channel_id": str(
                        _int_val(conn, "welcome_channel_id", guild_id=guild_id)
                    ),
                    "welcome_message": _str_val(
                        conn,
                        "welcome_message",
                        DEFAULT_WELCOME_MESSAGE,
                        guild_id=guild_id,
                    ),
                    "welcome_ping_role_id": str(
                        _int_val(conn, "welcome_ping_role_id", guild_id=guild_id)
                    ),
                    "leave_channel_id": str(
                        _int_val(conn, "leave_channel_id", guild_id=guild_id)
                    ),
                    "leave_message": _str_val(
                        conn,
                        "leave_message",
                        DEFAULT_LEAVE_MESSAGE,
                        guild_id=guild_id,
                    ),
                    "greeter_role_id": str(
                        _int_val(conn, "greeter_role_id", guild_id=guild_id)
                    ),
                    "greeter_chat_channel_id": str(
                        _int_val(conn, "greeter_chat_channel_id", guild_id=guild_id)
                    ),
                    "join_leave_log_channel_id": str(
                        _int_val(
                            conn,
                            "join_leave_log_channel_id",
                            _int_val(conn, "leave_channel_id", guild_id=guild_id),
                            guild_id=guild_id,
                        )
                    ),
                },
                "xp": {
                    "level_5_role_id": str(
                        _int_val(conn, "xp_level_5_role_id", guild_id=guild_id)
                    ),
                    "level_5_log_channel_id": str(
                        _int_val(
                            conn, "xp_level_5_log_channel_id", guild_id=guild_id
                        )
                    ),
                    "level_up_log_channel_id": str(
                        _int_val(
                            conn, "xp_level_up_log_channel_id", guild_id=guild_id
                        )
                    ),
                    "xp_grant_allowed_user_ids": [
                        str(i)
                        for i in _id_set_list(
                            conn, "xp_grant_allowed_user_ids", guild_id
                        )
                    ],
                    "xp_excluded_channel_ids": [
                        str(i)
                        for i in _id_set_list(
                            conn, "xp_excluded_channel_ids", guild_id
                        )
                    ],
                    # Algorithm coefficients (loaded with defaults)
                    **_xp_coefficients(conn, guild_id),
                },
                "prune": {
                    "role_id": str(prune_rule["role_id"]) if prune_rule else "0",
                    "inactivity_days": prune_rule["inactivity_days"]
                    if prune_rule
                    else 0,
                    "exemptions": exempt_users,
                },
                "spoiler": {
                    "spoiler_required_channels": [
                        str(i)
                        for i in _id_set_list(
                            conn, "spoiler_required_channels", guild_id
                        )
                    ],
                },
                "moderation": {
                    "jailed_role_id": str(
                        _int_val(conn, "jailed_role_id", guild_id=guild_id)
                    ),
                    "jail_category_id": str(
                        _int_val(conn, "jail_category_id", guild_id=guild_id)
                    ),
                    "ticket_category_id": str(
                        _int_val(conn, "ticket_category_id", guild_id=guild_id)
                    ),
                    "log_channel_id": str(
                        _int_val(conn, "log_channel_id", guild_id=guild_id)
                    ),
                    "transcript_channel_id": str(
                        _int_val(conn, "transcript_channel_id", guild_id=guild_id)
                    ),
                    "mod_role_ids": _str_val(
                        conn, "mod_role_ids", guild_id=guild_id
                    ),
                    "admin_role_ids": _str_val(
                        conn, "admin_role_ids", guild_id=guild_id
                    ),
                    "ticket_notify_on_create": _str_val(
                        conn,
                        "ticket_notify_on_create",
                        "1",
                        guild_id=guild_id,
                    ),
                    "warning_threshold": _int_val(
                        conn, "warning_threshold", 3, guild_id=guild_id
                    ),
                },
                "roles": {
                    name: {
                        "label": cfg["label"],
                        "role_id": str(cfg["role_id"]),
                        "log_channel_id": str(cfg["log_channel_id"]),
                        "announce_channel_id": str(cfg["announce_channel_id"]),
                        "grant_message": cfg["grant_message"],
                        "permissions": [
                            {"entity_type": et, "entity_id": str(eid)}
                            for et, eid in get_grant_permissions(
                                conn, guild_id, name
                            )
                        ],
                    }
                    for name, cfg in grant_roles.items()
                },
                "booster_roles": [
                    {
                        "role_key": r["role_key"],
                        "label": r["label"],
                        "role_id": str(r["role_id"]),
                        "image_path": r["image_path"],
                        "sort_order": r["sort_order"],
                    }
                    for r in get_booster_roles(conn, guild_id)
                ],
                "auto_delete": [
                    {
                        "channel_id": str(r["channel_id"]),
                        "max_age_seconds": int(r["max_age_seconds"]),
                        "interval_seconds": int(r["interval_seconds"]),
                        "last_run_ts": float(r["last_run_ts"]),
                        "max_age_display": _fmt_dur(int(r["max_age_seconds"])),
                        "interval_display": _fmt_dur(int(r["interval_seconds"])),
                    }
                    for r in list_auto_delete_rules_for_guild(ctx.db_path, guild_id)
                ],
            }

    return await run_query(_q)


# ── PUT: update a config section ───────────────────────────────────────


class GlobalConfigUpdate(BaseModel):
    tz_offset_hours: float | None = None
    mod_channel_id: str | None = None
    bypass_role_ids: list[str] | None = None
    recorded_bot_user_ids: list[str] | None = None
    booster_swatch_dir: str | None = None


@router.put("/config/global")
async def update_global(
    request: Request,
    body: GlobalConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            if body.tz_offset_hours is not None:
                set_config_value(
                    conn, "tz_offset_hours", str(body.tz_offset_hours), guild_id
                )
                ctx.tz_offset_hours = body.tz_offset_hours
            if body.mod_channel_id is not None:
                set_config_value(
                    conn, "mod_channel_id", body.mod_channel_id, guild_id
                )
                ctx.mod_channel_id = int(body.mod_channel_id)
            if body.bypass_role_ids is not None:
                clear_config_id_bucket(conn, "bypass_role_ids", guild_id)
                for rid in body.bypass_role_ids:
                    add_config_id(conn, "bypass_role_ids", int(rid), guild_id)
                ctx.bypass_role_ids = {int(r) for r in body.bypass_role_ids}
            if body.recorded_bot_user_ids is not None:
                clear_config_id_bucket(conn, "recorded_bot_user_ids", guild_id)
                for uid in body.recorded_bot_user_ids:
                    add_config_id(conn, "recorded_bot_user_ids", int(uid), guild_id)
                ctx.recorded_bot_user_ids = {
                    int(u) for u in body.recorded_bot_user_ids
                }
            if body.booster_swatch_dir is not None:
                set_config_value(
                    conn, "booster_swatch_dir", body.booster_swatch_dir, guild_id
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
    join_leave_log_channel_id: str | None = None


@router.put("/config/welcome")
async def update_welcome(
    request: Request,
    body: WelcomeConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    _FIELDS = {
        "welcome_channel_id": "welcome_channel_id",
        "welcome_message": "welcome_message",
        "welcome_ping_role_id": "welcome_ping_role_id",
        "leave_channel_id": "leave_channel_id",
        "leave_message": "leave_message",
        "greeter_role_id": "greeter_role_id",
        "greeter_chat_channel_id": "greeter_chat_channel_id",
        "join_leave_log_channel_id": "join_leave_log_channel_id",
    }

    def _q():
        with ctx.open_db() as conn:
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    set_config_value(conn, config_key, val, guild_id)
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
    # Algorithm coefficients
    message_word_xp: float | None = None
    reply_bonus_xp: float | None = None
    image_reaction_received_xp: float | None = None
    cooldown_thresholds_seconds: str | None = None
    cooldown_multipliers: str | None = None
    duplicate_multiplier: float | None = None
    pair_streak_threshold: int | None = None
    pair_streak_multiplier: float | None = None
    voice_award_xp: float | None = None
    voice_interval_seconds: int | None = None
    voice_min_humans: int | None = None
    manual_grant_xp: float | None = None
    level_curve_factor: float | None = None


@router.put("/config/xp")
async def update_xp(
    request: Request,
    body: XpConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            if body.level_5_role_id is not None:
                set_config_value(
                    conn, "xp_level_5_role_id", body.level_5_role_id, guild_id
                )
                ctx.level_5_role_id = int(body.level_5_role_id)
            if body.level_5_log_channel_id is not None:
                set_config_value(
                    conn,
                    "xp_level_5_log_channel_id",
                    body.level_5_log_channel_id,
                    guild_id,
                )
                ctx.level_5_log_channel_id = int(body.level_5_log_channel_id)
            if body.level_up_log_channel_id is not None:
                set_config_value(
                    conn,
                    "xp_level_up_log_channel_id",
                    body.level_up_log_channel_id,
                    guild_id,
                )
                ctx.level_up_log_channel_id = int(body.level_up_log_channel_id)
            if body.xp_grant_allowed_user_ids is not None:
                clear_config_id_bucket(
                    conn, "xp_grant_allowed_user_ids", guild_id
                )
                for uid in body.xp_grant_allowed_user_ids:
                    add_config_id(
                        conn, "xp_grant_allowed_user_ids", int(uid), guild_id
                    )
                ctx.xp_grant_allowed_user_ids = {
                    int(u) for u in body.xp_grant_allowed_user_ids
                }
            if body.xp_excluded_channel_ids is not None:
                clear_config_id_bucket(
                    conn, "xp_excluded_channel_ids", guild_id
                )
                for cid in body.xp_excluded_channel_ids:
                    add_config_id(
                        conn, "xp_excluded_channel_ids", int(cid), guild_id
                    )
                ctx.xp_excluded_channel_ids = {
                    int(c) for c in body.xp_excluded_channel_ids
                }

            # Persist algorithm coefficients
            _COEFF_FIELDS = [
                "message_word_xp",
                "reply_bonus_xp",
                "image_reaction_received_xp",
                "cooldown_thresholds_seconds",
                "cooldown_multipliers",
                "duplicate_multiplier",
                "pair_streak_threshold",
                "pair_streak_multiplier",
                "voice_award_xp",
                "voice_interval_seconds",
                "voice_min_humans",
                "manual_grant_xp",
                "level_curve_factor",
            ]
            for field_name in _COEFF_FIELDS:
                val = getattr(body, field_name, None)
                if val is not None:
                    set_config_value(
                        conn,
                        f"{_XP_COEFF_PREFIX}{field_name}",
                        str(val),
                        guild_id,
                    )

            # Reload live XP settings on ctx
            if hasattr(ctx, "reload_xp_settings"):
                ctx.reload_xp_settings()

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
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        from services.inactivity_prune_service import (
            remove_prune_rule,
            upsert_prune_rule,
        )

        if (
            body.role_id
            and body.inactivity_days
            and body.inactivity_days > 0
            and body.role_id != "0"
        ):
            upsert_prune_rule(
                ctx.db_path, guild_id, int(body.role_id), body.inactivity_days
            )
        elif body.role_id == "0" or (
            body.inactivity_days is not None and body.inactivity_days <= 0
        ):
            remove_prune_rule(ctx.db_path, guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.put("/config/prune/exemptions/{user_id}")
async def add_prune_exemption(
    user_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        add_prune_exception(ctx.db_path, guild_id, int(user_id))
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/prune/exemptions/{user_id}")
async def delete_prune_exemption(
    user_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        remove_prune_exception(ctx.db_path, guild_id, int(user_id))
        return {"ok": True}

    return await run_query(_q)


class PrunePreviewRequest(BaseModel):
    role_id: str
    inactivity_days: int
    exempt_user_ids: list[str] | None = None


@router.post("/config/prune/preview")
async def preview_prune(
    request: Request,
    body: PrunePreviewRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Return the members who would be pruned with the given settings.

    Does not perform any role changes. Uses saved exemptions unless
    ``exempt_user_ids`` is provided (allows preview of unsaved changes).
    """
    import time as _time

    from xp_system import get_member_last_activity_map

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    if not body.role_id or body.role_id == "0" or body.inactivity_days <= 0:
        return {"role_name": None, "candidates": []}

    role_id = int(body.role_id)
    inactivity_days = int(body.inactivity_days)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        raise HTTPException(503, "Discord guild not available")

    role = guild.get_role(role_id)
    if role is None:
        raise HTTPException(404, "Role not found")

    if body.exempt_user_ids is None:
        exempt_ids = get_prune_exception_ids(ctx.db_path, guild_id)
    else:
        exempt_ids = {int(u) for u in body.exempt_user_ids}

    candidates = [m for m in role.members if not m.bot and m.id not in exempt_ids]
    candidate_ids = [m.id for m in candidates]

    def _q():
        with ctx.open_db() as conn:
            return get_member_last_activity_map(conn, guild_id, candidate_ids)

    activity_map = await run_query(_q)

    now_ts = _time.time()
    cutoff_ts = now_ts - inactivity_days * 86400

    to_prune: list[dict] = []
    for member in candidates:
        activity = activity_map.get(member.id)
        if activity is None:
            continue
        if activity.created_at < cutoff_ts:
            days = (now_ts - activity.created_at) / 86400.0
            to_prune.append(
                {
                    "id": str(member.id),
                    "name": member.display_name,
                    "last_activity_ts": activity.created_at,
                    "days_inactive": round(days, 1),
                }
            )

    to_prune.sort(key=lambda x: x["last_activity_ts"])

    return {
        "role_name": role.name,
        "role_member_count": len(role.members),
        "considered_count": len(candidates),
        "candidates": to_prune,
    }


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
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

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
                    set_config_value(conn, config_key, val, guild_id)
            if body.warning_threshold is not None:
                set_config_value(
                    conn,
                    "warning_threshold",
                    str(body.warning_threshold),
                    guild_id,
                )
        return {"ok": True}

    return await run_query(_q)


class RoleGrantUpdate(BaseModel):
    label: str | None = None
    role_id: str | None = None
    log_channel_id: str | None = None
    announce_channel_id: str | None = None
    grant_message: str | None = None
    permissions: list[dict] | None = None


@router.put("/config/roles/{grant_name}")
async def update_role_grant(
    grant_name: str,
    request: Request,
    body: RoleGrantUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            existing = get_grant_roles(conn, guild_id)
            if grant_name in existing:
                cur = existing[grant_name]
                upsert_grant_role(
                    conn,
                    guild_id,
                    grant_name,
                    label=body.label if body.label is not None else cur["label"],
                    role_id=int(body.role_id)
                    if body.role_id is not None
                    else cur["role_id"],
                    log_channel_id=int(body.log_channel_id)
                    if body.log_channel_id is not None
                    else cur["log_channel_id"],
                    announce_channel_id=int(body.announce_channel_id)
                    if body.announce_channel_id is not None
                    else cur["announce_channel_id"],
                    grant_message=body.grant_message
                    if body.grant_message is not None
                    else cur["grant_message"],
                )
            else:
                upsert_grant_role(
                    conn,
                    guild_id,
                    grant_name,
                    label=body.label or grant_name.replace("_", " ").title(),
                    role_id=int(body.role_id) if body.role_id else 0,
                    log_channel_id=int(body.log_channel_id)
                    if body.log_channel_id
                    else 0,
                    announce_channel_id=int(body.announce_channel_id)
                    if body.announce_channel_id
                    else 0,
                    grant_message=body.grant_message or "",
                )
            if body.permissions is not None:
                for et, eid in get_grant_permissions(conn, guild_id, grant_name):
                    remove_grant_permission(conn, guild_id, grant_name, et, eid)
                for perm in body.permissions:
                    add_grant_permission(
                        conn,
                        guild_id,
                        grant_name,
                        perm["entity_type"],
                        int(perm["entity_id"]),
                    )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/roles/{grant_name}")
async def delete_role_grant(
    grant_name: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            delete_grant_role(conn, guild_id, grant_name)
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
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            if body.spoiler_required_channels is not None:
                clear_config_id_bucket(conn, "spoiler_required_channels", guild_id)
                for cid in body.spoiler_required_channels:
                    add_config_id(
                        conn, "spoiler_required_channels", int(cid), guild_id
                    )
                ctx.spoiler_required_channels = {
                    int(c) for c in body.spoiler_required_channels
                }
        return {"ok": True}

    return await run_query(_q)


# ── Booster roles ─────────────────────────────────────────────────────


class BoosterRoleUpdate(BaseModel):
    label: str
    role_id: str
    image_path: str = ""
    sort_order: int = 0


@router.put("/config/booster-roles/{role_key}")
async def update_booster_role(
    role_key: str,
    request: Request,
    body: BoosterRoleUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            upsert_booster_role(
                conn,
                guild_id,
                role_key,
                label=body.label,
                role_id=int(body.role_id) if body.role_id else 0,
                image_path=body.image_path,
                sort_order=body.sort_order,
            )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/booster-roles/{role_key}")
async def remove_booster_role(
    role_key: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        with ctx.open_db() as conn:
            delete_booster_role(conn, guild_id, role_key)
        return {"ok": True}

    return await run_query(_q)


# ── Auto-delete schedules ────────────────────────────────────────────


class AutoDeleteRuleUpdate(BaseModel):
    max_age_seconds: int
    interval_seconds: int


@router.put("/config/auto-delete/{channel_id}")
async def update_auto_delete(
    channel_id: str,
    request: Request,
    body: AutoDeleteRuleUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        upsert_auto_delete_rule(
            ctx.db_path,
            guild_id,
            int(channel_id),
            body.max_age_seconds,
            body.interval_seconds,
        )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/auto-delete/{channel_id}")
async def remove_auto_delete(
    channel_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    _require_primary_guild(request)

    def _q():
        remove_auto_delete_rule(ctx.db_path, guild_id, int(channel_id))
        return {"ok": True}

    return await run_query(_q)
