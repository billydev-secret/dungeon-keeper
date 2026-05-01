"""Tests for beta_tools.safety — all five safety-rail layers."""

from __future__ import annotations


import pytest

from beta_tools.safety import assert_safe_to_start


def _set_minimal_env(monkeypatch, *, env="dev", db_path="dk_dev.db"):
    monkeypatch.setenv("BOT_ENV", env)
    monkeypatch.setenv("DISCORD_TOKEN_DEV", "main-dev-token")
    monkeypatch.setenv("GUILD_ID_DEV", "9001")
    monkeypatch.setenv("DB_PATH_DEV", db_path)
    monkeypatch.setenv("AUDIT_CHANNEL_DEV", "9999")
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "1")


def test_assert_safe_to_start_passes_in_dev(monkeypatch):
    _set_minimal_env(monkeypatch)
    # should not raise
    assert_safe_to_start()


def test_assert_safe_to_start_exits_in_prod(monkeypatch):
    _set_minimal_env(monkeypatch, env="prod", db_path="dungeonkeeper.db")
    monkeypatch.setenv("DISCORD_TOKEN_PROD", "prod-token")
    monkeypatch.setenv("GUILD_ID_PROD", "1")
    monkeypatch.setenv("DB_PATH_PROD", "dungeonkeeper.db")
    monkeypatch.setenv("AUDIT_CHANNEL_PROD", "0")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_tools_disabled(monkeypatch):
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "0")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_db_path_missing_dev(monkeypatch):
    _set_minimal_env(monkeypatch, db_path="dungeonkeeper.db")
    with pytest.raises(SystemExit):
        assert_safe_to_start()


def test_assert_safe_to_start_exits_when_tools_id_matches_prod_id(monkeypatch):
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("EXPECTED_BOT_ID_PROD", "10001")  # collide with TOOLS
    with pytest.raises(SystemExit):
        assert_safe_to_start()
