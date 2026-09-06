"""Tier 1: whisper repo config get/set."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_repo import (
    backfill_whisper_launcher_channel,
    get_whisper_config,
    set_whisper_config_value,
    set_whisper_launcher_ids,
)

GUILD = 9001


def test_get_config_defaults(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.guild_id == GUILD
    assert cfg.role_id == 0
    assert cfg.channel_id == 0
    assert cfg.log_channel_id == 0
    assert cfg.launcher_message_id == 0
    assert cfg.launcher_channel_id == 0
    assert cfg.sender_feedback is False
    assert cfg.cooldown_seconds == 30
    assert cfg.hourly_cap_per_target == 5


def test_set_and_get_config_value(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD, "whisper_channel_id", "12345")
        set_whisper_config_value(conn, GUILD, "whisper_role_id", "67890")
        set_whisper_config_value(conn, GUILD, "whisper_log_channel_id", "11111")
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.channel_id == 12345
    assert cfg.role_id == 67890
    assert cfg.log_channel_id == 11111


def test_set_and_get_rate_limit_config(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD, "whisper_cooldown_seconds", "60")
        set_whisper_config_value(conn, GUILD, "whisper_hourly_cap_per_target", "3")
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.cooldown_seconds == 60
    assert cfg.hourly_cap_per_target == 3


def test_set_and_get_launcher_ids(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_whisper_launcher_ids(conn, GUILD, 4242, 555)
        cfg = get_whisper_config(conn, GUILD)
    assert cfg.launcher_channel_id == 4242
    assert cfg.launcher_message_id == 555


def test_sender_feedback_dial_reads_as_bool(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD, "whisper_sender_feedback", "1")
        assert get_whisper_config(conn, GUILD).sender_feedback is True
        set_whisper_config_value(conn, GUILD, "whisper_sender_feedback", "0")
        assert get_whisper_config(conn, GUILD).sender_feedback is False


@pytest.mark.parametrize(
    ("stored", "expected", "changed"),
    [
        pytest.param((0, 555), (8001, 555), True, id="legacy-launcher-pinned-to-the-feed"),
        pytest.param((4242, 555), (4242, 555), False, id="already-pinned-is-left-alone"),
        pytest.param((0, 0), (0, 0), False, id="never-posted-gets-no-channel"),
    ],
)
def test_backfill_launcher_channel(sync_db_path: Path, stored, expected, changed):
    """``whisper_launcher_channel_id`` arrived without a migration, and the
    boot bootstrap only writes it when it reposts — so a launcher already at
    the bottom of the feed kept a message id with no channel for as long as
    it stayed there. Pinning it at boot (the feed channel at boot is where it
    was posted) is what lets a later repoint delete it from the OLD channel
    instead of hunting for it in the new one."""
    channel_id, message_id = stored
    with open_db(sync_db_path) as conn:
        set_whisper_config_value(conn, GUILD, "whisper_channel_id", "8001")
        set_whisper_launcher_ids(conn, GUILD, channel_id, message_id)
        assert backfill_whisper_launcher_channel(conn, GUILD) is changed
        assert backfill_whisper_launcher_channel(conn, GUILD) is False  # idempotent
        cfg = get_whisper_config(conn, GUILD)
    assert (cfg.launcher_channel_id, cfg.launcher_message_id) == expected
