"""``game_history`` — the history INSERT shared by the self-stored games."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from bot_modules.games.utils.game_history import history_insert, history_started_at


def test_started_at_matches_the_current_timestamp_shape():
    assert history_started_at(1_700_000_000.0) == "2023-11-14 22:13:20"


def _rows(db_path: Path) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM games_game_history").fetchall()
    return [dict(r) for r in rows]


def test_history_insert_records_once_per_game_id(sync_db_path: Path):
    sql, params = history_insert(
        game_id="chicken:7", game_type="chicken", channel_id=100, host_id=1,
        player_count=3, round_count=1, payload={"players": [1, 2, 3]},
        started_at=1_700_000_000.0, guild_id=9001,
    )
    with sqlite3.connect(sync_db_path) as conn:
        conn.execute(sql, params)
        conn.execute(sql, params)  # a replayed terminal hook

    rows = _rows(sync_db_path)
    assert len(rows) == 1
    row = rows[0]
    assert (row["game_id"], row["game_type"], row["guild_id"]) == ("chicken:7", "chicken", 9001)
    assert (row["host_id"], row["player_count"], row["round_count"]) == (1, 3, 1)
    assert row["started_at"] == "2023-11-14 22:13:20"
    assert row["ended_at"]  # the column default stamps the close
    assert json.loads(row["payload"]) == {"players": [1, 2, 3]}
