"""AI prompt registry: what is editable, and where the rows live.

Two defects live here.

*The prompts are bot-wide but used to be stored per guild.* The dashboard panel
is primary-guild-only, so a second guild had no surface at all — it silently ran
whatever stale ``guild_id=0`` row happened to exist. Prompts are now written at
``guild_id=0``, which every guild's reader resolves through
``get_config_value``'s legacy fallback, so what the panel shows is what every
guild runs. That also makes "Restore Original" work: it used to delete only the
active guild's row, leaving a legacy guild-0 override to fall back onto, so the
button put the *old override* back rather than the shipped default.

*The Rules Watch guard prompt was not in the registry.* The one automated
per-message AI moderation surface was the only one whose instructions an admin
could not read or change.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import ai_config
from tests.db_template import migrated_db

PRIMARY = 1469491362444480666
SECOND = 1476525656115515484


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ai_config.db"
    migrated_db(path)
    return path


# ── registry ───────────────────────────────────────────────────────────


def test_rules_watch_guard_prompt_is_editable():
    """The automatic guard's instructions are a registry entry like the rest."""
    from bot_modules.services.ai_moderation_service import _RULES_WATCH_SYSTEM

    info = {p.key: p for p in ai_config.list_prompts()}
    assert "ai_prompt_rules_watch" in info
    assert info["ai_prompt_rules_watch"].default_factory() == _RULES_WATCH_SYSTEM


def test_the_registry_carries_no_model_dial():
    """One model is loaded at a time, so a per-command model would be a lie."""
    assert not hasattr(ai_config.PromptInfo, "model_key")
    for name in ("get_mod_model", "get_wellness_model", "get_command_model",
                 "set_mod_model", "set_wellness_model", "set_command_model",
                 "KNOWN_MODELS"):
        assert not hasattr(ai_config, name), f"{name} still pretends to be read"


# ── storage scope ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", [p.key for p in ai_config.list_prompts()])
def test_a_global_override_reaches_every_guild(db_path, key):
    with open_db(db_path) as conn:
        ai_config.set_prompt(conn, key, "custom instructions", 0)
    with open_db(db_path) as conn:
        for guild_id in (0, PRIMARY, SECOND):
            assert ai_config.get_prompt(conn, key, guild_id) == "custom instructions"


def test_resetting_the_global_row_restores_the_shipped_default(db_path):
    key = "ai_prompt_query_channel"
    with open_db(db_path) as conn:
        ai_config.set_prompt(conn, key, "These are bulk messages.", 0)
    with open_db(db_path) as conn:
        ai_config.reset_prompt(conn, key, 0)
    with open_db(db_path) as conn:
        text, is_override = ai_config.get_prompt_with_source(conn, key, SECOND)
    assert is_override is False
    assert text == ai_config._PROMPTS_BY_KEY[key].default_factory()


def test_an_unknown_prompt_key_is_refused(db_path):
    with open_db(db_path) as conn:
        with pytest.raises(KeyError):
            ai_config.set_prompt(conn, "ai_prompt_nope", "x", 0)
        with pytest.raises(KeyError):
            ai_config.reset_prompt(conn, "ai_prompt_nope", 0)
        with pytest.raises(KeyError):
            ai_config.get_prompt(conn, "ai_prompt_nope", 0)


def test_get_prompt_from_path_survives_a_missing_database(tmp_path):
    key = "ai_prompt_review"
    text = ai_config.get_prompt_from_path(tmp_path / "nope.db", key)
    assert text == ai_config._PROMPTS_BY_KEY[key].default_factory()


# ── the guard actually uses the configured prompt ──────────────────────


@pytest.mark.asyncio
async def test_the_guard_runs_the_configured_prompt(db_path, monkeypatch):
    from bot_modules.services import ai_moderation_service, ollama_client

    with open_db(db_path) as conn:
        set_config_value(conn, "ai_prompt_rules_watch", "GUARD OVERRIDE", 0)

    seen = {}

    async def _fake_chat(*, system, user_content, **kwargs):
        seen["system"] = system
        return '{"verdict": "flag", "rule": "2", "reason": "r", "confidence": 0.9}'

    monkeypatch.setattr(ollama_client, "chat", _fake_chat)

    result = await ai_moderation_service.ai_rules_watch_check(
        "[12:00] someone: hello",
        db_path=db_path,
        guild_id=SECOND,
    )

    assert seen["system"] == "GUARD OVERRIDE"
    assert result.verdict == "flag"


@pytest.mark.asyncio
async def test_the_guard_falls_back_to_the_shipped_prompt(monkeypatch):
    from bot_modules.services import ai_moderation_service, ollama_client

    seen = {}

    async def _fake_chat(*, system, user_content, **kwargs):
        seen["system"] = system
        return '{"verdict": "ok", "rule": null, "reason": null, "confidence": 0.1}'

    monkeypatch.setattr(ollama_client, "chat", _fake_chat)

    result = await ai_moderation_service.ai_rules_watch_check("[12:00] a: hi")

    assert seen["system"] == ai_moderation_service._RULES_WATCH_SYSTEM
    assert result.verdict == "ok"
