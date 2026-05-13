"""Tier 1 unit tests: whisper migration creates expected tables/indexes."""
from __future__ import annotations

from pathlib import Path

from bot_modules.core.db_utils import open_db


def test_whispers_table_exists(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='whispers'"
        ).fetchone()
    assert row is not None


def test_whispers_columns(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rows = conn.execute("PRAGMA table_info(whispers)").fetchall()
    columns = {r["name"] for r in rows}
    assert columns >= {
        "id", "guild_id", "sender_id", "target_id", "message",
        "created_at", "state", "solved", "exposed", "guesses_left",
        "channel_msg_id", "dm_msg_id",
    }


def test_whisper_guesses_table_exists(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='whisper_guesses'"
        ).fetchone()
    assert row is not None


def test_whisper_indexes_exist(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "idx_whispers_target" in names
    assert "idx_whispers_sender" in names
    assert "idx_whisper_guesses_whisper" in names
