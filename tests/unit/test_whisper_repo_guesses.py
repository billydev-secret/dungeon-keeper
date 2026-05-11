"""Tier 1: whisper guesses + state mutators."""
from __future__ import annotations

from pathlib import Path

from db_utils import open_db
from services.whisper_repo import (
    decrement_guesses_left,
    get_whisper,
    insert_guess,
    insert_whisper,
    list_guesses,
    mark_solved,
    mark_exposed,
    try_consume_guess,
)

GUILD, SENDER, TARGET = 9001, 1001, 2001


def test_insert_and_list_guess(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        insert_guess(conn, whisper_id=wid, guessed_id=999, correct=False)
        insert_guess(conn, whisper_id=wid, guessed_id=SENDER, correct=True)
        guesses = list_guesses(conn, whisper_id=wid)
    assert len(guesses) == 2
    assert sum(1 for g in guesses if g.correct) == 1


def test_decrement_guesses_left(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        decrement_guesses_left(conn, wid)
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.guesses_left == 2


def test_mark_solved(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        mark_solved(conn, wid)
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.solved is True


def test_mark_exposed(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        mark_exposed(conn, wid)
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.exposed is True


def test_guesses_cascade_delete(sync_db_path: Path):
    """Deleting a whisper should cascade to its guesses."""
    with open_db(sync_db_path) as conn:
        # SQLite needs FKs explicitly enabled per-connection
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        insert_guess(conn, whisper_id=wid, guessed_id=999, correct=False)
        conn.execute("DELETE FROM whispers WHERE id = ?", (wid,))
        guesses = list_guesses(conn, whisper_id=wid)
    assert guesses == []


# ── B5: try_consume_guess ────────────────────────────────────────────────────


def test_try_consume_guess_first_two_succeed_third_fails(sync_db_path: Path):
    """With guesses_left=2: first two calls return True, third returns False."""
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        # Default guesses_left is 3; set to 2 for this test
        conn.execute("UPDATE whispers SET guesses_left = 2 WHERE id = ?", (wid,))

    with open_db(sync_db_path) as conn:
        r1 = try_consume_guess(conn, wid)
    with open_db(sync_db_path) as conn:
        r2 = try_consume_guess(conn, wid)
    with open_db(sync_db_path) as conn:
        r3 = try_consume_guess(conn, wid)

    assert r1 is True
    assert r2 is True
    assert r3 is False


def test_try_consume_guess_fails_when_already_solved(sync_db_path: Path):
    """If whisper is already solved, try_consume_guess returns False."""
    from services.whisper_repo import mark_solved
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        mark_solved(conn, wid)
        result = try_consume_guess(conn, wid)
    assert result is False
