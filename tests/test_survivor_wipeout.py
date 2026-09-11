"""Tests for §1.6's wipeout rule — stage 6b of docs/plans/survivor.md.

A week that kills every living player is either **annulled** (through
``wipeout_annul_through_week``: nobody dies, burned teams stay burned) or,
past that week, **ends the season** in an equal split of both pots.

The cases that carry the design, and would be quietly wrong without a test:

* the annul is a *recorded* decision, so a later correction cannot dissolve
  it and bury a roster the Reckoning has already announced survived;
* teams stay burned across an annul — the satchel is the only thing the
  struck week leaves behind, and only that week is struck;
* a wipeout is a question about *that week*, not about the season, so
  re-grading Week 1 long after the field died out in Week 2 annuls nothing;
* the split pays the pot to the coin, and a retried sweep pays it once.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_service import (
    SeasonError,
    create_season,
    eliminate_player,
    get_season,
)
from bot_modules.survivor.logic import burned_teams, join_season, place_pick
from bot_modules.survivor.payout import (
    even_shares,
    payout_receipt,
    settle_season_end,
    streak_winners,
)
from bot_modules.survivor.settle import is_wipeout, manual_settle, run_settle
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR

# Two weeks. Two games in week 1 so a correction can flip one player's fate
# without flipping everyone's; one in week 2, which is all the wipeouts need.
W1 = NOW + DAY
W2 = NOW + 8 * DAY
GAMES = [
    (1, "g1", "SEA", "NE", W1),
    (1, "g1b", "GB", "CHI", W1),
    (2, "g2", "PHI", "DAL", W2),
]
AFTER_W1 = W1 + 5 * HOUR
AFTER_W2 = W2 + 5 * HOUR


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        for week, game_id, home, away, ts in GAMES:
            conn.execute(
                "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
                " kickoff_utc) VALUES (?,?,?,?,?,?)",
                (YEAR, week, game_id, home, away, _iso(ts)),
            )
    return db_path


def _season(conn, **overrides) -> dict:
    return get_season(
        conn, create_season(conn, GID, "S", YEAR, overrides=overrides or None)
    )


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


def _alive(conn, season) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM survivor_players "
        "WHERE season_id = ? AND status = 'alive'",
        (season["id"],),
    ).fetchone()[0]


def _wipe_week_one(conn, season, user_ids=(1, 2, 3)):
    """Everyone picks the loser of week 1; the sweep grades it."""
    for user_id in user_ids:
        join_season(conn, season, user_id, NOW)
        place_pick(conn, season, user_id, 1, "NE", NOW)
    _finalize(conn, "g1", "SEA")
    return run_settle(conn, season, AFTER_W1)


# ── the annul ──────────────────────────────────────────────────────────


def test_wipeout_inside_the_window_annuls_the_week(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        report = _wipe_week_one(conn, season)

        assert report.annulled == [1]
        assert report.season_ended is None
        for user_id in (1, 2, 3):
            row = _player(conn, season, user_id)
            assert (row["status"], row["eliminated_week"]) == ("alive", None)
            assert row["strikes_used"] == 0
        # …but the team they spent is gone for good (§1.6).
        assert burned_teams(conn, season["id"], 1) == {"NE"}
        # The decision is on the season, where recompute_player reads it.
        assert get_season(conn, season["id"])["config"]["annulled_weeks"] == [1]


def test_annul_leaves_the_season_running(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        _wipe_week_one(conn, season)
        assert get_season(conn, season["id"])["status"] != "complete"
        assert _alive(conn, season) == 3  # somebody is there to play week 2


def test_annul_strikes_only_its_own_week(db):
    """One strike allowed, a real loss in week 1, a wipeout in week 2. The
    annul must erase week 2 and nothing else: resurrect everyone, but leave
    week 1's strike standing so the season still has teeth."""
    with open_db(db) as conn:
        season = _season(conn, strikes=1, wipeout_annul_through_week=13)
        for user_id in (1, 2, 3):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, "NE", NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])
        assert _alive(conn, season) == 3  # a strike, not a death

        for user_id in (1, 2, 3):
            place_pick(conn, season, user_id, 2, "DAL", W1 + HOUR)
        _finalize(conn, "g2", "PHI")
        report = run_settle(conn, season, AFTER_W2)

        assert report.annulled == [2]
        for user_id in (1, 2, 3):
            row = _player(conn, season, user_id)
            assert (row["status"], row["strikes_used"]) == ("alive", 1)
        assert burned_teams(conn, season["id"], 1) == {"NE", "DAL"}


def test_a_correction_does_not_dissolve_an_announced_annul(db):
    """Why the annul is recorded rather than re-derived.

    Player 3's game is corrected a week later, so week 1 no longer killed
    the whole field. Re-deriving the rule now would un-annul the week and
    kill players 1 and 2 — after the Reckoning told the channel nobody died.
    """
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        for user_id, team in ((1, "NE"), (2, "NE"), (3, "CHI")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        _finalize(conn, "g1b", "GB")
        assert run_settle(conn, season, AFTER_W1).annulled == [1]
        season = get_season(conn, season["id"])

        manual_settle(conn, YEAR, "g1b", "CHI", [season])

        assert get_season(conn, season["id"])["config"]["annulled_weeks"] == [1]
        assert _alive(conn, season) == 3


def test_a_second_sweep_annuls_nothing_new(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        _wipe_week_one(conn, season)
        season = get_season(conn, season["id"])

        again = run_settle(conn, season, AFTER_W1 + HOUR)

        assert again.annulled == []
        assert get_season(conn, season["id"])["config"]["annulled_weeks"] == [1]


# ── the boundary ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("through", "expect_annul"),
    [
        pytest.param(1, True, id="week-equals-dial-annuls"),
        pytest.param(13, True, id="well-inside-the-window"),
        pytest.param(18, True, id="dial-at-the-top-always-annuls"),
        pytest.param(0, False, id="dial-at-zero-always-splits"),
    ],
)
def test_annul_window_boundary(db, through, expect_annul):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=through)
        report = _wipe_week_one(conn, season)

        assert bool(report.annulled) is expect_annul
        assert (report.season_ended is None) is expect_annul


# ── what is not a wipeout ──────────────────────────────────────────────


def test_one_survivor_is_not_a_wipeout(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        for user_id, team in ((1, "NE"), (2, "NE"), (3, "SEA")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        report = run_settle(conn, season, AFTER_W1)

        assert report.annulled == []
        assert report.season_ended is None
        assert _player(conn, season, 1)["status"] == "ghost"
        assert _player(conn, season, 3)["status"] == "alive"


def test_attrition_without_a_football_death_is_not_a_wipeout(db):
    """The last player leaving the server empties the roster, but §1.6 is
    about a week that *killed* the field. Annulling here would resurrect
    somebody who is no longer in the guild, every sweep, forever."""
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=13)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "SEA", NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        eliminate_player(conn, season["id"], 1, 1, source="left")

        assert is_wipeout(conn, season, 1) is False


def test_a_later_death_keeps_an_earlier_week_from_annulling(db):
    """The 'nobody died after' clause. Week 1 kills two of three; week 2
    kills the last one. Asking about week 1 afterwards must not see "nobody
    alive, two died in week 1" and annul a week that wiped out nobody."""
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=0)
        for user_id, team in ((1, "NE"), (2, "NE"), (3, "SEA")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])

        place_pick(conn, season, 3, 2, "DAL", W1 + HOUR)
        _finalize(conn, "g2", "PHI")
        run_settle(conn, season, AFTER_W2)

        assert is_wipeout(conn, season, 2) is True
        assert is_wipeout(conn, season, 1) is False


# ── the split ──────────────────────────────────────────────────────────


def test_wipeout_past_the_window_ends_the_season_and_pays(db):
    with open_db(db) as conn:
        season = _season(
            conn, strikes=0, wipeout_annul_through_week=0,
            pot_seed=1000, ghost_pot_pct=20, buyin_coins=0,
        )
        report = _wipe_week_one(conn, season)

        receipt = report.season_ended
        assert receipt is not None
        assert report.annulled == []
        assert get_season(conn, season["id"])["status"] == "complete"
        # No ghost had a streak, so the side-pot folded into the main split
        # rather than staying in a pot nobody can ever win.
        assert (receipt["main_pot"], receipt["ghost_pot"]) == (1000, 0)
        assert receipt["ghost"] == []
        assert {uid for uid, _ in receipt["main"]} == {1, 2, 3}
        # To the coin, and really moved — read back from the ledger.
        assert sum(amount for _, amount in receipt["main"]) == 1000
        assert sorted(amount for _, amount in receipt["main"]) == [333, 333, 334]
        paid = payout_receipt(conn, season)
        assert sum(r["amount"] for r in paid) == 1000
        assert {r["pot"] for r in paid} == {"main"}


def test_the_split_pays_only_that_weeks_players(db):
    with open_db(db) as conn:
        season = _season(
            conn, strikes=0, wipeout_annul_through_week=0,
            pot_seed=900, ghost_pot_pct=0, buyin_coins=0,
        )
        # Player 3 goes out in week 1 and has no share in week 2's split.
        for user_id, team in ((1, "SEA"), (2, "SEA"), (3, "NE")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])

        for user_id in (1, 2):
            place_pick(conn, season, user_id, 2, "DAL", W1 + HOUR)
        _finalize(conn, "g2", "PHI")
        report = run_settle(conn, season, AFTER_W2)

        assert report.season_ended["main"] == [(1, 450), (2, 450)]


def test_the_ghost_pot_goes_to_the_longest_streak(db):
    with open_db(db) as conn:
        season = _season(
            conn, strikes=0, wipeout_annul_through_week=0,
            pot_seed=1000, ghost_pot_pct=20, buyin_coins=0,
        )
        # Player 3 dies in week 1, then calls week 2 right — one streak.
        for user_id, team in ((1, "SEA"), (2, "SEA"), (3, "NE")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])

        place_pick(conn, season, 3, 2, "PHI", W1 + HOUR)
        for user_id in (1, 2):
            place_pick(conn, season, user_id, 2, "DAL", W1 + HOUR)
        _finalize(conn, "g2", "PHI")
        report = run_settle(conn, season, AFTER_W2)

        receipt = report.season_ended
        assert receipt["ghost"] == [(3, 200)]
        assert receipt["main"] == [(1, 400), (2, 400)]
        assert {r["pot"] for r in payout_receipt(conn, season)} == {"main", "ghost"}


def test_a_settled_season_refuses_a_second_payout(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0, wipeout_annul_through_week=0)
        _wipe_week_one(conn, season)
        season = get_season(conn, season["id"])

        with pytest.raises(SeasonError):
            settle_season_end(conn, season, 1, AFTER_W1)
        # And the retried sweep simply finds nothing left to do.
        assert run_settle(conn, season, AFTER_W1 + HOUR).season_ended is None
        assert len(payout_receipt(conn, season)) == 3


# ── the pure pieces ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pot", "users", "expected"),
    [
        pytest.param(900, [3, 1, 2], [(1, 300), (2, 300), (3, 300)], id="even"),
        pytest.param(10, [1, 2, 3], [(1, 4), (2, 3), (3, 3)], id="remainder"),
        pytest.param(2, [1, 2, 3], [(1, 1), (2, 1)], id="zero-shares-dropped"),
        pytest.param(7, [5, 5, 5], [(5, 7)], id="duplicates-count-once"),
        pytest.param(0, [1, 2], [], id="empty-pot"),
        pytest.param(100, [], [], id="nobody-left"),
    ],
)
def test_even_shares(pot, users, expected):
    shares = even_shares(pot, users)
    assert shares == expected
    assert sum(amount for _, amount in shares) == (pot if users else 0)


def test_streak_winners_split_a_tie(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        # Player 4 survives, so week 1 is an ordinary cull, not a wipeout.
        for user_id, team in ((1, "NE"), (2, "NE"), (3, "NE"), (4, "SEA")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])

        # Two ghosts run a streak of one; the third skips the week.
        for user_id in (1, 2):
            place_pick(conn, season, user_id, 2, "PHI", W1 + HOUR)
        _finalize(conn, "g2", "PHI")
        run_settle(conn, season, AFTER_W2)

        assert streak_winners(conn, season, AFTER_W2) == [1, 2]


def test_streak_winners_is_empty_when_no_ghost_has_one(db):
    with open_db(db) as conn:
        season = _season(conn, strikes=0)
        for user_id, team in ((1, "NE"), (2, "SEA")):
            join_season(conn, season, user_id, NOW)
            place_pick(conn, season, user_id, 1, team, NOW)
        _finalize(conn, "g1", "SEA")
        run_settle(conn, season, AFTER_W1)
        season = get_season(conn, season["id"])

        assert _player(conn, season, 1)["status"] == "ghost"
        assert streak_winners(conn, season, AFTER_W1) == []
