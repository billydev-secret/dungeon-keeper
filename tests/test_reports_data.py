from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.interaction_graph import init_interaction_tables, record_interactions
from bot_modules.services.message_store import init_member_events_table, init_message_tables, record_member_event, store_message
from bot_modules.services.reports_data import get_animated_heatmap_data, get_greeter_log_sessions, get_greeter_response_data, get_interaction_graph_data, get_one_sided_attention_data
from migrations import apply_migrations_sync


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


@pytest.fixture
def ig_conn(tmp_path):
    """Migrated DB with interaction tables — for the interaction-graph report."""
    path = tmp_path / "ig.db"
    apply_migrations_sync(path)
    with open_db(path) as c:
        init_interaction_tables(c)
        yield c


def _mark_bot(conn, guild_id, user_id):
    conn.execute(
        "INSERT INTO known_users (guild_id, user_id, is_bot) VALUES (?, ?, 1)"
        " ON CONFLICT(guild_id, user_id) DO UPDATE SET is_bot = 1",
        (guild_id, user_id),
    )


def test_interaction_graph_excludes_bots(ig_conn):
    """Bots on either endpoint drop out of nodes, edges, and top pairs."""
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], amount=3)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], amount=9)
    record_interactions(ig_conn, guild_id=1, from_user_id=99, to_user_ids=[2], amount=7)
    _mark_bot(ig_conn, 1, 99)

    data = get_interaction_graph_data(ig_conn, guild_id=1)

    node_ids = {n["user_id"] for n in data["nodes"]}
    assert node_ids == {"1", "2"}
    assert "99" not in node_ids
    edge_ids = {e["from_id"] for e in data["edges"]} | {e["to_id"] for e in data["edges"]}
    assert "99" not in edge_ids
    pair_ids = {p["from_id"] for p in data["top_pairs"]} | {p["to_id"] for p in data["top_pairs"]}
    assert "99" not in pair_ids


def test_interaction_graph_excludes_bots_windowed(ig_conn):
    """Bot exclusion also holds on the days-windowed (log-table) query path."""
    import time as _t

    now = int(_t.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], ts=now, message_id=2)
    _mark_bot(ig_conn, 1, 99)

    data = get_interaction_graph_data(ig_conn, guild_id=1, days=7)
    node_ids = {n["user_id"] for n in data["nodes"]}
    assert node_ids == {"1", "2"}


def test_animated_heatmap_excludes_bots(ig_conn):
    """The stable top-N user set for the heatmap never includes a bot."""
    import time as _t

    now = int(_t.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    # Bot 99 is heavily interacted with — would top the volume ranking if kept.
    for i in range(5):
        record_interactions(
            ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], ts=now, message_id=100 + i
        )
    _mark_bot(ig_conn, 1, 99)

    data = get_animated_heatmap_data(ig_conn, guild_id=1, days=30)
    assert "99" not in {u["user_id"] for u in data["users"]}


def test_one_sided_attention_report_excludes_bots(ig_conn):
    """The One-Sided Attention report pulls bot ids from known_users and drops
    any pair touching one, while a lopsided human pair still surfaces."""
    import time as _t

    now = int(_t.time())
    # Lopsided human 1 → human 2 (target silent): should flag.
    for i in range(20):
        record_interactions(
            ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now - i * 3600, message_id=i
        )
    # Lopsided human 1 → bot 99: must NOT flag once 99 is marked a bot.
    for i in range(20):
        record_interactions(
            ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], ts=now - i * 3600, message_id=100 + i
        )
    _mark_bot(ig_conn, 1, 99)

    data = get_one_sided_attention_data(ig_conn, guild_id=1)
    pairs = {(c["from_id"], c["to_id"]) for c in data["candidates"]}
    assert ("1", "2") in pairs
    assert ("1", "99") not in pairs
    assert all("99" not in (c["from_id"], c["to_id"]) for c in data["candidates"])
