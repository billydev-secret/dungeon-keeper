"""Tests for survivor/sim.py — the fake-advance testing rig.

The rig must drive the REAL machinery (its settles run through
manual_settle, so grading is derived) and must be incapable of touching a
real season: every entry point refuses non-synthetic years.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.survivor_loop import PollState, run_poll_cycle
from bot_modules.services.survivor_service import create_season, get_season
from bot_modules.survivor.logic import join_season, place_pick
from bot_modules.survivor.sim import (
    SIM_YEAR_MIN,
    SimError,
    generate_schedule,
    is_sim_year,
    settle_kicked,
)
from tests.db_template import migrated_db

GID = 100
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


def _season(conn, year=SIM_YEAR_MIN):
    return get_season(conn, create_season(conn, GID, "Sim", year))


def test_real_years_are_refused(db):
    with open_db(db) as conn:
        season = _season(conn, year=2026)
        with pytest.raises(SimError, match="synthetic"):
            generate_schedule(conn, season, weeks=2, minutes_per_week=10, now=NOW)
        with pytest.raises(SimError, match="synthetic"):
            settle_kicked(conn, season, mode="chalk", now=NOW, live_seasons=[season])
    assert not is_sim_year(2026) and is_sim_year(SIM_YEAR_MIN)


def test_generate_schedule_shape_and_regeneration(db):
    with open_db(db) as conn:
        season = _season(conn)
        created = generate_schedule(
            conn, season, weeks=3, minutes_per_week=10, now=NOW
        )
        assert created == 9  # 3 weeks × 3 games
        rows = conn.execute(
            "SELECT week, game_id, home, away, favorite, favorite_prob,"
            " kickoff_utc FROM nfl_games WHERE season_year = ? ORDER BY week",
            (SIM_YEAR_MIN,),
        ).fetchall()
        assert len(rows) == 9
        assert all(r["favorite"] and r["favorite_prob"] for r in rows)
        assert all(r["home"] != r["away"] for r in rows)
        # Regeneration replaces, never accretes.
        generate_schedule(conn, season, weeks=1, minutes_per_week=10, now=NOW)
        count = conn.execute(
            "SELECT COUNT(*) FROM nfl_games WHERE season_year = ?",
            (SIM_YEAR_MIN,),
        ).fetchone()[0]
        assert count == 3


@pytest.mark.parametrize(
    ("mode", "check"),
    [
        pytest.param("chalk", lambda o, fav, dog: o == fav, id="chalk"),
        pytest.param("upset", lambda o, fav, dog: o == dog, id="upset"),
        pytest.param(
            "random", lambda o, fav, dog: o in (fav, dog, "TIE"), id="random"
        ),
    ],
)
def test_settle_kicked_modes_flow_through_real_grading(db, mode, check):
    with open_db(db) as conn:
        season = _season(conn)
        generate_schedule(conn, season, weeks=1, minutes_per_week=5, now=NOW)
        join_season(conn, season, 1, NOW)
        first = conn.execute(
            "SELECT game_id, home, away, favorite FROM nfl_games "
            "WHERE season_year = ? ORDER BY kickoff_utc LIMIT 1",
            (SIM_YEAR_MIN,),
        ).fetchone()
        place_pick(conn, season, 1, 1, first["favorite"], NOW + 59)

        # Nothing kicked yet → nothing settles.
        assert settle_kicked(
            conn, season, mode=mode, now=NOW, live_seasons=[season],
        ) == []
        # After the compressed week: everything kicked settles.
        later = NOW + 6 * 60
        settled = settle_kicked(
            conn, season, mode=mode, now=later, live_seasons=[season],
            rng=random.Random(7),
        )
        assert len(settled) == 3
        for game_id, outcome in settled:
            row = conn.execute(
                "SELECT home, away, favorite, winner, status, result_source "
                "FROM nfl_games WHERE game_id = ?",
                (game_id,),
            ).fetchone()
            dog = row["away"] if row["favorite"] == row["home"] else row["home"]
            assert check(outcome, row["favorite"], dog)
            assert row["result_source"] == "manual"  # the feed can't cross it
        # The pick graded through the real pipeline.
        pick = conn.execute(
            "SELECT result FROM survivor_picks WHERE season_id = ? AND user_id = 1",
            (season["id"],),
        ).fetchone()
        assert pick["result"] is not None


async def test_poll_cycle_never_fetches_sim_years(db):
    with open_db(db) as conn:
        season = _season(conn)
        generate_schedule(conn, season, weeks=1, minutes_per_week=5, now=NOW)

    calls = []

    async def fetch(week, year):
        calls.append((week, year))
        return {"events": []}

    # First cycle would be a "refresh" — sim-only seasons have no real years,
    # so zero fetches ever.
    state = PollState()
    await run_poll_cycle(db, state, NOW, fetch)
    assert calls == []

    # A sim window open (game imminent) still counts as an attempted poll,
    # so the settle sweep (locks, groundskeeper) runs in compressed time.
    state = PollState(last_poll=0.0, last_refresh=NOW)
    await run_poll_cycle(db, state, NOW + 90, fetch)
    assert calls == []
    assert state.last_poll == NOW + 90


def test_weekly_task_force_bypasses_clock_not_state(db):
    from bot_modules.survivor import tasks
    from bot_modules.services.survivor_service import update_config

    with open_db(db) as conn:
        season = _season(conn)
        update_config(conn, season["id"], {"channel_id": 555})
        season = get_season(conn, season["id"])
        generate_schedule(conn, season, weeks=2, minutes_per_week=5, now=NOW)
        # NOW is Wednesday noon UTC but the sim week's "Wednesday 9am" gate
        # is irrelevant under force; without force the gate applies normally.
        assert tasks.slate_due(conn, season, NOW + 61, 0.0, force=True) == 1
        # State still guards once-per-week even when forced.
        update_config(conn, season["id"], {"last_slate_week": 1})
        season = get_season(conn, season["id"])
        assert tasks.slate_due(conn, season, NOW + 61, 0.0, force=True) is None
