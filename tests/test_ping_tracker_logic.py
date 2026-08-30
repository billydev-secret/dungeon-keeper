"""Unit tests for bot_modules.services.ping_tracker_service.

The interesting behaviour is not any single function but the counting rules the
whole feature rests on: a ping is recorded once, turnout is *distinct people*
deduped across posting and reacting, the pinger and bots never count as their
own response, and the window is a real boundary. Those are exercised against a
migrated DB rather than mocks, because they are SQL.
"""

from __future__ import annotations

import json
import types

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import ping_tracker_service as pts
from tests.db_template import migrated_db

GUILD = 10
CHANNEL = 500
OTHER_CHANNEL = 501
PINGER = 1
ALICE = 2
BOB = 3
BOT_USER = 9
ROLE_A = 111
ROLE_B = 222

T0 = 1_000_000.0


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "ping.db"
    migrated_db(path)
    with open_db(path) as c:
        for uid, is_bot in ((PINGER, 0), (ALICE, 0), (BOB, 0), (BOT_USER, 1)):
            c.execute(
                "INSERT OR REPLACE INTO known_users "
                "(guild_id, user_id, username, display_name, updated_at, is_bot,"
                " current_member) VALUES (?,?,?,?,?,?,1)",
                (GUILD, uid, f"u{uid}", f"u{uid}", 0.0, is_bot),
            )
        yield c


def add_ping(conn, message_id=100, *, author=PINGER, roles=(ROLE_A,),
             everyone=False, source=pts.SOURCE_MEMBER, ts=T0, ref=None,
             channel=CHANNEL):
    return pts.record_ping_event(
        conn,
        message_id=message_id,
        guild_id=GUILD,
        channel_id=channel,
        author_id=author,
        role_ids=roles,
        everyone=everyone,
        source=source,
        ts=ts,
        ref=ref,
    )


def add_message(conn, message_id, author, ts, channel=CHANNEL):
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, content, ts)"
        " VALUES (?,?,?,?,?,?)",
        (message_id, GUILD, channel, author, "x", ts),
    )


def add_reaction(conn, message_id, reactor, ts, author=PINGER):
    conn.execute(
        "INSERT OR REPLACE INTO reaction_log "
        "(guild_id, reactor_id, author_id, channel_id, message_id, ts)"
        " VALUES (?,?,?,?,?,?)",
        (GUILD, reactor, author, CHANNEL, message_id, ts),
    )


def turnout(conn, *, window=30, include_bots=False, ping_id=100):
    posters, reactors = pts.query_responders(
        conn, GUILD, since_ts=0, window_minutes=window, include_bots=include_bots
    )
    return len(set(posters.get(ping_id, {})) | set(reactors.get(ping_id, set())))


# ── Extracting a ping ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "content, role_ids, everyone",
    [
        ("<@&111> come play", [111], False),
        # De-duplicated, first-appearance order preserved.
        ("<@&222> <@&111> <@&222>", [222, 111], False),
        ("@everyone listen up", [], True),
        ("@here quick one", [], True),
        ("<@&111> and @everyone", [111], True),
        # A *user* mention is not a role ping — the distinction the whole
        # feature exists for.
        ("<@111> hey you", [], False),
        ("just talking", [], False),
        ("", [], False),
        (None, [], False),
    ],
)
def test_parse_role_mentions(content, role_ids, everyone):
    assert pts.parse_role_mentions(content) == (role_ids, everyone)


def test_role_pings_from_message_reads_structured_fields_not_text():
    """The ingest path must not depend on content — that is what makes ping
    tracking work at storage level "none"."""
    message = types.SimpleNamespace(
        content=None,
        role_mentions=[types.SimpleNamespace(id=ROLE_A), types.SimpleNamespace(id=ROLE_A)],
        mention_everyone=True,
    )
    assert pts.role_pings_from_message(message) == ([ROLE_A], True)


def test_role_pings_from_message_ignores_unpermitted_everyone():
    """Discord leaves mention_everyone False when the author couldn't ping —
    typing the word is not pinging the server."""
    message = types.SimpleNamespace(
        content="@everyone please", role_mentions=[], mention_everyone=False
    )
    assert pts.role_pings_from_message(message) == ([], False)


def test_role_pings_from_message_tolerates_a_bare_object():
    assert pts.role_pings_from_message(types.SimpleNamespace()) == ([], False)


@pytest.mark.parametrize(
    "roles, everyone, expected",
    [((), False, False), ((ROLE_A,), False, True), ((), True, True)],
)
def test_is_ping(roles, everyone, expected):
    assert pts.is_ping(roles, everyone) is expected


@pytest.mark.parametrize(
    "value, expected",
    [(None, 30), (0, 1), (-5, 1), (30, 30), (99999, 1440), ("x", 30), (7, 7)],
)
def test_clamp_window_minutes(value, expected):
    assert pts.clamp_window_minutes(value) == expected


@pytest.mark.parametrize(
    "is_bot, is_self, expected",
    [
        (False, False, pts.SOURCE_MEMBER),
        (True, True, pts.SOURCE_BOT),
        # The distinction that makes the report readable: on prod, third-party
        # bots sent 71% of all role pings.
        (True, False, pts.SOURCE_EXTERNAL),
    ],
)
def test_ingest_source(is_bot, is_self, expected):
    assert pts.ingest_source(is_bot=is_bot, is_self=is_self) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("self", (pts.SOURCE_BOT, pts.SOURCE_GAME_START)),
        ("member", (pts.SOURCE_MEMBER,)),
        ("external", (pts.SOURCE_EXTERNAL,)),
        ("all", pts.ALL_SOURCES),
        # A bad query string shows everything rather than 500ing or, worse,
        # showing an empty report that looks like an answer.
        (None, pts.ALL_SOURCES),
        ("nonsense", pts.ALL_SOURCES),
    ],
)
def test_resolve_sources(name, expected):
    assert pts.resolve_sources(name) == expected


# ── Writing ───────────────────────────────────────────────────────────────


def test_record_ping_event_is_idempotent(conn):
    assert add_ping(conn) is True
    assert add_ping(conn) is False
    assert conn.execute("SELECT COUNT(*) FROM ping_events").fetchone()[0] == 1


def test_record_ping_event_skips_a_message_that_pinged_nobody(conn):
    assert add_ping(conn, roles=(), everyone=False) is False
    assert conn.execute("SELECT COUNT(*) FROM ping_events").fetchone()[0] == 0


def test_record_ping_event_stores_every_role(conn):
    add_ping(conn, roles=(ROLE_A, ROLE_B))
    stored = conn.execute("SELECT role_ids FROM ping_events").fetchone()[0]
    assert json.loads(stored) == [ROLE_A, ROLE_B]


def test_stamp_ping_source_refuses_an_unknown_source(conn):
    """A typo'd source silently splits the report's breakdown in two."""
    add_ping(conn)
    with pytest.raises(ValueError):
        pts.stamp_ping_source(conn, 100, "gaem_start", "g1")


def test_stamp_ping_source_reports_a_miss_rather_than_raising(conn):
    assert pts.stamp_ping_source(conn, 999, pts.SOURCE_GAME_START, "g1") is False


def test_game_start_ping_wins_when_the_launcher_arrives_first(conn):
    pts.record_game_start_ping(
        conn, message_id=100, guild_id=GUILD, channel_id=CHANNEL,
        author_id=BOT_USER, role_ids=[ROLE_A], game_id="g1", ts=T0,
    )
    # Ingest then sees the same message and records it generically.
    add_ping(conn, author=BOT_USER, source=pts.SOURCE_BOT)
    row = conn.execute("SELECT source, ref FROM ping_events").fetchone()
    assert tuple(row) == (pts.SOURCE_GAME_START, "g1")


def test_game_start_ping_wins_when_ingest_arrives_first(conn):
    """The other order of the same race — the launcher must still upgrade it."""
    add_ping(conn, author=BOT_USER, source=pts.SOURCE_BOT)
    pts.record_game_start_ping(
        conn, message_id=100, guild_id=GUILD, channel_id=CHANNEL,
        author_id=BOT_USER, role_ids=[ROLE_A], game_id="g1", ts=T0,
    )
    row = conn.execute("SELECT source, ref FROM ping_events").fetchone()
    assert tuple(row) == (pts.SOURCE_GAME_START, "g1")


def test_game_start_ping_records_nothing_when_no_role_was_pinged(conn):
    pts.record_game_start_ping(
        conn, message_id=100, guild_id=GUILD, channel_id=CHANNEL,
        author_id=BOT_USER, role_ids=[], game_id="g1", ts=T0,
    )
    assert conn.execute("SELECT COUNT(*) FROM ping_events").fetchone()[0] == 0


def test_store_message_records_the_ping_it_saw(conn):
    """The wiring that matters: the archive path is what feeds ping_events, and
    it must do so from the role list it was handed, not from the text."""
    from bot_modules.services.message_store import store_message

    store_message(
        conn,
        message_id=100,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=PINGER,
        content=None,
        reply_to_id=None,
        ts=int(T0),
        attachment_urls=[],
        mention_ids=[],
        role_mention_ids=[ROLE_A],
        mention_everyone=True,
        ping_source=pts.SOURCE_BOT,
        retain_content=False,
    )
    row = pts.query_pings(conn, GUILD, since_ts=0)[0]
    assert row["role_ids"] == [ROLE_A]
    assert row["everyone"] is True
    assert row["source"] == pts.SOURCE_BOT


def test_store_message_records_nothing_for_an_ordinary_message(conn):
    from bot_modules.services.message_store import store_message

    store_message(
        conn,
        message_id=101,
        guild_id=GUILD,
        channel_id=CHANNEL,
        author_id=PINGER,
        content="hello",
        reply_to_id=None,
        ts=int(T0),
        attachment_urls=[],
        mention_ids=[ALICE],
    )
    assert pts.query_pings(conn, GUILD, since_ts=0) == []


# ── Counting turnout ──────────────────────────────────────────────────────


def test_turnout_counts_distinct_people_not_messages(conn):
    add_ping(conn)
    for i, ts in enumerate((T0 + 60, T0 + 120, T0 + 180)):
        add_message(conn, 200 + i, ALICE, ts)
    assert turnout(conn) == 1


def test_turnout_dedupes_someone_who_posts_and_reacts(conn):
    add_ping(conn)
    add_message(conn, 200, ALICE, T0 + 60)
    add_reaction(conn, 100, ALICE, T0 + 90)
    assert turnout(conn) == 1


def test_turnout_unions_posters_and_reactors(conn):
    add_ping(conn)
    add_message(conn, 200, ALICE, T0 + 60)
    add_reaction(conn, 100, BOB, T0 + 90)
    assert turnout(conn) == 2


def test_pinger_is_never_their_own_response(conn):
    add_ping(conn)
    add_message(conn, 200, PINGER, T0 + 60)
    add_reaction(conn, 100, PINGER, T0 + 60)
    assert turnout(conn) == 0


def test_bots_are_excluded_by_default_and_opt_in(conn):
    add_ping(conn)
    add_message(conn, 200, BOT_USER, T0 + 60)
    assert turnout(conn) == 0
    assert turnout(conn, include_bots=True) == 1


def test_the_ping_message_itself_is_not_a_response(conn):
    """The ping is a row in `messages` too; counting it would give every ping a
    free responder."""
    add_ping(conn, author=ALICE)
    add_message(conn, 100, ALICE, T0)
    assert turnout(conn) == 0


@pytest.mark.parametrize(
    "offset_seconds, counted",
    [
        (-60, False),   # before the ping
        (0, False),     # simultaneous — strictly after only
        (60, True),
        (1800, True),   # exactly on the boundary
        (1801, False),  # past it
    ],
)
def test_window_boundaries(conn, offset_seconds, counted):
    add_ping(conn)
    add_message(conn, 200, ALICE, T0 + offset_seconds)
    assert turnout(conn, window=30) == (1 if counted else 0)


def test_a_wider_window_finds_more_people(conn):
    add_ping(conn)
    add_message(conn, 200, ALICE, T0 + 60)
    add_message(conn, 201, BOB, T0 + 7200)
    assert turnout(conn, window=30) == 1
    assert turnout(conn, window=180) == 2


def test_turnout_is_scoped_to_the_pinged_channel(conn):
    add_ping(conn)
    add_message(conn, 200, ALICE, T0 + 60, channel=OTHER_CHANNEL)
    assert turnout(conn) == 0


def test_message_volume_is_reported_alongside_headcount(conn):
    add_ping(conn)
    for i, ts in enumerate((T0 + 60, T0 + 120, T0 + 180)):
        add_message(conn, 200 + i, ALICE, ts)
    posters, _ = pts.query_responders(conn, GUILD, since_ts=0, window_minutes=30)
    assert sum(posters[100].values()) == 3


# ── Reading the pings back ────────────────────────────────────────────────


def test_query_pings_returns_newest_first_and_parses_roles(conn):
    add_ping(conn, 100, ts=T0, roles=(ROLE_A,))
    add_ping(conn, 101, ts=T0 + 500, roles=(ROLE_A, ROLE_B), everyone=True)
    rows = pts.query_pings(conn, GUILD, since_ts=0)
    assert [r["message_id"] for r in rows] == [101, 100]
    assert rows[0]["role_ids"] == [ROLE_A, ROLE_B]
    assert rows[0]["everyone"] is True


def test_query_pings_survives_a_malformed_role_list(conn):
    """One bad row costs its by-role line, not the whole report."""
    add_ping(conn)
    conn.execute("UPDATE ping_events SET role_ids = 'not json'")
    assert pts.query_pings(conn, GUILD, since_ts=0)[0]["role_ids"] == []


def test_query_pings_honours_the_since_cutoff(conn):
    add_ping(conn, 100, ts=T0)
    add_ping(conn, 101, ts=T0 + 5000)
    assert len(pts.query_pings(conn, GUILD, since_ts=T0 + 1000)) == 1


def test_query_pings_filters_by_sender(conn):
    add_ping(conn, 100, source=pts.SOURCE_MEMBER)
    add_ping(conn, 101, source=pts.SOURCE_EXTERNAL, ts=T0 + 10)
    add_ping(conn, 102, source=pts.SOURCE_GAME_START, ts=T0 + 20)

    def ids(name):
        return sorted(
            r["message_id"]
            for r in pts.query_pings(
                conn, GUILD, since_ts=0, sources=pts.resolve_sources(name)
            )
        )

    assert ids("all") == [100, 101, 102]
    assert ids("self") == [102]
    assert ids("member") == [100]
    assert ids("external") == [101]


def test_responders_are_not_counted_for_a_filtered_out_ping(conn):
    """The filter has to reach both queries — otherwise turnout is computed for
    pings the report will never show."""
    add_ping(conn, 100, source=pts.SOURCE_EXTERNAL)
    add_message(conn, 200, ALICE, T0 + 60)
    posters, _ = pts.query_responders(
        conn, GUILD, since_ts=0, window_minutes=30,
        sources=pts.resolve_sources("self"),
    )
    assert posters == {}


# ── Game rosters ──────────────────────────────────────────────────────────


def test_game_player_counts_reads_finished_games(conn):
    conn.execute(
        "INSERT INTO games_game_history "
        "(game_id, game_type, channel_id, host_id, player_count, started_at)"
        " VALUES ('g1', 'mlt', ?, ?, 7, '2026-01-01')",
        (CHANNEL, PINGER),
    )
    assert pts.query_game_player_counts(conn, ["g1"]) == {"g1": 7}


def test_game_player_counts_falls_back_to_a_live_lobby(conn):
    conn.execute(
        "INSERT INTO games_active_games (game_id, channel_id, game_type, host_id, payload)"
        " VALUES ('g2', ?, 'mlt', ?, ?)",
        (CHANNEL, PINGER, json.dumps({"players": [ALICE, BOB, ALICE]})),
    )
    assert pts.query_game_player_counts(conn, ["g2"]) == {"g2": 2}


def test_game_player_counts_omits_a_game_it_cannot_read(conn):
    """Absent means "we don't know" — the panel renders that as blank, and it
    must never be confused with a roster of zero."""
    assert pts.query_game_player_counts(conn, ["ghost"]) == {}
    assert pts.query_game_player_counts(conn, []) == {}


# ── Backfill ──────────────────────────────────────────────────────────────


def test_backfill_finds_historical_pings_and_is_idempotent(conn):
    add_message(conn, 300, ALICE, T0)
    add_message(conn, 301, BOT_USER, T0 + 10)
    add_message(conn, 302, ALICE, T0 + 20)
    conn.execute("UPDATE messages SET content = '<@&111> anyone about?' WHERE message_id = 300")
    conn.execute("UPDATE messages SET content = '@everyone game time' WHERE message_id = 301")
    conn.execute("UPDATE messages SET content = 'just chatting' WHERE message_id = 302")

    stats = pts.backfill_ping_events(
        conn, GUILD, bot_ids=[BOT_USER], self_id=BOT_USER
    )
    assert stats["recorded"] == 2
    assert pts.backfill_ping_events(
        conn, GUILD, bot_ids=[BOT_USER], self_id=BOT_USER
    )["recorded"] == 0

    rows = {r["message_id"]: r for r in pts.query_pings(conn, GUILD, since_ts=0)}
    assert rows[300]["role_ids"] == [ROLE_A]
    assert rows[300]["source"] == pts.SOURCE_MEMBER
    assert rows[301]["everyone"] is True
    assert rows[301]["source"] == pts.SOURCE_BOT


def test_backfill_labels_a_third_party_bot_as_external(conn):
    add_message(conn, 300, BOT_USER, T0)
    conn.execute("UPDATE messages SET content = '<@&111> wordle time' WHERE message_id = 300")
    # self_id names a *different* bot, so BOT_USER is somebody else's.
    pts.backfill_ping_events(conn, GUILD, bot_ids=[BOT_USER], self_id=12345)
    assert pts.query_pings(conn, GUILD, since_ts=0)[0]["source"] == pts.SOURCE_EXTERNAL


def test_backfill_never_downgrades_a_live_captured_ping(conn):
    """The live row is better than anything text parsing can produce."""
    add_ping(conn, 300, author=BOT_USER, source=pts.SOURCE_GAME_START, ref="g1")
    add_message(conn, 300, BOT_USER, T0)
    conn.execute("UPDATE messages SET content = '<@&111> starting' WHERE message_id = 300")
    pts.backfill_ping_events(conn, GUILD, bot_ids=[BOT_USER])
    row = conn.execute("SELECT source, ref FROM ping_events").fetchone()
    assert tuple(row) == (pts.SOURCE_GAME_START, "g1")


def test_known_bot_ids(conn):
    assert pts.known_bot_ids(conn, GUILD) == [BOT_USER]


# ── The report ────────────────────────────────────────────────────────────


def ping_row(message_id, *, roles=(ROLE_A,), everyone=False, ts=T0,
             channel=CHANNEL, source=pts.SOURCE_MEMBER, ref=None):
    return {
        "message_id": message_id,
        "channel_id": channel,
        "author_id": PINGER,
        "role_ids": list(roles),
        "everyone": everyone,
        "source": source,
        "ref": ref,
        "ts": ts,
    }


def test_report_headline_numbers():
    pings = [ping_row(1), ping_row(2, ts=T0 + 86400), ping_row(3, ts=T0 + 172800)]
    report = pts.build_ping_report(
        pings,
        {1: {ALICE: 2}, 2: {ALICE: 1, BOB: 1}},
        {},
        window_minutes=30,
        window_label="Last 30 Days",
    )
    assert report["total_pings"] == 3
    assert report["total_turnout"] == 3
    assert report["silent_pings"] == 1
    assert report["silent_pct"] == 33.3
    assert report["median_turnout"] == 1
    assert [p["day"] for p in report["series"]] == sorted(p["day"] for p in report["series"])


def test_report_is_empty_but_valid_with_no_pings():
    report = pts.build_ping_report([], {}, {}, window_minutes=30, window_label="x")
    assert report["total_pings"] == 0
    assert report["median_turnout"] == 0.0
    assert report["silent_pct"] == 0.0
    assert report["series"] == []


def test_a_multi_role_ping_counts_once_overall_but_under_each_role():
    """The by-role counts can legitimately sum past the headline — the question
    that table answers is per-role, and both roles really were pinged."""
    report = pts.build_ping_report(
        [ping_row(1, roles=(ROLE_A, ROLE_B))],
        {1: {ALICE: 1}},
        {},
        window_minutes=30,
        window_label="x",
        role_names={ROLE_A: "Gamers", ROLE_B: "Night Owls"},
    )
    assert report["total_pings"] == 1
    assert {r["label"] for r in report["by_role"]} == {"Gamers", "Night Owls"}
    assert sum(r["pings"] for r in report["by_role"]) == 2


def test_everyone_gets_its_own_breakdown_line():
    report = pts.build_ping_report(
        [ping_row(1, roles=(), everyone=True)],
        {1: {ALICE: 1}},
        {},
        window_minutes=30,
        window_label="x",
    )
    assert [r["label"] for r in report["by_role"]] == ["@everyone"]
    assert report["entries"][0]["role_labels"] == ["@everyone"]


def test_a_deleted_role_keeps_a_numeric_label_rather_than_vanishing():
    report = pts.build_ping_report(
        [ping_row(1)], {}, {}, window_minutes=30, window_label="x", role_names={}
    )
    assert report["by_role"][0]["label"] == f"Role {ROLE_A}"


def test_report_attaches_the_game_roster_only_to_game_pings():
    report = pts.build_ping_report(
        [
            ping_row(1, source=pts.SOURCE_GAME_START, ref="g1"),
            ping_row(2, ts=T0 + 10),
        ],
        {},
        {},
        window_minutes=30,
        window_label="x",
        game_players={"g1": 6},
    )
    entries = {int(e["message_id"]): e for e in report["entries"]}
    assert entries[1]["players"] == 6
    # None, not 0 — no game was attached, which is not the same as an empty one.
    assert entries[2]["players"] is None


def test_report_entry_carries_both_headcount_and_volume():
    report = pts.build_ping_report(
        [ping_row(1)], {1: {ALICE: 4}}, {1: {BOB}}, window_minutes=30, window_label="x"
    )
    entry = report["entries"][0]
    assert (entry["turnout"], entry["messages"], entry["reactors"]) == (2, 4, 1)


def test_report_ids_are_strings_so_snowflakes_survive_json():
    report = pts.build_ping_report(
        [ping_row(1234567890123456789, roles=(ROLE_A,))],
        {},
        {},
        window_minutes=30,
        window_label="x",
    )
    entry = report["entries"][0]
    assert isinstance(entry["message_id"], str)
    assert isinstance(entry["channel_id"], str)
    assert isinstance(entry["author_id"], str)
    assert all(isinstance(r, str) for r in entry["role_ids"])


def test_breakdown_orders_best_turnout_first():
    report = pts.build_ping_report(
        [ping_row(1, roles=(ROLE_A,)), ping_row(2, roles=(ROLE_B,), ts=T0 + 10)],
        {1: {ALICE: 1}, 2: {ALICE: 1, BOB: 1}},
        {},
        window_minutes=30,
        window_label="x",
        role_names={ROLE_A: "Quiet", ROLE_B: "Loud"},
    )
    assert [r["label"] for r in report["by_role"]] == ["Loud", "Quiet"]


def test_report_days_use_the_guild_offset_not_utc():
    """23:30 UTC on the 1st is still the 1st at -7, and the 2nd at +2."""
    ts = 1_767_309_000.0  # 2026-01-01 23:10 UTC
    west = pts.build_ping_report(
        [ping_row(1, ts=ts)], {}, {}, window_minutes=30, window_label="x",
        tz_offset_hours=-7,
    )
    east = pts.build_ping_report(
        [ping_row(1, ts=ts)], {}, {}, window_minutes=30, window_label="x",
        tz_offset_hours=2,
    )
    assert west["series"][0]["day"] == "2026-01-01"
    assert east["series"][0]["day"] == "2026-01-02"


# ── Erasure ───────────────────────────────────────────────────────────────


def test_purge_clears_the_erased_members_pings_and_leaves_others(conn):
    from bot_modules.services.privacy_service import purge_user_data

    add_ping(conn, 100, author=PINGER)
    add_ping(conn, 101, author=ALICE)
    purge_user_data(conn, GUILD, PINGER)
    remaining = [r[0] for r in conn.execute("SELECT author_id FROM ping_events")]
    assert remaining == [ALICE]


def test_ping_author_column_is_exportable():
    """An access request can only see the table if the column is a known
    subject column."""
    from bot_modules.services.privacy_service import SUBJECT_ID_COLUMNS

    assert "author_id" in SUBJECT_ID_COLUMNS
