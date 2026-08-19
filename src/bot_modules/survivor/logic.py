"""Survivor pick/join/lock logic — stage 3 of docs/plans/survivor.md.

Pure functions over a caller-owned connection and an injected ``now``; the
cog, views, and routes are glue on top. The rules implemented here are spec
§1–§2: per-game locking (a pick locks at *that game's* kickoff, not the
week's), no team twice per season (voids return the team, §1.3), byes and
kicked-off games filtered from the legal set *and* validated server-side
(§6.4), and the Thursday trap (§6.1) falling out of per-game locks naturally.

Money moves only through economy_service with survivor-owned ledger kinds
(spec §5.1) — never a bare wallet UPDATE.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from bot_modules.services import economy_service
from bot_modules.services.survivor_service import add_player

# Ledger kinds (spec §5.1). The economy metrics group by kind, so the
# feature's whole cash flow is visible as its own lines, never as
# unexplained mint.
KIND_BUYIN = "survivor_buyin"
KIND_GAUNTLET_FEE = "survivor_gauntlet_fee"
KIND_WEEKLY_WIN = "survivor_weekly_win"

# ESPN abbreviations by conference, for the dual-select pick panel
# (Discord's 25-option cap vs 32 teams, spec §2.4). The full set is pinned
# against the captured week-1 fixture in tests — if ESPN renames an
# abbreviation, the fixture refresh will catch it here.
AFC_TEAMS = frozenset({
    "BUF", "MIA", "NE", "NYJ",          # East
    "BAL", "CIN", "CLE", "PIT",         # North
    "HOU", "IND", "JAX", "TEN",         # South
    "DEN", "KC", "LAC", "LV",           # West
})
NFC_TEAMS = frozenset({
    "DAL", "NYG", "PHI", "WSH",
    "CHI", "DET", "GB", "MIN",
    "ATL", "CAR", "NO", "TB",
    "ARI", "LAR", "SEA", "SF",
})
ALL_TEAMS = AFC_TEAMS | NFC_TEAMS

# Satchel wealth signal (§2.4): ambient scarcity awareness, never advice.
SATCHEL_LOW_WATER = 12


class PickError(ValueError):
    """A pick/join rule refused the action (message is member-presentable)."""


class GauntletPendingError(PickError):
    """Late entry requires the gauntlet replay, which is stage 5b. The door
    stays open in spirit — the error copy says the road is coming."""


@dataclass(frozen=True)
class OpenGame:
    """One not-yet-kicked-off side a player could still pick."""

    team: str
    opponent: str
    is_home: bool
    game_id: str
    week: int
    kickoff_ts: float


def kickoff_ts(kickoff_utc: str) -> float:
    """Epoch seconds for a stored ISO kickoff."""
    dt = datetime.fromisoformat(kickoff_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── weeks ──────────────────────────────────────────────────────────────


def pick_week(conn: sqlite3.Connection, season_year: int, now: float) -> int | None:
    """The week members are currently picking for: the earliest week that
    still has an open (scheduled, future-kickoff) game. None once the season
    has no open games left."""
    rows = conn.execute(
        "SELECT week, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND status = 'scheduled' ORDER BY week",
        (season_year,),
    ).fetchall()
    open_weeks = [
        int(r["week"]) for r in rows if kickoff_ts(r["kickoff_utc"]) > now
    ]
    return min(open_weeks) if open_weeks else None


def elapsed_weeks(conn: sqlite3.Connection, season_year: int, now: float) -> list[int]:
    """Weeks whose every game has kicked off — the gauntlet-replayed ones
    (§1.9). A week with any open game is picked live instead, so it is not
    elapsed even if half its games are done (mid-week join, §1.9)."""
    rows = conn.execute(
        "SELECT week, status, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? ORDER BY week",
        (season_year,),
    ).fetchall()
    weeks: dict[int, bool] = {}
    for r in rows:
        week = int(r["week"])
        is_open = r["status"] == "scheduled" and kickoff_ts(r["kickoff_utc"]) > now
        weeks[week] = weeks.get(week, True) and not is_open
    return sorted(w for w, fully_kicked in weeks.items() if fully_kicked)


# ── the satchel ────────────────────────────────────────────────────────


def burned_teams(conn: sqlite3.Connection, season_id: int, user_id: int) -> set[str]:
    """Teams this player can never pick again: every pick except voided ones
    (§1.3 — a postponed game returns the team to the pool)."""
    rows = conn.execute(
        "SELECT team FROM survivor_picks "
        "WHERE season_id = ? AND user_id = ? "
        "AND (result IS NULL OR result != 'void')",
        (season_id, user_id),
    ).fetchall()
    return {r["team"] for r in rows}


def satchel(conn: sqlite3.Connection, season_id: int, user_id: int) -> set[str]:
    """Teams still available to this player across the rest of the season."""
    return set(ALL_TEAMS) - burned_teams(conn, season_id, user_id)


def legal_teams(
    conn: sqlite3.Connection,
    season: dict,
    user_id: int,
    week: int,
    now: float,
    *,
    slot: int = 1,
) -> list[OpenGame]:
    """The pickable set: unburned ∩ playing this week ∩ not yet kicked off
    (§2.4). Byes fall out naturally — a team not playing has no game row.
    In a double-pick week the other slot's team is excluded too (no pairing
    a team with itself)."""
    burned = burned_teams(conn, season["id"], user_id)
    other_slot = conn.execute(
        "SELECT team, game_id FROM survivor_picks "
        "WHERE season_id = ? AND user_id = ? AND week = ? AND slot != ?",
        (season["id"], user_id, week, slot),
    ).fetchone()
    # The other slot excludes its WHOLE game, not just its team — picking
    # both sides of one matchup would be a deterministic hedge buying
    # exactly one strike (stage-4 review).
    excluded = burned | ({other_slot["team"]} if other_slot else set())
    excluded_games = {other_slot["game_id"]} if other_slot else set()

    rows = conn.execute(
        "SELECT game_id, week, home, away, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND week = ? AND status = 'scheduled'",
        (season["season_year"], week),
    ).fetchall()
    out: list[OpenGame] = []
    for r in rows:
        if r["game_id"] in excluded_games:
            continue
        ts = kickoff_ts(r["kickoff_utc"])
        if ts <= now:
            continue
        for team, opponent, is_home in (
            (r["home"], r["away"], True),
            (r["away"], r["home"], False),
        ):
            if team not in excluded:
                out.append(OpenGame(
                    team=team, opponent=opponent, is_home=is_home,
                    game_id=r["game_id"], week=int(r["week"]), kickoff_ts=ts,
                ))
    out.sort(key=lambda g: (g.kickoff_ts, g.team))
    return out


# ── picking ────────────────────────────────────────────────────────────


def get_pick(
    conn: sqlite3.Connection, season_id: int, user_id: int, week: int, slot: int = 1
) -> dict | None:
    row = conn.execute(
        "SELECT team, game_id, auto_assigned, result FROM survivor_picks "
        "WHERE season_id = ? AND user_id = ? AND week = ? AND slot = ?",
        (season_id, user_id, week, slot),
    ).fetchone()
    if row is None:
        return None
    return {
        "team": row["team"],
        "game_id": row["game_id"],
        "auto_assigned": bool(row["auto_assigned"]),
        "result": row["result"],
    }


def place_pick(
    conn: sqlite3.Connection,
    season: dict,
    user_id: int,
    week: int,
    team: str,
    now: float,
    *,
    slot: int = 1,
) -> OpenGame:
    """Place or change a pick, enforcing every §1.2 guard. Returns the game
    picked, for the confirmation embed. Raises PickError with the reason."""
    if season["status"] == "complete":
        raise PickError("This season is over.")

    player = conn.execute(
        "SELECT status FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()
    if player is None:
        raise PickError("You're not in this season — use the Join button on the season post.")
    if player["status"] == "ghost" and not season["config"]["ghost_streak"]:
        raise PickError("Ghost picks are disabled this season.")

    current = pick_week(conn, season["season_year"], now)
    if current is None:
        raise PickError("No games left to pick this season.")
    if week != current:
        raise PickError(f"Picks are open for week {current}, not week {week}.")

    existing = get_pick(conn, season["id"], user_id, week, slot)
    if existing is not None:
        if existing["result"] is not None and existing["result"] != "void":
            # A settled result is the record — re-picking must never erase a
            # graded loss before the Reckoning. Only 'void' frees the slot
            # (§1.3: a postponement returns the team and the week).
            raise PickError(
                f"Your {existing['team']} pick is already settled — "
                "the record stands."
            )
        if existing["result"] is None:
            game = conn.execute(
                "SELECT kickoff_utc FROM nfl_games "
                "WHERE season_year = ? AND game_id = ?",
                (season["season_year"], existing["game_id"]),
            ).fetchone()
            if game is not None and kickoff_ts(game["kickoff_utc"]) <= now:
                raise PickError(
                    f"Your {existing['team']} pick locked at kickoff — "
                    "you ride it now."
                )

    legal = {g.team: g for g in legal_teams(
        conn, season, user_id, week, now, slot=slot
    )}
    choice = legal.get(team)
    if choice is None:
        # One message per §1.2 reason, so the refusal teaches the rule.
        if team not in ALL_TEAMS:
            raise PickError(f"Never heard of {team!r}.")
        if team in burned_teams(conn, season["id"], user_id):
            raise PickError(f"{team} is already used — one team per season.")
        raise PickError(
            f"{team} isn't available this week — bye, already kicked off, "
            "or your other slot."
        )

    conn.execute(
        "INSERT INTO survivor_picks "
        "(season_id, guild_id, user_id, week, slot, team, game_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(season_id, user_id, week, slot) DO UPDATE SET "
        "team = excluded.team, game_id = excluded.game_id, "
        "auto_assigned = 0, locked_at = NULL, result = NULL",
        (season["id"], season["guild_id"], user_id, week, slot, team,
         choice.game_id),
    )
    return choice


# ── joining ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JoinResult:
    charged: int


def join_season(
    conn: sqlite3.Connection, season: dict, user_id: int, now: float
) -> JoinResult:
    """Enroll a member (spec §1.1/§2.2): one entry per person, buy-in debited
    through the economy service in the same transaction as the entry row.

    Late entry (any fully-elapsed week) is the gauntlet's job — stage 5b.
    Until it ships this raises :class:`GauntletPendingError` rather than
    silently skipping the replay, the fee, and the inherited fate.
    """
    if season["status"] == "complete":
        raise PickError("This season is over.")
    config = season["config"]

    elapsed = elapsed_weeks(conn, season["season_year"], now)
    if elapsed:
        if config["late_entry"] == "closed":
            raise PickError("Enrollment closed at Week 1 kickoff this season.")
        raise GauntletPendingError(
            "The season is underway — late entry runs through the Gauntlet. "
            "Use the Join button on the season post."
        )

    already = conn.execute(
        "SELECT 1 FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()
    if already is not None:
        raise PickError("One entry per person — and you're already in.")

    buyin = int(config["buyin_coins"])
    if buyin > 0:
        ok = economy_service.apply_debit(
            conn, season["guild_id"], user_id, buyin, KIND_BUYIN,
            meta={"season_id": season["id"]},
        )
        if not ok:
            raise PickError(
                f"The buy-in is {buyin:,} coins — your balance is short."
            )
    if not add_player(conn, season, user_id, joined_at=now):
        # Race loser: the duplicate SELECT above passed before the winner
        # committed. Raising here rolls this transaction back, debit and
        # all — the caller must be on open_db_immediate (the views are) so
        # two confirms serialize instead of double-charging.
        raise PickError("One entry per person — and you're already in.")
    return JoinResult(charged=buyin)


# ── status & board ─────────────────────────────────────────────────────


def history_rows(
    conn: sqlite3.Connection,
    season: dict,
    user_id: int,
    *,
    through_week: int | None = None,
) -> list[dict]:
    """Pick history enriched with the game each pick rode (2026-08-19,
    Billy's #11 — "put both teams, and a little more information"): the
    opponent, home/away, and the game's winner join in from nfl_games so
    the embed can say who the pick played and how it ended. One helper for
    both faces — the public /survivor history passes ``through_week`` (the
    last reckoned week); the personal panel button passes None for all."""
    sql = (
        "SELECT p.week, p.team, p.result, p.auto_assigned, "
        " g.home, g.away, g.winner, g.status "
        "FROM survivor_picks p "
        "LEFT JOIN nfl_games g ON g.season_year = ? AND g.week = p.week "
        " AND (g.home = p.team OR g.away = p.team) "
        "WHERE p.season_id = ? AND p.user_id = ?"
    )
    params: list[object] = [season["season_year"], season["id"], user_id]
    if through_week is not None:
        sql += " AND p.week <= ?"
        params.append(through_week)
    sql += " ORDER BY p.week, p.slot"
    return [
        {
            "week": int(r["week"]),
            "team": r["team"],
            "result": r["result"],
            "auto_assigned": bool(r["auto_assigned"]),
            "opponent": (
                (r["away"] if r["home"] == r["team"] else r["home"])
                if r["home"] is not None else None
            ),
            "is_home": r["home"] == r["team"] if r["home"] is not None else None,
            "winner": r["winner"],
        }
        for r in conn.execute(sql, params).fetchall()
    ]


def player_status(
    conn: sqlite3.Connection, season: dict, user_id: int, now: float
) -> dict | None:
    """Everything the ephemeral /survivor status needs. None if not entered."""
    player = conn.execute(
        "SELECT status, strikes_used, eliminated_week "
        "FROM survivor_players WHERE season_id = ? AND user_id = ?",
        (season["id"], user_id),
    ).fetchone()
    if player is None:
        return None
    week = pick_week(conn, season["season_year"], now)
    pick = get_pick(conn, season["id"], user_id, week) if week else None
    locked = False
    kickoff = None
    if pick is not None:
        game = conn.execute(
            "SELECT kickoff_utc FROM nfl_games "
            "WHERE season_year = ? AND game_id = ?",
            (season["season_year"], pick["game_id"]),
        ).fetchone()
        if game is not None:
            kickoff = kickoff_ts(game["kickoff_utc"])
            locked = kickoff <= now
    # The just-played, still-ungraded pick (settle lag: MNF unfinished, the
    # Reckoning not yet fired) must not vanish from the card — a last-strike
    # member deserves better than a clean "no pick yet" minutes before the
    # sweep grades their fate.
    pending = conn.execute(
        "SELECT week, team FROM survivor_picks "
        "WHERE season_id = ? AND user_id = ? AND result IS NULL "
        "AND week != COALESCE(?, -1) ORDER BY week DESC LIMIT 1",
        (season["id"], user_id, week),
    ).fetchone()
    bag = satchel(conn, season["id"], user_id)
    streak = None
    if player["status"] == "ghost":
        streak = ghost_streaks(conn, season, now).get(user_id)
    return {
        "status": player["status"],
        "strikes_used": int(player["strikes_used"]),
        "strikes_allowed": int(season["config"]["strikes"]),
        "eliminated_week": player["eliminated_week"],
        "week": week,
        "pick": pick,
        "pick_locked": locked,
        "pick_kickoff_ts": kickoff,
        "pending": (
            {"week": int(pending["week"]), "team": pending["team"]}
            if pending else None
        ),
        "satchel_count": len(bag),
        "satchel_low": len(bag) < SATCHEL_LOW_WATER,
        "streak": streak,
    }


def board_data(conn: sqlite3.Connection, season: dict, now: float) -> dict:
    """The public board (§2.6): alive roster with weeks survived, graveyard
    with week of death, the pots, and the most-burned-teams meta-stat."""
    players = conn.execute(
        "SELECT user_id, status, strikes_used, eliminated_week "
        "FROM survivor_players WHERE season_id = ?",
        (season["id"],),
    ).fetchall()
    survived = {
        int(r["user_id"]): int(r["n"])
        for r in conn.execute(
            "SELECT user_id, COUNT(DISTINCT week) AS n FROM survivor_picks "
            "WHERE season_id = ? AND result IN ('win', 'loss', 'tie') "
            "GROUP BY user_id",
            (season["id"],),
        ).fetchall()
    }
    alive = sorted(
        (
            {
                "user_id": int(r["user_id"]),
                "strikes_used": int(r["strikes_used"]),
                "weeks_survived": survived.get(int(r["user_id"]), 0),
            }
            for r in players
            if r["status"] == "alive"
        ),
        key=lambda p: (-p["weeks_survived"], p["strikes_used"], p["user_id"]),
    )
    graveyard = sorted(
        (
            {
                "user_id": int(r["user_id"]),
                "eliminated_week": r["eliminated_week"],
            }
            for r in players
            if r["status"] == "ghost"
        ),
        key=lambda p: (p["eliminated_week"] or 0, p["user_id"]),
    )
    most_burned = [
        (r["team"], int(r["n"]))
        for r in conn.execute(
            # Settled picks only: counting result-IS-NULL rows would let the
            # public board leak the current week's secret picks (§1.2).
            "SELECT team, COUNT(*) AS n FROM survivor_picks "
            "WHERE season_id = ? AND result IN ('win', 'loss', 'tie') "
            "GROUP BY team ORDER BY n DESC, team LIMIT 5",
            (season["id"],),
        ).fetchall()
    ]
    return {
        "week": pick_week(conn, season["season_year"], now),
        "alive": alive,
        "graveyard": graveyard,
        "pots": pot_totals(conn, season),
        "most_burned": most_burned,
    }


def pot_totals(conn: sqlite3.Connection, season: dict) -> dict[str, int]:
    """Main and ghost pots as they stand (spec §5.1). The seed is booked
    (split by ``ghost_pot_pct``), never minted until payout; buy-ins are
    recycled player coin read back from the ledger. Gauntlet fees join in
    stage 5b via their own kind + pot routing in meta."""
    config = season["config"]
    seed = int(config["pot_seed"])
    ghost_share = seed * int(config["ghost_pot_pct"]) // 100
    pots = {"main": seed - ghost_share, "ghost": ghost_share}
    rows = conn.execute(
        # Season-scoped via json_extract on the meta the debit writers always
        # include. NOT a LIKE: '"season_id": 1' is a substring of
        # '"season_id": 12', so a prefix match would hand season 1 the
        # buy-ins of seasons 10-19. json_valid guard per the register.py
        # precedent — meta is NULL on plenty of rows.
        "SELECT kind, meta, SUM(-amount) AS total FROM econ_ledger "
        "WHERE guild_id = ? AND amount < 0 AND kind IN (?, ?) "
        "AND COALESCE(json_extract("
        "CASE WHEN json_valid(meta) THEN meta ELSE '{}' END, "
        "'$.season_id'), 0) = ? GROUP BY kind, meta",
        (season["guild_id"], KIND_BUYIN, KIND_GAUNTLET_FEE, season["id"]),
    ).fetchall()
    for r in rows:
        pot = "ghost" if (r["meta"] and '"pot": "ghost"' in r["meta"]) else "main"
        pots[pot] += int(r["total"] or 0)
    return pots


# ── ghost streak (§1.7, stage 6a) ─────────────────────────────────────


def ghost_streaks(
    conn: sqlite3.Connection, season: dict, now: float
) -> dict[int, dict]:
    """Per-ghost streak stats: ``{user_id: {current, best}}``.

    A streak is consecutive CORRECT picks in elapsed weeks after death. A
    missed elapsed week breaks it (decided 2026-08-17: the side-pot rewards
    showing up); a void leaves it untouched; an unsettled pick is pending —
    neither extends nor breaks until it grades.
    """
    ghosts = conn.execute(
        "SELECT user_id, eliminated_week FROM survivor_players "
        "WHERE season_id = ? AND status = 'ghost' "
        "AND eliminated_week IS NOT NULL",
        (season["id"],),
    ).fetchall()
    if not ghosts:
        return {}
    elapsed = elapsed_weeks(conn, season["season_year"], now)
    picks = {
        (int(r["user_id"]), int(r["week"])): r["result"]
        for r in conn.execute(
            "SELECT user_id, week, result FROM survivor_picks "
            "WHERE season_id = ? AND slot = 1",
            (season["id"],),
        ).fetchall()
    }
    out: dict[int, dict] = {}
    for g in ghosts:
        user_id = int(g["user_id"])
        death = int(g["eliminated_week"])
        run = best = 0
        for week in elapsed:
            if week <= death:
                continue
            result = picks.get((user_id, week), "MISS")
            if result == "MISS":
                run = 0
            elif result == "win":
                run += 1
                best = max(best, run)
            elif result in ("void", None):
                pass  # untouched
            else:
                run = 0
        out[user_id] = {"current": run, "best": best}
    return out