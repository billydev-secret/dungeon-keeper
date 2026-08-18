"""The Gauntlet — late entry's replay engine (spec §1.1, §4.2, stage 5b).

Join any week: every fully-elapsed week is replayed as that week's chalk —
the highest win-probability favorite by the FROZEN closing odds, no reuse —
and graded against stored winners. **Deterministic by design**: the replay
reads only ``nfl_games.favorite/favorite_prob/winner``, never live odds, so
two joiners entering at the same moment inherit byte-identical lines.

The replay ends at death *(clarified 2026-08-18)*: replaying chalk past the
fatal week would either hand the new ghost an unearned streak of picks they
never made, or burn teams for a corpse. The fatal line's teams are burned;
"picking from day one" means the ghost game starts now, live. The fee still
charges every elapsed week — waiting is what's priced, not surviving.

Pure functions over a caller-owned connection; the join view is glue.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bot_modules.services import economy_service
from bot_modules.services.survivor_service import add_player
from bot_modules.survivor.logic import (
    KIND_BUYIN,
    KIND_GAUNTLET_FEE,
    PickError,
    elapsed_weeks,
)


@dataclass(frozen=True)
class ReplayWeek:
    week: int
    team: str | None        # None = void week (no legal chalk, §6.10)
    game_id: str | None
    result: str             # win|loss|tie|void
    strikes_after: int
    fatal: bool


@dataclass(frozen=True)
class GauntletFate:
    weeks: tuple[ReplayWeek, ...]
    elapsed_count: int      # fee basis: every elapsed week, replayed or not
    strikes_used: int
    dead: bool
    death_week: int | None
    burned: tuple[str, ...]
    fee: int


def compute_fate(conn: sqlite3.Connection, season: dict, now: float) -> GauntletFate:
    """The would-be joiner's inherited line, computed without side effects —
    this is what the receipt shows *before* anyone pays (§4.2)."""
    config = season["config"]
    elapsed = elapsed_weeks(conn, season["season_year"], now)
    allowed = int(config["strikes"])
    losing = {"loss", "tie"} if config["tie_rule"] == "loss" else {"loss"}
    dp_start = int(config["double_pick_start_week"])

    used: set[str] = set()
    weeks: list[ReplayWeek] = []
    strikes = 0
    death_week: int | None = None

    for week in elapsed:
        # Chalk candidates: settled or postponed games with a frozen favorite
        # not yet used by this line. Unsettled stragglers are excluded — the
        # replay grades only stored winners (§4.2), and a pending game can't
        # be inherited without changing fate when it settles.
        rows = conn.execute(
            "SELECT game_id, favorite, favorite_prob, winner, status "
            "FROM nfl_games WHERE season_year = ? AND week = ? "
            "AND favorite IS NOT NULL "
            "AND (winner IS NOT NULL OR status = 'postponed')",
            (season["season_year"], week),
        ).fetchall()
        candidates = sorted(
            (r for r in rows if r["favorite"] not in used),
            key=lambda r: (
                -(r["favorite_prob"] if r["favorite_prob"] is not None else -1.0),
                r["favorite"],
            ),
        )
        # §6.9: double-pick era replays top-two chalk; one fate per week.
        slots = 2 if dp_start and week >= dp_start else 1
        chosen = candidates[:slots]
        if not chosen:
            # §6.10: no legal chalk — the week is voided. Survive, no burn.
            weeks.append(ReplayWeek(week, None, None, "void", strikes, False))
            if death_week is not None:
                break
            continue
        week_lost = False
        week_rows: list[tuple] = []
        for r in chosen:
            team = r["favorite"]
            if r["status"] == "postponed":
                # §6.9: a voided chalk game replays as void — team returns.
                week_rows.append((team, r["game_id"], "void"))
                continue
            used.add(team)
            if r["winner"] == "TIE":
                result = "tie"
            elif r["winner"] == team:
                result = "win"
            else:
                result = "loss"
            week_lost |= result in losing
            week_rows.append((team, r["game_id"], result))
        if week_lost:
            strikes += 1
        fatal = week_lost and strikes > allowed and death_week is None
        if fatal:
            death_week = week
        for team, game_id, result in week_rows:
            weeks.append(
                ReplayWeek(week, team, game_id, result, strikes, fatal)
            )
        if death_week is not None:
            break  # the replay ends at death — the line is over

    fee = int(config["gauntlet_fee_per_week"]) * len(elapsed)
    return GauntletFate(
        weeks=tuple(weeks),
        elapsed_count=len(elapsed),
        strikes_used=min(strikes, allowed + 1),
        dead=death_week is not None,
        death_week=death_week,
        burned=tuple(sorted(used)),
        fee=fee,
    )


def execute_gauntlet_join(
    conn: sqlite3.Connection,
    season: dict,
    user_id: int,
    fate: GauntletFate,
    now: float,
) -> None:
    """Charge and enroll a late joiner with their inherited fate.

    Same transaction discipline as the plain join: duplicate check, buy-in
    debit, gauntlet fee debit — the fee routes by arrival state (§1.1: alive
    arrivals feed the main pot, dead-on-arrival fees feed the ghost pot) —
    then the entry row and the replayed pick rows, all or nothing. Callers
    hold an IMMEDIATE transaction (this is a money path).
    """
    if season["status"] == "complete":
        raise PickError("This season is over.")
    already = conn.execute(
        "SELECT 1 FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()
    if already is not None:
        raise PickError("One entry per person — and you're already in.")

    config = season["config"]
    buyin = int(config["buyin_coins"])
    total = buyin + fate.fee
    balance = economy_service.get_balance(conn, season["guild_id"], user_id)
    if total > 0 and balance < total:
        raise PickError(
            f"Late entry costs {total:,} coins ({fate.fee:,} late fee"
            + (f" + {buyin:,} buy-in" if buyin else "")
            + ") — your balance is short."
        )
    if buyin > 0:
        economy_service.apply_debit(
            conn, season["guild_id"], user_id, buyin, KIND_BUYIN,
            meta={"season_id": season["id"]},
        )
    if fate.fee > 0:
        economy_service.apply_debit(
            conn, season["guild_id"], user_id, fate.fee, KIND_GAUNTLET_FEE,
            meta={
                "season_id": season["id"],
                "pot": "ghost" if fate.dead else "main",
                "weeks": fate.elapsed_count,
            },
        )
    if not add_player(conn, season, user_id, joined_at=now):
        raise PickError("One entry per person — and you're already in.")

    for rw in fate.weeks:
        if rw.team is None:
            continue  # a fully-void week leaves no row — nothing happened
        conn.execute(
            "INSERT INTO survivor_picks "
            "(season_id, guild_id, user_id, week, slot, team, game_id, result) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                season["id"], season["guild_id"], user_id, rw.week,
                _slot_for(fate.weeks, rw), rw.team, rw.game_id, rw.result,
            ),
        )
    if fate.dead:
        # Picks-derived death: the replayed losses ARE pick rows, so the
        # settle engine's recompute agrees with this verdict forever.
        conn.execute(
            "UPDATE survivor_players SET status = 'ghost', strikes_used = ?, "
            "eliminated_week = ?, elimination_source = 'picks' "
            "WHERE season_id = ? AND user_id = ?",
            (fate.strikes_used, fate.death_week, season["id"], user_id),
        )
    else:
        conn.execute(
            "UPDATE survivor_players SET strikes_used = ? "
            "WHERE season_id = ? AND user_id = ?",
            (fate.strikes_used, season["id"], user_id),
        )


def _slot_for(weeks: tuple[ReplayWeek, ...], target: ReplayWeek) -> int:
    """Slot number within the week (double-pick era replays two rows)."""
    slot = 1
    for rw in weeks:
        if rw is target:
            return slot
        if rw.week == target.week and rw.team is not None:
            slot += 1
    return slot


def ghost_only_join(
    conn: sqlite3.Connection, season: dict, user_id: int, now: float
) -> None:
    """late_entry='ghost_only': arrive straight into Ghost Streak — no
    replay, no fee (there is no living game to buy an option on), full
    satchel. Source 'entry' so recomputation never resurrects them."""
    if season["status"] == "complete":
        raise PickError("This season is over.")
    if not add_player(conn, season, user_id, joined_at=now):
        raise PickError("One entry per person — and you're already in.")
    elapsed = elapsed_weeks(conn, season["season_year"], now)
    conn.execute(
        "UPDATE survivor_players SET status = 'ghost', eliminated_week = ?, "
        "elimination_source = 'entry' WHERE season_id = ? AND user_id = ?",
        (max(elapsed) if elapsed else 0, season["id"], user_id),
    )
