"""Survivor polling loop — stage 4 of docs/plans/survivor.md (spec §4.2).

Every 10 minutes **during game windows** — derived from the ingested
schedule, never hardcoded days, so international 9:30am ET games and
late-season Saturdays are covered — plus one daily off-window refresh that
re-ingests the whole season (flex scheduling moves kickoffs, §6.2; it also
heals any weeks a create-season ingest failed to fetch). After any ingest,
the settle engine runs for every live season sharing that schedule.

``run_poll_cycle`` is the testable core: pure decisions over the DB plus an
injected ``fetch`` callable, no sleeps, no network. ``survivor_poll_loop``
is the thin forever-glue the cog registers as a startup task.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot_modules.core.db_utils import open_db
from bot_modules.services import survivor_espn as espn
from bot_modules.services.survivor_service import get_active_season
from bot_modules.survivor.logic import kickoff_ts
from bot_modules.survivor.settle import SettleReport, run_settle

log = logging.getLogger("dungeonkeeper.survivor")

TICK_SECONDS = 60
POLL_INTERVAL = 600          # §4.2: every 10 min during game windows
REFRESH_INTERVAL = 86400     # one daily off-window full refresh
# A game window: from just before kickoff until the game should long be
# over. Games run ~3¼ hours; the margin absorbs overtime and TV delays.
WINDOW_LEAD = 600.0
WINDOW_TAIL = 5 * 3600.0


@dataclass
class PollState:
    """Loop memory. In-process only — a restart just polls/refreshes once
    more than strictly needed, which is harmless and self-healing."""

    last_poll: float = 0.0
    last_refresh: float = 0.0


@dataclass
class CycleResult:
    fetched_weeks: list[int] = field(default_factory=list)
    refreshed: bool = False
    reports: dict[int, SettleReport] = field(default_factory=dict)


def live_game_weeks(
    conn: sqlite3.Connection, season_year: int, now: float
) -> list[int]:
    """Weeks with a game inside its window right now: in progress, or
    scheduled with kickoff within [-tail, +lead] of now. Empty = no window
    open, nothing worth polling."""
    rows = conn.execute(
        "SELECT week, status, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND status IN ('scheduled', 'in')",
        (season_year,),
    ).fetchall()
    weeks: set[int] = set()
    for r in rows:
        if r["status"] == "in":
            weeks.add(int(r["week"]))
            continue
        ts = kickoff_ts(r["kickoff_utc"])
        if ts - WINDOW_LEAD <= now <= ts + WINDOW_TAIL:
            weeks.add(int(r["week"]))
    return sorted(weeks)


async def run_poll_cycle(
    db_path: Path,
    state: PollState,
    now: float,
    fetch,
) -> CycleResult:
    """One decision+work pass. ``fetch(week, season_year) -> payload dict``
    is injected (aiohttp in prod, fixtures in tests). Commits its own work.
    """
    result = CycleResult()

    def _seasons():
        with open_db(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT guild_id FROM survivor_seasons "
                "WHERE status != 'complete'"
            ).fetchall()
            return [
                season
                for r in rows
                if (season := get_active_season(conn, int(r["guild_id"])))
            ]

    seasons = await asyncio.to_thread(_seasons)
    if not seasons:
        return result
    years = sorted({s["season_year"] for s in seasons})

    do_refresh = now - state.last_refresh >= REFRESH_INTERVAL
    ingested = False
    attempted = False

    if do_refresh:
        # Daily full sweep: every week, catches flex moves and heals gaps.
        for year in years:
            payloads = []
            for week in espn.REGULAR_SEASON_WEEKS:
                try:
                    payloads.append(await fetch(week, year))
                except Exception as exc:  # noqa: BLE001 — fail-soft per week
                    log.warning("survivor poll: refresh week %s failed (%s)", week, exc)
            ingested |= await _ingest_payloads(db_path, year, payloads, result)
        if ingested:
            state.last_refresh = now
        else:
            # Total failure (ESPN outage) must not lock the schedule out for
            # a day — with an empty nfl_games there are no poll windows to
            # recover through either. Retry at the poll cadence instead.
            state.last_refresh = now - REFRESH_INTERVAL + POLL_INTERVAL
        state.last_poll = now
        result.refreshed = True
    elif now - state.last_poll >= POLL_INTERVAL:

        def _windows():
            with open_db(db_path) as conn:
                return {
                    year: live_game_weeks(conn, year, now) for year in years
                }

        windows = await asyncio.to_thread(_windows)
        wanted = [(y, w) for y, weeks in windows.items() for w in weeks]
        if wanted:
            attempted = True
            state.last_poll = now
            for year, week in wanted:
                try:
                    payload = await fetch(week, year)
                except Exception as exc:  # noqa: BLE001
                    log.warning("survivor poll: week %s failed (%s)", week, exc)
                    continue
                ingested |= await _ingest_payloads(db_path, year, [payload], result)

    # Settle runs whenever we ingested — and also on any attempted window
    # poll (even one whose fetches all failed), so the groundskeeper fires
    # near the final kickoff from stored schedule during an ESPN outage.
    if ingested or result.refreshed or attempted:
        def _settle():
            reports = {}
            with open_db(db_path) as conn:
                for season in seasons:
                    report = run_settle(conn, season, now)
                    if report.any_change():
                        reports[season["id"]] = report
                conn.commit()
            return reports

        result.reports = await asyncio.to_thread(_settle)
        for season_id, report in result.reports.items():
            log.info(
                "survivor settle: season %s graded=%d voided=%d assigned=%d "
                "cap_eliminated=%d",
                season_id, report.graded, report.voided,
                len(report.auto_assigned), len(report.cap_eliminated),
            )
    return result


async def _ingest_payloads(db_path, year, payloads, result) -> bool:
    parsed: list[espn.ParsedGame] = []
    for payload in payloads:
        games, skipped = espn.parse_scoreboard(payload)
        if skipped:
            log.warning("survivor poll: %d malformed events skipped", skipped)
        parsed.extend(games)
    if not parsed:
        return False

    def _q():
        with open_db(db_path) as conn:
            espn.ingest_games(conn, year, parsed)
            conn.commit()

    await asyncio.to_thread(_q)
    result.fetched_weeks.extend(sorted({g.week for g in parsed}))
    return True


async def survivor_poll_loop(bot, db_path: Path) -> None:
    """Forever-loop glue. One exception never kills the loop."""
    import aiohttp

    state = PollState()

    async def _fetch(week: int, year: int) -> dict:
        async with aiohttp.ClientSession() as session:
            return await espn.fetch_scoreboard(session, week, year)

    await bot.wait_until_ready()
    while True:
        try:
            await run_poll_cycle(db_path, state, time.time(), _fetch)
        except Exception:
            log.exception("survivor poll loop tick failed")
        await asyncio.sleep(TICK_SECONDS)
