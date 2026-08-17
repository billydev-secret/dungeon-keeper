"""ESPN scoreboard parsing + schedule ingest for Survivor (spec §4).

The data source is ESPN's unofficial, unversioned scoreboard endpoint, so the
parser is deliberately paranoid: a malformed event is skipped and counted,
never raised, and every settle path must also work through ``admin settle``.
Tests run on saved JSON under ``tests/fixtures/espn/`` — the suite never
touches the network; only this module's ``fetch_scoreboard`` does, at runtime.

Field notes from the captured fixtures (2026-08-17):

- The season-year selector is ``dates=``, NOT ``year=`` — ``year`` is
  silently ignored and hands back the current season.
- Kickoffs come as minute-precision Zulu (``2026-09-10T00:20Z``).
- **Completed games carry no odds at all**, which is why ``nfl_games``
  freezes favorite/favorite_prob at the last pre-kickoff poll: closing odds
  are unrecoverable after the fact, and the gauntlet replay (§4.2) depends
  on the frozen copy.
- Odds ride the first provider entry (Draft Kings). The favorite comes from
  ``homeTeamOdds/awayTeamOdds.favorite``; its probability is the vig-stripped
  two-way implied probability from the moneyline closes.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("dungeonkeeper.survivor")

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?week={week}&seasontype={seasontype}&dates={year}"
)

# ESPN status.type.name → our nfl_games.status vocabulary. Anything not
# listed (halftime, end-period, delayed, new statuses ESPN invents) maps to
# 'in': the game has kicked off and isn't settled, which is all the lock and
# settle logic ever needs to know.
_STATUS_MAP = {
    "STATUS_SCHEDULED": "scheduled",
    "STATUS_FINAL": "final",
    "STATUS_POSTPONED": "postponed",
    "STATUS_CANCELED": "postponed",
    "STATUS_CANCELLED": "postponed",
}


@dataclass(frozen=True)
class ParsedGame:
    game_id: str
    week: int
    home: str
    away: str
    kickoff_utc: str            # full ISO 8601, UTC
    status: str                 # scheduled|in|final|postponed
    favorite: str | None        # abbr; None when odds absent/unreadable
    favorite_prob: float | None
    winner: str | None          # abbr | 'TIE' | None


def parse_scoreboard(payload: dict) -> tuple[list[ParsedGame], int]:
    """Parse a scoreboard payload into games plus a skipped-event count.

    Defensive by design: an event missing anything essential is skipped and
    counted, so one malformed entry can't take down a poll. The caller logs
    a nonzero skip count; the fixtures pin that good payloads skip zero.
    """
    games: list[ParsedGame] = []
    skipped = 0
    for event in payload.get("events") or []:
        try:
            parsed = _parse_event(event)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            skipped += 1
            log.warning(
                "survivor espn: skipping malformed event %s (%s)",
                (event or {}).get("id", "?"), exc,
            )
            continue
        games.append(parsed)
    return games, skipped


def _parse_event(event: dict) -> ParsedGame:
    comp = event["competitions"][0]
    status = _STATUS_MAP.get(comp["status"]["type"]["name"], "in")

    home_abbr = away_abbr = None
    home_entry = away_entry = None
    for entry in comp["competitors"]:
        abbr = entry["team"]["abbreviation"]
        if entry.get("homeAway") == "home":
            home_abbr, home_entry = abbr, entry
        elif entry.get("homeAway") == "away":
            away_abbr, away_entry = abbr, entry
    if not home_abbr or not away_abbr:
        raise ValueError("missing home/away competitor")

    favorite, favorite_prob = _parse_favorite(comp, home_abbr, away_abbr)

    winner = None
    if status == "final":
        winner = _parse_winner(home_entry, away_entry, home_abbr, away_abbr)

    return ParsedGame(
        game_id=str(event["id"]),
        week=int(event["week"]["number"]),
        home=home_abbr,
        away=away_abbr,
        kickoff_utc=_parse_kickoff(event["date"]),
        status=status,
        favorite=favorite,
        favorite_prob=favorite_prob,
        winner=winner,
    )


def _parse_kickoff(raw: str) -> str:
    """Normalize ESPN's minute-precision Zulu stamp to full ISO UTC."""
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_favorite(
    comp: dict, home_abbr: str, away_abbr: str
) -> tuple[str | None, float | None]:
    """Favorite abbr + vig-stripped implied win probability, or (None, None).

    Odds are best-effort everywhere: completed games have none, and a week
    where ESPN never published a line leaves the columns NULL — auto-assign
    falls back to best record (spec §1.2) and the gauntlet treats it like a
    void week (§6.9's spirit) rather than inventing a coin flip.
    """
    odds_list = comp.get("odds") or []
    if not odds_list:
        return None, None
    odds = odds_list[0]

    if (odds.get("homeTeamOdds") or {}).get("favorite"):
        fav_abbr, fav_side, dog_side = home_abbr, "home", "away"
    elif (odds.get("awayTeamOdds") or {}).get("favorite"):
        fav_abbr, fav_side, dog_side = away_abbr, "away", "home"
    else:
        return None, None

    moneyline = odds.get("moneyline") or {}
    fav_q = _implied(_close_odds(moneyline, fav_side))
    dog_q = _implied(_close_odds(moneyline, dog_side))
    if fav_q is None or dog_q is None or fav_q + dog_q <= 0:
        # A favorite with no readable moneyline still beats nothing: keep the
        # abbr with a null prob so "who was chalk" survives even when "by how
        # much" didn't.
        return fav_abbr, None
    return fav_abbr, round(fav_q / (fav_q + dog_q), 4)


def _close_odds(moneyline: dict, side: str) -> str | None:
    return ((moneyline.get(side) or {}).get("close") or {}).get("odds")


def _implied(american: str | None) -> float | None:
    """American moneyline → raw implied probability (vig included)."""
    if not american:
        return None
    try:
        ml = float(str(american).replace("+", ""))
    except ValueError:
        return None
    if ml == 0:
        return None
    if ml < 0:
        return -ml / (-ml + 100.0)
    return 100.0 / (ml + 100.0)


def _parse_winner(
    home_entry: dict | None,
    away_entry: dict | None,
    home_abbr: str,
    away_abbr: str,
) -> str | None:
    """Winner of a final: ESPN's flag first, scores as the tiebreak.

    Ties (§1.3) have no ``winner`` flag on either side — equal scores make
    it 'TIE'. A final with no flag and unreadable scores returns None:
    unsettled, flagged at the Reckoning, and `admin settle`'s job.
    """
    for entry, abbr in ((home_entry, home_abbr), (away_entry, away_abbr)):
        if entry is not None and entry.get("winner"):
            return abbr
    try:
        home_score = int((home_entry or {}).get("score"))  # type: ignore[arg-type]
        away_score = int((away_entry or {}).get("score"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if home_score == away_score:
        return "TIE"
    return home_abbr if home_score > away_score else away_abbr


# ── ingest ─────────────────────────────────────────────────────────────


def ingest_games(
    conn: sqlite3.Connection, season_year: int, games: list[ParsedGame]
) -> dict[str, int]:
    """Upsert parsed games into ``nfl_games``. Idempotent; returns counts.

    Refresh rules (spec §4.2 / §6.2):

    - ``kickoff_utc``, ``week`` and ``status`` always track the feed — flex
      scheduling moves kickoffs, and locks validate against current times.
    - ``favorite``/``favorite_prob`` update **only while the game is still
      scheduled**, so the stored value is naturally frozen at the last
      pre-kickoff poll — the gauntlet's determinism guarantee. Post-kickoff
      polls (where ESPN drops odds entirely) can never null them out.
    - ``winner`` is set once, when a final arrives with one, and an existing
      winner is never overwritten: a manual ``admin settle`` outranks the
      feed, and correcting it is another settle, not a poll.
    """
    counts = {"inserted": 0, "updated": 0}
    for game in games:
        row = conn.execute(
            "SELECT status, favorite, winner FROM nfl_games "
            "WHERE season_year = ? AND game_id = ?",
            (season_year, game.game_id),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO nfl_games (season_year, week, game_id, home, away,"
                " kickoff_utc, status, favorite, favorite_prob, winner)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    season_year, game.week, game.game_id, game.home, game.away,
                    game.kickoff_utc, game.status,
                    game.favorite if game.status == "scheduled" else None,
                    game.favorite_prob if game.status == "scheduled" else None,
                    game.winner,
                ),
            )
            counts["inserted"] += 1
            continue

        sets = ["week = ?", "kickoff_utc = ?", "status = ?"]
        params: list[object] = [game.week, game.kickoff_utc, game.status]
        if game.status == "scheduled" and game.favorite is not None:
            sets += ["favorite = ?", "favorite_prob = ?"]
            params += [game.favorite, game.favorite_prob]
        if game.winner is not None and row["winner"] is None:
            sets.append("winner = ?")
            params.append(game.winner)
        params += [season_year, game.game_id]
        conn.execute(
            f"UPDATE nfl_games SET {', '.join(sets)} "
            "WHERE season_year = ? AND game_id = ?",
            params,
        )
        counts["updated"] += 1
    return counts


async def fetch_scoreboard(
    session, week: int, season_year: int, *, seasontype: int = 2
) -> dict:
    """Fetch one week's scoreboard. The only network path in the module —
    everything else runs on stored JSON, in prod and in tests alike."""
    url = SCOREBOARD_URL.format(week=week, seasontype=seasontype, year=season_year)
    async with session.get(url, timeout=30) as resp:
        resp.raise_for_status()
        return await resp.json()


REGULAR_SEASON_WEEKS = range(1, 19)


async def fetch_season(
    session, season_year: int
) -> tuple[list[ParsedGame], int, list[int]]:
    """Fetch and parse the full regular season, week by week.

    Returns (games, skipped_events, failed_weeks). A week whose fetch dies is
    recorded and skipped rather than aborting the sweep — create-season runs
    this once (spec §4.2) and the daily refresh (stage 4) heals any gaps, so
    partial success beats none. Network and DB stay separated: the caller
    ingests the returned games on its own connection.
    """
    games: list[ParsedGame] = []
    skipped = 0
    failed_weeks: list[int] = []
    for week in REGULAR_SEASON_WEEKS:
        try:
            payload = await fetch_scoreboard(session, week, season_year)
        except Exception as exc:  # noqa: BLE001 — unversioned API, fail soft
            log.warning("survivor espn: week %s fetch failed (%s)", week, exc)
            failed_weeks.append(week)
            continue
        week_games, week_skipped = parse_scoreboard(payload)
        games.extend(week_games)
        skipped += week_skipped
    return games, skipped, failed_weeks
