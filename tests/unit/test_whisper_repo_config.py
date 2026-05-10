"""Tier 1: whisper repo config get/set."""
from __future__ import annotations

from pathlib import Path

from db_utils import open_db
from services.whisper_repo import get_whisper_config, set_whisper_config_value

GUILD = 9001


def test_get_config_defaults(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.guild_id == GUILD
    assert cfg.role_id == 0
    assert cfg.channel_id == 0
    assert cfg.log_channel_id == 0


def test_set_and_get_config_value(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD, "whisper_channel_id", "12345")
        set_whisper_config_value(conn, GUILD, "whisper_role_id", "67890")
        set_whisper_config_value(conn, GUILD, "whisper_log_channel_id", "11111")
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.channel_id == 12345
    assert cfg.role_id == 67890
    assert cfg.log_channel_id == 11111
