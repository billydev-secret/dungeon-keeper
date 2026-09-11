"""The board-sticky dial: off by default, per-guild, one key for every game
that wires itself to it (today, Mt. Rushmore Draft)."""

from __future__ import annotations

import sqlite3

from bot_modules.services import game_board_sticky_service as svc


def _config_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, key TEXT NOT NULL, "
        "value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    return conn


def test_board_sticky_defaults_off():
    conn = _config_conn()
    assert svc.get_board_sticky_enabled(conn) is False


def test_board_sticky_toggle_round_trips():
    conn = _config_conn()
    svc.set_board_sticky_enabled(conn, True)
    assert svc.get_board_sticky_enabled(conn) is True
    svc.set_board_sticky_enabled(conn, False)
    assert svc.get_board_sticky_enabled(conn) is False


def test_board_sticky_is_per_guild():
    conn = _config_conn()
    svc.set_board_sticky_enabled(conn, True, guild_id=42)
    assert svc.get_board_sticky_enabled(conn, guild_id=42) is True
    # A different guild keeps the (off) default rather than inheriting 42's.
    assert svc.get_board_sticky_enabled(conn, guild_id=99) is False
