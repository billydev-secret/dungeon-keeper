"""Config endpoints — read and update bot configuration from the dashboard."""

from __future__ import annotations

import io
import os
from datetime import date
import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from bot_modules.core.db_utils import (
    add_config_id,
    add_grant_permission,
    clear_config_id_bucket,
    delete_grant_role,
    get_config_id_set,
    get_config_value,
    get_grant_permissions,
    get_grant_roles,
    open_db,
    parse_bool,
    remove_grant_permission,
    set_config_value,
    upsert_grant_role,
)
from bot_modules.services.message_store import (
    SUPPORTED_STORAGE_LEVELS,
    STORAGE_LEVEL_NONE,
    purge_guild_message_content,
)
from bot_modules.services.auto_delete_service import (
    format_duration_seconds as _fmt_dur,
)
from bot_modules.services.auto_delete_service import (
    list_auto_delete_rules_for_guild_with_conn,
    remove_auto_delete_rule,
    upsert_auto_delete_rule,
)
from bot_modules.services.auto_react_service import (
    list_auto_react_rules_for_guild_with_conn,
    parse_emojis,
    remove_auto_react_rule,
    upsert_auto_react_rule,
)
from bot_modules.services.booster_roles import (
    _IMAGE_EXTS,
    delete_booster_role,
    get_booster_panel_refs,
    get_booster_roles,
    get_guild_swatch_dir,
    post_or_update_booster_panel,
    resolve_swatch_directory,
    swatch_file_info,
    sync_swatches,
    upsert_booster_role,
)
from bot_modules.services.quote_renderer import (
    BorderStyle,
    analyze_border_opening,
    guild_border_path,
)
from bot_modules.services.inactivity_prune_service import (
    add_prune_exception,
    get_prune_exception_ids,
    get_prune_rule as _get_prune_rule,
    remove_prune_exception,
)
from bot_modules.services.dm_perms_service import (
    get_dm_mode_role_ids,
    get_dms_config_with_conn,
    set_audit_channel,
    set_dm_mode_role_ids,
    set_panel_settings,
    set_request_channel,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_game_host, require_perms, run_query
from bot_modules.core.xp_system import _XP_COEFF_PREFIX, DEFAULT_XP_SETTINGS
from bot_modules.services.confessions_service import (
    GuildConfig as _ConfessionsGuildConfig,
    get_config as _confessions_get_config,
    get_config_conn as _confessions_get_config_conn,
    upsert_config as _confessions_upsert_config,
)
from bot_modules.services.branding_service import (
    ACCENT_MODE_AVATAR,
    ACCENT_MODE_CUSTOM,
    ACCENT_HEX_UNSET,
    get_branding_conn,
    upsert_branding,
)
from bot_modules.core.branding import invalidate_accent_cache
from bot_modules.services.starboard_service import (
    get_starboard_config as _get_starboard_config,
    upsert_starboard_config as _upsert_starboard_config,
)
from bot_modules.services.guess_repo import get_guess_config as _get_guess_config
from bot_modules.services.whisper_repo import get_whisper_config as _get_whisper_config
from bot_modules.cogs.needle_cog import (
    _delete_channel as _needle_delete_channel,
    _get_global_config as _needle_get_global_config,
    _list_channels as _needle_list_channels,
    _upsert_channel as _needle_upsert_channel,
)
from bot_modules.cogs.bump_tracker_cog import (
    _add_site as _bump_add_site,
    _get_config as _bump_get_config,
    _get_all_logs as _bump_get_all_logs,
    _list_sites as _bump_list_sites,
    _log_bump as _bump_log_bump,
    _remove_site as _bump_remove_site,
    _set_detector as _bump_set_detector,
    _upsert_config as _bump_upsert_config,
)
from bot_modules.starboard.filters import validate_emoji as _starboard_validate_emoji
from bot_modules.cogs.pen_pals_cog import (
    _get_config as _pp_get_config,
    _get_pool as _pp_get_pool,
    _set_config as _pp_set_config,
)
from bot_modules.services.voice_transcription_service import (
    DEFAULT_MODEL as _VT_DEFAULT_MODEL,
    VALID_MODELS as _VT_VALID_MODELS,
    download_model_to_cache as _vt_download_model,
    get_config as _vt_get_config,
    is_available as _vt_is_available,
    model_is_cached as _vt_model_is_cached,
    set_config as _vt_set_config,
)
from bot_modules.services.ollama_client import is_available as _ollama_is_available

_STARBOARD_EXCLUDED_BUCKET = "starboard_excluded_channels"
_RISKY_PING_KEY = "risky_ping_role_id"
_RISKY_MIN_GAME_KEY = "risky_min_game_seconds"
_BIRTHDAY_DEFAULT_MESSAGE = "Happy birthday, {mention}! 🎂\n{request}"
_POLICY_VOTE_TIMEOUT_KEY = "policy_vote_timeout_hours"
_POLICY_VOTE_TIMEOUT_DEFAULT = 72

router = APIRouter()


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


def _bool_val(conn, key: str, default: bool = False, guild_id: int = 0) -> bool:
    raw = get_config_value(conn, key, "1" if default else "0", guild_id)
    return parse_bool(raw)


def _xp_coefficients(conn, guild_id: int = 0) -> dict:
    """Read XP algorithm coefficients from the config table, with defaults.

    Guild-scoped rows take precedence; falls back to ``guild_id=0`` legacy rows
    via ``get_config_value``.
    """
    from bot_modules.core.db_utils import get_config_value

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
        "reaction_given_xp": _f("reaction_given_xp", d.reaction_given_xp),
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


def _lookup_member_name(uid: int, guild, conn, guild_id: int) -> str:
    if guild:
        m = guild.get_member(uid)
        if m:
            return m.display_name
    row = conn.execute(
        "SELECT display_name, username FROM known_users WHERE guild_id = ? AND user_id = ?",
        (guild_id, uid),
    ).fetchone()
    return (row["display_name"] or row["username"] or str(uid)) if row else str(uid)


# ── DM perms config helper ────────────────────────────────────────────


def _dms_section_with_conn(conn, guild_id: int) -> dict:
    cfg = get_dms_config_with_conn(conn, guild_id)
    return {k: str(v) for k, v in cfg.items()}


# ── Starboard config helper ──────────────────────────────────────────


def _bulk_cleanup_section(conn, guild_id: int) -> dict:
    from bot_modules.services.bulk_cleanup_service import (
        DEFAULT_AGE_DAYS,
        EXCLUDED_BUCKET,
        _AGE_DAYS_KEY,
        _ENABLED_KEY,
        _LAST_RUN_KEY,
    )

    enabled = (
        get_config_value(
            conn, _ENABLED_KEY, "0", guild_id, allow_legacy_fallback=False
        )
        == "1"
    )
    try:
        age_days = int(
            get_config_value(
                conn,
                _AGE_DAYS_KEY,
                str(DEFAULT_AGE_DAYS),
                guild_id,
                allow_legacy_fallback=False,
            )
        )
    except (TypeError, ValueError):
        age_days = DEFAULT_AGE_DAYS
    try:
        last_run_ts = float(
            get_config_value(
                conn, _LAST_RUN_KEY, "0", guild_id, allow_legacy_fallback=False
            )
        )
    except (TypeError, ValueError):
        last_run_ts = 0.0

    return {
        "enabled": enabled,
        "age_days": age_days,
        "last_run_ts": last_run_ts,
        "excluded_channels": [
            str(i)
            for i in sorted(
                get_config_id_set(
                    conn, EXCLUDED_BUCKET, guild_id, allow_legacy_fallback=False
                )
            )
        ],
    }


def _starboard_section(conn, guild_id: int) -> dict:
    import sqlite3

    try:
        row = _get_starboard_config(conn, guild_id)
    except sqlite3.OperationalError:
        row = None
    if row:
        channel_id = int(row["channel_id"])
        threshold = int(row["threshold"])
        emoji = row["emoji"]
        enabled = bool(row["enabled"])
    else:
        channel_id = 0
        threshold = 3
        emoji = "⭐"
        enabled = True
    return {
        "channel_id": str(channel_id),
        "threshold": threshold,
        "emoji": emoji,
        "enabled": enabled,
        "excluded_channels": [
            str(i) for i in _id_set_list(conn, _STARBOARD_EXCLUDED_BUCKET, guild_id)
        ],
    }


# ── Birthday config helper ────────────────────────────────────────────


def _birthday_section(conn, guild_id: int) -> dict:
    return {
        "birthday_channel_id": str(
            _int_val(conn, "birthday_channel_id", guild_id=guild_id)
        ),
        "birthday_message": _str_val(
            conn, "birthday_message", _BIRTHDAY_DEFAULT_MESSAGE, guild_id=guild_id
        ),
        "birthday_pin": _bool_val(conn, "birthday_pin", guild_id=guild_id),
        "birthday_channel_id_2": str(
            _int_val(conn, "birthday_channel_id_2", guild_id=guild_id)
        ),
        "birthday_message_2": _str_val(
            conn, "birthday_message_2", _BIRTHDAY_DEFAULT_MESSAGE, guild_id=guild_id
        ),
        "birthday_pin_2": _bool_val(conn, "birthday_pin_2", guild_id=guild_id),
    }


# ── Bot identity section ─────────────────────────────────────────


def _bot_identity_section(guild) -> dict:
    if guild is None:
        return {"nick": "", "avatar_url": ""}
    return {
        "nick": guild.me.nick or "",
        "avatar_url": str(guild.me.display_avatar.url),
    }


def _branding_section(conn, guild_id: int) -> dict:
    cfg = get_branding_conn(conn, guild_id)
    accent_hex = f"#{cfg.accent_hex:06X}" if cfg.has_custom_colour() else ""
    return {
        "accent_mode": cfg.normalized_mode(),
        "accent_hex": accent_hex,
    }


def _guess_section(conn, guild_id: int) -> dict:
    gc = _get_guess_config(conn, guild_id)
    return {
        "channel_id": str(gc.guess_channel_id),
        "role_id": str(gc.guess_role_id),
        "crop_difficulty": gc.crop_difficulty,
        "guess_cooldown_seconds": gc.guess_cooldown_seconds,
        "min_image_dimension_px": gc.min_image_dimension_px,
        "max_image_size_mb": gc.max_image_size_mb,
    }


def _needle_section(conn, guild_id: int) -> dict:
    channels = _needle_list_channels(conn, guild_id)
    gcfg = _needle_get_global_config(conn, guild_id)
    return {
        "channels": [
            {
                "channel_id": str(c.channel_id),
                "title_type": c.title_type,
                "custom_title": c.custom_title,
                "include_bots": c.include_bots,
                "slowmode": c.slowmode,
                "delete_behavior": c.delete_behavior,
                "reply_type": c.reply_type,
                "custom_reply": c.custom_reply,
                "status_reactions": c.status_reactions,
                "archive_immediately": c.archive_immediately,
                "default_reactions": c.default_reactions,
            }
            for c in channels
        ],
        "emoji_unanswered": gcfg.emoji_unanswered,
        "emoji_archived": gcfg.emoji_archived,
        "emoji_locked": gcfg.emoji_locked,
        "default_reply": gcfg.default_reply,
    }


def _whisper_section(conn, guild_id: int) -> dict:
    wc = _get_whisper_config(conn, guild_id)
    return {
        "channel_id": str(wc.channel_id),
        "role_id": str(wc.role_id),
        "log_channel_id": str(wc.log_channel_id),
    }


def _risky_section(conn, guild_id: int) -> dict:
    ping_role = get_config_value(conn, _RISKY_PING_KEY, "0", guild_id=guild_id)
    min_secs = get_config_value(conn, _RISKY_MIN_GAME_KEY, "0", guild_id=guild_id)
    return {
        "ping_role_id": ping_role,
        "min_game_seconds": int(min_secs),
    }


def _policy_section(conn, guild_id: int) -> dict:
    return {
        "vote_timeout_hours": _int_val(
            conn,
            _POLICY_VOTE_TIMEOUT_KEY,
            _POLICY_VOTE_TIMEOUT_DEFAULT,
            guild_id=guild_id,
        ),
    }


def _greeting_watch_section(conn, guild_id: int) -> dict:
    # Read the same keys the ingest hook (GuildConfig) and the monitor loop act
    # on, so the panel never disagrees with what actually fires. channel_ids is
    # a CSV of watched-channel ids (same shape as mod_role_ids).
    return {
        "enabled": _bool_val(conn, "greeting_watch_enabled", guild_id=guild_id),
        "channel_ids": _str_val(
            conn, "greeting_watch_channel_ids", guild_id=guild_id
        ),
        "notify_user_id": str(
            _int_val(conn, "greeting_watch_notify_user_id", guild_id=guild_id)
        ),
        "window_minutes": _int_val(
            conn, "greeting_watch_window_minutes", 10, guild_id=guild_id
        ),
    }


def _rules_watch_section(conn, guild_id: int, db_path) -> dict:
    # Read these the same way the monitor does (get_config_value with the
    # default legacy fallback) so the panel never disagrees with what the
    # listener actually acts on. ``guard_available`` mirrors the Ollama gate in
    # monitor._process — enabling with no guard model records nothing, so we
    # surface it read-only to explain a still-empty queue.
    return {
        "enabled": _bool_val(conn, "rules_watch_enabled", guild_id=guild_id),
        "channel_id": str(
            _int_val(conn, "rules_watch_channel_id", guild_id=guild_id)
        ),
        "guard_available": _ollama_is_available(db_path),
    }


# ── Auto-react config helper ──────────────────────────────────────────


def _auto_react_section(conn, guild_id: int) -> list:
    return [
        {
            "channel_id": str(r["channel_id"]),
            "emojis": parse_emojis(r["emojis"]),
            "enabled": bool(r["enabled"]),
        }
        for r in list_auto_react_rules_for_guild_with_conn(conn, guild_id)
    ]


def _pen_pals_section(conn, guild_id: int) -> dict:
    cfg = _pp_get_config(conn, guild_id)
    pool_size = len(_pp_get_pool(conn, guild_id))
    if cfg is None:
        return {
            "enabled": False,
            "category_id": None,
            "opt_in_role_id": None,
            "question_category": "sfw",
            "log_channel_id": None,
            "auto_round_dow": -1,
            "auto_round_hour": 12,
            "panel_channel_id": None,
            "pool_size": pool_size,
        }
    return {
        "enabled": bool(cfg["enabled"]),
        "category_id": str(cfg["category_id"]) if cfg["category_id"] else None,
        "opt_in_role_id": str(cfg["opt_in_role_id"]) if cfg["opt_in_role_id"] else None,
        "question_category": cfg["question_category"] or "sfw",
        "log_channel_id": str(cfg["log_channel_id"]) if cfg["log_channel_id"] else None,
        "auto_round_dow": int(cfg["auto_round_dow"]),
        "auto_round_hour": int(cfg["auto_round_hour"]),
        "panel_channel_id": str(cfg["panel_channel_id"]) if cfg["panel_channel_id"] else None,
        "pool_size": pool_size,
    }


def _vt_models_status() -> list[dict]:
    return [{"name": m, "cached": _vt_model_is_cached(m)} for m in _VT_VALID_MODELS]


def _voice_transcription_section(conn, guild_id: int) -> dict:
    cfg = _vt_get_config(conn, guild_id)
    if cfg is None:
        return {
            "enabled": False,
            "model_name": _VT_DEFAULT_MODEL,
            "channel_ids": [],
            "available": _vt_is_available(),
            "models": _vt_models_status(),
        }
    return {
        "enabled": cfg.enabled,
        "model_name": cfg.model_name,
        "channel_ids": [str(c) for c in cfg.channel_ids],
        "available": _vt_is_available(),
        "models": _vt_models_status(),
    }


def _bump_tracker_section(conn, guild_id: int) -> dict:
    import time as _time
    cfg = _bump_get_config(conn, guild_id)
    site_rows = _bump_list_sites(conn, guild_id)
    logs = _bump_get_all_logs(conn, guild_id)
    now = _time.time()

    detector_by_name = {
        r["site_name"]: {
            "detector_bot_id": str(r["detector_bot_id"]) if r["detector_bot_id"] else None,
            "detector_pattern": r["detector_pattern"] or "",
        }
        for r in site_rows
    }

    sites = []
    for r in logs:
        bumped_at = r["bumped_at"]
        cooldown = r["cooldown_seconds"]
        if bumped_at is None:
            ready = True
            seconds_remaining = 0
        else:
            elapsed = now - bumped_at
            ready = elapsed >= cooldown
            seconds_remaining = max(0, int(cooldown - elapsed))
        det = detector_by_name.get(r["site_name"], {})
        sites.append({
            "site_name": r["site_name"],
            "cooldown_seconds": cooldown,
            "bumped_at": bumped_at,
            "ready": ready,
            "seconds_remaining": seconds_remaining,
            "notified": bool(r["notified"]) if r["notified"] is not None else False,
            "detector_bot_id": det.get("detector_bot_id"),
            "detector_pattern": det.get("detector_pattern", ""),
        })
    if cfg is None:
        return {"configured": False, "enabled": False, "channel_id": None, "role_id": None, "sites": sites}
    return {
        "configured": True,
        "enabled": bool(cfg["enabled"]),
        "channel_id": str(cfg["channel_id"]) if cfg["channel_id"] else None,
        "role_id": str(cfg["role_id"]) if cfg["role_id"] else None,
        "sites": sites,
    }


# ── Confessions config helper ─────────────────────────────────────────


def _confessions_section(guild_id: int, bot, conn) -> dict:
    cfg = _confessions_get_config_conn(conn, guild_id)
    if cfg is None:
        return {"configured": False}
    guild = bot.get_guild(guild_id) if bot is not None else None

    return {
        "configured": True,
        "dest_channel_id": str(cfg.dest_channel_id),
        "log_channel_id": str(cfg.log_channel_id),
        "cooldown_seconds": cfg.cooldown_seconds,
        "max_chars": cfg.max_chars,
        "panic": cfg.panic,
        "replies_enabled": cfg.replies_enabled,
        "notify_op_on_reply": cfg.notify_op_on_reply,
        "per_day_limit": cfg.per_day_limit,
        "launcher_channel_id": str(cfg.launcher_channel_id),
        "launcher_message_id": str(cfg.launcher_message_id),
        "blocked_users": [
            {"id": str(uid), "name": _lookup_member_name(uid, guild, conn, guild_id)}
            for uid in sorted(cfg.blocked_set())
        ],
    }


# ── GET: full config snapshot ──────────────────────────────────────────


@router.get("/config")
async def get_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            from bot_modules.services.welcome_service import (
                DEFAULT_LEAVE_MESSAGE,
                DEFAULT_WELCOME_MESSAGE,
            )

            prune_rule = _get_prune_rule(ctx.db_path, guild_id)
            prune_exempt_ids = get_prune_exception_ids(ctx.db_path, guild_id)
            grant_roles = get_grant_roles(conn, guild_id)
            booster_panel_refs = get_booster_panel_refs(conn, guild_id)

            bot = getattr(ctx, "bot", None)
            prune_guild = bot.get_guild(guild_id) if bot is not None else None

            exempt_users = [
                {"id": str(uid), "name": _lookup_member_name(uid, prune_guild, conn, guild_id)}
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
                "privacy": {
                    # "none" (default) keeps only derivations (XP/sentiment/
                    # interactions); "all" archives raw message content.
                    "message_storage_level": _str_val(
                        conn,
                        "message_storage_level",
                        STORAGE_LEVEL_NONE,
                        guild_id=guild_id,
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
                    "welcome_ping_member": _bool_val(
                        conn, "welcome_ping_member", guild_id=guild_id
                    ),
                    "welcome_trigger": _str_val(
                        conn, "welcome_trigger", "join", guild_id=guild_id
                    ),
                    "unverified_role_id": str(
                        _int_val(conn, "unverified_role_id", guild_id=guild_id)
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
                    "server_guide_channel_id": str(
                        _int_val(conn, "server_guide_channel_id", guild_id=guild_id)
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
                "auto_role": {
                    "auto_role_ids": [
                        str(i)
                        for i in _id_set_list(conn, "auto_role_ids", guild_id)
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
                        "required_role_id": str(cfg["required_role_id"]),
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
                "booster_panel_channel_id": (
                    str(booster_panel_refs[0][0]) if booster_panel_refs else "0"
                ),
                "auto_delete": [
                    {
                        "channel_id": str(r["channel_id"]),
                        "max_age_seconds": int(r["max_age_seconds"]),
                        "interval_seconds": int(r["interval_seconds"]),
                        "last_run_ts": float(r["last_run_ts"]),
                        "media_only": bool(r["media_only"]),
                        "max_age_display": _fmt_dur(int(r["max_age_seconds"])),
                        "interval_display": _fmt_dur(int(r["interval_seconds"])),
                    }
                    for r in list_auto_delete_rules_for_guild_with_conn(conn, guild_id)
                ],
                "bulk_cleanup": _bulk_cleanup_section(conn, guild_id),
                "confessions": _confessions_section(guild_id, bot, conn),
                "dms": _dms_section_with_conn(conn, guild_id),
                "starboard": _starboard_section(conn, guild_id),
                "birthday": _birthday_section(conn, guild_id),
                "bot_identity": _bot_identity_section(prune_guild),
                "branding": _branding_section(conn, guild_id),
                "guess": _guess_section(conn, guild_id),
                "whisper": _whisper_section(conn, guild_id),
                "needle": _needle_section(conn, guild_id),
                "risky": _risky_section(conn, guild_id),
                "policy": _policy_section(conn, guild_id),
                "rules_watch": _rules_watch_section(conn, guild_id, ctx.db_path),
                "greeting_watch": _greeting_watch_section(conn, guild_id),
                "auto_react": _auto_react_section(conn, guild_id),
                "bump_tracker": _bump_tracker_section(conn, guild_id),
                "pen_pals": _pen_pals_section(conn, guild_id),
                "voice_transcription": _voice_transcription_section(conn, guild_id),
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

    def _q():
        with ctx.open_db() as conn:
            if body.tz_offset_hours is not None:
                set_config_value(
                    conn, "tz_offset_hours", str(body.tz_offset_hours), guild_id
                )
            if body.mod_channel_id is not None:
                set_config_value(
                    conn, "mod_channel_id", body.mod_channel_id, guild_id
                )
            if body.bypass_role_ids is not None:
                clear_config_id_bucket(conn, "bypass_role_ids", guild_id)
                for rid in body.bypass_role_ids:
                    add_config_id(conn, "bypass_role_ids", int(rid), guild_id)
            if body.recorded_bot_user_ids is not None:
                clear_config_id_bucket(conn, "recorded_bot_user_ids", guild_id)
                for uid in body.recorded_bot_user_ids:
                    add_config_id(conn, "recorded_bot_user_ids", int(uid), guild_id)
            if body.booster_swatch_dir is not None:
                # booster_swatch_dir is a single host filesystem path read
                # globally (guild_id=0); pin the write there to avoid a
                # write-to-active / read-from-0 mismatch.
                set_config_value(conn, "booster_swatch_dir", body.booster_swatch_dir, 0)
        return {"ok": True}

    result = await run_query(_q)
    # tz_offset_hours, mod_channel_id, bypass_role_ids, recorded_bot_user_ids are
    # read per-guild (guild_config snapshot or fresh tz read); refresh the cache.
    ctx.invalidate_guild_config(guild_id)
    return result


@router.get("/config/support-access")
async def get_support_access(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            val = get_config_value(conn, "support_access_enabled", "0", guild_id, allow_legacy_fallback=False)
            return {"enabled": val == "1"}

    return await run_query(_q)


class SupportAccessUpdate(BaseModel):
    enabled: bool


@router.put("/config/support-access")
async def update_support_access(
    request: Request,
    body: SupportAccessUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            set_config_value(conn, "support_access_enabled", "1" if body.enabled else "0", guild_id)
        return {"ok": True}

    return await run_query(_q)


class PrivacyConfigUpdate(BaseModel):
    message_storage_level: str | None = None


@router.put("/config/privacy")
async def update_privacy(
    request: Request,
    body: PrivacyConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Set the message-content storage level for the active guild.

    Switching to ``none`` immediately purges this guild's already-stored
    message content (text/attachments/embeds) while leaving every derivation
    (XP, sentiment scores, interactions, member activity) intact.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    level = body.message_storage_level
    if level is None:
        return {"ok": True, "purged": 0}
    if level not in SUPPORTED_STORAGE_LEVELS:
        raise HTTPException(
            400,
            f"Unsupported storage level {level!r}; "
            f"expected one of {sorted(SUPPORTED_STORAGE_LEVELS)}",
        )

    def _q():
        with ctx.open_db() as conn:
            set_config_value(conn, "message_storage_level", level, guild_id)
            purged = 0
            if level == STORAGE_LEVEL_NONE:
                purged = purge_guild_message_content(conn, guild_id)
        return {"ok": True, "purged": purged}

    result = await run_query(_q)
    # on_message reads the level via ctx.guild_config(gid); refresh the snapshot
    # so the next message respects the new level without a restart.
    ctx.invalidate_guild_config(guild_id)
    return result


class WelcomeConfigUpdate(BaseModel):
    welcome_channel_id: str | None = None
    welcome_message: str | None = None
    welcome_ping_role_id: str | None = None
    welcome_ping_member: bool | None = None
    welcome_trigger: str | None = None
    unverified_role_id: str | None = None
    leave_channel_id: str | None = None
    leave_message: str | None = None
    greeter_role_id: str | None = None
    greeter_chat_channel_id: str | None = None
    server_guide_channel_id: str | None = None
    join_leave_log_channel_id: str | None = None


@router.put("/config/welcome")
async def update_welcome(
    request: Request,
    body: WelcomeConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    _FIELDS = {
        "welcome_channel_id": "welcome_channel_id",
        "welcome_message": "welcome_message",
        "welcome_ping_role_id": "welcome_ping_role_id",
        "welcome_trigger": "welcome_trigger",
        "unverified_role_id": "unverified_role_id",
        "leave_channel_id": "leave_channel_id",
        "leave_message": "leave_message",
        "greeter_role_id": "greeter_role_id",
        "greeter_chat_channel_id": "greeter_chat_channel_id",
        "server_guide_channel_id": "server_guide_channel_id",
        "join_leave_log_channel_id": "join_leave_log_channel_id",
    }

    def _q():
        with ctx.open_db() as conn:
            if body.welcome_ping_member is not None:
                set_config_value(
                    conn,
                    "welcome_ping_member",
                    "1" if body.welcome_ping_member else "0",
                    guild_id,
                )
            for field_name, config_key in _FIELDS.items():
                val = getattr(body, field_name)
                if val is not None:
                    set_config_value(conn, config_key, val, guild_id)
        return {"ok": True}

    result = await run_query(_q)
    # Welcome/leave handlers read these via ctx.guild_config(guild_id); drop the
    # cached snapshot so the next event reloads the edited values.
    ctx.invalidate_guild_config(guild_id)
    return result


@router.get("/config/welcome/preview")
async def welcome_preview(
    request: Request,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Render welcome and leave embeds for the calling admin (used as a sample member)."""
    from bot_modules.services.welcome_service import (
        DEFAULT_LEAVE_MESSAGE,
        DEFAULT_WELCOME_MESSAGE,
        build_leave_embed,
        build_welcome_embed,
    )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(503, "Guild not available")

    member = guild.get_member(int(user.user_id))
    if member is None:
        # Fall back to bot user as a stand-in
        member = guild.me  # type: ignore[assignment]
    if member is None:
        raise HTTPException(503, "No member context available for preview")

    from bot_modules.bios.resurrect import resolve_member_bio_link
    from bot_modules.bios.trigger import resolve_bio_placeholders
    from bot_modules.services.welcome_service import server_guide_mention_for

    def _q():
        with ctx.open_db() as conn:
            wm = get_config_value(
                conn, "welcome_message", DEFAULT_WELCOME_MESSAGE, guild_id
            )
            lm = get_config_value(
                conn, "leave_message", DEFAULT_LEAVE_MESSAGE, guild_id
            )
            bl, bcm = resolve_bio_placeholders(conn, guild_id)
            try:
                sgcid = int(
                    get_config_value(conn, "server_guide_channel_id", "0", guild_id)
                )
            except (TypeError, ValueError):
                sgcid = 0
            return wm, lm, bl, bcm, sgcid

    welcome_msg, leave_msg, bio_link, bios_channel_mention, server_guide_channel_id = (
        await run_query(_q)
    )
    server_guide_mention = server_guide_mention_for(server_guide_channel_id)

    try:
        member_bio_link = await resolve_member_bio_link(ctx, member)
    except Exception:
        member_bio_link = ""

    welcome_embed = build_welcome_embed(
        member,
        welcome_msg,
        bio_link=bio_link,
        bios_channel_mention=bios_channel_mention,
        member_bio_link=member_bio_link,
        server_guide_mention=server_guide_mention,
    )
    leave_embed = build_leave_embed(
        member,
        leave_msg,
        bio_link=bio_link,
        bios_channel_mention=bios_channel_mention,
        member_bio_link=member_bio_link,
        server_guide_mention=server_guide_mention,
    )

    def _to_dict(e) -> dict:
        return {
            "title": e.title or "",
            "description": e.description or "",
            "color": e.color.value if e.color else None,
            "thumbnail_url": e.thumbnail.url if e.thumbnail and e.thumbnail.url else None,
            "footer": e.footer.text if e.footer and e.footer.text else "",
        }

    return {
        "welcome": _to_dict(welcome_embed),
        "leave": _to_dict(leave_embed),
        "sample_user_name": member.display_name,
    }


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
    reaction_given_xp: float | None = None
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

    def _q():
        with ctx.open_db() as conn:
            if body.level_5_role_id is not None:
                set_config_value(
                    conn, "xp_level_5_role_id", body.level_5_role_id, guild_id
                )
            if body.level_5_log_channel_id is not None:
                set_config_value(
                    conn,
                    "xp_level_5_log_channel_id",
                    body.level_5_log_channel_id,
                    guild_id,
                )
            if body.level_up_log_channel_id is not None:
                set_config_value(
                    conn,
                    "xp_level_up_log_channel_id",
                    body.level_up_log_channel_id,
                    guild_id,
                )
            if body.xp_grant_allowed_user_ids is not None:
                clear_config_id_bucket(
                    conn, "xp_grant_allowed_user_ids", guild_id
                )
                for uid in body.xp_grant_allowed_user_ids:
                    add_config_id(
                        conn, "xp_grant_allowed_user_ids", int(uid), guild_id
                    )
            if body.xp_excluded_channel_ids is not None:
                clear_config_id_bucket(
                    conn, "xp_excluded_channel_ids", guild_id
                )
                for cid in body.xp_excluded_channel_ids:
                    add_config_id(
                        conn, "xp_excluded_channel_ids", int(cid), guild_id
                    )

            # Persist algorithm coefficients
            _COEFF_FIELDS = [
                "message_word_xp",
                "reply_bonus_xp",
                "image_reaction_received_xp",
                "reaction_given_xp",
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

        return {"ok": True}

    result = await run_query(_q)
    # XP config is read per-guild via ctx.guild_config(gid); drop the snapshot so
    # the next message/voice tick reloads the edited values.
    ctx.invalidate_guild_config(guild_id)
    return result


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

    def _q():
        from bot_modules.services.inactivity_prune_service import (
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

    from bot_modules.core.xp_system import get_member_last_activity_map

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

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

    result = await run_query(_q)
    # Permission checks read mod/admin roles via ctx.guild_config(guild_id); drop
    # the cached snapshot so the next check reloads the edited roles.
    ctx.invalidate_guild_config(guild_id)
    return result


class RulesWatchConfigUpdate(BaseModel):
    enabled: bool | None = None
    channel_id: str | None = None


@router.put("/config/rules-watch")
async def update_rules_watch(
    request: Request,
    body: RulesWatchConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Enable/disable the Rules Watch monitor and set its alert channel.

    No cache invalidation needed: the monitor reads ``rules_watch_enabled`` and
    ``rules_watch_channel_id`` straight from the DB on every message (see
    ``RulesWatchMonitor._is_enabled`` / ``_alert_channel_id``), so the next
    message picks up the change without a restart.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.enabled is not None:
                set_config_value(
                    conn,
                    "rules_watch_enabled",
                    "1" if body.enabled else "0",
                    guild_id,
                )
            if body.channel_id is not None:
                set_config_value(
                    conn, "rules_watch_channel_id", body.channel_id, guild_id
                )
        return {"ok": True}

    return await run_query(_q)


class GreetingWatchConfigUpdate(BaseModel):
    enabled: bool | None = None
    channel_ids: str | None = None
    notify_user_id: str | None = None
    window_minutes: int | None = None


@router.put("/config/greeting-watch")
async def update_greeting_watch(
    request: Request,
    body: GreetingWatchConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Configure Greeting Watch — which channels to watch, who to DM, how long
    to wait before flagging a greeting as unanswered."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.enabled is not None:
                set_config_value(
                    conn,
                    "greeting_watch_enabled",
                    "1" if body.enabled else "0",
                    guild_id,
                )
            if body.channel_ids is not None:
                set_config_value(
                    conn, "greeting_watch_channel_ids", body.channel_ids, guild_id
                )
            if body.notify_user_id is not None:
                set_config_value(
                    conn,
                    "greeting_watch_notify_user_id",
                    body.notify_user_id,
                    guild_id,
                )
            if body.window_minutes is not None:
                set_config_value(
                    conn,
                    "greeting_watch_window_minutes",
                    str(body.window_minutes),
                    guild_id,
                )
        return {"ok": True}

    result = await run_query(_q)
    # The ingest hook reads these off the cached GuildConfig snapshot; drop it so
    # the next message picks up the change without a restart.
    ctx.invalidate_guild_config(guild_id)
    return result


class RoleGrantUpdate(BaseModel):
    label: str | None = None
    role_id: str | None = None
    log_channel_id: str | None = None
    announce_channel_id: str | None = None
    grant_message: str | None = None
    required_role_id: str | None = None
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
                    required_role_id=int(body.required_role_id)
                    if body.required_role_id is not None
                    else cur["required_role_id"],
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
                    required_role_id=int(body.required_role_id)
                    if body.required_role_id
                    else 0,
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

    result = await run_query(_q)
    ctx.invalidate_guild_config(guild_id)
    return result


@router.delete("/config/roles/{grant_name}")
async def delete_role_grant(
    grant_name: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            delete_grant_role(conn, guild_id, grant_name)
        return {"ok": True}

    result = await run_query(_q)
    ctx.invalidate_guild_config(guild_id)
    return result


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

    def _q():
        with ctx.open_db() as conn:
            if body.spoiler_required_channels is not None:
                clear_config_id_bucket(conn, "spoiler_required_channels", guild_id)
                for cid in body.spoiler_required_channels:
                    add_config_id(
                        conn, "spoiler_required_channels", int(cid), guild_id
                    )
        return {"ok": True}

    result = await run_query(_q)
    # on_message reads spoiler channels via ctx.guild_config(gid); refresh it.
    ctx.invalidate_guild_config(guild_id)
    return result


class AutoRoleConfigUpdate(BaseModel):
    auto_role_ids: list[str] | None = None


@router.put("/config/auto-role")
async def update_auto_role(
    request: Request,
    body: AutoRoleConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.auto_role_ids is not None:
                clear_config_id_bucket(conn, "auto_role_ids", guild_id)
                for rid in body.auto_role_ids:
                    add_config_id(conn, "auto_role_ids", int(rid), guild_id)
        return {"ok": True}

    result = await run_query(_q)
    ctx.invalidate_guild_config(guild_id)
    return result


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

    def _q():
        with ctx.open_db() as conn:
            delete_booster_role(conn, guild_id, role_key)
        return {"ok": True}

    return await run_query(_q)


class BoosterPanelPostRequest(BaseModel):
    channel_id: str


@router.post("/config/booster-roles/post-panel")
async def post_booster_panel(
    request: Request,
    body: BoosterPanelPostRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Re-post the booster cosmetic role panel in the chosen channel."""
    import discord

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")
    try:
        channel_id = int(body.channel_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid channel_id")
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(400, "Channel must be a text channel in this guild")

    msgs = await post_or_update_booster_panel(ctx.db_path, guild, channel)
    if not msgs:
        raise HTTPException(400, "No booster roles configured.")
    return {"ok": True, "message_count": len(msgs)}


@router.post("/config/booster-roles/sync-swatches")
async def sync_booster_swatches(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Scan the configured swatch directory; create/remove roles to match."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")

    try:
        created, removed = await sync_swatches(ctx.db_path, guild)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "created": created, "removed": removed}


# ── Managed swatch uploads (per-guild folder) ────────────────────────

_MAX_SWATCH_BYTES = 8 * 1024 * 1024


def _safe_swatch_name(filename: str | None) -> str:
    """Reject path traversal / unsupported types; return a bare filename."""
    name = os.path.basename(filename or "")
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise HTTPException(400, "Invalid filename")
    ext = os.path.splitext(name)[1].lower()
    if ext not in _IMAGE_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")
    return name


def _swatch_listing(db_path, guild_id: int) -> dict:
    managed = get_guild_swatch_dir(db_path, guild_id)
    active = resolve_swatch_directory(db_path, guild_id)
    return {
        "ok": True,
        "files": swatch_file_info(managed),
        "managed_dir": str(managed),
        "active_dir": active,
        "using_managed": active == str(managed),
    }


@router.get("/config/booster-roles/swatches")
async def list_booster_swatches(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """List uploaded swatch files in this guild's managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    return _swatch_listing(ctx.db_path, guild_id)


@router.post("/config/booster-roles/swatches")
async def upload_booster_swatches(
    request: Request,
    files: list[UploadFile] = File(...),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Save one or more uploaded swatch images into the managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    managed = get_guild_swatch_dir(ctx.db_path, guild_id)

    saved: list[str] = []
    for upload in files:
        name = _safe_swatch_name(upload.filename)
        content = await upload.read()
        if not content:
            continue
        if len(content) > _MAX_SWATCH_BYTES:
            raise HTTPException(400, f"{name} exceeds the 8 MB limit")
        target = managed / name
        if target.resolve().parent != managed.resolve():
            raise HTTPException(400, "Invalid filename")
        target.write_bytes(content)
        saved.append(name)

    return {**_swatch_listing(ctx.db_path, guild_id), "saved": saved}


@router.delete("/config/booster-roles/swatches/{filename}")
async def delete_booster_swatch(
    filename: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Delete a single uploaded swatch from the managed folder."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    name = _safe_swatch_name(filename)
    managed = get_guild_swatch_dir(ctx.db_path, guild_id)
    target = managed / name
    if target.resolve().parent != managed.resolve():
        raise HTTPException(400, "Invalid filename")
    if not target.is_file():
        raise HTTPException(404, "File not found")
    target.unlink()
    return _swatch_listing(ctx.db_path, guild_id)


# ── Quote card border (per-guild uploaded frame) ─────────────────────
#
# A guild can upload one PNG/WEBP frame that becomes its default quote-card
# border. The renderer composites it over the whole card using its own alpha
# channel, so an opaque upload would hide the quote entirely — the upload path
# therefore requires a real, partly-transparent alpha channel and re-encodes to
# a clean RGBA PNG at the exact path the bot renderer reads
# (``db_path.parent/quote_borders/<guild_id>/border.png``).

_MAX_QUOTE_BORDER_BYTES = 8 * 1024 * 1024
_QUOTE_BORDER_MAX_DIM = 2000


def _quote_border_meta(db_path, guild_id: int) -> dict:
    path = guild_border_path(db_path, guild_id)
    if not path.is_file():
        return {"exists": False, "width": None, "height": None}
    width = height = None
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            width, height = im.size
    except Exception:
        pass
    return {"exists": True, "width": width, "height": height}


@router.get("/config/quote-border")
async def get_quote_border(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Metadata about this guild's uploaded quote-card border."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    return _quote_border_meta(ctx.db_path, guild_id)


@router.get("/config/quote-border/image")
async def get_quote_border_image(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Serve the raw border PNG for preview (admin only, guild-scoped)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    path = guild_border_path(ctx.db_path, guild_id)
    if not path.is_file():
        raise HTTPException(404, "No quote border set")
    return FileResponse(path, media_type="image/png")


@router.post("/config/quote-border")
async def upload_quote_border(
    request: Request,
    file: UploadFile = File(...),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Upload + normalize a per-guild quote-card border.

    Rejects opaque images (they would cover the whole card) and re-encodes to a
    clean RGBA PNG so the renderer always gets a safe, alpha-carrying frame.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    content = await file.read(_MAX_QUOTE_BORDER_BYTES + 1)
    if len(content) > _MAX_QUOTE_BORDER_BYTES:
        raise HTTPException(413, "Border image must be 8 MB or smaller.")
    if not content:
        raise HTTPException(400, "Empty file.")

    from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            fmt = (im.format or "").upper()
            img = im.convert("RGBA")
    except (UnidentifiedImageError, OSError):
        raise HTTPException(400, "Unsupported or corrupt image.")

    # SVG can't reach here (PIL won't open it) and JPEG/GIF lack a usable alpha
    # channel for a see-through frame, so restrict to the two formats that do.
    if fmt not in ("PNG", "WEBP"):
        raise HTTPException(
            400, "Use a PNG or WEBP with transparency (JPEG/GIF have no usable alpha)."
        )

    # getextrema() on the single-band alpha returns (min, max); min>=250 means
    # effectively no transparency. (Guard the union type the stubs declare.)
    alpha_min = img.getchannel("A").getextrema()[0]
    if isinstance(alpha_min, tuple):
        alpha_min = alpha_min[0]
    if alpha_min >= 250:
        raise HTTPException(
            400,
            "This image has no transparent areas — it would cover the whole quote. "
            "Upload a frame PNG with a see-through center.",
        )

    img.thumbnail(
        (_QUOTE_BORDER_MAX_DIM, _QUOTE_BORDER_MAX_DIM), Image.Resampling.LANCZOS
    )

    target = guild_border_path(ctx.db_path, guild_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp sibling and confirm the frame leaves a usable opening at the
    # render canvas (900×500) before replacing any existing border — this is the
    # real guard: the renderer fits the quote into this opening, so "no opening"
    # is the failure mode, catching center-covered frames the opaque check misses.
    tmp = target.with_name("border.tmp.png")
    img.save(tmp, format="PNG")
    probe = BorderStyle(
        name="pending", path=tmp, flip=False, luma_key=False, mask_fit=True
    )
    if analyze_border_opening(probe, 900, 500) is None:
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            400,
            "The frame leaves no clear opening for the quote — use a border with a "
            "larger see-through center.",
        )
    tmp.replace(target)

    return _quote_border_meta(ctx.db_path, guild_id)


@router.delete("/config/quote-border")
async def delete_quote_border(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Remove this guild's uploaded quote border."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    path = guild_border_path(ctx.db_path, guild_id)
    if path.is_file():
        path.unlink()
    return _quote_border_meta(ctx.db_path, guild_id)


# ── Auto-delete schedules ────────────────────────────────────────────


class AutoDeleteRuleUpdate(BaseModel):
    max_age_seconds: int
    interval_seconds: int
    media_only: bool = False


@router.put("/config/auto-delete/{channel_id}")
async def update_auto_delete(
    channel_id: str,
    request: Request,
    body: AutoDeleteRuleUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        upsert_auto_delete_rule(
            ctx.db_path,
            guild_id,
            int(channel_id),
            body.max_age_seconds,
            body.interval_seconds,
            media_only=body.media_only,
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

    def _q():
        remove_auto_delete_rule(ctx.db_path, guild_id, int(channel_id))
        return {"ok": True}

    return await run_query(_q)


# ── Auto-react config ────────────────────────────────────────────────


class AutoReactRuleUpdate(BaseModel):
    emojis: list[str]
    enabled: bool = True


@router.put("/config/auto-react/{channel_id}")
async def update_auto_react(
    channel_id: str,
    request: Request,
    body: AutoReactRuleUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        upsert_auto_react_rule(
            ctx.db_path,
            guild_id,
            int(channel_id),
            body.emojis,
            body.enabled,
        )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/auto-react/{channel_id}")
async def remove_auto_react(
    channel_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        remove_auto_react_rule(ctx.db_path, guild_id, int(channel_id))
        return {"ok": True}

    return await run_query(_q)


# ── Bump tracker config ──────────────────────────────────────────────


class BumpTrackerConfigUpdate(BaseModel):
    channel_id: str | None = None
    role_id: str | None = None
    enabled: bool | None = None


class BumpTrackerSiteUpdate(BaseModel):
    cooldown_hours: float
    detector_bot_id: str | None = None
    detector_pattern: str = ""


class BumpTrackerDetectorUpdate(BaseModel):
    detector_bot_id: str
    detector_pattern: str = ""


@router.put("/config/bump-tracker")
async def update_bump_tracker(
    request: Request,
    body: BumpTrackerConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with open_db(ctx.db_path) as conn:
            _bump_upsert_config(
                conn,
                guild_id,
                channel_id=int(body.channel_id) if body.channel_id else None,
                role_id=int(body.role_id) if body.role_id else None,
                enabled=body.enabled,
            )
        return {"ok": True}

    return await run_query(_q)


@router.put("/config/bump-tracker/sites/{site_name}")
async def update_bump_tracker_site(
    site_name: str,
    request: Request,
    body: BumpTrackerSiteUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with open_db(ctx.db_path) as conn:
            _bump_add_site(
                conn,
                guild_id,
                site_name,
                int(body.cooldown_hours * 3600),
                detector_bot_id=int(body.detector_bot_id) if body.detector_bot_id else 0,
                detector_pattern=body.detector_pattern,
            )
        return {"ok": True}

    return await run_query(_q)


@router.put("/config/bump-tracker/sites/{site_name}/detector")
async def update_bump_tracker_detector(
    site_name: str,
    request: Request,
    body: BumpTrackerDetectorUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with open_db(ctx.db_path) as conn:
            updated = _bump_set_detector(
                conn, guild_id, site_name, int(body.detector_bot_id), body.detector_pattern
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Site not found")
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/bump-tracker/sites/{site_name}")
async def delete_bump_tracker_site(
    site_name: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with open_db(ctx.db_path) as conn:
            _bump_remove_site(conn, guild_id, site_name)
        return {"ok": True}

    return await run_query(_q)


@router.post("/config/bump-tracker/sites/{site_name}/log")
async def log_bump_tracker_bump(
    site_name: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with open_db(ctx.db_path) as conn:
            sites = [r["site_name"] for r in _bump_list_sites(conn, guild_id)]
            if site_name not in sites:
                raise HTTPException(status_code=404, detail="Site not found")
            _bump_log_bump(conn, guild_id, site_name)
        return {"ok": True}

    return await run_query(_q)


# ── Pen Pals config ──────────────────────────────────────────────────


class PenPalsConfigUpdate(BaseModel):
    enabled: bool = False
    category_id: str | None = None
    opt_in_role_id: str | None = None
    question_category: str = "sfw"
    log_channel_id: str | None = None
    auto_round_dow: int = -1
    auto_round_hour: int = 12
    panel_channel_id: str | None = None


@router.put("/config/pen-pals")
async def update_pen_pals_config(
    request: Request,
    body: PenPalsConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    new_channel_id = int(body.panel_channel_id) if body.panel_channel_id else 0

    def _q() -> tuple[int, int]:
        with open_db(ctx.db_path) as conn:
            existing = _pp_get_config(conn, guild_id)
            old_channel_id = int(existing["panel_channel_id"]) if existing and existing["panel_channel_id"] else 0
            old_message_id = int(existing["panel_message_id"]) if existing and existing["panel_message_id"] else 0
            _pp_set_config(
                conn,
                guild_id,
                enabled=body.enabled,
                category_id=int(body.category_id) if body.category_id else 0,
                opt_in_role_id=int(body.opt_in_role_id) if body.opt_in_role_id else 0,
                question_category=body.question_category,
                log_channel_id=int(body.log_channel_id) if body.log_channel_id else 0,
                auto_round_dow=body.auto_round_dow,
                auto_round_hour=body.auto_round_hour,
                panel_channel_id=new_channel_id,
            )
            return old_channel_id, old_message_id

    old_channel_id, old_message_id = await run_query(_q)

    if ctx.bot:
        ctx.bot.dispatch(
            "pen_pals_panel_refresh",
            guild_id, new_channel_id, old_channel_id, old_message_id,
        )

    return {"ok": True}


# ── Voice transcription config ───────────────────────────────────────


class VoiceTranscriptionConfigUpdate(BaseModel):
    enabled: bool = False
    model_name: str = _VT_DEFAULT_MODEL
    channel_ids: list[str] = []


class VoiceTranscriptionDownloadRequest(BaseModel):
    model_name: str = _VT_DEFAULT_MODEL


@router.put("/config/voice-transcription")
async def update_voice_transcription_config(
    request: Request,
    body: VoiceTranscriptionConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    model = body.model_name if body.model_name in _VT_VALID_MODELS else _VT_DEFAULT_MODEL
    channel_ids = tuple(int(c) for c in body.channel_ids if c)

    def _q() -> dict:
        with open_db(ctx.db_path) as conn:
            _vt_set_config(
                conn,
                guild_id,
                enabled=body.enabled,
                model_name=model,
                channel_ids=channel_ids,
            )
        return {"ok": True}

    return await run_query(_q)


@router.post("/config/voice-transcription/download")
async def download_voice_transcription_model(
    request: Request,
    body: VoiceTranscriptionDownloadRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if not _vt_is_available():
        raise HTTPException(400, "faster-whisper isn't installed on the bot host.")
    if body.model_name not in _VT_VALID_MODELS:
        raise HTTPException(400, f"Unknown model {body.model_name!r}.")

    try:
        # Network fetch — can take a while; keep it off the event loop.
        await run_query(_vt_download_model, body.model_name)
    except Exception as exc:
        raise HTTPException(502, f"Download failed: {exc}") from exc

    return {"ok": True, "cached": _vt_model_is_cached(body.model_name)}


# ── Confessions config ───────────────────────────────────────────────


class ConfessionsConfigUpdate(BaseModel):
    dest_channel_id: str | None = None
    log_channel_id: str | None = None
    cooldown_seconds: int | None = None
    max_chars: int | None = None
    panic: bool | None = None
    replies_enabled: bool | None = None
    notify_op_on_reply: bool | None = None
    per_day_limit: int | None = None


@router.put("/config/confessions")
async def update_confessions(
    request: Request,
    body: ConfessionsConfigUpdate,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        cfg = _confessions_get_config(ctx.db_path, guild_id)
        if cfg is None:
            dest = int(body.dest_channel_id or 0)
            log = int(body.log_channel_id or 0)
            cfg = _ConfessionsGuildConfig(guild_id=guild_id, dest_channel_id=dest, log_channel_id=log)
        if body.dest_channel_id is not None:
            cfg.dest_channel_id = int(body.dest_channel_id)
        if body.log_channel_id is not None:
            cfg.log_channel_id = int(body.log_channel_id)
        if body.cooldown_seconds is not None:
            cfg.cooldown_seconds = body.cooldown_seconds
        if body.max_chars is not None:
            cfg.max_chars = body.max_chars
        if body.panic is not None:
            cfg.panic = body.panic
        if body.replies_enabled is not None:
            cfg.replies_enabled = body.replies_enabled
        if body.notify_op_on_reply is not None:
            cfg.notify_op_on_reply = body.notify_op_on_reply
        if body.per_day_limit is not None:
            cfg.per_day_limit = body.per_day_limit
        _confessions_upsert_config(ctx.db_path, cfg)
        return {"ok": True}

    return await run_query(_q)


@router.put("/config/confessions/block/{user_id}")
async def block_confessions_user(
    user_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        cfg = _confessions_get_config(ctx.db_path, guild_id)
        if cfg is None:
            raise HTTPException(404, "Confessions not configured for this guild")
        s = cfg.blocked_set()
        s.add(int(user_id))
        cfg.blocked_user_ids = sorted(s)
        _confessions_upsert_config(ctx.db_path, cfg)
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/confessions/block/{user_id}")
async def unblock_confessions_user(
    user_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        cfg = _confessions_get_config(ctx.db_path, guild_id)
        if cfg is None:
            raise HTTPException(404, "Confessions not configured for this guild")
        s = cfg.blocked_set()
        s.discard(int(user_id))
        cfg.blocked_user_ids = sorted(s)
        _confessions_upsert_config(ctx.db_path, cfg)
        return {"ok": True}

    return await run_query(_q)


class PostButtonRequest(BaseModel):
    channel_id: str


@router.post("/config/confessions/post-button")
async def post_confessions_button(
    request: Request,
    body: PostButtonRequest,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    cog = getattr(bot, "cogs", {}).get("ConfessionsCog")
    if cog is None:
        raise HTTPException(503, "Confessions module not loaded")

    success = await cog.web_post_launcher(guild_id, int(body.channel_id))
    if not success:
        raise HTTPException(500, "Failed to post confession button — check channel and bot permissions")
    return {"ok": True}


# ── DM perms config ──────────────────────────────────────────────────


class DmsConfigUpdate(BaseModel):
    request_channel_id: str | None = None
    audit_channel_id: str | None = None
    open_role_id: str | None = None
    ask_role_id: str | None = None
    closed_role_id: str | None = None


@router.put("/config/dms")
async def update_dms(
    request: Request,
    body: DmsConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    role_fields = (body.open_role_id, body.ask_role_id, body.closed_role_id)

    def _q():
        if body.request_channel_id is not None:
            set_request_channel(ctx.db_path, guild_id, int(body.request_channel_id))
        if body.audit_channel_id is not None:
            set_audit_channel(ctx.db_path, guild_id, int(body.audit_channel_id))
        if any(f is not None for f in role_fields):
            current = get_dm_mode_role_ids(ctx.db_path, guild_id)
            merged = {
                "open": int(body.open_role_id) if body.open_role_id is not None else current["open"],
                "ask": int(body.ask_role_id) if body.ask_role_id is not None else current["ask"],
                "closed": int(body.closed_role_id) if body.closed_role_id is not None else current["closed"],
            }
            set_dm_mode_role_ids(
                ctx.db_path, guild_id,
                open_role_id=merged["open"],
                ask_role_id=merged["ask"],
                closed_role_id=merged["closed"],
            )
            return merged
        return None

    merged = await run_query(_q)

    # Poke the cog's in-memory cache so the new roles apply without a restart.
    if merged is not None:
        bot = getattr(ctx, "bot", None)
        cog = bot.get_cog("DmPermsCog") if bot is not None else None
        if cog is not None:
            cog.mode_role_ids[guild_id] = merged
    return {"ok": True}


@router.post("/config/dms/post-panel")
async def post_dms_panel(
    request: Request,
    body: PostButtonRequest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import discord

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    cog = bot.get_cog("DmPermsCog")
    if cog is None:
        raise HTTPException(503, "DmPermsCog not loaded")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")

    channel_id = int(body.channel_id)
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(400, "Channel not found or not a text channel")
    perms = channel.permissions_for(guild.me)
    missing = [
        label
        for flag, label in (
            (perms.view_channel, "View Channel"),
            (perms.send_messages, "Send Messages"),
            (perms.embed_links, "Embed Links"),
        )
        if not flag
    ]
    if missing:
        raise HTTPException(
            400,
            f"The bot can't post in #{channel.name} — missing permissions: "
            f"{', '.join(missing)}",
        )

    message_id = await cog._ensure_panel(guild, channel_id, force_repost=True)
    if message_id is None:
        raise HTTPException(
            502, f"Discord rejected the post in #{channel.name} — panel was not posted"
        )
    set_panel_settings(ctx.db_path, guild_id, channel_id, message_id)
    cog.panel_settings[guild_id] = {"panel_channel_id": channel_id, "panel_message_id": message_id}
    return {"ok": True}


# ── Starboard config ─────────────────────────────────────────────────


class StarboardConfigUpdate(BaseModel):
    channel_id: str | None = None
    threshold: int | None = None
    emoji: str | None = None
    enabled: bool | None = None
    excluded_channels: list[str] | None = None


@router.put("/config/starboard")
async def update_starboard(
    request: Request,
    body: StarboardConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = _get_starboard_config(conn, guild_id)
            channel_id = int(row["channel_id"]) if row else 0
            threshold = int(row["threshold"]) if row else 3
            emoji = row["emoji"] if row else "⭐"
            enabled = int(row["enabled"]) if row else 1

            if body.channel_id is not None:
                channel_id = int(body.channel_id)
            if body.threshold is not None:
                if body.threshold < 1:
                    raise HTTPException(400, "Threshold must be at least 1")
                threshold = body.threshold
            if body.emoji is not None:
                ok, error_message = _starboard_validate_emoji(body.emoji)
                if not ok:
                    raise HTTPException(400, error_message)
                emoji = body.emoji.strip()
            if body.enabled is not None:
                enabled = 1 if body.enabled else 0

            _upsert_starboard_config(
                conn,
                guild_id,
                channel_id=channel_id,
                threshold=threshold,
                emoji=emoji,
                enabled=enabled,
            )

            if body.excluded_channels is not None:
                clear_config_id_bucket(conn, _STARBOARD_EXCLUDED_BUCKET, guild_id)
                for cid in body.excluded_channels:
                    add_config_id(
                        conn, _STARBOARD_EXCLUDED_BUCKET, int(cid), guild_id
                    )
        return {"ok": True}

    return await run_query(_q)


class BulkCleanupUpdate(BaseModel):
    enabled: bool | None = None
    age_days: int | None = None
    excluded_channels: list[str] | None = None


@router.put("/config/bulk-cleanup")
async def update_bulk_cleanup(
    request: Request,
    body: BulkCleanupUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.bulk_cleanup_service import (
        EXCLUDED_BUCKET,
        MIN_AGE_DAYS,
        _AGE_DAYS_KEY,
        _ENABLED_KEY,
    )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.age_days is not None and body.age_days < MIN_AGE_DAYS:
                raise HTTPException(
                    400, f"Age must be at least {MIN_AGE_DAYS} day(s)"
                )
            if body.enabled is not None:
                set_config_value(
                    conn, _ENABLED_KEY, "1" if body.enabled else "0", guild_id
                )
            if body.age_days is not None:
                set_config_value(conn, _AGE_DAYS_KEY, str(int(body.age_days)), guild_id)
            if body.excluded_channels is not None:
                clear_config_id_bucket(conn, EXCLUDED_BUCKET, guild_id)
                for cid in body.excluded_channels:
                    add_config_id(conn, EXCLUDED_BUCKET, int(cid), guild_id)
        return {"ok": True}

    return await run_query(_q)


# ── Birthday config ──────────────────────────────────────────────────


class BirthdayConfigUpdate(BaseModel):
    birthday_channel_id: str | None = None
    birthday_message: str | None = None
    birthday_pin: bool | None = None
    birthday_channel_id_2: str | None = None
    birthday_message_2: str | None = None
    birthday_pin_2: bool | None = None


@router.put("/config/birthday")
async def update_birthday(
    request: Request,
    body: BirthdayConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.birthday_channel_id is not None:
                set_config_value(
                    conn, "birthday_channel_id", body.birthday_channel_id, guild_id
                )
            if body.birthday_message is not None:
                msg = body.birthday_message.strip()
                if not msg:
                    raise HTTPException(400, "Message cannot be empty")
                set_config_value(conn, "birthday_message", msg, guild_id)
            if body.birthday_pin is not None:
                set_config_value(
                    conn, "birthday_pin", "1" if body.birthday_pin else "0", guild_id
                )
            if body.birthday_channel_id_2 is not None:
                set_config_value(
                    conn, "birthday_channel_id_2", body.birthday_channel_id_2, guild_id
                )
            if body.birthday_message_2 is not None:
                msg2 = body.birthday_message_2.strip()
                if not msg2:
                    raise HTTPException(400, "Message cannot be empty")
                set_config_value(conn, "birthday_message_2", msg2, guild_id)
            if body.birthday_pin_2 is not None:
                set_config_value(
                    conn, "birthday_pin_2", "1" if body.birthday_pin_2 else "0", guild_id
                )
        return {"ok": True}

    return await run_query(_q)


# ── Risky Rolls config ───────────────────────────────────────────────


class RiskyConfigUpdate(BaseModel):
    ping_role_id: str | None = None
    min_game_seconds: int | None = None


@router.put("/config/risky")
async def update_risky(
    request: Request,
    body: RiskyConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    if body.min_game_seconds is not None and body.min_game_seconds < 0:
        raise HTTPException(400, "min_game_seconds cannot be negative")

    new_ping_role: int | None = None
    clear_ping_role = False
    new_min_secs: int | None = None
    clear_min_secs = False

    def _q():
        nonlocal new_ping_role, clear_ping_role, new_min_secs, clear_min_secs
        with ctx.open_db() as conn:
            if body.ping_role_id is not None:
                role_id = int(body.ping_role_id)
                if role_id == 0:
                    conn.execute(
                        "DELETE FROM config WHERE guild_id = ? AND key = ?",
                        (guild_id, _RISKY_PING_KEY),
                    )
                    clear_ping_role = True
                else:
                    set_config_value(conn, _RISKY_PING_KEY, str(role_id), guild_id)
                    new_ping_role = role_id
            if body.min_game_seconds is not None:
                secs = body.min_game_seconds
                if secs == 0:
                    conn.execute(
                        "DELETE FROM config WHERE guild_id = ? AND key = ?",
                        (guild_id, _RISKY_MIN_GAME_KEY),
                    )
                    clear_min_secs = True
                else:
                    set_config_value(conn, _RISKY_MIN_GAME_KEY, str(secs), guild_id)
                    new_min_secs = secs
        return {"ok": True}

    result = await run_query(_q)

    # Update in-memory cache on the event loop, not from the thread.
    from bot_modules.services.risky_roll import state as rr_state  # noqa: PLC0415

    if clear_ping_role:
        rr_state.ping_roles.pop(guild_id, None)
    elif new_ping_role is not None:
        rr_state.ping_roles[guild_id] = new_ping_role
    if clear_min_secs:
        rr_state.min_game_seconds.pop(guild_id, None)
    elif new_min_secs is not None:
        rr_state.min_game_seconds[guild_id] = new_min_secs

    return result


class PolicyConfigUpdate(BaseModel):
    vote_timeout_hours: int | None = None


@router.put("/config/policy")
async def update_policy(
    request: Request,
    body: PolicyConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    if body.vote_timeout_hours is not None and body.vote_timeout_hours < 1:
        raise HTTPException(400, "vote_timeout_hours must be at least 1")

    def _q():
        with ctx.open_db() as conn:
            if body.vote_timeout_hours is not None:
                set_config_value(
                    conn,
                    _POLICY_VOTE_TIMEOUT_KEY,
                    str(body.vote_timeout_hours),
                    guild_id,
                )
        return {"ok": True}

    return await run_query(_q)


@router.get("/birthday/calendar")
async def birthday_calendar(
    request: Request,
    days: int = 90,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot is not None else None

    def _q():
        today = date.today()
        with ctx.open_db() as conn:
            rows = conn.execute(
                "SELECT user_id, birth_month, birth_day, preference FROM member_birthdays"
                " WHERE guild_id = ? ORDER BY birth_month, birth_day",
                (guild_id,),
            ).fetchall()
            result = []
            for row in rows:
                uid = row["user_id"]
                m, d = row["birth_month"], row["birth_day"]
                pref = row["preference"]
                try:
                    bday_this_year = date(today.year, m, d)
                except ValueError:
                    bday_this_year = date(today.year, m, 28)
                next_bday = bday_this_year if bday_this_year >= today else (
                    date(today.year + 1, m, d)
                    if m != 2 or d <= 28
                    else date(today.year + 1, m, 28)
                )
                days_until = (next_bday - today).days
                if days_until > days:
                    continue
                name = _lookup_member_name(uid, guild, conn, guild_id)
                result.append({
                    "user_id": str(uid),
                    "name": name,
                    "birth_month": m,
                    "birth_day": d,
                    "next_date": next_bday.isoformat(),
                    "days_until": days_until,
                    "preference": pref,
                })
        result.sort(key=lambda x: x["days_until"])
        return result

    return await run_query(_q)


_GUESS_VALID_DIFFICULTIES = {"easy", "medium", "hard"}

_GUESS_AUDIT_ACTIONS = {"submit", "delete", "solve", "guess_cap_hit"}


@router.get("/guess/audit")
async def list_guess_audit(
    request: Request,
    limit: int = 100,
    action: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """List recent Guess audit events for the active guild.

    Query params:
        limit: max rows (1-500, default 100)
        action: optional filter — submit | delete | solve | guess_cap_hit
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    capped_limit = max(1, min(500, limit))
    if action is not None and action not in _GUESS_AUDIT_ACTIONS:
        raise HTTPException(400, f"action must be one of {sorted(_GUESS_AUDIT_ACTIONS)}")

    def _q():
        from bot_modules.services.guess_repo import list_audit_events
        with ctx.open_db() as conn:
            events = list_audit_events(
                conn, guild_id, limit=capped_limit, action=action
            )
        return {
            "events": [
                {
                    "id": e.id,
                    "ts": e.ts,
                    "actor_id": str(e.actor_id),
                    "action": e.action,
                    "round_id": e.round_id,
                    "details": e.details,
                }
                for e in events
            ],
        }

    return await run_query(_q)


class GuessConfigUpdate(BaseModel):
    channel_id: str | None = None
    role_id: str | None = None
    crop_difficulty: str | None = None
    guess_cooldown_seconds: int | None = None
    min_image_dimension_px: int | None = None
    max_image_size_mb: int | None = None


@router.put("/config/guess")
async def update_guess_config(
    request: Request,
    body: GuessConfigUpdate,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # Guess only posts in age-gated channels (parity with the retired
    # /guess setup command). Best-effort: enforced when the bot can resolve
    # the channel; skipped when the cache can't see it.
    if body.channel_id:
        try:
            new_channel_id = int(body.channel_id)
        except ValueError:
            return {"ok": False, "detail": "channel_id must be a numeric channel ID"}
        bot = getattr(ctx, "bot", None)
        guild = bot.get_guild(guild_id) if bot else None
        if guild is not None:
            channel = guild.get_channel(new_channel_id)
            if channel is None:
                return {"ok": False, "detail": "Channel not found in this guild"}
            if not getattr(channel, "is_nsfw", lambda: False)():
                return {
                    "ok": False,
                    "detail": "Guess only posts in age-gated channels — enable the channel's NSFW flag first",
                }

    def _q():
        from bot_modules.services.guess_repo import set_guess_config_value
        if body.crop_difficulty is not None and body.crop_difficulty not in _GUESS_VALID_DIFFICULTIES:
            return {"ok": False, "detail": f"crop_difficulty must be one of {sorted(_GUESS_VALID_DIFFICULTIES)}"}
        with ctx.open_db() as conn:
            if body.channel_id is not None:
                set_guess_config_value(conn, guild_id, "guess_channel_id", body.channel_id)
            if body.role_id is not None:
                set_guess_config_value(conn, guild_id, "guess_role_id", body.role_id)
            if body.crop_difficulty is not None:
                set_guess_config_value(conn, guild_id, "guess_crop_difficulty", body.crop_difficulty)
            if body.guess_cooldown_seconds is not None:
                set_guess_config_value(conn, guild_id, "guess_guess_cooldown_seconds", str(body.guess_cooldown_seconds))
            if body.min_image_dimension_px is not None:
                set_guess_config_value(conn, guild_id, "guess_min_image_dimension_px", str(body.min_image_dimension_px))
            if body.max_image_size_mb is not None:
                set_guess_config_value(conn, guild_id, "guess_max_image_size_mb", str(body.max_image_size_mb))
        return {"ok": True}

    return await run_query(_q)


class WhisperConfigUpdate(BaseModel):
    channel_id: str | None = None
    role_id: str | None = None
    log_channel_id: str | None = None


@router.put("/config/whisper")
async def update_whisper_config(
    request: Request,
    body: WhisperConfigUpdate,
    _: AuthenticatedUser = Depends(require_game_host),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        from bot_modules.services.whisper_repo import set_whisper_config_value
        with ctx.open_db() as conn:
            if body.channel_id is not None:
                set_whisper_config_value(conn, guild_id, "whisper_channel_id", body.channel_id)
            if body.role_id is not None:
                set_whisper_config_value(conn, guild_id, "whisper_role_id", body.role_id)
            if body.log_channel_id is not None:
                set_whisper_config_value(conn, guild_id, "whisper_log_channel_id", body.log_channel_id)
        return {"ok": True}

    return await run_query(_q)


# ── Bot identity (per-guild) ─────────────────────────────────────────


@router.post("/config/bot-identity")
async def update_bot_identity(
    request: Request,
    nick: str | None = Form(default=None),
    avatar_url: str | None = Form(default=None),
    avatar_file: UploadFile | None = File(default=None),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    import discord
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")

    # Resolve avatar bytes: file takes priority over URL
    avatar_bytes: bytes | None = None
    if avatar_file is not None:
        content = await avatar_file.read()
        if content:
            avatar_bytes = content
    elif avatar_url:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(avatar_url, follow_redirects=True, timeout=10.0)
                response.raise_for_status()
                avatar_bytes = response.content
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise HTTPException(400, f"Failed to fetch avatar URL: {exc}")

    edit_kwargs: dict = {}
    if nick is not None:
        edit_kwargs["nick"] = nick
    if avatar_bytes is not None:
        edit_kwargs["avatar"] = avatar_bytes

    if edit_kwargs:
        try:
            await guild.me.edit(**edit_kwargs)
        except discord.HTTPException as exc:
            raise HTTPException(400, f"Discord rejected the update: {exc}")

    return {
        "ok": True,
        "nick": guild.me.nick or "",
        "avatar_url": str(guild.me.display_avatar.url),
    }


# ── Branding (embed accent colour) ────────────────────────────────────


class BrandingConfigUpdate(BaseModel):
    accent_mode: str | None = None
    accent_hex: str | None = None


def _parse_hex_colour(raw: str) -> int:
    """Parse a ``#RRGGBB`` (or ``RRGGBB``) string to an int, raising ValueError."""
    s = raw.strip().lstrip("#")
    if len(s) != 6:
        raise ValueError("expected 6-digit hex colour")
    return int(s, 16)


@router.put("/config/branding")
async def update_branding(
    request: Request,
    body: BrandingConfigUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    new_mode: str | None = None
    if body.accent_mode is not None:
        new_mode = body.accent_mode.strip().lower()
        if new_mode not in (ACCENT_MODE_AVATAR, ACCENT_MODE_CUSTOM):
            raise HTTPException(400, "accent_mode must be 'avatar' or 'custom'")

    hex_provided = body.accent_hex is not None
    new_hex = ACCENT_HEX_UNSET
    if hex_provided:
        raw = (body.accent_hex or "").strip()
        if raw:
            try:
                new_hex = _parse_hex_colour(raw)
            except ValueError:
                raise HTTPException(400, "accent_hex must be a #RRGGBB colour")

    def _q():
        with ctx.open_db() as conn:
            cfg = get_branding_conn(conn, guild_id)
        if new_mode is not None:
            cfg.accent_mode = new_mode
        if hex_provided:
            cfg.accent_hex = new_hex
        upsert_branding(ctx.db_path, cfg)
        return {
            "ok": True,
            "accent_mode": cfg.normalized_mode(),
            "accent_hex": f"#{cfg.accent_hex:06X}" if cfg.has_custom_colour() else "",
        }

    result = await run_query(_q)
    invalidate_accent_cache(guild_id)
    return result


# ── Needle (auto-thread) config ──────────────────────────────────────────────


_NEEDLE_VALID_TITLE_TYPES   = {"first_fifty", "first_line", "user_date", "custom"}
_NEEDLE_VALID_DELETE_BEHAVIORS = {"archive_if_empty", "archive", "delete", "nothing"}
_NEEDLE_VALID_REPLY_TYPES   = {"default", "custom", "none"}


class NeedleChannelUpdate(BaseModel):
    title_type:          str  = "first_fifty"
    custom_title:        str  = ""
    include_bots:        bool = False
    slowmode:            int  = 0
    delete_behavior:     str  = "archive_if_empty"
    reply_type:          str  = "default"
    custom_reply:        str  = ""
    status_reactions:    bool = False
    archive_immediately: bool = False
    default_reactions:   str  = ""


class NeedleGlobalUpdate(BaseModel):
    emoji_unanswered: str | None = None
    emoji_archived:   str | None = None
    emoji_locked:     str | None = None
    default_reply:    str | None = None


@router.put("/config/needle/settings")
async def update_needle_settings(
    request: Request,
    body: NeedleGlobalUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.emoji_unanswered is not None:
                set_config_value(conn, "needle_emoji_unanswered", body.emoji_unanswered, guild_id)
            if body.emoji_archived is not None:
                set_config_value(conn, "needle_emoji_archived", body.emoji_archived, guild_id)
            if body.emoji_locked is not None:
                set_config_value(conn, "needle_emoji_locked", body.emoji_locked, guild_id)
            if body.default_reply is not None:
                set_config_value(conn, "needle_default_reply", body.default_reply, guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.put("/config/needle/{channel_id}")
async def upsert_needle_channel(
    channel_id: str,
    request: Request,
    body: NeedleChannelUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    if body.title_type not in _NEEDLE_VALID_TITLE_TYPES:
        raise HTTPException(400, f"title_type must be one of {sorted(_NEEDLE_VALID_TITLE_TYPES)}")
    if body.delete_behavior not in _NEEDLE_VALID_DELETE_BEHAVIORS:
        raise HTTPException(400, f"delete_behavior must be one of {sorted(_NEEDLE_VALID_DELETE_BEHAVIORS)}")
    if body.reply_type not in _NEEDLE_VALID_REPLY_TYPES:
        raise HTTPException(400, f"reply_type must be one of {sorted(_NEEDLE_VALID_REPLY_TYPES)}")
    if body.slowmode < 0 or body.slowmode > 21600:
        raise HTTPException(400, "slowmode must be between 0 and 21600 seconds")

    def _q():
        with ctx.open_db() as conn:
            _needle_upsert_channel(
                conn,
                guild_id=guild_id,
                channel_id=int(channel_id),
                title_type=body.title_type,  # type: ignore[arg-type]
                custom_title=body.custom_title,
                include_bots=body.include_bots,
                slowmode=body.slowmode,
                delete_behavior=body.delete_behavior,
                reply_type=body.reply_type,
                custom_reply=body.custom_reply,
                status_reactions=body.status_reactions,
                archive_immediately=body.archive_immediately,
                default_reactions=body.default_reactions,
            )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/config/needle/{channel_id}")
async def remove_needle_channel(
    channel_id: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            removed = _needle_delete_channel(conn, guild_id, int(channel_id))
        if not removed:
            raise HTTPException(404, "Channel not configured for auto-threading")
        return {"ok": True}

    return await run_query(_q)
