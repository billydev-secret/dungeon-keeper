"""Tests for survivor/reckoning.py + tasks.py decisions + ghost streaks.

Stages 5 and 6a of docs/plans/survivor.md: the three-act data assembly,
deterministic flavor rotation, leaver elimination (§6.14), the weekly-task
due logic over guild-local clocks, and the streak rules as decided
2026-08-17 (miss breaks, void neither, unsettled pending).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_service import (
    create_season,
    eliminate_player,
    get_season,
    update_config,
)
from bot_modules.survivor import tasks
from bot_modules.survivor.logic import ghost_streaks, join_season, place_pick
from bot_modules.survivor.reckoning import (
    GROUNDSKEEPER_LINE,
    build_reckoning_data,
    eulogy_for,
    eliminate_leavers,
    next_reckoning_week,
    rotate,
)
from bot_modules.survivor.settle import run_settle
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR
THU = NOW + DAY
MON = NOW + 5 * DAY
AFTER_W1 = MON + 5 * HOUR

WEEK1 = [
    ("g-thu", "SEA", "NE", THU),
    ("g-mon", "KC", "LV", MON),
]
WEEK2 = [("g2", "SF", "ARI", NOW + 12 * DAY)]
WEEK3 = [("g3", "BUF", "MIA", NOW + 19 * DAY)]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, games in ((1, WEEK1), (2, WEEK2), (3, WEEK3)):
            for gid, home, away, ts in games:
                conn.execute(
                    "INSERT INTO nfl_games (season_year, week, game_id, home, away, kickoff_utc)"
                    " VALUES (?,?,?,?,?,?)",
                    (YEAR, week, gid, home, away, _iso(ts)),
                )
    return db_path


def _season(conn, **overrides):
    return get_season(conn, create_season(conn, GID, "S", YEAR,
                                          overrides=overrides or None))


def _finalize(conn, game_id, winner):
    conn.execute(
        "UPDATE nfl_games SET status = 'final', winner = ? WHERE game_id = ?",
        (winner, game_id),
    )


def _settled_week1(conn, season):
    """Two players: 1 picks SEA (wins), 2 picks NE (dies, sudden death)."""
    join_season(conn, season, 1, NOW)
    join_season(conn, season, 2, NOW)
    place_pick(conn, season, 1, 1, "SEA", NOW)
    place_pick(conn, season, 2, 1, "NE", NOW)
    _finalize(conn, "g-thu", "SEA")
    _finalize(conn, "g-mon", "KC")
    run_settle(conn, season, AFTER_W1)


# ── reckoning week + rotation ──────────────────────────────────────────


def test_next_reckoning_week_gates_on_elapsed(db):
    with open_db(db) as conn:
        season = _season(conn)
        assert next_reckoning_week(conn, season, NOW) is None  # nothing kicked
        assert next_reckoning_week(conn, season, AFTER_W1) == 1
        update_config(conn, season["id"], {"last_reckoned_week": 1})
        season = get_season(conn, season["id"])
        assert next_reckoning_week(conn, season, AFTER_W1) is None  # wk2 open


def test_rotation_is_deterministic_and_cycles():
    lines = ["a", "b", "c"]
    assert [rotate(lines, k) for k in (1, 2, 3, 4)] == ["b", "c", "a", "b"]
    assert rotate([], 5) == ""


# ── the three acts ─────────────────────────────────────────────────────


def test_reckoning_data_toll_ledger_deaths(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        from bot_modules.services.survivor_service import seed_default_flavor

        seed_default_flavor(conn, GID)
        _settled_week1(conn, season)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert (data["before"], data["after"]) == (2, 1)
        assert data["toll_line"] and "{" not in data["toll_line"]
        # Deaths sorted first, states honest.
        assert [e["user_id"] for e in data["deaths"]] == [2]
        assert data["deaths"][0]["fatal_team"] == "NE"
        assert [e["user_id"] for e in data["ledger"]] == [1]
        assert data["ledger"][0]["teams"].startswith("SEA")
        assert data["stragglers"] == 0


def test_reckoning_no_deaths_uses_no_death_corpus(db):
    with open_db(db) as conn:
        season = _season(conn)
        from bot_modules.services.survivor_service import seed_default_flavor

        seed_default_flavor(conn, GID)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        _finalize(conn, "g-thu", "SEA")
        _finalize(conn, "g-mon", "KC")
        run_settle(conn, season, AFTER_W1)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert data["deaths"] == []
        assert data["toll_line"]  # a no_death line, filled
        assert "{week}" not in data["toll_line"]


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        pytest.param("cap", "groundskeeper", id="cap-fixed-line"),
        pytest.param("missed", "never picked", id="missed-fixed-line"),
        pytest.param("left", "walked out", id="leaver-fixed-line"),
    ],
)
def test_eulogy_fixed_lines_by_source(source, needle):
    entry = {"source": source, "fatal_team": None, "user_id": 1}
    line = eulogy_for(entry, {"week": 3, "eulogy_lines": ["{name} fell."]},
                      "Loaf", 0)
    assert needle in line and "Loaf" in line


def test_eulogy_corpus_fills_slots_and_rotates():
    data = {"week": 3, "eulogy_lines": ["{name} rode {team} in wk {week}.",
                                        "{name} fell."]}
    entry = {"source": "picks", "fatal_team": "NE", "user_id": 1}
    line0 = eulogy_for(entry, data, "Loaf", 0)
    line1 = eulogy_for(entry, data, "Loaf", 1)
    assert line0 != line1
    assert "{" not in line0 + line1  # every slot filled
    assert GROUNDSKEEPER_LINE not in (line0, line1)


def test_leavers_die_at_the_reckoning(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)
        # Player 1 (alive) has left the guild; player 2 is already a ghost.
        gone = eliminate_leavers(conn, season, 1, present_ids={2, 999})
        assert gone == [1]
        row = conn.execute(
            "SELECT status, elimination_source FROM survivor_players "
            "WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchone()
        assert (row["status"], row["elimination_source"]) == ("ghost", "left")
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert {e["user_id"] for e in data["deaths"]} == {1, 2}


def test_arrivals_gate_counts_gauntlet_walkers_only(db):
    with open_db(db) as conn:
        season = _season(conn)
        # Enrolled before the first kickoff: never walked anything.
        join_season(conn, season, 6, NOW)
        # Mid-week join after Thursday kicked: a real gate arrival.
        join_season(conn, season, 7, THU + HOUR)
        data = build_reckoning_data(conn, season, 1, AFTER_W1)
        assert [a["user_id"] for a in data["arrivals"]] == [7]


# ── ghost streaks (§1.7 as decided) ────────────────────────────────────


def test_ghost_streaks_miss_breaks_void_neither(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=1)
        # wk2: ghost picks SF, wins. wk3: no pick (miss) → streak resets;
        # best stays 1.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 2, 1, 'SF', 'g2', 'win')",
            (season["id"], GID),
        )
        now = NOW + 21 * DAY  # weeks 1-3 all elapsed
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 0, "best": 1}
        # A void in wk3 instead of a miss leaves the streak untouched.
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot,"
            " team, game_id, result) VALUES (?, ?, 1, 3, 1, 'BUF', 'g3', 'void')",
            (season["id"], GID),
        )
        streaks = ghost_streaks(conn, season, now)
        assert streaks[1] == {"current": 1, "best": 1}


def test_ghost_streaks_ignore_pre_death_wins(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)  # player 2's loss is fatal
        _settled_week1(conn, season)  # player 1 WON week 1, alive
        # Player 2 died wk1; their wk1 loss must not seed a streak, and the
        # living player has no streak entry at all.
        streaks = ghost_streaks(conn, season, AFTER_W1)
        assert streaks[2] == {"current": 0, "best": 0}
        assert 1 not in streaks


# ── weekly-task due logic ──────────────────────────────────────────────


def _cfg_season(conn, **overrides):
    season = _season(conn, **overrides)
    update_config(conn, season["id"], {"channel_id": 555})
    return get_season(conn, season["id"])


@pytest.mark.parametrize(
    ("dow_offset_days", "hour", "due"),
    [
        # NOW is Wednesday 12:00 UTC; slate_hour default 9, offset 0.
        pytest.param(0, 12, True, id="wednesday-after-hour"),
        pytest.param(-1, 12, False, id="tuesday-not-yet"),
        pytest.param(2, 12, True, id="friday-catchup"),
    ],
)
def test_slate_due_frame(db, dow_offset_days, hour, due):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        now = NOW + dow_offset_days * DAY
        got = tasks.slate_due(conn, season, now, 0.0)
        assert (got == 1) is due


def test_slate_due_once_per_week(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        assert tasks.slate_due(conn, season, NOW, 0.0) == 1
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert tasks.slate_due(conn, season, NOW, 0.0) is None
        # Next week's slate becomes due after MNF rolls pick_week forward.
        assert tasks.slate_due(conn, season, NOW + 7 * DAY, 0.0) == 2


def test_reckoning_due_tuesday_after_week_elapsed(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        # Week 1 fully kicked Monday night; Tuesday 9am+ is the moment.
        tue_early = MON + 8 * HOUR    # Tue 01:00 UTC
        tue_nine = MON + 21 * HOUR    # Tue 14:00 UTC — past 9 local (UTC)
        assert tasks.reckoning_due(conn, season, tue_early, 0.0) is None
        assert tasks.reckoning_due(conn, season, tue_nine, 0.0) == 1


def test_lastcall_due_saturday(db):
    with open_db(db) as conn:
        season = _cfg_season(conn)
        sat_evening = NOW + 3 * DAY + 7 * HOUR  # Sat 19:00 UTC, hour 18 due
        assert tasks.lastcall_due(conn, season, NOW, 0.0) is None  # Wednesday
        assert tasks.lastcall_due(conn, season, sat_evening, 0.0) == 1
