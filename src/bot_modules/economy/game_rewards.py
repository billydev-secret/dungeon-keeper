"""Currency payouts for completed games (participation + win rewards).

Stage 1 faucet wiring. Party games pay out at ``game_manager.end_game``; the
six duel cogs pay at their resolution points. Every credit goes through
``economy_service.award_game_reward``; the whole payout is wrapped so a failure
is logged and never propagates — economy must never block game flow.

``resolve_winners`` turns a completed game's payload into the list of winning
user ids, modelled on the "best moment" extraction in
``games_session/logic.py``. Only game types with a genuine per-user winner
return ids; everything else (including wyr, whose "most divisive" is a question,
not a player) returns ``[]``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import (
    award_game_reward,
    load_econ_settings,
    member_is_booster,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)


async def pay_game_rewards(
    bot: "Bot",
    guild_id: int,
    participant_ids: Sequence[int | str],
    winner_ids: Sequence[int | str],
    game_type: str,
) -> None:
    """Credit participation to every participant and a win bonus to winners.

    No-op unless the guild's economy is enabled. Duplicate ids, bots, and
    non-positive/unresolvable ids are dropped; winners also receive
    participation and are restricted to resolved participants. Per-member
    failures are logged and never propagate.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None:
            return

        def _coerce(raw: object) -> int | None:
            try:
                return int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        def _valid(uid: int) -> bool:
            if uid <= 0:
                return False
            member = guild.get_member(uid)
            return member is not None and not member.bot

        coerced = [c for c in (_coerce(u) for u in participant_ids) if c is not None]
        participants = [uid for uid in dict.fromkeys(coerced) if _valid(uid)]
        if not participants:
            return
        allowed = set(participants)
        winners = {c for c in (_coerce(u) for u in winner_ids) if c in allowed}

        db_path = bot.ctx.db_path

        def _load():
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return

        boosters = {uid: member_is_booster(bot, guild_id, uid) for uid in participants}

        def _credit() -> None:
            with open_db(db_path) as conn:
                for uid in participants:
                    booster = boosters[uid]
                    try:
                        award_game_reward(
                            conn, settings, guild_id, uid,
                            kind="game_participation", booster=booster,
                        )
                        if uid in winners:
                            award_game_reward(
                                conn, settings, guild_id, uid,
                                kind="game_win", booster=booster,
                            )
                    except Exception:
                        log.exception(
                            "game reward failed for user %s (%s)", uid, game_type
                        )

        await asyncio.to_thread(_credit)
    except Exception:
        log.exception("pay_game_rewards failed for guild %s (%s)", guild_id, game_type)


# ── Winner extraction ─────────────────────────────────────────────────────────


def _winners_nhie(payload: dict[str, Any]) -> list[int]:
    """Guiltiest player — highest guilt score."""
    scores = payload.get("guilt_scores") or {}
    if not scores:
        return []
    guiltiest = max(scores, key=lambda k: scores[k])
    return [int(guiltiest)]


def _winners_ttl(payload: dict[str, Any]) -> list[int]:
    """Best liar — fooled the most others."""
    scores = payload.get("scores") or {}
    if not scores:
        return []
    best = max(scores.items(), key=lambda kv: (kv[1] or {}).get("fooled", 0))
    return [int(best[0])]


def _winners_hottakes(payload: dict[str, Any]) -> list[int]:
    """Author of the highest-rated take."""
    results = payload.get("results") or []
    if not results:
        return []
    hottest = max(results, key=lambda r: r.get("avg", 0))
    author = hottest.get("author")
    return [int(author)] if author is not None else []


_WINNER_RESOLVERS: dict[str, Callable[[dict[str, Any]], list[int]]] = {
    "nhie": _winners_nhie,
    "ttl": _winners_ttl,
    "hottakes": _winners_hottakes,
}


def resolve_winners(game_type: str, payload: dict) -> list[int]:
    """Winning user ids for a completed game; ``[]`` for types with no winner.

    Table-driven and defensive: an unknown game type or a malformed payload
    yields ``[]`` rather than raising into the game-end path.
    """
    resolver = _WINNER_RESOLVERS.get(game_type)
    if resolver is None:
        return []
    try:
        return resolver(payload or {})
    except Exception:
        log.debug("resolve_winners failed for %s", game_type, exc_info=True)
        return []
