"""Survivor settle engine — stage 4 of docs/plans/survivor.md (spec §4.2).

Grades picks against ``nfl_games``, applies strikes and eliminations, runs
the groundskeeper's auto-assign, and stays **idempotent and correctable**:

- A pick's result is a pure function of its game's stored state, so
  re-running a sweep changes nothing (§6.7), and a corrected winner (the
  panel's manual settle) re-grades the pick and *recomputes* the player —
  a wrongly-recorded loss un-burns the strike and resurrects the player.
- Player life-state derives from graded picks alone, EXCEPT deaths whose
  source is not 'picks' (groundskeeper cap, admin, leaver) — those are
  decisions, not derivations, and survive recomputation.
- Ghosts' picks grade like everyone's (Ghost Streak reads them later) but
  never cost strikes — the death week caps what counts.

Pure functions over a caller-owned connection and injected ``now``; the
polling loop and the settle route are thin callers.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot_modules.services.survivor_service import eliminate_player, set_season_status
from bot_modules.survivor.logic import burned_teams, kickoff_ts

log = logging.getLogger("dungeonkeeper.survivor")

# The auto-assign-cap elimination's fixed line (§1.2) — deliberately not in
# the flavor corpus; it is THE line for this one path.
GROUNDSKEEPER_DECLINE = "the groundskeeper stopped covering for {name}."

# How long past the final kickoff the groundskeeper may still assign. Covers
# the 10-minute poll cadence plus restart slack; beyond it the closer is deep
# in progress and an assignment would carry mid-game information no member's
# own pick could — the pickless survive instead (stage-4 review).
ASSIGN_GRACE_SECONDS = 30 * 60


@dataclass
class SettleReport:
    """What one sweep did — for logs and the panel's week card."""

    graded: int = 0
    voided: int = 0
    recomputed: list[int] = field(default_factory=list)
    auto_assigned: list[tuple[int, str]] = field(default_factory=list)
    cap_eliminated: list[int] = field(default_factory=list)
    no_legal_team: list[int] = field(default_factory=list)
    locked: int = 0
    graded_weeks: set[int] = field(default_factory=set)  # for the addendum

    def any_change(self) -> bool:
        return bool(
            self.graded or self.voided or self.auto_assigned
            or self.cap_eliminated or self.locked
        )


def expected_result(team: str, status: str, winner: str | None) -> str | None:
    """The result a pick on ``team`` must carry given its game's state.
    None = not settleable yet. Pure — this is the whole grading rule (§1.3)."""
    if status == "postponed":
        return "void"
    if status == "final" and winner is not None:
        if winner == "TIE":
            return "tie"
        return "win" if winner == team else "loss"
    return None


def losing_results(config: dict) -> set[str]:
    """Which results burn a strike: tie counts per ``tie_rule`` (§1.3)."""
    return {"loss", "tie"} if config["tie_rule"] == "loss" else {"loss"}


# ── player recomputation ───────────────────────────────────────────────


def recompute_player(conn: sqlite3.Connection, season: dict, user_id: int) -> None:
    """Re-derive a player's strikes and life-state from their graded picks.

    One fate per week (§6.8): a week with any losing result costs exactly one
    strike no matter how many slots lost or how many sweeps graded them.
    Non-'picks' deaths (cap/admin/left) are decisions and stand if they came
    no later than the derived death; a derived death is applied with source
    'picks' so a later correction can undo it.
    """
    row = conn.execute(
        "SELECT status, eliminated_week, elimination_source "
        "FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()
    if row is None:
        return

    losing = losing_results(season["config"])
    allowed = int(season["config"]["strikes"])
    weeks = conn.execute(
        "SELECT week, result FROM survivor_picks "
        "WHERE season_id = ? AND user_id = ? AND result IS NOT NULL "
        "ORDER BY week",
        (season["id"], user_id),
    ).fetchall()
    losing_weeks = sorted({int(r["week"]) for r in weeks if r["result"] in losing})
    derived_death = (
        losing_weeks[allowed] if len(losing_weeks) > allowed else None
    )

    stored_death = (
        int(row["eliminated_week"])
        if row["eliminated_week"] is not None
        and row["elimination_source"] not in (None, "picks")
        else None
    )
    candidates = [w for w in (derived_death, stored_death) if w is not None]
    final_week = min(candidates) if candidates else None
    if final_week is None:
        status, source = "alive", None
    elif stored_death is not None and final_week == stored_death:
        # The earliest death is the stored decision (min() already proved it
        # is no later than any derived one) — it keeps its source.
        status, source = "ghost", row["elimination_source"]
    else:
        status, source = "ghost", "picks"
    strikes = len(
        [w for w in losing_weeks if final_week is None or w <= final_week]
    )
    conn.execute(
        "UPDATE survivor_players SET status = ?, strikes_used = ?, "
        "eliminated_week = ?, elimination_source = ? "
        "WHERE season_id = ? AND user_id = ?",
        (status, strikes, final_week, source, season["id"], user_id),
    )


# ── grading ────────────────────────────────────────────────────────────


def apply_game_result(
    conn: sqlite3.Connection, season: dict, game_id: str, report: SettleReport
) -> None:
    """Grade every pick on one game per its current stored state, recomputing
    any player whose result changed. Safe to call any number of times."""
    game = conn.execute(
        "SELECT week, status, winner, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND game_id = ?",
        (season["season_year"], game_id),
    ).fetchone()
    if game is None:
        return
    picks = conn.execute(
        "SELECT user_id, slot, team, result FROM survivor_picks "
        "WHERE season_id = ? AND game_id = ?",
        (season["id"], game_id),
    ).fetchall()
    changed: set[int] = set()
    for pick in picks:
        exp = expected_result(pick["team"], game["status"], game["winner"])
        if exp is None or pick["result"] == exp:
            continue
        conn.execute(
            "UPDATE survivor_picks SET result = ?, "
            "locked_at = COALESCE(locked_at, ?) "
            "WHERE season_id = ? AND user_id = ? AND game_id = ? AND slot = ?",
            (
                exp, game["kickoff_utc"],
                season["id"], pick["user_id"], game_id, pick["slot"],
            ),
        )
        changed.add(int(pick["user_id"]))
        report.graded_weeks.add(int(game["week"]))
        if exp == "void":
            report.voided += 1
        else:
            report.graded += 1
    for user_id in changed:
        recompute_player(conn, season, user_id)
        report.recomputed.append(user_id)


# ── the sweep ──────────────────────────────────────────────────────────


def run_settle(conn: sqlite3.Connection, season: dict, now: float) -> SettleReport:
    """One full settle pass for one season. Idempotent — a no-news sweep is
    a no-op (§6.7). Callers commit."""
    report = SettleReport()
    year = season["season_year"]

    # 0. Season goes 'active' at the first kickoff (§2.2's copy flip).
    if season["status"] == "enrolling":
        first = conn.execute(
            "SELECT MIN(kickoff_utc) AS k FROM nfl_games WHERE season_year = ?",
            (year,),
        ).fetchone()
        if first and first["k"] and kickoff_ts(first["k"]) <= now:
            set_season_status(conn, season["id"], "active")

    # 1. Grade picks whose games have news (final-with-winner or postponed).
    rows = conn.execute(
        "SELECT DISTINCT p.game_id FROM survivor_picks p "
        "JOIN nfl_games g ON g.season_year = ? AND g.game_id = p.game_id "
        "WHERE p.season_id = ? AND p.result IS NULL "
        "AND (g.status = 'postponed' OR (g.status = 'final' AND g.winner IS NOT NULL))",
        (year, season["id"]),
    ).fetchall()
    for r in rows:
        apply_game_result(conn, season, r["game_id"], report)

    # 2. Stamp locked_at on kicked, still-ungraded picks (audit trail).
    cur = conn.execute(
        "UPDATE survivor_picks SET locked_at = ("
        "  SELECT g.kickoff_utc FROM nfl_games g "
        "  WHERE g.season_year = ? AND g.game_id = survivor_picks.game_id) "
        "WHERE season_id = ? AND locked_at IS NULL AND result IS NULL "
        "AND EXISTS (SELECT 1 FROM nfl_games g WHERE g.season_year = ? "
        "  AND g.game_id = survivor_picks.game_id "
        "  AND g.status IN ('in', 'final'))",
        (year, season["id"], year),
    )
    report.locked = cur.rowcount or 0

    # 3. The groundskeeper (§1.2): auto-assign at each week's final kickoff.
    for week in _weeks_at_final_kickoff(conn, year, now):
        _auto_assign_week(conn, season, week, now, report)
    return report


def _weeks_at_final_kickoff(
    conn: sqlite3.Connection, year: int, now: float
) -> list[int]:
    """Weeks whose final kickoff (postponed games excluded) has passed but
    whose final game(s) aren't final yet — the auto-assign window. Once the
    last game goes final the window is over: too late to assign anything
    without known-result information, so the pickless simply survive."""
    rows = conn.execute(
        "SELECT week, status, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND status != 'postponed'",
        (year,),
    ).fetchall()
    by_week: dict[int, list] = {}
    for r in rows:
        by_week.setdefault(int(r["week"]), []).append(r)
    out = []
    for week, games in by_week.items():
        final_kick = max(kickoff_ts(g["kickoff_utc"]) for g in games)
        if not final_kick <= now <= final_kick + ASSIGN_GRACE_SECONDS:
            continue
        closers = [
            g for g in games if kickoff_ts(g["kickoff_utc"]) == final_kick
        ]
        if any(g["status"] != "final" for g in closers):
            out.append(week)
    return sorted(out)


def _auto_assign_week(
    conn: sqlite3.Connection,
    season: dict,
    week: int,
    now: float,
    report: SettleReport,
) -> None:
    games = conn.execute(
        "SELECT game_id, home, away, kickoff_utc, status, favorite, favorite_prob "
        "FROM nfl_games WHERE season_year = ? AND week = ? AND status != 'postponed'",
        (season["season_year"], week),
    ).fetchall()
    if not games:
        return
    final_kick = max(kickoff_ts(g["kickoff_utc"]) for g in games)
    final_kick_iso = datetime.fromtimestamp(final_kick, timezone.utc).isoformat()
    # The assignment pool: sides of the closing game(s) only — every earlier
    # game is under way or done, and assigning from those would be a pick
    # made with the result known (§1.2 as amended).
    pool: list[tuple[str, str, float]] = []  # (team, game_id, prob)
    for g in games:
        if kickoff_ts(g["kickoff_utc"]) != final_kick or g["status"] == "final":
            continue
        for team in (g["home"], g["away"]):
            prob = (
                float(g["favorite_prob"])
                if g["favorite"] == team and g["favorite_prob"] is not None
                else 0.0
            )
            pool.append((team, g["game_id"], prob))
    # Highest win probability first; abbr breaks ties deterministically.
    pool.sort(key=lambda t: (-t[2], t[0]))

    # The pickless: alive, entered before the assignment moment, no slot-1
    # pick this week. Ghosts are never covered (§1.7 as decided).
    pickless = conn.execute(
        "SELECT p.user_id FROM survivor_players p "
        "WHERE p.season_id = ? AND p.status = 'alive' AND p.joined_at <= ? "
        "AND NOT EXISTS (SELECT 1 FROM survivor_picks k WHERE "
        "  k.season_id = p.season_id AND k.user_id = p.user_id "
        "  AND k.week = ? AND k.slot = 1)",
        (season["id"], final_kick_iso, week),
    ).fetchall()
    if not pickless:
        return
    if season["config"]["missed_pick"] == "eliminate":
        # The harsher ruleset: no groundskeeper at all — pickless at the
        # final kickoff is an elimination (source 'missed', a decision that
        # survives recomputation like the cap's).
        for row in pickless:
            user_id = int(row["user_id"])
            eliminate_player(
                conn, season["id"], user_id, week, source="missed"
            )
            report.cap_eliminated.append(user_id)
        return
    max_assigns = int(season["config"]["max_auto_assigns"])
    for row in pickless:
        user_id = int(row["user_id"])
        used = conn.execute(
            "SELECT COUNT(DISTINCT week) FROM survivor_picks "
            "WHERE season_id = ? AND user_id = ? AND auto_assigned = 1",
            (season["id"], user_id),
        ).fetchone()[0]
        if int(used) >= max_assigns:
            # The fourth time the groundskeeper is needed, he declines (§1.2).
            eliminate_player(conn, season["id"], user_id, week, source="cap")
            report.cap_eliminated.append(user_id)
            continue
        burned = burned_teams(conn, season["id"], user_id)
        choice = next(
            ((team, gid) for team, gid, _ in pool if team not in burned), None
        )
        if choice is None:
            # Edge #13: every legal side of the closing game is burned —
            # the week is voided for them (no row, no cap charge, survive).
            report.no_legal_team.append(user_id)
            continue
        team, game_id = choice
        conn.execute(
            "INSERT INTO survivor_picks "
            "(season_id, guild_id, user_id, week, slot, team, game_id,"
            " auto_assigned, locked_at) VALUES (?, ?, ?, ?, 1, ?, ?, 1, ?)",
            (season["id"], season["guild_id"], user_id, week, team, game_id,
             final_kick_iso),
        )
        report.auto_assigned.append((user_id, team))


# ── manual settle (the panel's escape hatch) ───────────────────────────


def manual_settle(
    conn: sqlite3.Connection,
    season_year: int,
    game_id: str,
    outcome: str,
    live_seasons: list[dict],
) -> dict:
    """Record a result by hand and re-grade every live season that shares the
    schedule. ``outcome`` is a team abbr, 'TIE', or 'VOID' (postpones the
    game — picks void, teams return). Overwriting an existing winner is the
    correction path: grading is derived, so strikes and deaths follow.
    Returns {old_winner, old_status, reports: {season_id: SettleReport}}.
    """
    game = conn.execute(
        "SELECT status, winner, home, away FROM nfl_games "
        "WHERE season_year = ? AND game_id = ?",
        (season_year, game_id),
    ).fetchone()
    if game is None:
        raise ValueError("No such game.")
    if outcome not in ("TIE", "VOID", game["home"], game["away"]):
        raise ValueError(
            f"Outcome must be {game['home']}, {game['away']}, TIE, or VOID."
        )
    old = {"old_winner": game["winner"], "old_status": game["status"]}
    # result_source='manual' is the marker the feed refuses to cross
    # (survivor_espn.ingest_games) — without it the next poll would flip a
    # VOID back to the feed's status and re-arm the winner guard.
    if outcome == "VOID":
        conn.execute(
            "UPDATE nfl_games SET status = 'postponed', winner = NULL, "
            "result_source = 'manual' WHERE season_year = ? AND game_id = ?",
            (season_year, game_id),
        )
    else:
        conn.execute(
            "UPDATE nfl_games SET status = 'final', winner = ?, "
            "result_source = 'manual' WHERE season_year = ? AND game_id = ?",
            (outcome, season_year, game_id),
        )
    reports: dict[int, SettleReport] = {}
    for season in live_seasons:
        if season["season_year"] != season_year:
            continue
        report = SettleReport()
        apply_game_result(conn, season, game_id, report)
        reports[season["id"]] = report
    return {**old, "reports": reports}
