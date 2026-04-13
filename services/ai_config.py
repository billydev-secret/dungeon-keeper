"""Runtime configuration for AI commands (model + system prompts).

Stores model IDs and system prompts in the existing ``config`` table under
well-known keys. Each AI prompt has a hardcoded default (the original
baked-in string) which is returned when no override has been written. This
keeps fresh installations working out of the box while letting admins edit
prompts from the dashboard.

A small per-prompt metadata table (``_PROMPTS``) powers the read-any,
write-any API the web UI depends on. Adding a new editable prompt is a
matter of appending a single entry here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from db_utils import get_config_value, open_db

# ── Model defaults ─────────────────────────────────────────────────────

DEFAULT_MOD_MODEL = "claude-opus-4-6"
DEFAULT_WELLNESS_MODEL = "claude-haiku-4-5-20251001"

# Known-good model IDs we suggest in the dashboard. Admins can still type
# any string; this is only a dropdown hint.
KNOWN_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]

_MOD_MODEL_KEY = "ai_mod_model"
_WELLNESS_MODEL_KEY = "ai_wellness_model"


# ── Prompt registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptInfo:
    key: str  # config key used for storage
    label: str  # human-readable name shown in the dashboard
    description: str  # one-line description
    default_factory: Callable[[], str]  # returns the baked-in default
    model_key: str = ""  # per-command model override key (empty = use global)


def _default_watch_check() -> str:
    from services.ai_moderation_service import _WATCH_CHECK_SYSTEM

    return _WATCH_CHECK_SYSTEM


def _default_review() -> str:
    from services.ai_moderation_service import _REVIEW_SYSTEM

    return _REVIEW_SYSTEM


def _default_scan() -> str:
    from services.ai_moderation_service import _SCAN_SYSTEM

    return _SCAN_SYSTEM


def _default_query_user() -> str:
    from services.ai_moderation_service import _QUERY_SYSTEM

    return _QUERY_SYSTEM


def _default_query_channel() -> str:
    from services.ai_moderation_service import _CHANNEL_QUERY_SYSTEM

    return _CHANNEL_QUERY_SYSTEM


def _default_wellness() -> str:
    from services.wellness_ai import _ENCOURAGEMENT_SYSTEM

    return _ENCOURAGEMENT_SYSTEM


_PROMPTS: list[PromptInfo] = [
    PromptInfo(
        key="ai_prompt_watch_check",
        label="Watch — live rule check",
        description="System prompt for /ai watch (single-message rule check).",
        default_factory=_default_watch_check,
        model_key="ai_model_watch_check",
    ),
    PromptInfo(
        key="ai_prompt_review",
        label="Review — user history",
        description="System prompt for /ai review.",
        default_factory=_default_review,
        model_key="ai_model_review",
    ),
    PromptInfo(
        key="ai_prompt_scan",
        label="Scan — recent channel messages",
        description="System prompt for /ai scan.",
        default_factory=_default_scan,
        model_key="ai_model_scan",
    ),
    PromptInfo(
        key="ai_prompt_query_user",
        label="Query — user question",
        description="System prompt for /ai query (free-form question about a user).",
        default_factory=_default_query_user,
        model_key="ai_model_query_user",
    ),
    PromptInfo(
        key="ai_prompt_query_channel",
        label="Query — channel question",
        description="System prompt for /ai channel (free-form question about a channel).",
        default_factory=_default_query_channel,
        model_key="ai_model_query_channel",
    ),
    PromptInfo(
        key="ai_prompt_wellness_encouragement",
        label="Wellness encouragement",
        description="System prompt for the weekly wellness encouragement note.",
        default_factory=_default_wellness,
        model_key="ai_model_wellness",
    ),
]

# Convenience lookup
_PROMPTS_BY_KEY: dict[str, PromptInfo] = {p.key: p for p in _PROMPTS}


def list_prompts() -> list[PromptInfo]:
    return list(_PROMPTS)


def get_prompt_info(key: str) -> PromptInfo | None:
    return _PROMPTS_BY_KEY.get(key)


# ── Read helpers ───────────────────────────────────────────────────────


def get_mod_model(conn: sqlite3.Connection) -> str:
    return (
        get_config_value(conn, _MOD_MODEL_KEY, DEFAULT_MOD_MODEL) or DEFAULT_MOD_MODEL
    )


def get_wellness_model(conn: sqlite3.Connection) -> str:
    return (
        get_config_value(conn, _WELLNESS_MODEL_KEY, DEFAULT_WELLNESS_MODEL)
        or DEFAULT_WELLNESS_MODEL
    )


def get_command_model(conn: sqlite3.Connection, prompt_key: str) -> str:
    """Return the model for a specific command.

    Checks the per-command model key first, then falls back to the global
    moderation model (or wellness model for the wellness prompt).
    """
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info and info.model_key:
        per_cmd = get_config_value(conn, info.model_key, "")
        if per_cmd:
            return per_cmd
    # Fallback to global
    if prompt_key == "ai_prompt_wellness_encouragement":
        return get_wellness_model(conn)
    return get_mod_model(conn)


def get_command_model_with_source(
    conn: sqlite3.Connection,
    prompt_key: str,
) -> tuple[str, bool]:
    """Return ``(model, is_per_command)``.

    ``is_per_command`` is True when a per-command override is set.
    """
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info and info.model_key:
        per_cmd = get_config_value(conn, info.model_key, "")
        if per_cmd:
            return per_cmd, True
    if prompt_key == "ai_prompt_wellness_encouragement":
        return get_wellness_model(conn), False
    return get_mod_model(conn), False


def get_command_model_from_path(db_path: Path, prompt_key: str) -> str:
    """Like ``get_command_model`` but opens the DB itself."""
    try:
        with open_db(db_path) as conn:
            return get_command_model(conn, prompt_key)
    except sqlite3.Error:
        return DEFAULT_MOD_MODEL


def get_prompt(conn: sqlite3.Connection, key: str) -> str:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "")
    if raw:
        return raw
    return info.default_factory()


def get_prompt_with_source(conn: sqlite3.Connection, key: str) -> tuple[str, bool]:
    """Return ``(text, is_override)``. ``is_override`` is True when the value
    comes from the config table rather than the baked-in default."""
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "")
    if raw:
        return raw, True
    return info.default_factory(), False


# Variants that open the database themselves, for service code paths that
# only have a ``db_path`` (e.g. ``wellness_ai``).


def get_mod_model_from_path(db_path: Path) -> str:
    try:
        with open_db(db_path) as conn:
            return get_mod_model(conn)
    except sqlite3.Error:
        return DEFAULT_MOD_MODEL


def get_wellness_model_from_path(db_path: Path) -> str:
    try:
        with open_db(db_path) as conn:
            return get_wellness_model(conn)
    except sqlite3.Error:
        return DEFAULT_WELLNESS_MODEL


def get_prompt_from_path(db_path: Path, key: str) -> str:
    try:
        with open_db(db_path) as conn:
            return get_prompt(conn, key)
    except sqlite3.Error:
        info = _PROMPTS_BY_KEY.get(key)
        return info.default_factory() if info else ""


# ── Write helpers ──────────────────────────────────────────────────────


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_mod_model(conn: sqlite3.Connection, model: str) -> None:
    set_config(conn, _MOD_MODEL_KEY, model)


def set_wellness_model(conn: sqlite3.Connection, model: str) -> None:
    set_config(conn, _WELLNESS_MODEL_KEY, model)


def set_command_model(conn: sqlite3.Connection, prompt_key: str, model: str) -> None:
    """Set a per-command model override. Pass empty string to clear it."""
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info is None or not info.model_key:
        raise KeyError(f"Unknown AI prompt key: {prompt_key}")
    if model:
        set_config(conn, info.model_key, model)
    else:
        conn.execute("DELETE FROM config WHERE key = ?", (info.model_key,))


def set_prompt(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    set_config(conn, key, value)


def reset_prompt(conn: sqlite3.Connection, key: str) -> None:
    """Delete the override so the baked-in default is used again."""
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    conn.execute("DELETE FROM config WHERE key = ?", (key,))
