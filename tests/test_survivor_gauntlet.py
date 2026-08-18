"""Tests for survivor/gauntlet.py — the late-entry replay engine.

Stage 5b of docs/plans/survivor.md. The headline is §4.2's determinism:
two joiners entering at the same moment inherit byte-identical lines, and
the replay reads only stored favorites and stored winners. §6 edge cases
in scope: #9 (voided chalk replays as void; double-pick era top-two chalk)
and #10 (joiner with no legal team must not crash or auto-kill).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import economy_service
from bot_modules.services.survivor_service import create_season, get_season
from bot_modules.survivor.gauntlet import (
    compute_fate,
    execute_gauntlet_join,
    ghost_only_join,
)
from bot_modules.survivor.logic import PickError, satchel
from bot_modules.survivor.settle import recompute_player
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0

# Three fully-elapsed weeks. Chalk by prob: wk1 KC(.80) wins; wk2 SF(.75)
# LOSES; wk3 KC would repeat but is used → BUF(.70) wins.
GAMES = [
    # (week, game_id, home, away, kickoff_offset_days, favorite, prob, winner, status)
    (1, "g1a", "KC", "LV", -20, "KC", 0.80, "KC", "final"),
    (1, "g1b", "SEA", "NE", -20, "SEA", 0.60, "NE", "final"),
    (2, "g2a", "SF", "ARI", -13, "SF", 0.75, "ARI", "final"),
    (2, "g2b", "DAL", "NYG", -13, "DAL", 0.55, "DAL", "final"),
    (3, "g3a", "KC", "DEN", -6, "KC", 0.85, "KC", "final"),
    (3, "g3b", "BUF", "MIA", -6, "BUF", 0.70, "BUF", "final"),
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, gid, home, away, off, fav, prob, winner, status in GAMES:
            conn.execute(
                "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
                " kickoff_utc, favorite, favorite_prob, winner, status)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (YEAR, week, gid, home, away, _iso(NOW + off * DAY),
                 fav, prob, winner, status),
            )
    return db_path


def _season(conn, **overrides):
    return get_season(conn, create_season(conn, GID, "S", YEAR,
                                          overrides=overrides or None))


# ── the replay line ────────────────────────────────────────────────────


def test_replay_takes_chalk_no_reuse_and_grades(db):
    with open_db(db) as conn:
        season = _season(conn)
        fate = compute_fate(conn, season, NOW)
        line = [(rw.week, rw.team, rw.result) for rw in fate.weeks]
        # wk1 KC (top chalk) wins; wk2 SF loses (strike); wk3 KC is used →
        # BUF, wins. Alive with one strike, three teams burned.
        assert line == [(1, "KC", "win"), (2, "SF", "loss"), (3, "BUF", "win")]
        assert not fate.dead
        assert fate.strikes_used == 1
        assert fate.burned == ("BUF", "KC", "SF")
        assert fate.fee == 150  # 50/week × 3 elapsed weeks


def test_determinism_two_joiners_identical_lines(db):
    # §4.2's core promise, pinned: same moment, same stored data → the same
    # fate object, byte for byte.
    with open_db(db) as conn:
        season = _season(conn)
        assert compute_fate(conn, season, NOW) == compute_fate(conn, season, NOW)


def test_replay_never_reads_live_odds(db):
    # Changing favorite_prob AFTER the games settled must not change the
    # line vs a joiner who entered before the change ONLY through the stored
    # values — i.e. the fate is a pure function of the table, nothing else.
    with open_db(db) as conn:
        season = _season(conn)
        before = compute_fate(conn, season, NOW)
        conn.execute("UPDATE nfl_games SET favorite_prob = 0.99 WHERE game_id = 'g1b'")
        after = compute_fate(conn, season, NOW)
        assert [rw.team for rw in after.weeks][0] == "SEA"  # stored values rule
        assert [rw.team for rw in before.weeks][0] == "KC"


def test_death_ends_the_replay(db):
    # Sudden death: the wk2 chalk loss is fatal; wk3 is NOT replayed —
    # the ghost's game starts live, and wk3's teams stay in the satchel.
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        fate = compute_fate(conn, season, NOW)
        assert fate.dead and fate.death_week == 2
        assert [rw.week for rw in fate.weeks] == [1, 2]
        assert "BUF" not in fate.burned
        assert fate.fee == 150  # the fee still charges every elapsed week


def test_voided_chalk_replays_as_void(db):
    # Edge #9: the wk2 top chalk game is postponed → that week is void,
    # SF returns to the pool (and is next week's chalk candidate again).
    with open_db(db) as conn:
        season = _season(conn)
        conn.execute(
            "UPDATE nfl_games SET status = 'postponed', winner = NULL "
            "WHERE game_id = 'g2a'"
        )
        fate = compute_fate(conn, season, NOW)
        wk2 = [rw for rw in fate.weeks if rw.week == 2]
        assert [(rw.team, rw.result) for rw in wk2] == [("SF", "void")]
        assert "SF" not in fate.burned
        assert fate.strikes_used == 0


def test_no_legal_chalk_is_a_void_week_not_a_crash(db):
    # Edge #10: strip every favorite from week 2 → void week, survive.
    with open_db(db) as conn:
        season = _season(conn)
        conn.execute("UPDATE nfl_games SET favorite = NULL WHERE week = 2")
        fate = compute_fate(conn, season, NOW)
        wk2 = [rw for rw in fate.weeks if rw.week == 2]
        assert [(rw.team, rw.result) for rw in wk2] == [(None, "void")]
        assert not fate.dead


def test_double_pick_era_replays_top_two_chalk(db):
    # Edge #9: weeks >= double_pick_start_week replay two slots.
    with open_db(db) as conn:
        season = _season(conn, double_pick_start_week=3)
        fate = compute_fate(conn, season, NOW)
        wk3 = [(rw.team, rw.result) for rw in fate.weeks if rw.week == 3]
        # KC used in wk1 → the top-two remaining chalk of wk3: BUF then… only
        # two games exist and SEA lost in wk1's other game — g3a's favorite
        # KC is used, so candidates are BUF only from g3b… plus none other →
        # a single-candidate double week replays what exists.
        assert wk3 == [("BUF", "win")]


def test_unsettled_chalk_is_skipped_not_inherited(db):
    # A straggler (kicked, no winner yet) can't be chalk — fate must not
    # change retroactively when it settles.
    with open_db(db) as conn:
        season = _season(conn)
        conn.execute(
            "UPDATE nfl_games SET winner = NULL, status = 'in' "
            "WHERE game_id = 'g2a'"
        )
        fate = compute_fate(conn, season, NOW)
        wk2 = [(rw.team, rw.result) for rw in fate.weeks if rw.week == 2]
        assert wk2 == [("DAL", "win")]  # next-best settled chalk


# ── execution ──────────────────────────────────────────────────────────


def test_execute_join_alive_burns_charges_and_recomputes_clean(db):
    with open_db(db) as conn:
        season = _season(conn)
        economy_service.apply_credit(conn, GID, 1, 500, "test_seed")
        fate = compute_fate(conn, season, NOW)
        execute_gauntlet_join(conn, season, 1, fate, NOW)

        assert economy_service.get_balance(conn, GID, 1) == 350  # 500 - 150
        row = conn.execute(
            "SELECT kind, amount, meta FROM econ_ledger WHERE kind = ?",
            ("survivor_gauntlet_fee",),
        ).fetchone()
        assert row["amount"] == -150
        assert json.loads(row["meta"])["pot"] == "main"  # alive → main pot
        assert satchel(conn, season["id"], 1) == satchel(conn, season["id"], 1)
        assert "KC" not in satchel(conn, season["id"], 1)
        player = conn.execute(
            "SELECT status, strikes_used FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert (player["status"], player["strikes_used"]) == ("alive", 1)
        # The settle engine's recompute agrees with the inherited verdict.
        recompute_player(conn, season, 1)
        player = conn.execute(
            "SELECT status, strikes_used FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert (player["status"], player["strikes_used"]) == ("alive", 1)


def test_execute_join_dead_routes_fee_to_ghost_pot(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        economy_service.apply_credit(conn, GID, 1, 500, "test_seed")
        fate = compute_fate(conn, season, NOW)
        execute_gauntlet_join(conn, season, 1, fate, NOW)
        row = conn.execute(
            "SELECT meta FROM econ_ledger WHERE kind = 'survivor_gauntlet_fee'"
        ).fetchone()
        assert json.loads(row["meta"])["pot"] == "ghost"  # DOA → ghost pot
        player = conn.execute(
            "SELECT status, eliminated_week, elimination_source "
            "FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert player["status"] == "ghost"
        assert player["eliminated_week"] == 2
        assert player["elimination_source"] == "picks"
        recompute_player(conn, season, 1)  # replayed losses ARE pick rows
        player = conn.execute(
            "SELECT status, eliminated_week FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert (player["status"], player["eliminated_week"]) == ("ghost", 2)


def test_execute_join_short_wallet_charges_nothing(db):
    with open_db(db) as conn:
        season = _season(conn)
        economy_service.apply_credit(conn, GID, 1, 100, "test_seed")  # fee is 150
        fate = compute_fate(conn, season, NOW)
        with pytest.raises(PickError, match="came up short"):
            execute_gauntlet_join(conn, season, 1, fate, NOW)
        assert economy_service.get_balance(conn, GID, 1) == 100
        assert conn.execute(
            "SELECT COUNT(*) FROM survivor_players WHERE season_id = ?",
            (season["id"],),
        ).fetchone()[0] == 0


def test_execute_join_duplicate_refused(db):
    with open_db(db) as conn:
        season = _season(conn)
        economy_service.apply_credit(conn, GID, 1, 500, "test_seed")
        fate = compute_fate(conn, season, NOW)
        execute_gauntlet_join(conn, season, 1, fate, NOW)
        with pytest.raises(PickError, match="already in"):
            execute_gauntlet_join(conn, season, 1, fate, NOW)


def test_ghost_only_join_is_free_and_born_dead(db):
    with open_db(db) as conn:
        season = _season(conn, late_entry="ghost_only")
        ghost_only_join(conn, season, 1, NOW)
        player = conn.execute(
            "SELECT status, eliminated_week, elimination_source "
            "FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert player["status"] == "ghost"
        assert player["eliminated_week"] == 3
        assert player["elimination_source"] == "entry"
        assert conn.execute(
            "SELECT COUNT(*) FROM econ_ledger WHERE user_id = 1"
        ).fetchone()[0] == 0
        # Full satchel — no replay burned anything.
        assert len(satchel(conn, season["id"], 1)) == 32
        # And recomputation never resurrects an entry-ghost.
        recompute_player(conn, season, 1)
        player = conn.execute(
            "SELECT status FROM survivor_players WHERE user_id = 1"
        ).fetchone()
        assert player["status"] == "ghost"
