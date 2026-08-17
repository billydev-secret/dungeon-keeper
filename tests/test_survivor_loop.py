"""Tests for services/survivor_loop.py — window detection and the poll cycle.

The cycle runs with an injected ``fetch`` returning hand-built scoreboard
payloads — no network, no sleeps; the forever-loop is untested glue.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_loop import (
    POLL_INTERVAL,
    PollState,
    live_game_weeks,
    run_poll_cycle,
)
from bot_modules.services.survivor_service import create_season, get_season
from bot_modules.survivor.logic import join_season, place_pick
from tests.db_template import migrated_db

GID = 100
YEAR = 2026
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0
DAY = 24 * HOUR
THU = NOW + DAY


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _event(game_id, week, home, away, kickoff, *, status="STATUS_SCHEDULED",
           winner=None):
    def competitor(abbr, home_away):
        entry = {"team": {"abbreviation": abbr}, "homeAway": home_away}
        if winner is not None:
            entry["winner"] = abbr == winner
            entry["score"] = "20" if abbr == winner else "10"
        return entry

    return {
        "id": game_id,
        "week": {"number": week},
        "date": datetime.fromtimestamp(kickoff, timezone.utc)
        .strftime("%Y-%m-%dT%H:%MZ"),
        "competitions": [{
            "status": {"type": {"name": status}},
            "competitors": [competitor(home, "home"), competitor(away, "away")],
            "odds": [],
        }],
    }


def _payload(*events):
    return {"events": list(events)}


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO nfl_games (season_year, week, game_id, home, away, kickoff_utc)"
            " VALUES (?, 1, 'g-thu', 'SEA', 'NE', ?)",
            (YEAR, _iso(THU)),
        )
    return db_path


class Fetcher:
    """Injectable fetch: records calls, serves a payload table."""

    def __init__(self, payloads=None, fail_weeks=()):
        self.payloads = payloads or {}
        self.fail_weeks = set(fail_weeks)
        self.calls: list[tuple[int, int]] = []

    async def __call__(self, week: int, year: int) -> dict:
        self.calls.append((week, year))
        if week in self.fail_weeks:
            raise ConnectionError("boom")
        return self.payloads.get(week, _payload())


# ── window detection ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(THU - 2 * HOUR, [], id="hours-before-no-window"),
        pytest.param(THU - 300, [1], id="just-before-kickoff"),
        pytest.param(THU + 3 * HOUR, [1], id="mid-game"),
        pytest.param(THU + 6 * HOUR, [], id="long-after"),
    ],
)
def test_live_game_weeks_scheduled(db, now, expected):
    with open_db(db) as conn:
        assert live_game_weeks(conn, YEAR, now) == expected


def test_live_game_weeks_in_progress_always_counts(db):
    with open_db(db) as conn:
        conn.execute("UPDATE nfl_games SET status = 'in' WHERE game_id = 'g-thu'")
        # Even far outside the kickoff window: 'in' means poll until final.
        assert live_game_weeks(conn, YEAR, THU + 8 * HOUR) == [1]


# ── the cycle ──────────────────────────────────────────────────────────


async def test_no_live_season_no_fetches(db):
    fetch = Fetcher()
    result = await run_poll_cycle(db, PollState(), NOW, fetch)
    assert fetch.calls == []
    assert not result.refreshed


async def test_first_cycle_is_a_full_refresh(db):
    with open_db(db) as conn:
        create_season(conn, GID, "S", YEAR)
    fetch = Fetcher(fail_weeks={5})  # one dead week must not stop the sweep
    state = PollState()
    result = await run_poll_cycle(db, state, NOW, fetch)
    assert result.refreshed
    assert len(fetch.calls) == 18
    assert state.last_refresh == NOW


async def test_window_poll_fetches_only_live_weeks_and_settles(db):
    with open_db(db) as conn:
        season_id = create_season(conn, GID, "S", YEAR)
        season = get_season(conn, season_id)
        join_season(conn, season, 1, NOW)
        place_pick(conn, season, 1, 1, "NE", NOW)

    final_payload = _payload(
        _event("g-thu", 1, "SEA", "NE", THU, status="STATUS_FINAL", winner="SEA")
    )
    fetch = Fetcher(payloads={1: final_payload})
    # Refresh recent; a window is open (mid-game).
    now = THU + 3 * HOUR
    state = PollState(last_poll=0.0, last_refresh=now - HOUR)
    result = await run_poll_cycle(db, state, now, fetch)
    assert fetch.calls == [(1, YEAR)]
    assert state.last_poll == now
    # The ingested final flowed straight through the settle engine.
    assert result.reports
    with open_db(db) as conn:
        row = conn.execute(
            "SELECT result FROM survivor_picks WHERE season_id = ? AND user_id = 1",
            (season_id,),
        ).fetchone()
        assert row["result"] == "loss"
        player = conn.execute(
            "SELECT strikes_used FROM survivor_players "
            "WHERE season_id = ? AND user_id = 1",
            (season_id,),
        ).fetchone()
        assert player["strikes_used"] == 1


async def test_poll_respects_interval_and_windows(db):
    with open_db(db) as conn:
        create_season(conn, GID, "S", YEAR)
    fetch = Fetcher()

    # No window open → nothing fetched even though the interval elapsed.
    state = PollState(last_poll=0.0, last_refresh=NOW)
    await run_poll_cycle(db, state, NOW, fetch)  # Wednesday, no games near
    assert fetch.calls == []

    # Window open but polled recently → wait.
    now = THU + HOUR
    state = PollState(last_poll=now - POLL_INTERVAL + 60, last_refresh=now)
    await run_poll_cycle(db, state, now, fetch)
    assert fetch.calls == []
