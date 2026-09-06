"""Component-level: whisper config web routes."""
from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_models import WhisperState

_PANEL = (
    Path(__file__).resolve().parents[2]
    / "src" / "web_server" / "static" / "js" / "panels" / "mod-whisper-audit.js"
)


def test_whisper_section_reads_defaults(sync_db_path: Path):
    from web_server.routes.config import _whisper_section
    with open_db(sync_db_path) as conn:
        section = _whisper_section(conn, 9001)
    assert section["channel_id"] == "0"
    assert section["role_id"] == "0"
    assert section["log_channel_id"] == "0"
    assert section["cooldown_seconds"] == 30
    assert section["hourly_cap_per_target"] == 5


def test_whisper_section_reads_set_values(sync_db_path: Path):
    from bot_modules.services.whisper_repo import set_whisper_config_value
    from web_server.routes.config import _whisper_section
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, 9001, "whisper_channel_id", "777")
        set_whisper_config_value(conn, 9001, "whisper_role_id", "888")
        set_whisper_config_value(conn, 9001, "whisper_log_channel_id", "999")
        set_whisper_config_value(conn, 9001, "whisper_cooldown_seconds", "60")
        set_whisper_config_value(conn, 9001, "whisper_hourly_cap_per_target", "3")
        section = _whisper_section(conn, 9001)
    assert section == {
        "channel_id": "777",
        "role_id": "888",
        "log_channel_id": "999",
        "cooldown_seconds": 60,
        "hourly_cap_per_target": 3,
        # Unset here, so this is the schema default the panel now exposes —
        # it was a hard-coded 3 with no control until 2026-08-30.
        "guesses_per_whisper": 3,
        # Ships dark: existing whispers must not start DMing their senders
        # until an admin flips it (2026-09 review, rotation-rooms-159).
        "sender_feedback": False,
    }


def test_whisper_config_update_schema_present():
    from web_server.routes.config import WhisperConfigUpdate
    body = WhisperConfigUpdate(
        channel_id="111", role_id="222", log_channel_id="333",
        cooldown_seconds=45, hourly_cap_per_target=4,
    )
    assert body.channel_id == "111"
    assert body.cooldown_seconds == 45
    assert body.hourly_cap_per_target == 4


def test_whisper_audit_filter_offers_exactly_the_states_that_exist():
    """The audit panel's State filter used to list Expired / Rejected /
    Accepted — states no whisper has ever been in — and omitted Shared, the
    one a mod might actually want (140 prod rows). The options are the
    model's ``WhisperState`` literal, nothing more and nothing less
    (2026-09 review, rotation-rooms-163)."""
    src = _PANEL.read_text(encoding="utf-8")
    block = re.search(r"const STATE_LABELS = \{(.*?)\};", src, re.S)
    assert block, "STATE_LABELS not found in mod-whisper-audit.js"
    offered = set(re.findall(r"^\s*(\w+):\s*\"", block.group(1), re.M))
    assert offered == set(get_args(WhisperState))
