from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.interaction_graph import init_interaction_tables, record_interactions
from bot_modules.services.message_store import init_member_events_table, init_message_tables, record_member_event, store_message
from bot_modules.services.channel_rollup import build_resolver
from bot_modules.services.activity_graphs import TAG_ORDER
from bot_modules.services.nsfw_classifier_service import DEFAULT_LABEL_SET
from bot_modules.services.reports_data import get_channel_comparison_data, get_greeter_log_sessions, get_greeter_response_data, get_interaction_graph_data, get_interaction_series, get_nsfw_tag_mix_data, get_one_sided_attention_data
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


# ── Interaction series (the Connection Graph's replay) ─────────────────────


def _series_pairs(data):
    return {(p["a"], p["b"]): p["w"] for p in data["pairs"]}


def test_interaction_series_bins_weekly_and_merges_direction(ig_conn):
    """Rows land in the right weekly bin, and both directions of a pair merge
    into one undirected weight vector."""
    now = int(time.time())
    wk = 7 * 86400
    # Three interactions this week, split across both directions.
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now - 100, message_id=2)
    record_interactions(ig_conn, guild_id=1, from_user_id=2, to_user_ids=[1], ts=now - 200, message_id=3)
    # Two more safely inside the bin two weeks back (ts exactly on a bin edge
    # belongs to the later bin, so keep clear of the boundary).
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now - 2 * wk + 100, message_id=4)
    record_interactions(ig_conn, guild_id=1, from_user_id=2, to_user_ids=[1], ts=now - 2 * wk + 200, message_id=5)

    data = get_interaction_series(ig_conn, guild_id=1, weeks=8)
    assert data["weeks"] == 8
    assert data["bin_seconds"] == wk
    vec = _series_pairs(data)[("1", "2")]
    assert len(vec) == 8
    # Bin indices are derived from the payload's own `start`, not from when
    # the test happens to run: a wall-clock-relative assertion would pass or
    # fail on which side of a week boundary `now` fell.
    def _bin(ts):
        return min(int((ts - data["start"]) // wk), data["weeks"] - 1)

    assert vec[_bin(now)] == 3           # this week, incl. the now-edge fold
    assert vec[_bin(now - 2 * wk + 100)] == 2
    assert sum(vec) == 5
    assert sum(vec[: _bin(now - 2 * wk + 100)]) == 0   # nothing earlier


def test_interaction_series_excludes_bots(ig_conn):
    """The same bot-endpoint exclusion as the live graph query."""
    now = int(time.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=2)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], ts=now, message_id=3)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[99], ts=now, message_id=4)
    _mark_bot(ig_conn, 1, 99)

    data = get_interaction_series(ig_conn, guild_id=1)
    assert {n["user_id"] for n in data["nodes"]} == {"1", "2"}
    assert ("1", "99") not in _series_pairs(data)


def test_interaction_series_roster_limit_drops_pairs_outside_it(ig_conn):
    """Only the top-`limit` members by span total stay, and pairs touching a
    dropped member go with them."""
    now = int(time.time())
    mid = 0
    def talk(a, b, n):
        nonlocal mid
        for _ in range(n):
            mid += 1
            record_interactions(ig_conn, guild_id=1, from_user_id=a, to_user_ids=[b], ts=now, message_id=mid)
    talk(1, 2, 8)   # totals: 1=8, 2=8…
    talk(3, 4, 4)   # 3=4, 4=4
    talk(3, 5, 3)   # 3=7, 5=3
    talk(2, 6, 2)   # 2=10, 6=2 — 6 sits alone below the cut
    data = get_interaction_series(ig_conn, guild_id=1, limit=10)
    assert {n["user_id"] for n in data["nodes"]} == {"1", "2", "3", "4", "5", "6"}
    assert ("2", "6") in _series_pairs(data)

    # limit clamps to a floor of 5 (the live panel's own Max Nodes minimum),
    # so six members against limit=5 is the smallest cut this can show.
    data = get_interaction_series(ig_conn, guild_id=1, limit=5)
    assert {n["user_id"] for n in data["nodes"]} == {"1", "2", "3", "4", "5"}
    assert ("2", "6") not in _series_pairs(data)


def test_interaction_series_floors_one_off_pairs(ig_conn):
    """A pair with a single interaction across the whole span is noise the
    payload does not carry."""
    now = int(time.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=2)
    record_interactions(ig_conn, guild_id=1, from_user_id=2, to_user_ids=[3], ts=now, message_id=3)

    data = get_interaction_series(ig_conn, guild_id=1)
    pairs = _series_pairs(data)
    assert ("1", "2") in pairs
    assert ("2", "3") not in pairs


def test_interaction_series_attaches_join_leave_stamps(ig_conn):
    """Roster members carry their in-span member_events stamps, sorted; other
    event types and non-roster members are ignored."""
    init_member_events_table(ig_conn)
    now = int(time.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=2)
    ev = [
        # Deliberately out of chronological order: the payload must sort, and
        # an insertion-ordered result would pass without sorting at all.
        (1, 1, "join", now - 1000),
        (1, 1, "leave", now - 2000),
        (1, 1, "join", now - 3000),
        (1, 1, "ban", now - 500),        # unrelated type: ignored
        (1, 2, "ban", now - 400),        # 2 has events, none of them join/leave
        (1, 777, "join", now - 100),     # not on the roster: ignored
    ]
    ig_conn.executemany(
        "INSERT INTO member_events (guild_id, user_id, event_type, ts) VALUES (?, ?, ?, ?)", ev
    )

    data = get_interaction_series(ig_conn, guild_id=1)
    by_id = {n["user_id"]: n for n in data["nodes"]}
    assert by_id["1"]["joins"] == [now - 3000, now - 1000]
    assert by_id["1"]["leaves"] == [now - 2000]
    # 2 HAS a member_events row, just not a membership one — so an empty list
    # here means "filtered by type", not "no rows existed".
    assert by_id["2"]["joins"] == []
    assert by_id["2"]["leaves"] == []
    assert "777" not in by_id


def test_interaction_series_clamps_weeks_and_gives_clusters(ig_conn):
    """weeks clamps to [4, 60]; every roster member gets an integer cluster."""
    now = int(time.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=2)

    data = get_interaction_series(ig_conn, guild_id=1, weeks=500)
    assert data["weeks"] == 60
    assert all(len(p["w"]) == 60 for p in data["pairs"])

    data = get_interaction_series(ig_conn, guild_id=1, weeks=1)
    assert data["weeks"] == 4


def test_interaction_series_separates_disconnected_groups(ig_conn):
    """Two groups that never talk to each other get different cluster ids.

    `isinstance(cluster_id, int)` would hold for a partition that collapsed
    every member into one group, which is exactly the failure worth catching:
    replay colours are the legend.
    """
    now = int(time.time())
    mid = 0

    def talk(a, b, n):
        nonlocal mid
        for _ in range(n):
            mid += 1
            record_interactions(ig_conn, guild_id=1, from_user_id=a, to_user_ids=[b], ts=now, message_id=mid)

    talk(1, 2, 5)
    talk(2, 3, 5)      # group A: 1-2-3
    talk(10, 11, 5)
    talk(11, 12, 5)    # group B: 10-11-12, no edge to A

    data = get_interaction_series(ig_conn, guild_id=1)
    by_id = {n["user_id"]: n["cluster_id"] for n in data["nodes"]}
    group_a = {by_id["1"], by_id["2"], by_id["3"]}
    group_b = {by_id["10"], by_id["11"], by_id["12"]}
    assert len(group_a) == 1 and len(group_b) == 1, "a connected group split apart"
    assert group_a != group_b, "two disconnected groups share a cluster id"


def test_interaction_series_drops_members_whose_every_pair_is_floored(ig_conn):
    """A member kept by the roster shortlist but with no surviving pair must
    not ship as a node.

    They could never be drawn (the client shows only members with a pair in
    the window) and would default to cluster_id 0 — which is the LARGEST real
    community, not an unclustered sentinel — inflating that community's chip.
    """
    now = int(time.time())
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=1)
    record_interactions(ig_conn, guild_id=1, from_user_id=1, to_user_ids=[2], ts=now, message_id=2)
    # 3's only pair is a one-off, floored out of `pairs`.
    record_interactions(ig_conn, guild_id=1, from_user_id=3, to_user_ids=[4], ts=now, message_id=3)

    data = get_interaction_series(ig_conn, guild_id=1)
    assert {n["user_id"] for n in data["nodes"]} == {"1", "2"}


# ── NSFW tag mix ─────────────────────────────────────────────────────


@pytest.fixture
def tag_conn(tmp_path):
    path = tmp_path / "tags.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _classify(conn, message_id: int, label: str | None, *, guild_id: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO nsfw_classifications
            (message_id, attachment_id, guild_id, channel_id, verdict,
             marqo_score, top_label, top_score, model, threshold, label_set,
             inference_ms, bytes, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (message_id, 1, guild_id, 999, 1, 0.9, label, 0.8, "320n", 0.5, "",
         10, 2048, int(time.time()) - 60),
    )
    conn.commit()


def test_tag_mix_gives_each_series_a_plain_english_name(tag_conn):
    _classify(tag_conn, 1, "FEMALE_BREAST_EXPOSED")
    _classify(tag_conn, 2, "MALE_GENITALIA_EXPOSED")

    data = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)

    names = {s["label"]: s["display"] for s in data["series"]}
    assert names == {
        "FEMALE_BREAST_EXPOSED": "Female chest",
        "MALE_GENITALIA_EXPOSED": "Male genitalia",
    }


def test_tag_mix_names_both_chest_labels_symmetrically(tag_conn):
    """The spoiler rule treats the two chest labels identically (CHEST_LABELS).

    Calling one a "breast" and the other a "chest" in a report read
    side-by-side would imply a distinction the code does not make.
    """
    _classify(tag_conn, 1, "FEMALE_BREAST_EXPOSED")
    _classify(tag_conn, 2, "MALE_BREAST_EXPOSED")

    data = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)

    assert [s["display"] for s in data["series"]] == ["Female chest", "Male chest"]


def test_tag_mix_titlecases_a_label_it_has_no_name_for(tag_conn):
    _classify(tag_conn, 1, "ZZ_SOMETHING_NEW")

    data = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)

    assert data["series"][0]["display"] == "Zz something new"


def test_tag_mix_assigns_no_colour(tag_conn):
    """Tags have no inherent colour, so the panel hands them out from the
    validated categorical palette. A colour here would silently win over it."""
    _classify(tag_conn, 1, "SEX_ACT")

    data = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)

    assert "color" not in data["series"][0]


def test_tag_mix_carries_the_window_label(tag_conn):
    _classify(tag_conn, 1, "SEX_ACT")

    assert get_nsfw_tag_mix_data(tag_conn, 1, "week", 0.0)["window_label"] == "Last 12 Weeks"
    assert get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)["window_label"] == "Last 30 Days"


def test_tag_mix_with_no_tagged_images_still_describes_its_window(tag_conn):
    data = get_nsfw_tag_mix_data(tag_conn, 1, "month", 0.0)

    assert data["series"] == []
    assert len(data["labels"]) == 12
    assert data["window_label"] == "Last 12 Months"


def test_tag_mix_passes_a_channel_filter_through(tag_conn):
    _classify(tag_conn, 1, "SEX_ACT")

    assert get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0, [999])["series"]
    assert get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0, [123])["series"] == []


def test_tag_mix_colour_slot_survives_a_window_that_drops_a_label(tag_conn):
    """The whole point of the fixed vocabulary order.

    Colouring by position in the returned array would move BUTTOCKS_EXPOSED
    every time a narrower window or a channel filter dropped one of the labels
    that sort before it — repainting a series that did nothing.
    """
    _classify(tag_conn, 1, "FEMALE_BREAST_EXPOSED")
    _classify(tag_conn, 2, "MALE_BREAST_EXPOSED")
    _classify(tag_conn, 3, "FEMALE_GENITALIA_EXPOSED")
    _classify(tag_conn, 4, "BUTTOCKS_EXPOSED")
    full = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)
    crowded = {s["label"]: s["order"] for s in full["series"]}

    # A window holding only the two outer labels: everything between them is gone.
    sparse_conn_labels = {"FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED"}
    tag_conn.execute(
        "DELETE FROM nsfw_classifications WHERE top_label NOT IN (?, ?)",
        tuple(sorted(sparse_conn_labels)),
    )
    tag_conn.commit()
    sparse = {s["label"]: s["order"] for s in
              get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)["series"]}

    assert sparse["BUTTOCKS_EXPOSED"] == crowded["BUTTOCKS_EXPOSED"]
    # ...and it is emphatically not the array index it would have had.
    assert sparse["BUTTOCKS_EXPOSED"] != 1


def test_tag_mix_order_matches_the_vocabulary_position(tag_conn):
    _classify(tag_conn, 1, "FEMALE_BREAST_EXPOSED")
    _classify(tag_conn, 2, "SEX_ACT")

    orders = {s["label"]: s["order"] for s in
              get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)["series"]}

    assert orders == {
        "FEMALE_BREAST_EXPOSED": TAG_ORDER.index("FEMALE_BREAST_EXPOSED"),
        "SEX_ACT": TAG_ORDER.index("SEX_ACT"),
    }


def test_tag_mix_sends_an_unknown_label_to_the_overflow_slot(tag_conn):
    _classify(tag_conn, 1, "ZZ_SOMETHING_NEW")

    order = get_nsfw_tag_mix_data(tag_conn, 1, "day", 0.0)["series"][0]["order"]

    assert order == len(TAG_ORDER)


def test_tag_vocabulary_matches_the_detector_exactly():
    """A drift guard, not a tautology.

    TAG_ORDER doubles as the palette map, so a label the detector gains and
    this list lacks would land in the overflow neutral — and a label removed
    from the detector would shift every colour after it. MALE_BREAST_EXPOSED
    was added to the vocabulary once already, with the bare-chest rule.
    """
    assert set(TAG_ORDER) == set(DEFAULT_LABEL_SET)
    assert len(TAG_ORDER) == len(set(TAG_ORDER)), "a label is listed twice"


def test_the_palette_overflow_slot_holds_the_label_production_never_sees():
    """Seven labels, six palette colours: whatever sits last is drawn in the
    grey overflow neutral.

    That slot is given to ANUS_EXPOSED, the one label the detector has never
    emitted on this server (0 rows against 682 tagged) — so in practice every
    label that actually occurs gets a real, validated colour. If that stops
    being true the tail wants folding into an "Other" band instead, which is
    what charts.js tells callers past six series to do.
    """
    import re

    charts = (
        Path(__file__).resolve().parents[1]
        / "src" / "web_server" / "static" / "js" / "charts.js"
    ).read_text(encoding="utf-8")
    palette = re.search(r"export const ROLE_COLORS = \[(.*?)\]", charts, re.S)
    assert palette, "ROLE_COLORS is gone from charts.js"
    n_colors = len(re.findall(r"#[0-9a-fA-F]{6}", palette.group(1)))

    overflowing = [t for t in TAG_ORDER if TAG_ORDER.index(t) >= n_colors]
    assert overflowing == ["ANUS_EXPOSED"], (
        f"{overflowing} would be drawn in the overflow neutral; only the label "
        f"production never sees belongs there"
    )
