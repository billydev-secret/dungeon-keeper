from __future__ import annotations

import sqlite3

import pytest

from services.message_store import init_member_events_table, init_message_tables, record_member_event, store_message
from services.reports_data import get_greeter_log_sessions, get_greeter_response_data


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_message_tables(c)
    init_member_events_table(c)
    yield c
    c.close()


def _join(conn, user_id: int, ts: float) -> None:
    record_member_event(conn, 1, user_id, "join", ts)


def _leave(conn, user_id: int, ts: float) -> None:
    record_member_event(conn, 1, user_id, "leave", ts)


def _store(conn, *, message_id: int, channel_id: int, author_id: int, ts: int, content: str | None = None) -> None:
    store_message(
        conn,
        message_id=message_id,
        guild_id=1,
        channel_id=channel_id,
        author_id=author_id,
        content=content,
        reply_to_id=None,
        ts=ts,
        attachment_urls=[],
        mention_ids=[],
        embeds=[],
    )


def test_greeter_response_tracks_greeted_left_and_waiting_sessions(conn):
    greeter_channel_id = 20
    greeter_id = 900

    _join(conn, user_id=100, ts=100)
    _store(conn, message_id=2, channel_id=greeter_channel_id, author_id=greeter_id, ts=220, content="hey there!")
    _join(conn, user_id=101, ts=300)
    _leave(conn, user_id=101, ts=450)
    _join(conn, user_id=102, ts=500)

    sessions = get_greeter_log_sessions(conn, guild_id=1)
    data = get_greeter_response_data(
        conn,
        guild_id=1,
        greeter_channel_id=greeter_channel_id,
        greeter_ids={greeter_id},
        sessions=sessions,
        now_ts=900,
    )

    assert len(sessions) == 3
    assert data["total_joins"] == 3
    assert data["count"] == 1
    assert data["left_before_greeting_count"] == 1
    assert data["awaiting_greeting_count"] == 1
    assert data["response_times_seconds"] == [120.0]

    assert data["entries"][0]["user_id"] == "102"
    assert data["entries"][0]["status"] == "awaiting_greeting"
    assert data["entries"][0]["wait_seconds"] == 400

    assert data["entries"][1]["user_id"] == "101"
    assert data["entries"][1]["status"] == "left_before_greeting"
    assert data["entries"][1]["left_at"] == 450
    assert data["entries"][1]["wait_seconds"] == 150

    assert data["entries"][2]["user_id"] == "100"
    assert data["entries"][2]["status"] == "greeted"
    assert data["entries"][2]["greeted_at"] == 220
    assert data["entries"][2]["response_seconds"] == 120
    assert data["entries"][2]["greeter_id"] == str(greeter_id)


def test_since_ts_filters_old_joins(conn):
    _join(conn, user_id=1, ts=100)
    _join(conn, user_id=2, ts=500)

    sessions = get_greeter_log_sessions(conn, guild_id=1, since_ts=400)
    assert len(sessions) == 1
    assert sessions[0]["user_id"] == 2


def test_rejoin_pairs_correctly(conn):
    _join(conn, user_id=1, ts=100)
    _leave(conn, user_id=1, ts=200)
    _join(conn, user_id=1, ts=300)

    sessions = get_greeter_log_sessions(conn, guild_id=1)
    assert len(sessions) == 2
    assert sessions[0]["left_at"] == 200.0
    assert sessions[1]["left_at"] is None
