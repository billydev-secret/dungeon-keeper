"""Survivor season simulator — the fake-advance testing rig (2026-08-18).

Season years >= :data:`SIM_YEAR_MIN` are **synthetic**: the poll loop never
fetches ESPN for them (there is nothing to fetch), and the dashboard grows a
Simulator card that drives a whole season through the REAL machinery —
per-game locks, the settle engine, the groundskeeper, the Reckoning — on a
schedule compressed to minutes per week. Nothing here has its own game
logic; the rig only writes ``nfl_games`` rows and calls the same
``manual_settle`` the escape-hatch buttons use, so what you test is what
ships.

Guard-railed hard: every entry point refuses non-synthetic season years, so
the rig can never touch a real season's schedule.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timezone

from bot_modules.survivor.logic import ALL_TEAMS, kickoff_ts
from bot_modules.survivor.settle import manual_settle

# Years at/above this are synthetic. Far above any real NFL season the bot
# will ever run, far below the year-9999 ISO ceiling.
SIM_YEAR_MIN = 2090

GAMES_PER_WEEK = 3


class SimError(ValueError):
    """A simulator rule refused the action (message is user-presentable)."""


def is_sim_year(season_year: int) -> bool:
    return season_year >= SIM_YEAR_MIN


def _require_sim(season: dict) -> None:
    if not is_sim_year(int(season["season_year"])):
        raise SimError(
            "The simulator only touches synthetic seasons (year "
            f"{SIM_YEAR_MIN}+). Create a season with year {SIM_YEAR_MIN} or "
            "later to test."
        )


def generate_schedule(
    conn: sqlite3.Connection,
    season: dict,
    *,
    weeks: int,
    minutes_per_week: int,
    now: float,
) -> int:
    """Lay down a compressed synthetic schedule starting one minute from
    now: ``weeks`` weeks, three games each, kickoffs spread across each
    week's span, rotating teams so no matchup repeats early, favorites with
    plausible frozen probabilities. Replaces any prior synthetic schedule
    for the year (re-running resets the season's board, not its players).
    """
    _require_sim(season)
    if not 1 <= weeks <= 18:
        raise SimError("Weeks must be 1–18.")
    if not 2 <= minutes_per_week <= 24 * 60:
        raise SimError("Minutes per week must be 2–1440.")
    year = int(season["season_year"])
    conn.execute("DELETE FROM nfl_games WHERE season_year = ?", (year,))

    teams = sorted(ALL_TEAMS)
    span = minutes_per_week * 60.0
    created = 0
    for week in range(1, weeks + 1):
        base = now + 60.0 + (week - 1) * span
        # Rotate the pairing each week so chalk has fresh teams to burn.
        offset = (week * 7) % len(teams)
        rotated = teams[offset:] + teams[:offset]
        for i in range(GAMES_PER_WEEK):
            home, away = rotated[2 * i], rotated[2 * i + 1]
            kickoff = base + i * (span / (GAMES_PER_WEEK + 1))
            prob = round(0.55 + 0.1 * i + 0.01 * week, 4)
            conn.execute(
                "INSERT INTO nfl_games (season_year, week, game_id, home,"
                " away, kickoff_utc, favorite, favorite_prob)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    year, week, f"sim-{year}-w{week}g{i}", home, away,
                    datetime.fromtimestamp(kickoff, timezone.utc).isoformat(),
                    home, min(prob, 0.95),
                ),
            )
            created += 1
    return created


def settle_kicked(
    conn: sqlite3.Connection,
    season: dict,
    *,
    mode: str,
    now: float,
    live_seasons: list[dict],
    rng: random.Random | None = None,
) -> list[tuple[str, str]]:
    """Settle every kicked, unsettled synthetic game through the real
    manual-settle pipeline. ``mode``: 'chalk' (favorite wins), 'upset'
    (underdog wins), 'random' (coin flip, ~15% ties for flavor). Returns
    [(game_id, outcome)]."""
    _require_sim(season)
    if mode not in ("chalk", "upset", "random"):
        raise SimError("Mode must be chalk, upset, or random.")
    rng = rng or random.Random()
    year = int(season["season_year"])
    rows = conn.execute(
        "SELECT game_id, home, away, favorite, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND status = 'scheduled' AND winner IS NULL "
        "ORDER BY kickoff_utc",
        (year,),
    ).fetchall()
    settled: list[tuple[str, str]] = []
    for r in rows:
        if kickoff_ts(r["kickoff_utc"]) > now:
            continue
        favorite = r["favorite"] or r["home"]
        underdog = r["away"] if favorite == r["home"] else r["home"]
        if mode == "chalk":
            outcome = favorite
        elif mode == "upset":
            outcome = underdog
        elif rng.random() < 0.15:
            outcome = "TIE"
        else:
            outcome = favorite if rng.random() < 0.6 else underdog
        manual_settle(conn, year, r["game_id"], outcome, live_seasons)
        settled.append((r["game_id"], outcome))
    return settled
