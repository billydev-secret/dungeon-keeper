from __future__ import annotations

import sqlite3
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.interaction_graph import init_interaction_tables, record_interactions
from bot_modules.services.message_store import init_member_events_table, init_message_tables, record_member_event, store_message
from bot_modules.services.channel_rollup import build_resolver
from bot_modules.services.reports_data import get_channel_comparison_data, get_greeter_log_sessions, get_greeter_response_data, get_interaction_graph_data, get_one_sided_attention_data
from tests.db_template import migrated_db


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
    migrated_db(path)
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


# ── Channel comparison: thread attribution ───────────────────────────
#
# The panel's rows are meant to be channels. A message posted in a thread
# carries the thread's own id, so without folding, every thread became a row
# of its own — see services/channel_rollup and todo #91.

CH = 100
THREAD = 101


@pytest.fixture
def cc_conn(tmp_path):
    """Migrated DB for the channel-comparison report."""
    path = tmp_path / "cc.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _cc_message(conn, *, mid: int, cid: int, aid: int, ts: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO messages "
        "(message_id, guild_id, channel_id, author_id, content, reply_to_id, ts)"
        " VALUES (?, 1, ?, ?, 'x', NULL, ?)",
        (mid, cid, aid, ts),
    )


def _cc_thread(conn, *, thread_id: int, parent_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO known_channels "
        "(guild_id, channel_id, channel_name, updated_at, parent_id, is_thread)"
        " VALUES (1, ?, '', 0, ?, 1)",
        (thread_id, parent_id),
    )


def _compare(conn, live_ids):
    return get_channel_comparison_data(
        conn,
        1,
        days=30,
        resolver=build_resolver(conn, 1, live_channel_ids=live_ids),
    )


def test_comparison_attributes_thread_messages_to_the_parent(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=THREAD, aid=2, ts=now - 120)
    _cc_message(cc_conn, mid=3, cid=THREAD, aid=3, ts=now - 180)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]
    assert rows[0]["message_count"] == 3
    assert rows[0]["unique_authors"] == 3


def test_comparison_counts_a_shared_author_once(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=THREAD, aid=1, ts=now - 120)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]
    assert rows[0]["message_count"] == 2
    assert rows[0]["unique_authors"] == 1
    # One author holding every message is maximal concentration, and the Gini
    # must see the merged distribution rather than two single-author channels.
    assert rows[0]["gini"] == 0.0


def test_comparison_weights_sentiment_by_volume_not_by_channel(cc_conn):
    # A three-word thread must not drag the parent's sentiment as hard as the
    # parent's own hundred messages. Averaging two averages would let it.
    now = int(time.time())
    for mid in range(1, 10):
        _cc_message(cc_conn, mid=mid, cid=CH, aid=1, ts=now - 60)
        cc_conn.execute(
            "INSERT INTO message_sentiment "
            "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
            " VALUES (?, 1, ?, 1.0, '', ?)",
            (mid, CH, now),
        )
    _cc_message(cc_conn, mid=99, cid=THREAD, aid=2, ts=now - 60)
    cc_conn.execute(
        "INSERT INTO message_sentiment "
        "(message_id, guild_id, channel_id, sentiment, emotion, computed_at)"
        " VALUES (99, 1, ?, -1.0, '', ?)",
        (THREAD, now),
    )
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    rows = _compare(cc_conn, [CH])["channels"]

    # Nine at +1 and one at -1 → 0.8, not the 0.0 of averaged averages.
    assert rows[0]["avg_sentiment"] == 0.8


def test_comparison_drops_a_thread_whose_parent_is_gone(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=THREAD, aid=1, ts=now - 60)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)

    # The guild still has channels — just not this thread's parent, so there is
    # nothing left to attribute the thread's messages to.
    rows = _compare(cc_conn, [999])["channels"]

    assert rows == []


def test_comparison_drops_a_pen_pals_channel(cc_conn):
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    _cc_message(cc_conn, mid=2, cid=555, aid=2, ts=now - 60)
    cc_conn.execute(
        "INSERT INTO pen_pals_sessions "
        "(session_id, guild_id, channel_id, user1_id, user2_id, started_at,"
        " expiry_at, next_question_at) VALUES ('s1', 1, 555, 1, 2, 0, 0, 0)"
    )

    rows = _compare(cc_conn, [CH, 555])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH)]


def test_comparison_ranks_by_the_folded_total(cc_conn):
    # #100 loses on its own messages but wins once its thread is counted, so
    # the ordering cannot be the SQL's.
    now = int(time.time())
    _cc_message(cc_conn, mid=1, cid=CH, aid=1, ts=now - 60)
    for mid in (2, 3):
        _cc_message(cc_conn, mid=mid, cid=THREAD, aid=1, ts=now - 60)
    _cc_thread(cc_conn, thread_id=THREAD, parent_id=CH)
    for mid in (10, 11):
        _cc_message(cc_conn, mid=mid, cid=200, aid=2, ts=now - 60)

    rows = _compare(cc_conn, [CH, 200])["channels"]

    assert [r["channel_id"] for r in rows] == [str(CH), "200"]
