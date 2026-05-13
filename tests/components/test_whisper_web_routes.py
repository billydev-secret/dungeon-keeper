"""Component-level: whisper config web routes."""
from __future__ import annotations

from pathlib import Path

from bot_modules.core.db_utils import open_db


def test_whisper_section_reads_defaults(sync_db_path: Path):
    from web_server.routes.config import _whisper_section
    with open_db(sync_db_path) as conn:
        section = _whisper_section(conn, 9001)
    assert section["channel_id"] == "0"
    assert section["role_id"] == "0"
    assert section["log_channel_id"] == "0"


def test_whisper_section_reads_set_values(sync_db_path: Path):
    from bot_modules.services.whisper_repo import set_whisper_config_value
    from web_server.routes.config import _whisper_section
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, 9001, "whisper_channel_id", "777")
        set_whisper_config_value(conn, 9001, "whisper_role_id", "888")
        set_whisper_config_value(conn, 9001, "whisper_log_channel_id", "999")
        section = _whisper_section(conn, 9001)
    assert section == {"channel_id": "777", "role_id": "888", "log_channel_id": "999"}


def test_whisper_config_update_schema_present():
    from web_server.routes.config import WhisperConfigUpdate
    body = WhisperConfigUpdate(channel_id="111", role_id="222", log_channel_id="333")
    assert body.channel_id == "111"
