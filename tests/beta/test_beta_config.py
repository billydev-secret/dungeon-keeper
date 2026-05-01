"""Tests for BetaConfig and load_beta_config."""

from __future__ import annotations

import pytest

from beta_tools.config import BetaConfig, load_beta_config


def test_beta_config_dataclass_fields():
    cfg = BetaConfig(
        tools_token="tools-token",
        tools_expected_id=10001,
        puppet_tokens=("p1", "p2", "p3"),
        puppet_expected_ids=(20001, 20002, 20003),
        enabled=True,
        ambient_rate_multiplier=1.0,
        ambient_autostart=True,
        llm_blend=False,
    )
    assert cfg.tools_token == "tools-token"
    assert cfg.puppet_tokens == ("p1", "p2", "p3")
    assert cfg.enabled is True


def test_load_beta_config_reads_env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_TOOLS_ENABLED", "1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.setenv("BETA_AMBIENT_RATE_MULTIPLIER", "1.5")
    monkeypatch.setenv("BETA_AMBIENT_AUTOSTART", "0")
    monkeypatch.setenv("BETA_LLM_BLEND", "1")
    cfg = load_beta_config()
    assert cfg.tools_token == "tools-token"
    assert cfg.tools_expected_id == 10001
    assert cfg.puppet_tokens == ("p1", "p2", "p3")
    assert cfg.puppet_expected_ids == (20001, 20002, 20003)
    assert cfg.enabled is True
    assert cfg.ambient_rate_multiplier == 1.5
    assert cfg.ambient_autostart is False
    assert cfg.llm_blend is True


def test_load_beta_config_missing_token_raises(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN_TOOLS", raising=False)
    with pytest.raises(KeyError):
        load_beta_config()


def test_load_beta_config_defaults_when_optional_missing(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN_TOOLS", "tools-token")
    monkeypatch.setenv("EXPECTED_BOT_ID_TOOLS", "10001")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_1", "p1")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_2", "p2")
    monkeypatch.setenv("BETA_PUPPET_TOKEN_3", "p3")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_1", "20001")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_2", "20002")
    monkeypatch.setenv("EXPECTED_BOT_ID_PUPPET_3", "20003")
    monkeypatch.delenv("BETA_TOOLS_ENABLED", raising=False)
    monkeypatch.delenv("BETA_AMBIENT_RATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("BETA_AMBIENT_AUTOSTART", raising=False)
    monkeypatch.delenv("BETA_LLM_BLEND", raising=False)
    cfg = load_beta_config()
    assert cfg.enabled is False  # default
    assert cfg.ambient_rate_multiplier == 1.0  # default
    assert cfg.ambient_autostart is True  # default
    assert cfg.llm_blend is False  # default
