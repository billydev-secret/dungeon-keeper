"""Tier 1: whisper reply repo helpers."""
from __future__ import annotations

from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.services.whisper_repo import (
    insert_reply,
    insert_whisper,
    list_replies_for_whisper,
)

GUILD, SENDER, TARGET = 9001, 1001, 2001


def test_insert_and_list_reply(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(
            conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x"
        )
        insert_reply(
            conn,
            whisper_id=wid,
            from_user_id=TARGET,
            to_user_id=SENDER,
            content="hello back",
        )
        insert_reply(
            conn,
            whisper_id=wid,
            from_user_id=SENDER,
            to_user_id=TARGET,
            content="thanks",
        )
        replies = list_replies_for_whisper(conn, whisper_id=wid)
    assert len(replies) == 2
    assert [r.content for r in replies] == ["hello back", "thanks"]
    assert [r.from_user_id for r in replies] == [TARGET, SENDER]


def test_list_replies_empty_when_none(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(
            conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x"
        )
        replies = list_replies_for_whisper(conn, whisper_id=wid)
    assert replies == []


def test_replies_cascade_delete(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        wid = insert_whisper(
            conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x"
        )
        insert_reply(
            conn, whisper_id=wid, from_user_id=TARGET, to_user_id=SENDER, content="r"
        )
        conn.execute("DELETE FROM whispers WHERE id = ?", (wid,))
        replies = list_replies_for_whisper(conn, whisper_id=wid)
    assert replies == []


def test_count_replies(sync_db_path: Path):
    from bot_modules.services.whisper_repo import count_replies
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(
            conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x"
        )
        assert count_replies(conn, wid) == 0
        insert_reply(
            conn, whisper_id=wid, from_user_id=TARGET, to_user_id=SENDER, content="r1"
        )
        assert count_replies(conn, wid) == 1
        insert_reply(
            conn, whisper_id=wid, from_user_id=SENDER, to_user_id=TARGET, content="r2"
        )
        assert count_replies(conn, wid) == 2


def test_count_replies_isolated_per_whisper(sync_db_path: Path):
    from bot_modules.services.whisper_repo import count_replies
    with open_db(sync_db_path) as conn:
        w1 = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="a")
        w2 = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="b")
        insert_reply(conn, whisper_id=w1, from_user_id=TARGET, to_user_id=SENDER, content="r")
    with open_db(sync_db_path) as conn:
        assert count_replies(conn, w1) == 1
        assert count_replies(conn, w2) == 0
