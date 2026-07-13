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
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy.logic import local_day_for
from bot_modules.economy.quest_views import post_signoff_card
from bot_modules.services.economy_quests_service import fire_trigger_quests
from bot_modules.services.economy_service import (
    EconSettings,
    award_game_reward,
    load_econ_settings,
    member_is_booster,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# Game types that count as the "duel" quest trigger; every other game_type
# reaching pay_game_rewards is a party game. Mirrors the six PvP cogs that
# call the faucet at their own resolution points.
_DUEL_GAME_TYPES = frozenset(
    {"chicken", "hot_potato", "hot_potato_group", "musical_chairs", "pressure", "quickdraw"}
)


async def pay_game_rewards(
    bot: "Bot",
    guild_id: int,
    participant_ids: Sequence[int | str],
    winner_ids: Sequence[int | str],
    game_type: str,
    *,
    occurrence: str | None = None,
) -> None:
    """Credit participation to every participant and a win bonus to winners.

    No-op unless the guild's economy is enabled. Duplicate ids, bots, and
    non-positive/unresolvable ids are dropped; winners also receive
    participation and are restricted to resolved participants. Per-member
    failures are logged and never propagate.

    Also fires the matching quest trigger ("duel" for the PvP cogs,
    "party_game" otherwise) for every participant. ``occurrence`` is the
    stable per-game id event quests dedupe on; without one, only
    daily/weekly trigger quests fire.
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

        is_duel = game_type in _DUEL_GAME_TYPES
        kind = "duel" if is_duel else "party_game"
        # Namespace with the game type: duel ids are per-type table PKs, so
        # chicken #5 and quickdraw #5 must not share one occurrence key.
        scoped = f"{game_type}:{occurrence}" if occurrence is not None else None
        await _fire_triggers(
            bot, guild, settings, kind, participants, boosters, scoped
        )
        if winners:
            win_kind = "duel_win" if is_duel else "game_win"
            await _fire_triggers(
                bot, guild, settings, win_kind, sorted(winners), boosters, scoped
            )
    except Exception:
        log.exception("pay_game_rewards failed for guild %s (%s)", guild_id, game_type)


async def fire_member_trigger(
    bot: "Bot",
    guild_id: int,
    user_id: int | None,
    trigger_kind: str,
    *,
    occurrence: str | None = None,
) -> None:
    """Fire a quest trigger for one member outside the game-faucet path.

    For modules with per-member actions but no participation payout (Risky
    Roll dares, Guess Who rounds). Same guarantees as ``pay_game_rewards``:
    no-op when the economy is off or the member is a bot/unresolvable, and
    a failure is logged, never raised into the calling game flow.
    """
    try:
        guild = bot.get_guild(guild_id)
        if guild is None or user_id is None:
            return
        member = guild.get_member(int(user_id))
        if member is None or member.bot:
            return

        db_path = bot.ctx.db_path

        def _load():
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id)

        settings = await asyncio.to_thread(_load)
        if not settings.enabled:
            return
        boosters = {member.id: member.premium_since is not None}
        await _fire_triggers(
            bot, guild, settings, trigger_kind, [member.id], boosters, occurrence
        )
    except Exception:
        log.exception(
            "fire_member_trigger failed for guild %s (%s)", guild_id, trigger_kind
        )


async def _fire_triggers(
    bot: "Bot",
    guild: discord.Guild,
    settings: EconSettings,
    trigger_kind: str,
    user_ids: Sequence[int],
    boosters: dict[int, bool],
    occurrence: str | None,
) -> None:
    """Auto-claim active trigger-kind quests for members; silent by design.

    Like the participation faucet, paid claims make no channel noise (a game
    ending with a dozen "quest complete" embeds would drown the recap) — the
    wallet ledger and /quests state carry the news. Sign-off claims do post
    the bank-channel card, since a manager has to be able to act on them.
    """
    db_path = bot.ctx.db_path

    def _fire() -> list[tuple[int, Any]]:
        results: list[tuple[int, Any]] = []
        with open_db(db_path) as conn:
            offset = get_tz_offset_hours(conn, guild.id)
            day = local_day_for(time.time(), offset)
            for uid in user_ids:
                fired = fire_trigger_quests(
                    conn,
                    settings,
                    guild.id,
                    trigger_kind,
                    uid,
                    local_day=day,
                    occurrence=occurrence,
                    booster=boosters.get(uid, False),
                )
                results.extend((uid, outcome) for _quest, outcome in fired)
        return results

    results = await asyncio.to_thread(_fire)

    for uid, outcome in results:
        if outcome.state != "pending":
            continue
        member = guild.get_member(uid)
        if member is None:
            continue
        try:
            accent = await resolve_accent_color(db_path, guild)
            await post_signoff_card(
                bot, bot.ctx, guild, settings, accent, int(outcome.claim_id), member
            )
        except Exception:
            log.exception(
                "sign-off card failed for claim %s (%s)", outcome.claim_id, trigger_kind
            )


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
