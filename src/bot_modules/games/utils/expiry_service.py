"""Archive the party games the 24-hour sweep reaps — and pay the ones that played.

A game's payout rides on ``end_game(bot=..., player_ids=...)``, which only a
game's own completion site passes. Hosts routinely never reach that site: every
Truth or Dare game in the guild's history (18 of 18) was left open and reaped by
this sweep instead, so every roster went unpaid. So the sweep archives with the
game's real payload and roster rather than the bare ``end_game`` it used before.

Games with no joined roster — ffa banner posts, photo challenges — resolve to an
empty roster and still pay nobody. That is correct rather than a gap: they are
posts, not games players sign into, so there is no set of players to credit. An
abandoned lobby nobody joined lands on the same empty roster, which is the whole
anti-farm guard — leaving a game open all day earns exactly what it played.
"""
from __future__ import annotations

import json
import logging

from bot_modules.games.utils.game_manager import end_game

log = logging.getLogger(__name__)

# game_type -> (payload key holding the joined roster, payload key counting rounds)
#
# Only games a player explicitly joins belong here. Adding a type is what makes
# the sweep pay it, so an unlisted type keeps the historical payout-free
# behaviour by default rather than paying a roster we guessed at.
_ROSTER_SPECS: dict[str, tuple[str, str]] = {
    "traditional": ("participants", "asked"),
}


def expired_game_archive(game_type: str, payload: dict | None) -> tuple[list[int], int]:
    """Return ``(player_ids, round_count)`` to archive an expired game with.

    Ids survive a JSON round-trip as strings and a payload can carry junk, so
    coerce and skip rather than raising — one malformed entry must not cost the
    rest of the room its payout. Duplicates collapse so a double-join can't be
    paid twice.
    """
    spec = _ROSTER_SPECS.get(game_type)
    if spec is None:
        return [], 0
    roster_key, round_key = spec
    payload = payload or {}

    roster: list[int] = []
    for raw in payload.get(roster_key) or []:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in roster:
            roster.append(uid)

    rounds = payload.get(round_key) or {}
    return roster, len(rounds)


async def sweep_expired_games(bot, db, *, max_age_hours: int = 24) -> int:
    """End every active game older than *max_age_hours*; return how many ended."""
    rows = await db.fetchall(
        "SELECT game_id, channel_id, game_type, payload FROM games_active_games "
        "WHERE created_at <= datetime('now', ?)",
        (f"-{int(max_age_hours)} hours",),
    )

    ended = 0
    for row in rows:
        game_id = row["game_id"]
        try:
            try:
                payload = json.loads(row["payload"]) if row["payload"] else {}
            except (TypeError, ValueError):
                # A corrupt payload costs this game its roster, not the sweep.
                log.warning("Unreadable payload on expiring game %s", game_id)
                payload = {}
            players, rounds = expired_game_archive(row["game_type"], payload)

            # bot= is what lets end_game both pay the roster and resolve the
            # guild for the history row; the bare call left guild_id = 0.
            await end_game(
                db, game_id,
                player_count=len(players), round_count=rounds, payload=payload,
                bot=bot, player_ids=players,
            )

            if row["game_type"] == "ama":
                ama_cog = bot.get_cog("AMACog")
                if ama_cog and hasattr(ama_cog, "cleanup_ended_game"):
                    await ama_cog.cleanup_ended_game(row["channel_id"], game_id)
            bot.active_views.pop(game_id, None)
            ended += 1
            log.info("Auto-expired game %s (%dh limit)", game_id, max_age_hours)
        except Exception:
            log.exception("Game cleanup failed for %s", game_id)
    return ended
