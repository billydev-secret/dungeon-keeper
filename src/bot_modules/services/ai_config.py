"""Runtime configuration for AI commands (system prompts).

Stores the system prompts in the existing ``config`` table under well-known
keys. Each AI prompt has a hardcoded default which is returned when no override
has been written, keeping fresh installations working out of the box.

There is deliberately no *model* setting here. Exactly one local model is
served at a time, chosen by the model-source settings (``llm_model_path`` /
``llm_hf_repo`` / ``llm_hf_file``) on the same panel and loaded at startup;
``ollama_client.chat`` takes no model argument, so a per-command model dial
would be a preference nothing enforces.

Prompts are **bot-wide**: the dashboard panel is primary-guild-only and writes
them at ``guild_id=0``, which every guild's readers resolve through
``get_config_value``'s legacy fallback.
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

# ── Prompt registry ────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptInfo:
    key: str
    label: str
    description: str
    default_factory: Callable[[], str]


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


def _default_rules_watch() -> str:
    from bot_modules.services.ai_moderation_service import _RULES_WATCH_SYSTEM
    return _RULES_WATCH_SYSTEM


_PROMPTS: list[PromptInfo] = [
    PromptInfo(
        key="ai_prompt_watch_check",
        label="Watch — live rule check",
        description="System prompt for /ai watch (single-message rule check).",
        default_factory=_default_watch_check,
    ),
    PromptInfo(
        key="ai_prompt_review",
        label="Review — user history",
        description="System prompt for /ai review.",
        default_factory=_default_review,
    ),
    PromptInfo(
        key="ai_prompt_scan",
        label="Scan — recent channel messages",
        description="System prompt for /ai scan.",
        default_factory=_default_scan,
    ),
    PromptInfo(
        key="ai_prompt_query_user",
        label="Query — user question",
        description="System prompt for /ai query (free-form question about a user).",
        default_factory=_default_query_user,
    ),
    PromptInfo(
        key="ai_prompt_query_channel",
        label="Query — channel question",
        description="System prompt for /ai channel (free-form question about a channel).",
        default_factory=_default_query_channel,
    ),
    PromptInfo(
        key="ai_prompt_wellness_encouragement",
        label="Wellness encouragement",
        description="System prompt for the weekly wellness encouragement note.",
        default_factory=_default_wellness,
    ),
    PromptInfo(
        key="ai_prompt_rules_watch",
        label="Rules Watch — automatic guard",
        description=(
            "System prompt for the passive Rules Watch guard, which reads every "
            "public message. It must keep asking for the JSON verdict block "
            "exactly as written — anything else and every message reads as fine."
        ),
        default_factory=_default_rules_watch,
    ),
]

_PROMPTS_BY_KEY: dict[str, PromptInfo] = {p.key: p for p in _PROMPTS}


def list_prompts() -> list[PromptInfo]:
    return list(_PROMPTS)


# ── Read helpers ───────────────────────────────────────────────────────


def get_prompt(conn: sqlite3.Connection, key: str, guild_id: int = 0) -> str:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "", guild_id)
    return raw if raw else info.default_factory()


def get_prompt_with_source(
    conn: sqlite3.Connection, key: str, guild_id: int = 0
) -> tuple[str, bool]:
    info = _PROMPTS_BY_KEY.get(key)
    if info is None:
        raise KeyError(f"Unknown AI prompt key: {key}")
    raw = get_config_value(conn, key, "", guild_id)
    if raw:
        return raw, True
    return info.default_factory(), False


def get_prompt_from_path(db_path: Path, key: str, guild_id: int = 0) -> str:
    try:
        with open_db(db_path) as conn:
            return get_prompt(conn, key, guild_id)
    except Exception:
        info = _PROMPTS_BY_KEY.get(key)
        return info.default_factory() if info else ""


# ── Write helpers ──────────────────────────────────────────────────────


def set_config(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    _db_set_config_value(conn, key, value, guild_id)


def set_prompt(conn: sqlite3.Connection, key: str, value: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    set_config(conn, key, value, guild_id)


def reset_prompt(conn: sqlite3.Connection, key: str, guild_id: int = 0) -> None:
    if key not in _PROMPTS_BY_KEY:
        raise KeyError(f"Unknown AI prompt key: {key}")
    delete_config_value(conn, key, guild_id)
