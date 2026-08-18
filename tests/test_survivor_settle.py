"""Tests for survivor/settle.py — grading, strikes, corrections, groundskeeper.

Stage 4 of docs/plans/survivor.md. §6 edge cases in scope: #7 (idempotent
settling — no double eliminations), #8 (double-pick one-fate), #13
(auto-assign dead end must not crash or auto-kill), plus the correction
path: a manually flipped winner resurrects a wrongly-dead player.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_service import (
    create_season,
    eliminate_player,
    get_season,
)
from bot_modules.survivor.logic import join_season, place_pick, satchel
from bot_modules.survivor.settle import (
    expected_result,
    manual_settle,
    run_settle,
)
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR

THU = NOW + DAY
SUN = NOW + 4 * DAY
MON = NOW + 5 * DAY          # the closing game of week 1
W2_SUN = NOW + 11 * DAY

WEEK1 = [
    # (game_id, home, away, kickoff, favorite, prob)
    ("g-thu", "SEA", "NE", THU, "SEA", 0.62),
    ("g-sun1", "SF", "ARI", SUN, "SF", 0.70),
    ("g-mon", "KC", "LV", MON, "KC", 0.81),
]
WEEK2 = [
    ("g2-a", "PHI", "DAL", W2_SUN, "PHI", 0.55),
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, games in ((1, WEEK1), (2, WEEK2)):
            for gid, home, away, ts, fav, prob in games:
                conn.execute(
                    "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
                    " kickoff_utc, favorite, favorite_prob) VALUES (?,?,?,?,?,?,?,?)",
                    (YEAR, week, gid, home, away, _iso(ts), fav, prob),
                )
    return db_path


def _season(conn, **overrides) -> dict:
    season_id = create_season(conn, GID, "S", YEAR, overrides=overrides or None)
    return get_season(conn, season_id)


def _finalize(conn, game_id: str, winner: str) -> None:
    conn.execute(
        "UPDATE nfl_games SET status = 'final', winner = ? "
        "WHERE season_year = ? AND game_id = ?",
        (winner, YEAR, game_id),
    )


def _player(conn, season, user_id):
    return conn.execute(
        "SELECT status, strikes_used, eliminated_week, elimination_source "
        "FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()


def _pick_result(conn, season, user_id, week=1, slot=1):
    row = conn.execute(
        "SELECT result FROM survivor_picks WHERE season_id = ? AND user_id = ?"
        " AND week = ? AND slot = ?",
        (season["id"], user_id, week, slot),
    ).fetchone()
    return row["result"] if row else None


# ── grading table ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "winner", "team", "expected"),
    [
        pytest.param("final", "SEA", "SEA", "win", id="win"),
        pytest.param("final", "SEA", "NE", "loss", id="loss"),
        pytest.param("final", "TIE", "SEA", "tie", id="tie"),
        pytest.param("postponed", None, "SEA", "void", id="void"),
        pytest.param("final", None, "SEA", None, id="final-no-winner-waits"),
        pytest.param("in", None, "SEA", None, id="in-progress-waits"),
        pytest.param("scheduled", None, "SEA", None, id="scheduled-waits"),
    ],
)
def test_expected_result(status, winner, team, expected):
    assert expected_result(team, status, winner) == expected


# ── strikes and eliminations ───────────────────────────────────────────


def test_first_loss_strikes_second_loss_kills(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)
        p = _player(conn, season, 1)
        assert (p["status"], p["strikes_used"]) == ("alive", 1)  # 💛→🖤

        # Week 2: second wrong week is the end.
        place_pick(conn, season, 1, 2, "DAL", MON + HOUR)
        _finalize(conn, "g2-a", "PHI")
        run_settle(conn, season, W2_SUN + 4 * HOUR)
        p = _player(conn, season, 1)
        assert (p["status"], p["eliminated_week"]) == ("ghost", 2)
        assert p["elimination_source"] == "picks"
        assert p["strikes_used"] == 2


def test_sudden_death_when_strikes_zero(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)
        p = _player(conn, season, 1)
        assert (p["status"], p["eliminated_week"]) == ("ghost", 1)


@pytest.mark.parametrize(
    ("tie_rule", "strikes_after"),
    [pytest.param("loss", 1, id="tie-is-loss"),
     pytest.param("survive", 0, id="tie-survives")],
)
def test_tie_rule(db, tie_rule, strikes_after):
    with open_db(db) as conn:
        season = _season(conn, tie_rule=tie_rule)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        _finalize(conn, "g-thu", "TIE")
        run_settle(conn, season, THU + 4 * HOUR)
        assert _pick_result(conn, season, 1) == "tie"
        p = _player(conn, season, 1)
        assert (p["status"], p["strikes_used"]) == ("alive", strikes_after)


def test_void_returns_team_and_costs_nothing(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        conn.execute(
            "UPDATE nfl_games SET status = 'postponed' WHERE game_id = 'g-thu'"
        )
        run_settle(conn, season, THU + HOUR)
        assert _pick_result(conn, season, 1) == "void"
        p = _player(conn, season, 1)
        assert (p["status"], p["strikes_used"]) == ("alive", 0)
        assert "SEA" in satchel(conn, season["id"], 1)  # §1.3: team returns


def test_settle_is_idempotent(db):
    # Edge #7: no double eliminations, no re-grades on a no-news sweep.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)
        _finalize(conn, "g-thu", "SEA")
        first = run_settle(conn, season, THU + 4 * HOUR)
        assert first.graded == 1
        second = run_settle(conn, season, THU + 5 * HOUR)
        assert not second.any_change()
        assert _player(conn, season, 1)["strikes_used"] == 1


def test_one_fate_per_week_across_sweeps(db):
    # Edge #8: two slots, one fate — graded in separate sweeps, still one
    # strike for the week.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id)"
            " VALUES (?, ?, 1, 1, 1, 'NE', 'g-thu'), (?, ?, 1, 1, 2, 'ARI', 'g-sun1')",
            (season["id"], GID, season["id"], GID),
        )
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)
        assert _player(conn, season, 1)["strikes_used"] == 1
        _finalize(conn, "g-sun1", "SF")
        run_settle(conn, season, SUN + 4 * HOUR)
        p = _player(conn, season, 1)
        assert p["strikes_used"] == 1          # one fate, not two
        assert p["status"] == "alive"          # strike, not death


def test_ghost_picks_grade_but_never_strike(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=1)  # admin death, week 1
        # Ghost picks week 2 (identical flow) and loses.
        place_pick(conn, season, 1, 2, "DAL", MON + HOUR)
        _finalize(conn, "g2-a", "PHI")
        run_settle(conn, season, W2_SUN + 4 * HOUR)
        assert _pick_result(conn, season, 1, week=2) == "loss"  # streak data
        p = _player(conn, season, 1)
        assert p["strikes_used"] == 0          # the dead don't strike
        assert (p["eliminated_week"], p["elimination_source"]) == (1, "admin")


# ── corrections through manual settle ──────────────────────────────────


def test_correction_resurrects_the_wrongly_dead(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)
        assert _player(conn, season, 1)["status"] == "ghost"

        out = manual_settle(conn, YEAR, "g-thu", "NE", [season])
        assert out["old_winner"] == "SEA"
        assert _pick_result(conn, season, 1) == "win"
        p = _player(conn, season, 1)
        assert (p["status"], p["strikes_used"]) == ("alive", 0)
        assert p["eliminated_week"] is None


def test_manual_void_unwinds_the_strike_and_returns_the_team(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)
        assert _player(conn, season, 1)["strikes_used"] == 1

        manual_settle(conn, YEAR, "g-thu", "VOID", [season])
        assert _pick_result(conn, season, 1) == "void"
        assert _player(conn, season, 1)["strikes_used"] == 0
        assert "NE" in satchel(conn, season["id"], 1)


def test_manual_settle_validates_outcome(db):
    with open_db(db) as conn:
        season = _season(conn)
        with pytest.raises(ValueError, match="Outcome must be"):
            manual_settle(conn, YEAR, "g-thu", "KC", [season])
        with pytest.raises(ValueError, match="No such game"):
            manual_settle(conn, YEAR, "nope", "SEA", [season])


def test_non_pick_deaths_survive_recomputation(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)     # a WIN
        eliminate_player(conn, season["id"], 1, week=1)  # admin says dead
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, THU + 4 * HOUR)       # win grades, recompute
        p = _player(conn, season, 1)
        assert (p["status"], p["elimination_source"]) == ("ghost", "admin")


# ── the groundskeeper ──────────────────────────────────────────────────


def test_auto_assign_takes_best_closing_side(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)      # never picks
        _finalize(conn, "g-thu", "SEA")
        _finalize(conn, "g-sun1", "SF")
        report = run_settle(conn, season, MON + 600)
        assert report.auto_assigned == [(1, "KC")]  # 0.81 favorite of the closer
        row = conn.execute(
            "SELECT team, auto_assigned, locked_at FROM survivor_picks "
            "WHERE season_id = ? AND user_id = 1 AND week = 1",
            (season["id"],),
        ).fetchone()
        assert (row["team"], row["auto_assigned"]) == ("KC", 1)
        assert row["locked_at"] is not None
        # Idempotent: the next sweep assigns nothing further.
        again = run_settle(conn, season, MON + 1200)
        assert again.auto_assigned == []


def test_auto_assign_skips_ghosts_and_late_joiners(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        eliminate_player(conn, season["id"], 1, week=1)   # ghost: never covered
        # Joins after the closing kickoff — not in this week's game.
        conn.execute(
            "INSERT INTO survivor_players (season_id, guild_id, user_id, joined_at)"
            " VALUES (?, ?, 2, ?)",
            (season["id"], GID, _iso(MON + 30 * 60)),
        )
        report = run_settle(conn, season, MON + 600)
        assert report.auto_assigned == []
        assert report.cap_eliminated == []


def test_auto_assign_dead_end_survives(db):
    # Edge #13: both closing-game sides burned → void week, no crash,
    # no cap charge, player survives.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        conn.execute(
            "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team, game_id, result)"
            " VALUES (?, ?, 1, 0, 1, 'KC', 'g-old', 'loss'),"
            " (?, ?, 1, 0, 2, 'LV', 'g-old2', 'loss')",
            (season["id"], GID, season["id"], GID),
        )
        report = run_settle(conn, season, MON + 600)
        assert report.no_legal_team == [1]
        assert report.auto_assigned == []
        p = _player(conn, season, 1)
        assert p["status"] == "alive"


def test_groundskeeper_declines_the_fourth_time(db):
    with open_db(db) as conn:
        season = _season(conn, max_auto_assigns=3)
        join_season(conn, season, 1, NOW)
        # Three prior covered weeks.
        for week in (0, -1, -2):
            conn.execute(
                "INSERT INTO survivor_picks (season_id, guild_id, user_id, week, slot, team,"
                " game_id, auto_assigned, result) VALUES (?, ?, 1, ?, 1, 'X', 'g', 1, 'win')",
                (season["id"], GID, week),
            )
        report = run_settle(conn, season, MON + 600)
        assert report.cap_eliminated == [1]
        p = _player(conn, season, 1)
        assert (p["status"], p["elimination_source"]) == ("ghost", "cap")
        assert p["eliminated_week"] == 1


def test_auto_assign_never_reaches_into_a_live_game(db):
    # Stage-4 review: a delayed sweep (restart, outage recovery) must not
    # assign into a closer already deep in progress — past the grace bound
    # the pickless simply survive.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        conn.execute("UPDATE nfl_games SET status = 'in' WHERE game_id = 'g-mon'")
        report = run_settle(conn, season, MON + 2 * HOUR)  # beyond the grace
        assert report.auto_assigned == []
        assert _player(conn, season, 1)["status"] == "alive"


def test_missed_pick_eliminate_ruleset(db):
    # Stage-4 review: the 'eliminate' dial was stored but nothing read it.
    with open_db(db) as conn:
        season = _season(conn, missed_pick="eliminate")
        join_season(conn, season, 1, NOW)
        report = run_settle(conn, season, MON + 600)
        assert report.cap_eliminated == [1]
        p = _player(conn, season, 1)
        assert (p["status"], p["elimination_source"]) == ("ghost", "missed")
        # And the decision survives a later recompute, like cap/admin deaths.
        _finalize(conn, "g-thu", "SEA")
        run_settle(conn, season, MON + 1200)
        assert _player(conn, season, 1)["elimination_source"] == "missed"


def test_manual_void_sticks_through_a_feed_poll(db):
    # Stage-4 review headline: ingest must never cross a manual result.
    from bot_modules.services.survivor_espn import ParsedGame, ingest_games

    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        manual_settle(conn, YEAR, "g-thu", "VOID", [season])
        assert _pick_result(conn, season, 1) == "void"

        # The feed still thinks the game is on, then final.
        feed_final = ParsedGame(
            game_id="g-thu", week=1, home="SEA", away="NE",
            kickoff_utc=_iso(THU), status="final", favorite=None,
            favorite_prob=None, winner="SEA",
        )
        ingest_games(conn, YEAR, [feed_final])
        row = conn.execute(
            "SELECT status, winner FROM nfl_games WHERE game_id = 'g-thu'"
        ).fetchone()
        assert (row["status"], row["winner"]) == ("postponed", None)
        run_settle(conn, season, MON + 600)
        assert _pick_result(conn, season, 1) == "void"  # the void stands
        assert _player(conn, season, 1)["strikes_used"] == 0


def test_auto_assign_window_closes_when_the_closer_goes_final(db):
    # Too late to assign without known-result info: the pickless survive.
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        for gid, winner in (("g-thu", "SEA"), ("g-sun1", "SF"), ("g-mon", "KC")):
            _finalize(conn, gid, winner)
        report = run_settle(conn, season, MON + 4 * HOUR)
        assert report.auto_assigned == []
        assert _player(conn, season, 1)["status"] == "alive"


# ── bookkeeping ────────────────────────────────────────────────────────


def test_locked_at_stamped_for_kicked_games(db):
    with open_db(db) as conn:
        season = _season(conn)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        conn.execute("UPDATE nfl_games SET status = 'in' WHERE game_id = 'g-thu'")
        report = run_settle(conn, season, THU + HOUR)
        assert report.locked == 1
        row = conn.execute(
            "SELECT locked_at FROM survivor_picks WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchone()
        assert row["locked_at"] == _iso(THU)


def test_season_activates_at_first_kickoff(db):
    with open_db(db) as conn:
        season = _season(conn)
        assert season["status"] == "enrolling"
        run_settle(conn, season, NOW)          # before any kickoff
        assert get_season(conn, season["id"])["status"] == "enrolling"
        run_settle(conn, season, THU + 1)
        assert get_season(conn, season["id"])["status"] == "active"
