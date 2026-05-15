"""Unit tests for the GamesDb async adapter."""

from __future__ import annotations

from bot_modules.services.games_db import GamesDb

_INSERT_Q = (
    "INSERT INTO games_question_bank (game_type, category, question_text) VALUES (?, ?, ?)"
)


async def test_execute_inserts_row(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(_INSERT_Q, ("wyr", "sfw", "Would you rather fly or be invisible?"))
    rows = await db.fetchall("SELECT * FROM games_question_bank")
    assert len(rows) == 1
    assert rows[0]["question_text"] == "Would you rather fly or be invisible?"


async def test_fetchone_returns_matching_row(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(_INSERT_Q, ("nhie", "nsfw", "Never have I ever?"))
    row = await db.fetchone(
        "SELECT * FROM games_question_bank WHERE game_type = ?", ("nhie",)
    )
    assert row is not None
    assert row["category"] == "nsfw"
    assert row["game_type"] == "nhie"


async def test_fetchone_returns_none_when_no_match(sync_db_path):
    db = GamesDb(sync_db_path)
    row = await db.fetchone(
        "SELECT * FROM games_question_bank WHERE question_id = ?", (99999,)
    )
    assert row is None


async def test_fetchall_returns_multiple_rows(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.executemany(
        _INSERT_Q,
        [
            ("wyr", "sfw", "A?"),
            ("wyr", "sfw", "B?"),
            ("wyr", "nsfw", "C?"),
        ],
    )
    rows = await db.fetchall("SELECT * FROM games_question_bank WHERE game_type = 'wyr'")
    assert len(rows) == 3


async def test_fetchall_returns_empty_list_when_no_rows(sync_db_path):
    db = GamesDb(sync_db_path)
    rows = await db.fetchall("SELECT * FROM games_question_bank")
    assert rows == []


async def test_executemany_inserts_batch(sync_db_path):
    db = GamesDb(sync_db_path)
    items = [("mlt", "sfw", f"Q{i}?") for i in range(5)]
    await db.executemany(_INSERT_Q, items)
    rows = await db.fetchall("SELECT * FROM games_question_bank")
    assert len(rows) == 5


async def test_lastrowid_returns_int_id(sync_db_path):
    db = GamesDb(sync_db_path)
    row_id = await db.lastrowid(_INSERT_Q, ("mlt", "sfw", "Pick one?"))
    assert isinstance(row_id, int)
    assert row_id > 0


async def test_lastrowid_id_matches_inserted_row(sync_db_path):
    db = GamesDb(sync_db_path)
    row_id = await db.lastrowid(_INSERT_Q, ("ama", "sfw", "Ask me anything?"))
    row = await db.fetchone(
        "SELECT * FROM games_question_bank WHERE question_id = ?", (row_id,)
    )
    assert row is not None
    assert row["game_type"] == "ama"


async def test_row_factory_allows_column_name_access(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(_INSERT_Q, ("wyr", "sfw", "Column access test?"))
    row = await db.fetchone("SELECT * FROM games_question_bank")
    assert row is not None
    assert row["game_type"] == "wyr"
    assert row["category"] == "sfw"
    assert row["question_text"] == "Column access test?"
    assert row["added_by"] == 0


async def test_execute_returns_cursor_with_rowcount(sync_db_path):
    db = GamesDb(sync_db_path)
    await db.execute(_INSERT_Q, ("nhie", "sfw", "First?"))
    cur = await db.execute(
        "UPDATE games_question_bank SET category = 'nsfw' WHERE game_type = 'nhie'"
    )
    assert cur.rowcount == 1
