"""Tests for services/survivor_espn.py — parser + ingest on saved JSON.

Stage 2 of docs/plans/survivor.md. The fixtures are real scoreboard payloads
captured 2026-08-17 (a pregame week with odds, a completed week without —
ESPN drops odds entirely once a game finishes, which is the whole reason
favorites are frozen at the last pre-kickoff poll). The suite never touches
the network; edge shapes ESPN wasn't serving that day (ties, postponements,
malformed events) are surgical mutations of the real payloads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_espn import (
    ingest_games,
    parse_scoreboard,
)
from tests.db_template import migrated_db

FIXTURES = Path(__file__).parent / "fixtures" / "espn"


@pytest.fixture(scope="module")
def pregame() -> dict:
    return json.loads(
        (FIXTURES / "2026_week1_pregame.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def final_week() -> dict:
    return json.loads(
        (FIXTURES / "2025_week15_final.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


# ── parsing real payloads ──────────────────────────────────────────────


def test_pregame_week_parses_fully(pregame):
    games, skipped = parse_scoreboard(pregame)
    assert skipped == 0
    assert len(games) == 16
    assert all(g.status == "scheduled" for g in games)
    assert all(g.week == 1 for g in games)
    assert all(g.winner is None for g in games)
    # Every pregame game in the capture had a published line.
    assert all(g.favorite in (g.home, g.away) for g in games)
    assert all(g.favorite_prob is None or 0.5 <= g.favorite_prob < 1 for g in games)
    # No team appears twice as home or twice as away in one week's slate.
    assert len({g.home for g in games} | {g.away for g in games}) == 32


def test_pregame_known_game_pins_the_numbers(pregame):
    games, _ = parse_scoreboard(pregame)
    opener = next(g for g in games if g.home == "SEA" and g.away == "NE")
    # Thursday opener: minute-precision Zulu normalized to full ISO UTC.
    assert opener.kickoff_utc == "2026-09-10T00:20:00+00:00"
    # DK closes -185/+154 → vig-stripped two-way implied probability. This
    # exact value is what the gauntlet replays forever, so pin it hard.
    assert opener.favorite == "SEA"
    assert opener.favorite_prob == 0.6225


def test_final_week_parses_winners_without_odds(final_week):
    games, skipped = parse_scoreboard(final_week)
    assert skipped == 0
    assert len(games) == 16
    assert all(g.status == "final" for g in games)
    # ESPN serves no odds for completed games — columns stay NULL.
    assert all(g.favorite is None and g.favorite_prob is None for g in games)
    assert all(g.winner is not None for g in games)
    atl = next(g for g in games if g.home == "TB" and g.away == "ATL")
    assert atl.winner == "ATL"  # 29–28 on the road, from the winner flag


# ── edge shapes via surgical mutation ──────────────────────────────────


def _first_event(payload: dict) -> dict:
    mutated = copy.deepcopy(payload)
    return mutated, mutated["events"][0]


def test_tie_is_detected_from_equal_scores(final_week):
    mutated, event = _first_event(final_week)
    for competitor in event["competitions"][0]["competitors"]:
        competitor.pop("winner", None)
        competitor["score"] = "24"
    games, skipped = parse_scoreboard(mutated)
    assert skipped == 0
    assert games[0].winner == "TIE"


def test_final_with_no_flag_and_unreadable_scores_stays_unsettled(final_week):
    mutated, event = _first_event(final_week)
    for competitor in event["competitions"][0]["competitors"]:
        competitor.pop("winner", None)
        competitor.pop("score", None)
    games, _ = parse_scoreboard(mutated)
    # None = flagged at the Reckoning and admin settle's job — never a guess.
    assert games[0].winner is None


@pytest.mark.parametrize(
    ("espn_status", "ours"),
    [
        pytest.param("STATUS_POSTPONED", "postponed", id="postponed"),
        pytest.param("STATUS_CANCELED", "postponed", id="canceled"),
        pytest.param("STATUS_IN_PROGRESS", "in", id="in-progress"),
        pytest.param("STATUS_HALFTIME", "in", id="halftime-maps-to-in"),
        pytest.param("STATUS_SOMETHING_NEW", "in", id="unknown-fails-safe-to-in"),
    ],
)
def test_status_mapping(pregame, espn_status, ours):
    mutated, event = _first_event(pregame)
    event["competitions"][0]["status"]["type"]["name"] = espn_status
    games, _ = parse_scoreboard(mutated)
    assert games[0].status == ours


def test_malformed_event_is_skipped_not_fatal(pregame):
    mutated, event = _first_event(pregame)
    del event["competitions"]
    games, skipped = parse_scoreboard(mutated)
    assert skipped == 1
    assert len(games) == 15  # the rest of the slate survives


def test_favorite_without_readable_moneyline_keeps_the_abbr(pregame):
    mutated, event = _first_event(pregame)
    event["competitions"][0]["odds"][0].pop("moneyline", None)
    games, _ = parse_scoreboard(mutated)
    assert games[0].favorite == "SEA"  # who was chalk survives
    assert games[0].favorite_prob is None  # by-how-much doesn't


def test_missing_odds_entirely_is_not_an_error(pregame):
    mutated, event = _first_event(pregame)
    event["competitions"][0].pop("odds", None)
    games, skipped = parse_scoreboard(mutated)
    assert skipped == 0
    assert games[0].favorite is None and games[0].favorite_prob is None


# ── ingest ─────────────────────────────────────────────────────────────


def _game_row(conn, game_id):
    return conn.execute(
        "SELECT * FROM nfl_games WHERE season_year = 2026 AND game_id = ?",
        (game_id,),
    ).fetchone()


def test_ingest_insert_then_idempotent_reingest(db, pregame):
    games, _ = parse_scoreboard(pregame)
    with open_db(db) as conn:
        assert ingest_games(conn, 2026, games) == {"inserted": 16, "updated": 0}
        assert ingest_games(conn, 2026, games) == {"inserted": 0, "updated": 16}
        count = conn.execute("SELECT COUNT(*) FROM nfl_games").fetchone()[0]
        assert count == 16


def test_favorite_freezes_at_last_prekickoff_poll(db, pregame):
    games, _ = parse_scoreboard(pregame)
    opener = next(g for g in games if g.home == "SEA")
    with open_db(db) as conn:
        ingest_games(conn, 2026, [opener])

        # Line moves while still scheduled → tracked (this IS the last poll).
        moved = _replace(opener, favorite="NE", favorite_prob=0.51)
        ingest_games(conn, 2026, [moved])
        row = _game_row(conn, opener.game_id)
        assert (row["favorite"], row["favorite_prob"]) == ("NE", 0.51)

        # Kickoff: post-kickoff payloads carry no odds. Frozen value stays.
        live = _replace(opener, status="in", favorite=None, favorite_prob=None)
        ingest_games(conn, 2026, [live])
        row = _game_row(conn, opener.game_id)
        assert (row["favorite"], row["favorite_prob"]) == ("NE", 0.51)
        assert row["status"] == "in"


def test_flex_scheduling_updates_kickoff_and_week(db, pregame):
    games, _ = parse_scoreboard(pregame)
    opener = games[0]
    with open_db(db) as conn:
        ingest_games(conn, 2026, [opener])
        flexed = _replace(
            opener, kickoff_utc="2026-09-11T00:20:00+00:00", week=2
        )
        ingest_games(conn, 2026, [flexed])
        row = _game_row(conn, opener.game_id)
        assert row["kickoff_utc"] == "2026-09-11T00:20:00+00:00"
        assert row["week"] == 2


def test_winner_sets_once_and_manual_settle_outranks_the_feed(db, pregame):
    games, _ = parse_scoreboard(pregame)
    opener = games[0]
    with open_db(db) as conn:
        ingest_games(conn, 2026, [opener])
        final = _replace(opener, status="final", winner=opener.home)
        ingest_games(conn, 2026, [final])
        assert _game_row(conn, opener.game_id)["winner"] == opener.home

        # A later feed disagreeing (or an admin having settled first) does
        # not overwrite — corrections go through admin settle, not the poll.
        contradiction = _replace(opener, status="final", winner=opener.away)
        ingest_games(conn, 2026, [contradiction])
        assert _game_row(conn, opener.game_id)["winner"] == opener.home


def test_final_first_ingest_never_backfills_odds(db, final_week):
    # A game first seen as final (bot was down all week): winner lands,
    # favorite stays NULL forever — closing odds are unrecoverable.
    games, _ = parse_scoreboard(final_week)
    with open_db(db) as conn:
        ingest_games(conn, 2025, games[:1])
        row = conn.execute(
            "SELECT * FROM nfl_games WHERE season_year = 2025 AND game_id = ?",
            (games[0].game_id,),
        ).fetchone()
        assert row["winner"] == games[0].winner
        assert row["favorite"] is None and row["favorite_prob"] is None


def _replace(game, **changes):
    from dataclasses import replace

    return replace(game, **changes)


# ── fetch_season aggregation (stub session, still no network) ──────────


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


class _StubSession:
    """Serves the pregame fixture for every week, erroring on chosen ones."""

    def __init__(self, payload, fail_weeks=()):
        self._payload = payload
        self._fail_weeks = set(fail_weeks)
        self.urls: list[str] = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        week = int(url.split("week=")[1].split("&")[0])
        if week in self._fail_weeks:
            raise ConnectionError(f"week {week} boom")
        return _StubResponse(self._payload)


async def test_fetch_season_sweeps_all_weeks_and_fails_soft(pregame):
    from bot_modules.services.survivor_espn import fetch_season

    session = _StubSession(pregame, fail_weeks={7, 12})
    games, skipped, failed = await fetch_season(session, 2026)
    assert failed == [7, 12]
    assert len(games) == 16 * 16  # 18 weeks minus the two failures
    assert skipped == 0
    # The season-year selector must be dates=, not year= — ESPN silently
    # ignores year and serves the current season (found the hard way).
    assert all("dates=2026" in url for url in session.urls)
    assert len(session.urls) == 18
