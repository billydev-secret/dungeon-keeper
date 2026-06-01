"""Pure decision logic for the Session Recap cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog handles fetching session
rows, resolving display names against the guild, and sending the
embed; this module turns raw payload dicts into the strings that go
into the recap.

Three reusable pieces:

* :func:`format_duration` — turns the start/end ISO timestamps into a
  short human duration ("2h 15m" / "12m"), falling back to ``"unknown"``
  on any parse error so a malformed row never crashes the cog.
* :func:`build_game_highlight` — per-game-type recap line. Each game
  has its own "best moment" rule (most divisive WYR question, guiltiest
  NHIE player, etc.) and this function dispatches on ``game_type``.
* :func:`build_highlights` — runs :func:`build_game_highlight` over a
  list of game-history rows and returns the formatted list of strings
  the embed renders as a bulleted block.

The cog passes ``name_lookup`` — a ``{user_id_str: display_name}`` dict
it builds against the live guild — into :func:`build_game_highlight`
so the logic never touches Discord. Unknown ids fall back to the raw
id string so even a missing member still produces a stable line.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot_modules.games.constants import GAME_ICONS, GAME_NAMES


def format_duration(started_at: str, last_game_at: str) -> str:
    """Format the time between two ISO-8601 timestamps as ``"Xh Ym"``.

    Returns ``"unknown"`` if either string fails to parse — the
    games_session_tracker rows are written by the bot itself but a
    malformed row from a manual DB edit shouldn't crash the recap.

    The hour component is dropped when the duration is under an hour,
    so a short session renders as ``"12m"`` rather than ``"0h 12m"``.
    """
    try:
        start_dt = datetime.fromisoformat(started_at)
        end_dt = datetime.fromisoformat(last_game_at)
    except (ValueError, TypeError):
        return "unknown"
    duration = end_dt - start_dt
    total_seconds = int(duration.total_seconds())
    if total_seconds < 0:
        return "unknown"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def build_game_highlight(
    game_type: str,
    payload: dict[str, Any],
    name_lookup: dict[str, str] | None = None,
) -> str:
    """Build the one-line recap for a single completed game.

    Each branch implements the "best moment" rule for that game type:

    * ``wyr``  — the most divisive question (closest A/B vote split)
    * ``nhie`` — the player with the highest guilt score
    * ``ttl``  — the player who fooled the most others
    * ``hottakes`` — the highest-rated take

    Other game types just return the bare ``"<icon> <name>"`` header.
    Unknown game types fall back to the raw key.

    ``name_lookup`` maps stringified user ids to display names; missing
    ids fall back to the id string so an absent member still renders a
    stable line instead of crashing.
    """
    lookup = name_lookup or {}
    icon = GAME_ICONS.get(game_type, "")
    name = GAME_NAMES.get(game_type, game_type)
    highlight = f"**{icon} {name}**"

    if game_type == "wyr":
        rounds = payload.get("rounds", {})
        if rounds:
            most_div = min(
                rounds.values(),
                key=lambda r: abs(len(r.get("a", [])) - len(r.get("b", []))),
            )
            highlight += f": Most divisive — {most_div.get('q', '')[:50]}"

    elif game_type == "nhie":
        guilt_scores = payload.get("guilt_scores", {})
        if guilt_scores:
            guiltiest_id = max(guilt_scores, key=guilt_scores.get)
            display = lookup.get(str(guiltiest_id), str(guiltiest_id))
            highlight += (
                f": Guiltiest — {display} ({guilt_scores[guiltiest_id]} guilty)"
            )

    elif game_type == "ttl":
        scores = payload.get("scores", {})
        if scores:
            best = max(scores.items(), key=lambda x: x[1].get("fooled", 0))
            display = lookup.get(str(best[0]), str(best[0]))
            highlight += f": Best Liar — {display}"

    elif game_type == "hottakes":
        results = payload.get("results", [])
        if results:
            hottest = max(results, key=lambda x: x.get("avg", 0))
            text = hottest.get("text", "")
            avg = hottest.get("avg", 0)
            highlight += f': Hottest — "{text[:40]}..." (avg {avg:.1f}/4)'

    return highlight


def build_highlights(
    game_histories: list[dict[str, Any]],
    name_lookup: dict[str, str] | None = None,
) -> list[str]:
    """Build the full list of highlight strings for the recap embed.

    ``game_histories`` is a list of dicts each with at least
    ``game_type`` and ``payload`` keys. Returns one formatted line per
    game in input order — the cog truncates to a reasonable count
    before stuffing into the embed.
    """
    return [
        build_game_highlight(history["game_type"], history["payload"], name_lookup)
        for history in game_histories
    ]
