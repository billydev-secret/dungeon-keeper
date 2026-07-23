"""Intake stale-card nudge loop.

Registered as a startup task factory (see ``__main__.py``); one ~10-minute
tick finds open intake cards with no progress for ``intake_stale_hours``
(any step tick resets the clock — see ``intake_service.stale_cards``) and
bumps each **once**: a reply under the card pinging the greeter role so
whoever's around can pick the intake up. ``nudged_at`` is stamped whether or
not the send lands, mirroring greeting watch — a permission failure must not
wedge a card into re-nudging every tick (it's already logged).

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


async def run_tick(bot: Bot, db_path: Path, now: float) -> None:
    for guild in bot.guilds:
        try:
            stale, greeter_role, hours = await asyncio.to_thread(
                _stale_sync, db_path, guild.id, now
            )
            for card in stale:
                await _nudge(bot, guild.id, card, greeter_role, hours)
                await asyncio.to_thread(
                    _mark_nudged_sync, db_path, int(card["id"]), now
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("intake nudge tick failed for guild %s", guild.id)


async def intake_loop(bot: Bot, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_tick(bot, db_path, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("intake nudge tick crashed")
        await asyncio.sleep(TICK_SECONDS)
