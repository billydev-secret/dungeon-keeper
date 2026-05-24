"""Runtime configuration for AI commands (model + system prompts).

Stores model IDs and system prompts in the existing ``config`` table under
well-known keys. Each AI prompt has a hardcoded default which is returned when
no override has been written, keeping fresh installations working out of the box.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import (
    delete_config_value,
    get_config_value,
    open_db,
    set_config_value as _db_set_config_value,
)
from bot_modules.services import ollama_client

# ── Model defaults ─────────────────────────────────────────────────────

DEFAULT_MOD_MODEL = ollama_client.default_model()
DEFAULT_WELLNESS_MODEL = ollama_client.default_model()

KNOWN_MODELS: list[str] = [
    "Llama-3.2-3B-Instruct-Q4_K_M.gguf",   # default
    "Llama-3.2-3B-Instruct-Q8_0.gguf",
    "Llama-3.1-8B-Instruct-Q4_K_M.gguf",
    "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
    "gemma-2-2b-it-Q4_K_M.gguf",
]

_MOD_MODEL_KEY = "ai_mod_model"
_WELLNESS_MODEL_KEY = "ai_wellness_model"


# ── Prompt registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptInfo:
    key: str
    label: str
    description: str
    default_factory: Callable[[], str]
    model_key: str = ""


def _default_watch_check() -> str:
    from bot_modules.services.ai_moderation_service import _WATCH_CHECK_SYSTEM
    return _WATCH_CHECK_SYSTEM


def _default_review() -> str:
    from bot_modules.services.ai_moderation_service import _REVIEW_SYSTEM
    return _REVIEW_SYSTEM


def _default_scan() -> str:
    from bot_modules.services.ai_moderation_service import _SCAN_SYSTEM
    return _SCAN_SYSTEM


def _default_query_user() -> str:
    from bot_modules.services.ai_moderation_service import _QUERY_SYSTEM
    return _QUERY_SYSTEM


def _default_query_channel() -> str:
    from bot_modules.services.ai_moderation_service import _CHANNEL_QUERY_SYSTEM
    return _CHANNEL_QUERY_SYSTEM


def _default_wellness() -> str:
    from bot_modules.services.wellness_ai import _ENCOURAGEMENT_SYSTEM
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

_PROMPTS_BY_KEY: dict[str, PromptInfo] = {p.key: p for p in _PROMPTS}


def list_prompts() -> list[PromptInfo]:
    return list(_PROMPTS)


def get_prompt_info(key: str) -> PromptInfo | None:
    return _PROMPTS_BY_KEY.get(key)


# ── Read helpers ───────────────────────────────────────────────────────


def get_mod_model(conn: sqlite3.Connection) -> str:
    return get_config_value(conn, _MOD_MODEL_KEY, DEFAULT_MOD_MODEL) or DEFAULT_MOD_MODEL


def get_wellness_model(conn: sqlite3.Connection) -> str:
    return get_config_value(conn, _WELLNESS_MODEL_KEY, DEFAULT_WELLNESS_MODEL) or DEFAULT_WELLNESS_MODEL


def get_command_model(conn: sqlite3.Connection, prompt_key: str) -> str:
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info and info.model_key:
        per_cmd = get_config_value(conn, info.model_key, "")
        if per_cmd:
            return per_cmd
    if prompt_key == "ai_prompt_wellness_encouragement":
        return get_wellness_model(conn)
    return get_mod_model(conn)


def get_command_model_with_source(
    conn: sqlite3.Connection, prompt_key: str
) -> tuple[str, bool]:
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info and info.model_key:
        per_cmd = get_config_value(conn, info.model_key, "")
        if per_cmd:
            return per_cmd, True
    if prompt_key == "ai_prompt_wellness_encouragement":
        return get_wellness_model(conn), False
    return get_mod_model(conn), False


def get_command_model_from_path(db_path: Path, prompt_key: str) -> str:
    try:
        with open_db(db_path) as conn:
            return get_command_model(conn, prompt_key)
    except Exception:
        return DEFAULT_MOD_MODEL


def get_prompt(conn: sqlite3.Connection, key: str) -> str:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "")
    return raw if raw else info.default_factory()


def get_prompt_with_source(conn: sqlite3.Connection, key: str) -> tuple[str, bool]:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "")
    if raw:
        return raw, True
    return info.default_factory(), False


def get_mod_model_from_path(db_path: Path) -> str:
    try:
        with open_db(db_path) as conn:
            return get_mod_model(conn)
    except Exception:
        return DEFAULT_MOD_MODEL


def get_wellness_model_from_path(db_path: Path) -> str:
    try:
        with open_db(db_path) as conn:
            return get_wellness_model(conn)
    except Exception:
        return DEFAULT_WELLNESS_MODEL


def get_prompt_from_path(db_path: Path, key: str) -> str:
    try:
        with open_db(db_path) as conn:
            return get_prompt(conn, key)
    except Exception:
        info = _PROMPTS_BY_KEY.get(key)
        return info.default_factory() if info else ""


# ── Write helpers ──────────────────────────────────────────────────────


def set_config(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    _db_set_config_value(conn, key, value, guild_id)


def set_mod_model(conn: sqlite3.Connection, model: str, guild_id: int = 0) -> None:
    set_config(conn, _MOD_MODEL_KEY, model, guild_id)


def set_wellness_model(conn: sqlite3.Connection, model: str, guild_id: int = 0) -> None:
    set_config(conn, _WELLNESS_MODEL_KEY, model, guild_id)


def set_command_model(
    conn: sqlite3.Connection, prompt_key: str, model: str, guild_id: int = 0
) -> None:
    info = _PROMPTS_BY_KEY.get(prompt_key)
    if info is None or not info.model_key:
        raise KeyError(f"Unknown AI prompt key: {prompt_key}")
    if model:
        set_config(conn, info.model_key, model, guild_id)
    else:
        delete_config_value(conn, info.model_key, guild_id)


def set_prompt(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    set_config(conn, key, value, guild_id)


def reset_prompt(conn: sqlite3.Connection, key: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    delete_config_value(conn, key, guild_id)
