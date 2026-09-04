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

The per-type roster reconstruction lives in ``game_roster``, shared with
``force_end_active_game`` so ``/games end`` and the sweep pay the same room.

When the sweep closes a game that paid, it says so in the channel — one line,
``📦 <Game> archived after 24h — N coins paid to M players.`` — because until
then the payout arrived a day later with nothing in the room to show for it
(platform-21; the reactive boards have no End control, so ~a third of rostered
games end this way). It is a notice, not a recap, and there is deliberately
**no inactivity close** behind it: the host is trusted to end the game and
the 24-hour sweep stays the safety net (games_system_spec.md, Non-goals).

A game whose live view exposes ``close_now`` — AMA — is closed through that
instead (social-prompt-33): the game's own completion site posts its recap
with the payout footer and pays, the way the feature rotation already ended
it, so the sweep's one-line notice is not needed there. If that closer
fails, the game falls through to the archive-and-pay path below rather than
staying open for another hour.
"""
from __future__ import annotations

import json
import logging

import discord

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils.game_manager import end_game
from bot_modules.games.utils.game_roster import roster_from_payload

log = logging.getLogger(__name__)

EXPIRE_REASON = "expired"


def archived_notice(game_type: str, coins_paid: int, player_count: int, *, max_age_hours: int = 24) -> str:
    """The one-line in-channel notice for a swept game that paid."""
    name = GAME_NAMES.get(game_type, game_type)
    players = "1 player" if player_count == 1 else f"{player_count} players"
    return f"📦 {name} archived after {int(max_age_hours)}h — {coins_paid:,} coins paid to {players}."


async def _post_notice(bot, channel_id: int, text: str) -> None:
    """Best effort: the channel may be gone, uncached, or closed to the bot."""
    try:
        channel = bot.get_channel(int(channel_id))
        send = getattr(channel, "send", None)
        if send is None:
            return
        await send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.info("Expiry notice not delivered in channel %s", channel_id)
    except Exception:
        log.exception("Expiry notice failed in channel %s", channel_id)


async def _close_through_live_view(bot, game_id: str, channel_id: int) -> bool:
    """Prefer a live view's ``close_now`` (duck-typed, as the feature rotation
    does) so the game posts its own recap. False when there is no such view,
    no channel to post in, or the close raised — the caller then archives."""
    view = getattr(bot, "active_views", {}).get(game_id)
    closer = getattr(view, "close_now", None)
    if closer is None:
        return False
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return False
    try:
        await closer(channel, reason=EXPIRE_REASON)
    except Exception:
        log.exception("Expiry: %s's own close failed; archiving it instead", game_id)
        return False
    return True


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
            players, rounds = roster_from_payload(row["game_type"], payload)

            if await _close_through_live_view(bot, game_id, row["channel_id"]):
                bot.active_views.pop(game_id, None)
                ended += 1
                log.info("Auto-expired game %s through its own close (%dh limit)", game_id, max_age_hours)
                continue

            # bot= is what lets end_game both pay the roster and resolve the
            # guild for the history row; the bare call left guild_id = 0.
            result = await end_game(
                db, game_id,
                player_count=len(players), round_count=rounds, payload=payload,
                bot=bot, player_ids=players, reason=EXPIRE_REASON,
            )
            if result is not None and result.coins_paid > 0:
                await _post_notice(
                    bot, row["channel_id"],
                    archived_notice(
                        row["game_type"], result.coins_paid, len(players),
                        max_age_hours=max_age_hours,
                    ),
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
