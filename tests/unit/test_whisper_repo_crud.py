"""Tier 1: whisper repo CRUD."""
from __future__ import annotations

from pathlib import Path

from db_utils import open_db
from services.whisper_repo import (
    get_whisper,
    insert_whisper,
    list_received,
    set_whisper_message_ids,
    update_whisper_state,
)

GUILD = 9001
SENDER = 1001
TARGET = 2001


def test_insert_and_get_whisper(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(
            conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="hi"
        )
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.guild_id == GUILD
    assert w.sender_id == SENDER
    assert w.target_id == TARGET
    assert w.message == "hi"
    assert w.state == "pending"
    assert w.solved is False
    assert w.exposed is False
    assert w.guesses_left == 3
    assert w.channel_msg_id is None
    assert w.dm_msg_id is None


def test_get_whisper_nonexistent_returns_none(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        assert get_whisper(conn, 99999) is None


def test_set_whisper_message_ids(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="hi")
        set_whisper_message_ids(conn, wid, channel_msg_id=111, dm_msg_id=222)
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.channel_msg_id == 111
    assert w.dm_msg_id == 222


def test_update_whisper_state(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="hi")
        update_whisper_state(conn, wid, "shared")
        w = get_whisper(conn, wid)
    assert w is not None
    assert w.state == "shared"


def test_list_received_filters_by_state(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        w1 = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="a")
        w2 = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="b")
        update_whisper_state(conn, w2, "hidden")
        pending = list_received(conn, guild_id=GUILD, target_id=TARGET, state="pending")
        hidden = list_received(conn, guild_id=GUILD, target_id=TARGET, state="hidden")
    assert {w.id for w in pending} == {w1}
    assert {w.id for w in hidden} == {w2}


def test_list_received_excludes_other_guilds(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="a")
        insert_whisper(conn, guild_id=8888, sender_id=SENDER, target_id=TARGET, message="other")
        results = list_received(conn, guild_id=GUILD, target_id=TARGET, state="pending")
    assert len(results) == 1


def test_delete_whisper(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        from services.whisper_repo import delete_whisper
        wid = insert_whisper(conn, guild_id=GUILD, sender_id=SENDER, target_id=TARGET, message="x")
        assert get_whisper(conn, wid) is not None
        delete_whisper(conn, wid)
        assert get_whisper(conn, wid) is None
