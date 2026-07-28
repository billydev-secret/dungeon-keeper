"""Event Echo — the I/O half: cooldown store, the sender, and the poll loop.

Three sources feed one sender:

  * **Party games** (and therefore scheduled games, which launch down the same
    path) — the ``event_echo_loop`` below.
  * **Cards Against Humanity** — ``echo_gamebot_lobby``, called from the
    ``on_message`` listener ``games_external_cog`` already runs over Gamebot.
  * **Discord's native scheduled events** — ``echo_discord_event``, called
    from ``events_cog`` when an event goes ``scheduled → active``.

Every one of them ends at :func:`echo_event`, which owns the destination, the
cooldowns and the dedupe claim. Adding a fourth source means writing a caller,
not touching the rules.

**Why a poll loop for party games.** The obvious hook is where each game posts
its lobby, but that is 28 ``update_game_message`` call sites across the game
cogs, and a game whose author forgets the call is a silent gap. Sweeping
``games_active_games`` is one place instead of 28, it picks up scheduled games
for free, and — like ``game_start_ping_service``, whose shape this borrows —
it survives a restart, which a per-lobby task does not. The table holds only
live games, a handful of rows, so polling it is cheap.

The cost of polling is that a game which opens *and finishes* inside one tick
is never echoed. That is the correct trade: an echo pointing at a game already
over is worse than no echo.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.games.constants import GAME_NAMES
from bot_modules.services.event_echo_logic import (
    GAMEBOT_ECHO_NAMES,
    RETENTION_SECONDS,
    SOURCE_DISCORD_EVENT,
    SOURCE_GAMEBOT,
    SOURCE_PARTY_GAME,
    build_echo_embed,
    decide,
    is_fresh,
    jump_url,
)

log = logging.getLogger(__name__)

# The destination. Its own key rather than reusing `denizen_announce_channel_id`
# (which is legacy role-grant plumbing that merely happens to point at the same
# channel) so either can move without dragging the other along.
CONFIG_CHANNEL_KEY = "event_echo_channel_id"

# Matches game_start_ping_service. An echo landing up to 15s after the lobby
# opens is immaterial for "come and join this".
POLL_SECONDS = 15


# ── Cooldown store (sync, over a plain connection — directly testable) ───────

def last_echo_times(
    conn: sqlite3.Connection, guild_id: int, echo_key: str
) -> tuple[float | None, float | None]:
    """``(last echo of this key, last echo of anything)`` for one guild.

    Suppressed rows are excluded from both: a refusal must not push the window
    out, or a single busy minute would cascade into a window that keeps
    receding and the feature would go quiet for good.
    """
    row = conn.execute(
        "SELECT MAX(CASE WHEN echo_key = ? THEN echoed_at END) AS same_type, "
        "       MAX(echoed_at) AS any_type "
        "FROM event_echo_log WHERE guild_id = ? AND suppressed = 0",
        (echo_key, guild_id),
    ).fetchone()
    if row is None:
        return None, None
    return row["same_type"], row["any_type"]


def claim_echo(
    conn: sqlite3.Connection,
    *,
    guild_id: int,
    source: str,
    echo_key: str,
    ref: str,
    now: float,
    suppressed: bool,
) -> bool:
    """Record this echo, returning False if this ``ref`` was already handled.

    The unique index on ``(guild_id, source, ref)`` is what makes this the
    dedupe point: the poll loop sees the same open lobby every 15 seconds and
    only the first tick gets True. Claiming *before* sending means a crash
    between the two loses an echo rather than repeating one — the failure
    direction that matters in a channel this busy.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO event_echo_log "
        "(guild_id, source, echo_key, ref, echoed_at, suppressed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (guild_id, source, echo_key, ref, now, 1 if suppressed else 0),
    )
    return cur.rowcount > 0


def prune_echo_log(conn: sqlite3.Connection, now: float) -> int:
    """Drop rows past the retention window; returns how many went."""
    cur = conn.execute(
        "DELETE FROM event_echo_log WHERE echoed_at < ?", (now - RETENTION_SECONDS,)
    )
    return cur.rowcount


def echo_channel_id(conn: sqlite3.Connection, guild_id: int) -> int | None:
    """The configured destination, or None when the feature isn't set up.

    Unset means off — the echo has no sensible default channel to invent, and
    picking one for an admin who never asked is how a bot ends up posting
    somewhere nobody expected.
    """
    raw = get_config_value(conn, CONFIG_CHANNEL_KEY, "", guild_id)
    try:
        return int(raw) or None
    except (TypeError, ValueError):
        return None


# ── The sender ──────────────────────────────────────────────────────────────

async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except discord.DiscordException:
        return None


async def echo_event(
    bot,
    *,
    guild: discord.Guild,
    source: str,
    echo_key: str,
    ref: str,
    game_name: str,
    origin_channel_id: int,
    url: str,
    host_name: str | None = None,
    now: float | None = None,
) -> bool:
    """Post one echo, subject to config, cooldowns and dedupe.

    Returns True only when a message actually went out. Every other path —
    unconfigured, unreachable destination, cooldown, already-seen ref —
    returns False, and the caller is not expected to care which.
    """
    now = time.time() if now is None else now
    db_path: Path = bot.ctx.db_path

    def _claim() -> tuple[int | None, bool]:
        with open_db(db_path) as conn:
            dest = echo_channel_id(conn, guild.id)
            if dest is None:
                return None, False
            last_same, last_any = last_echo_times(conn, guild.id, echo_key)
            verdict = decide(now=now, last_same_type=last_same, last_any=last_any)
            claimed = claim_echo(
                conn,
                guild_id=guild.id,
                source=source,
                echo_key=echo_key,
                ref=ref,
                now=now,
                suppressed=not verdict.allowed,
            )
            if not verdict.allowed and claimed:
                log.debug(
                    "event echo: suppressed %s/%s (%s cooldown)",
                    source,
                    echo_key,
                    verdict.reason,
                )
            return dest, (verdict.allowed and claimed)

    dest_id, go = await asyncio.to_thread(_claim)
    if dest_id is None or not go:
        return False

    channel = await _resolve_channel(bot, dest_id)
    if channel is None:
        log.warning("event echo: destination channel %s unreachable", dest_id)
        return False

    color = await resolve_accent_color(db_path, guild)
    embed = build_echo_embed(
        game_name=game_name,
        channel_id=origin_channel_id,
        url=url,
        host_name=host_name,
        color=color,
    )
    try:
        # Silent by design — see event_echo_logic's module docstring.
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.DiscordException:
        log.warning("event echo: send failed for %s/%s", source, ref, exc_info=True)
        return False
    return True


# ── Source 2: Gamebot (Cards Against Humanity) ──────────────────────────────

async def echo_gamebot_lobby(bot, message: discord.Message, sub_game: str) -> bool:
    """Echo a Gamebot lobby we care about.

    Called from the collector's ``on_message`` — the listener that already
    banks these messages for economy payouts — so CAH costs a branch on a hot
    path we were running anyway, not a second watcher to keep in sync with
    Gamebot's wording.
    """
    name = GAMEBOT_ECHO_NAMES.get(sub_game)
    if name is None or message.guild is None:
        return False
    return await echo_event(
        bot,
        guild=message.guild,
        source=SOURCE_GAMEBOT,
        echo_key=sub_game,
        ref=str(message.id),
        game_name=name,
        origin_channel_id=message.channel.id,
        url=message.jump_url,
    )


# ── Source 3: Discord's native scheduled events ─────────────────────────────

async def echo_discord_event(bot, event: discord.ScheduledEvent) -> bool:
    """Echo a native Discord event at the moment it goes live.

    Keyed on the event id, so the several ``on_scheduled_event_update`` calls
    Discord emits over an event's life produce at most one echo.
    """
    if event.guild is None:
        return False
    location = event.channel.id if event.channel is not None else None
    return await echo_event(
        bot,
        guild=event.guild,
        source=SOURCE_DISCORD_EVENT,
        echo_key=SOURCE_DISCORD_EVENT,
        ref=str(event.id),
        game_name=event.name,
        origin_channel_id=location if location is not None else event.guild.id,
        url=event.url,
        host_name=event.creator.display_name if event.creator else None,
    )


# ── Source 1: party games + scheduled games (the poll loop) ─────────────────

def _opened_at(row) -> float | None:
    """``created_at`` as an epoch, or None if it's missing or unparseable.

    SQLite writes ``CURRENT_TIMESTAMP`` as naive UTC text; a row predating
    some format change, or one written by a test with its own value, must not
    take the loop down.
    """
    raw = row["created_at"] if "created_at" in row.keys() else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


async def _process_game(bot, guild: discord.Guild, row, now: float) -> None:
    message_id = row["message_id"]
    if not message_id:
        # The lobby hasn't been posted yet (create_game runs before the send).
        # Skipping leaves it for the next tick, by which point there's a
        # message to link to — the whole point of the echo.
        return
    if not is_fresh(_opened_at(row), now):
        return

    game_type = str(row["game_type"])
    await echo_event(
        bot,
        guild=guild,
        source=SOURCE_PARTY_GAME,
        echo_key=game_type,
        ref=str(row["game_id"]),
        # A game type missing from GAME_NAMES still gets a readable name rather
        # than being dropped — a new game should show up in main chat on the
        # day it ships, not once someone remembers the display-name table.
        game_name=GAME_NAMES.get(game_type) or game_type.replace("_", " ").title(),
        origin_channel_id=int(row["channel_id"]),
        url=jump_url(guild.id, int(row["channel_id"]), int(message_id)),
        now=now,
    )


async def event_echo_loop(bot) -> None:
    """Sweep open party games and echo the ones that clear the cooldowns.

    Registered as a bot startup task. Only live rows are visible in
    ``games_active_games``, so a finished game drops out on its own and can
    never be echoed late.
    """
    await bot.wait_until_ready()
    db = bot.games_db
    db_path: Path = bot.ctx.db_path
    last_prune = 0.0

    while not bot.is_closed():
        try:
            now = time.time()
            guild: discord.Guild | None = bot.get_guild(bot.ctx.guild_id)
            if guild is not None:
                rows = await db.fetchall(
                    "SELECT * FROM games_active_games WHERE state IN ('open', 'joining')"
                )
                for row in rows:
                    try:
                        await _process_game(bot, guild, row, now)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "event echo: game %s failed to process", row["game_id"]
                        )

            # Hourly, not every tick — the table is tiny and the prune is pure
            # housekeeping.
            if now - last_prune > 3600:
                last_prune = now
                await asyncio.to_thread(_prune, db_path, now)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("event_echo_loop iteration error")
        await asyncio.sleep(POLL_SECONDS)


def _prune(db_path: Path, now: float) -> None:
    with open_db(db_path) as conn:
        prune_echo_log(conn, now)
