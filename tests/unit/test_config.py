"""Tier 1 unit tests: config loading and safety assertions."""


import pytest

from config import Config, load_config
from safety import check_db_path


# ── Config properties ─────────────────────────────────────────────────

def test_is_dev():
    cfg = Config(
        env="dev", token="t", guild_id=1, db_path="dk_dev.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    assert cfg.is_dev is True
    assert cfg.is_prod is False


def test_is_prod():
    cfg = Config(
        env="prod", token="t", guild_id=1, db_path="dk.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    assert cfg.is_prod is True
    assert cfg.is_dev is False


def test_reset_dev_db_only_in_dev():
    cfg_dev = Config(
        env="dev", token="t", guild_id=1, db_path="dk_dev.db",
        audit_channel_id=0, reset_dev_db=True, seed_dev_fixtures=False,
    )
    cfg_prod = Config(
        env="prod", token="t", guild_id=1, db_path="dk.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    assert cfg_dev.reset_dev_db is True
    assert cfg_prod.reset_dev_db is False


# ── load_config validation ─────────────────────────────────────────────

def test_load_config_bad_env(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "staging")
    monkeypatch.setenv("DISCORD_TOKEN_STAGING", "fake")
    with pytest.raises(ValueError, match="BOT_ENV must be"):
        load_config()


def test_load_config_dev(monkeypatch):
    monkeypatch.setenv("BOT_ENV", "dev")
    monkeypatch.setenv("DISCORD_TOKEN_DEV", "dev-token")
    monkeypatch.setenv("GUILD_ID_DEV", "9001")
    monkeypatch.setenv("DB_PATH_DEV", "dk_dev.db")
    monkeypatch.setenv("AUDIT_CHANNEL_DEV", "9999")
    cfg = load_config()
    assert cfg.env == "dev"
    assert cfg.token == "dev-token"
    assert cfg.guild_id == 9001
    assert cfg.is_dev


# ── check_db_path safety rail ─────────────────────────────────────────

def test_dev_cfg_requires_dev_in_path():
    cfg = Config(
        env="dev", token="t", guild_id=1, db_path="dk_dev.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    check_db_path(cfg)  # should not raise


def test_dev_cfg_missing_dev_in_path_exits(tmp_path):
    cfg = Config(
        env="dev", token="t", guild_id=1, db_path="dungeonkeeper.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    with pytest.raises(SystemExit):
        check_db_path(cfg)


def test_prod_cfg_with_dev_in_path_exits():
    cfg = Config(
        env="prod", token="t", guild_id=1, db_path="dk_dev.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    with pytest.raises(SystemExit):
        check_db_path(cfg)


def test_prod_cfg_without_dev_in_path():
    cfg = Config(
        env="prod", token="t", guild_id=1, db_path="dungeonkeeper.db",
        audit_channel_id=0, reset_dev_db=False, seed_dev_fixtures=False,
    )
    check_db_path(cfg)  # should not raise
