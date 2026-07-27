"""Start-countdown nudge for lobby games.

A lobby game opened with ``start_in`` advertises a start time in its lobby
embed (``<t:epoch:R>``), but the host still presses the button — the countdown
is advertising, not automation. This module is what taps the host on the
shoulder when the advertised moment arrives.

Two layers live here:
  * Pure predicates + copy — ``extract_start_epoch``, ``start_ping_due``,
    ``build_start_ping`` (unit-tested, no I/O).
  * The async polling loop ``game_start_ping_loop`` — registered as a bot
    startup task.

**Why a poll loop and not a per-lobby timer.** ``games_active_games`` holds
only live games (a handful of rows), so polling is cheap — and it survives a
restart for free. A per-lobby ``asyncio`` task dies with the process and would
need re-arming inside all six game recoverers; the loop simply finds the row
again on its next tick and nudges late. Late beats never for a lobby that is
still open. The cost is that the nudge lands up to ``POLL_SECONDS`` after the
advertised second, which is immaterial for "it's time to start".

State rides in the game's ``payload`` as two top-level keys — the six lobby
games have six different payload shapes, so top level is the only common
ground:

  * ``start_epoch``     — UTC epoch to nudge at; absent ⇒ no countdown, no nudge.
  * ``start_ping_sent`` — set once the nudge goes out, so a slow tick can't
    double-ping.

Clapback predates this and keeps its epoch under ``config.start_epoch`` (its
lobby view's timeout and its embed both read it there); ``extract_start_epoch``
falls back to that rather than duplicating the value into two places that can
drift apart.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import discord

from bot_modules.games.constants import GAME_NAMES, LOBBY_GAME_TYPES, LOBBY_START_BUTTON
from bot_modules.games.utils.game_manager import modify_payload

log = logging.getLogger(__name__)

# How often the loop sweeps open lobbies. The nudge lands within this many
# seconds of the advertised start.
POLL_SECONDS = 15

# Bounds on the host-supplied `start_in` (minutes). The slash params declare the
# same range, but a scheduled row's stored options bypass that validation.
START_IN_MAX_MINUTES = 60


# ── Pure predicates + copy ──────────────────────────────────────────────────

def resolve_start_epoch(options: dict, now: float | None = None) -> int | None:
    """Turn a host's ``start_in`` (minutes) into the epoch to nudge at.

    Shared by every lobby game's ``launch`` so the countdown means the same
    thing everywhere. Returns None when no countdown was asked for — absent,
    blank, unparseable, or non-positive all read as "just open the lobby".
    Over-long values clamp rather than reject: a stored schedule row's options
    never went through the slash command's range validation.
    """
    raw = options.get("start_in")
    if raw is None or raw == "":
        return None
    try:
        minutes = int(raw)
    except (TypeError, ValueError):
        return None
    if minutes <= 0:
        return None
    minutes = min(minutes, START_IN_MAX_MINUTES)
    base = time.time() if now is None else now
    return int(base + minutes * 60)

def extract_start_epoch(payload: dict) -> int | None:
    """Return the advertised start epoch, or None when there's no countdown.

    Prefers the top-level ``start_epoch`` written by the five games wired up
    for this feature, and falls back to clapback's older nested
    ``config.start_epoch``. A non-numeric or non-positive value reads as "no
    countdown" rather than raising — a malformed payload must not wedge the
    sweep for every other lobby.
    """
    raw = payload.get("start_epoch")
    if raw is None:
        config = payload.get("config")
        if isinstance(config, dict):
            raw = config.get("start_epoch")
    if raw is None:
        return None
    try:
        epoch = int(raw)
    except (TypeError, ValueError):
        return None
    return epoch if epoch > 0 else None


def start_ping_due(payload: dict, now: float) -> bool:
    """True when this lobby's host should be nudged on this tick.

    Due means: a countdown was advertised, the moment has arrived, and we
    haven't already nudged. The caller is responsible for only handing us rows
    that are still in the ``joining`` state — a game that already started has
    no one left to nudge.
    """
    if payload.get("start_ping_sent"):
        return False
    epoch = extract_start_epoch(payload)
    if epoch is None:
        return False
    return now >= epoch


def build_start_ping(game_type: str, host_id: int) -> str:
    """The nudge copy: mention the host, name the game, name the button.

    Button labels differ across the lobby games (``Start`` /
    ``Start Draft`` / ``Close & Assign`` / …), so the host is pointed at the
    control actually in front of them. An unregistered game type degrades to a
    generic "start button" rather than lying about a label.
    """
    game_label = GAME_NAMES.get(game_type, game_type)
    button = LOBBY_START_BUTTON.get(game_type)
    button_str = f"**{button}**" if button else "the start button"
    return (
        f"⏰ <@{host_id}> — time to start **{game_label}**! "
        f"Hit {button_str} when everyone's in."
    )


def host_only_mentions(host_id: int) -> discord.AllowedMentions:
    """Allow-list exactly the host, per the embed style guide.

    Never rely on the raw text: a display name or game label that happens to
    contain ``@everyone`` must not be able to ping the server.
    """
    return discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=[discord.Object(id=host_id)],
    )


# ── I/O ─────────────────────────────────────────────────────────────────────

async def send_start_ping(channel, game_type: str, host_id: int) -> bool:
    """Post the nudge in the game's channel. True when it landed.

    Swallows send failures (missing perms, deleted channel) and reports them —
    a lobby that can't be nudged is not a lobby worth crashing the sweep over.
    """
    try:
        await channel.send(
            build_start_ping(game_type, host_id),
            allowed_mentions=host_only_mentions(host_id),
        )
        return True
    except Exception:
        log.warning(
            "start ping failed for %s in channel %s",
            game_type, getattr(channel, "id", "?"), exc_info=True,
        )
        return False


async def _mark_sent(db, game_id: str) -> None:
    """Flag the nudge as delivered so the next tick skips this lobby."""
    def _set(payload):
        payload["start_ping_sent"] = True

    await modify_payload(db, game_id, _set)


async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


async def _process_lobby(bot, db, row, now: float) -> None:
    payload = json.loads(row["payload"]) if row["payload"] else {}
    if not start_ping_due(payload, now):
        return

    game_id = row["game_id"]
    channel = await _resolve_channel(bot, int(row["channel_id"]))
    if channel is None:
        # Unreachable channel is terminal for this lobby — mark it sent so we
        # don't re-attempt every tick for the life of the lobby.
        log.warning("start ping: channel %s unreachable for game %s", row["channel_id"], game_id)
        await _mark_sent(db, game_id)
        return

    # Claim before sending: a send that succeeds but whose flag write is lost
    # would double-ping on the next tick, which is the louder failure.
    await _mark_sent(db, game_id)
    await send_start_ping(channel, row["game_type"], int(row["host_id"]))


async def game_start_ping_loop(bot) -> None:
    """Poll open lobbies and nudge hosts whose start time has arrived.

    Registered as a bot startup task. Only ``joining`` rows are considered, so
    a game that was started early, cancelled, or timed out drops out of the
    sweep on its own — no stale "time to start" for a game already running.
    """
    await bot.wait_until_ready()
    db = bot.games_db
    lobby_types = tuple(sorted(LOBBY_GAME_TYPES))
    placeholders = ", ".join("?" for _ in lobby_types)

    while not bot.is_closed():
        try:
            now = time.time()
            rows = await db.fetchall(
                "SELECT * FROM games_active_games "
                f"WHERE state = 'joining' AND game_type IN ({placeholders})",
                lobby_types,
            )
            for row in rows:
                try:
                    await _process_lobby(bot, db, row, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("start ping: lobby %s failed to process", row["game_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("game_start_ping_loop iteration error")
        await asyncio.sleep(POLL_SECONDS)
