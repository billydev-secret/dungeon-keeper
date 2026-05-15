"""Tests for guess_audit_log repo functions."""
from __future__ import annotations

import json
from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.services.guess_repo import insert_audit_event, list_audit_events


def test_insert_and_list_event(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        insert_audit_event(
            conn, guild_id=1, actor_id=42, action="submit", round_id=7,
            details={"difficulty": "medium"},
        )

    with open_db(sync_db_path) as conn:
        events = list_audit_events(conn, guild_id=1)

    assert len(events) == 1
    e = events[0]
    assert e.actor_id == 42
    assert e.action == "submit"
    assert e.round_id == 7
    assert json.loads(e.details) == {"difficulty": "medium"}


def test_list_filters_by_action(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        insert_audit_event(conn, guild_id=1, actor_id=1, action="submit", round_id=1)
        insert_audit_event(conn, guild_id=1, actor_id=1, action="delete", round_id=1)
        insert_audit_event(conn, guild_id=1, actor_id=1, action="solve", round_id=1)

    with open_db(sync_db_path) as conn:
        deletes = list_audit_events(conn, guild_id=1, action="delete")
    assert len(deletes) == 1
    assert deletes[0].action == "delete"


def test_list_scoped_to_guild(sync_db_path: Path) -> None:
    with open_db(sync_db_path) as conn:
        insert_audit_event(conn, guild_id=1, actor_id=1, action="submit", round_id=1)
        insert_audit_event(conn, guild_id=2, actor_id=2, action="submit", round_id=2)

    with open_db(sync_db_path) as conn:
        guild1 = list_audit_events(conn, guild_id=1)
        guild2 = list_audit_events(conn, guild_id=2)

    assert len(guild1) == 1 and guild1[0].actor_id == 1
    assert len(guild2) == 1 and guild2[0].actor_id == 2


def test_list_orders_newest_first(sync_db_path: Path) -> None:
    import time as _time
    with open_db(sync_db_path) as conn:
        insert_audit_event(conn, guild_id=1, actor_id=1, action="submit", round_id=1)
        _time.sleep(0.01)
        insert_audit_event(conn, guild_id=1, actor_id=1, action="solve", round_id=1)

    with open_db(sync_db_path) as conn:
        events = list_audit_events(conn, guild_id=1)
    assert events[0].action == "solve"
    assert events[1].action == "submit"
