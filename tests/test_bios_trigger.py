"""Tests for the bios trigger embed and its admin-configurable copy."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from bot_modules.bios.trigger import (
    DEFAULT_TRIGGER_BODY,
    DEFAULT_TRIGGER_TITLE,
    build_trigger_embed,
    get_trigger_content,
)
from bot_modules.core.db_utils import set_config_value


def _conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def test_build_trigger_embed_defaults():
    embed = build_trigger_embed()
    assert embed.title == DEFAULT_TRIGGER_TITLE
    assert embed.description == DEFAULT_TRIGGER_BODY


def test_build_trigger_embed_custom_copy_and_color():
    embed = build_trigger_embed("🌾 Meadow", "Come on in.", 0x123456)
    assert embed.title == "🌾 Meadow"
    assert embed.description == "Come on in."
    assert embed.color is not None and embed.color.value == 0x123456


def test_get_trigger_content_defaults_when_unset(sync_db_path):
    with _conn(sync_db_path) as conn:
        title, body = get_trigger_content(conn, guild_id=42)
    assert title == DEFAULT_TRIGGER_TITLE
    assert body == DEFAULT_TRIGGER_BODY


def test_get_trigger_content_returns_configured(sync_db_path):
    with _conn(sync_db_path) as conn:
        set_config_value(conn, "bios_trigger_title", "🌾 Welcome to the Meadow", 42)
        set_config_value(conn, "bios_trigger_body", "Tap below — answer what you like.", 42)
        conn.commit()
        title, body = get_trigger_content(conn, guild_id=42)
    assert title == "🌾 Welcome to the Meadow"
    assert body == "Tap below — answer what you like."


def test_get_trigger_content_blank_falls_back_to_default(sync_db_path):
    with _conn(sync_db_path) as conn:
        set_config_value(conn, "bios_trigger_title", "", 42)
        conn.commit()
        title, body = get_trigger_content(conn, guild_id=42)
    assert title == DEFAULT_TRIGGER_TITLE
    assert body == DEFAULT_TRIGGER_BODY
