"""Intake stale-card nudge loop.

Registered as a startup task factory (see ``__main__.py``); one ~10-minute
tick finds open intake cards with no progress for ``intake_stale_hours``
(any step tick resets the clock — see ``intake_service.stale_cards``) and
bumps each **once**: a reply under the card pinging the greeter role so
whoever's around can pick the intake up. ``nudged_at`` is stamped whether or
not the send lands, mirroring greeting watch — a permission failure must not
wedge a card into re-nudging every tick (it's already logged).

Each tick first sweeps **finished** cards — every step ticked, still open —
and closes them (``intake_views.close_finished_cards``). The live tick paths
already close a card the instant its last box is ticked, so this is the
backstop for anything finished while the bot was down.

A stale card is only worth a ping if the member is actually reachable, so
each one is checked against the member's real state first
(``intake_service.nudge_action``): someone who never accepted membership
screening holds no roles and can't be greeted at all, and someone who left
unnoticed has no intake left to run. Neither pings; the screening case stays
unstamped so it can ping later, once accepting makes greeting possible.

Config is read from the DB each tick, so dashboard changes apply on the next
sweep without a restart. Every SQLite touch runs in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.services import intake_service as svc
from bot_modules.core.background import run_forever

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.intake")

TICK_SECONDS = 600.0


def _stale_sync(
    db_path: Path, guild_id: int, now: float
) -> tuple[list[Any], int, float]:
    """(stale cards, greeter role id, stale hours) for one enabled guild."""
    with open_db(db_path) as conn:
        if not svc.is_enabled(conn, guild_id):
            return [], 0, 0.0
        return (
            svc.stale_cards(conn, guild_id, now),
            svc.greeter_role_id(conn, guild_id),
            svc.stale_hours(conn, guild_id),
        )


def _mark_nudged_sync(db_path: Path, card_id: int, now: float) -> None:
    with open_db(db_path) as conn:
        svc.mark_nudged(conn, card_id, now)


async def _nudge(
    bot: Bot, guild_id: int, card: Any, greeter_role_id: int, hours: float
) -> None:
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(int(card["channel_id"])) if guild else None
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    user_id = int(card["user_id"])
    ping = f"<@&{greeter_role_id}>" if greeter_role_id > 0 else "Greeters"
    text = (
        f"{ping} — the intake for <@{user_id}> has had no progress for "
        f"{hours:g}h. Anyone around to pick it up?"
    )
    mentions = discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=greeter_role_id)] if greeter_role_id > 0 else False,
    )
    try:
        if int(card["message_id"]) > 0:
            # Reply under the card so the nudge carries its context.
            await channel.send(
                text,
                reference=discord.MessageReference(
                    message_id=int(card["message_id"]),
                    channel_id=int(card["channel_id"]),
                    guild_id=guild_id,
                    fail_if_not_exists=False,
                ),
                allowed_mentions=mentions,
            )
        else:
            await channel.send(text, allowed_mentions=mentions)
    except discord.HTTPException:
        log.warning("intake: stale nudge failed in guild %s", guild_id)


async def _presence(guild: discord.Guild, user_id: int) -> str:
    """Where a card's member stands: in, still screening, gone, or unknown.

    The cache answers for anyone the gateway has seen, which is the normal
    case; the fetch is the fallback for a cache miss, and only its explicit
    404 may conclude "gone" — a rate limit or an outage must not close
    somebody's card.
    """
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return svc.PRESENCE_GONE
        except (discord.HTTPException, TimeoutError):
            return svc.PRESENCE_UNKNOWN
    return svc.PRESENCE_SCREENING if member.pending else svc.PRESENCE_IN


async def run_tick(bot: Bot, db_path: Path, now: float) -> None:
    for guild in bot.guilds:
        try:
            from bot_modules.services.intake_views import close_finished_cards

            # Fully ticked cards close themselves on the tick that finishes
            # them; this catches the ones that finished while the bot was
            # down, and the backlog from before that existed.
            await close_finished_cards(bot.ctx, guild)
            stale, greeter_role, hours = await asyncio.to_thread(
                _stale_sync, db_path, guild.id, now
            )
            for card in stale:
                user_id = int(card["user_id"])
                action = svc.nudge_action(await _presence(guild, user_id))
                if action == svc.NUDGE_SKIP:
                    continue
                if action == svc.NUDGE_CLOSE_LEFT:
                    from bot_modules.services.intake_views import close_member_card

                    await close_member_card(
                        bot.ctx, guild, user_id, svc.RESOLUTION_LEFT
                    )
                    continue
                await _nudge(bot, guild.id, card, greeter_role, hours)
                await asyncio.to_thread(
                    _mark_nudged_sync, db_path, int(card["id"]), now
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("intake nudge tick failed for guild %s", guild.id)


async def intake_loop(bot: Bot, db_path: Path) -> None:
    await run_forever(
        bot,
        tick=lambda: run_tick(bot, db_path, time.time()),
        interval=TICK_SECONDS,
        label="intake nudge",
        logger=log,
    )
