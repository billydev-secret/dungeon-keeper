"""Start-countdown nudge, countdown auto-start and the Game Night ping.

A lobby game opened with ``start_in`` advertises a start time in its lobby
embed (``<t:epoch:R>``). For most lobby games the host still presses the
button — the countdown is advertising, not automation — and this module is
what taps the host on the shoulder when the advertised moment arrives. A game
whose cog registers a **countdown auto-starter** (``bot.lobby_auto_starters``,
Clapback since 2026-09-04, clapback-8) is started *by this sweep* at the
advertised moment instead, provided enough players have joined; short of the
floor the host is nudged once and the idle-lobby close below takes it from
there — and the game still starts itself the moment the floor is reached
after that. A scheduled lobby of such a game therefore runs with nobody at the
keyboard (the scheduler stamps a default countdown when the schedule names
none).

Two layers live here:
  * Pure predicates + copy — ``extract_start_epoch``, ``start_ping_due``,
    ``auto_start_due``, ``build_start_ping``, ``build_game_night_ping``
    (unit-tested, no I/O).
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

**Idle lobbies** (discovery-6). A lobby opened *without* a countdown used to
get nothing: no nudge, and — outside Clapback's own 10-minute view timeout —
no close until the 24-hour sweep, so an abandoned Story Builder sat all day
holding the channel. The same sweep now applies two per-guild dials from the
Games Global Config page (``config`` table, 0 switches a step off):

  * after ``IDLE_NUDGE_KEY`` minutes a countdown-less ``joining`` lobby is
    nudged once, the same way a countdown lobby is at its advertised start;
  * after ``IDLE_CANCEL_KEY`` minutes a ``joining`` lobby that still holds
    fewer than its game's minimum roster (``LOBBY_MIN_PLAYERS``, or the floor
    the payload names) is closed — ``end_game`` with ``reason='lobby_timeout'``
    and **no payout** — and its lobby message is retired. A countdown lobby's
    hour starts at its advertised start rather than at open.

A lobby that *could* start is never closed: that is the host's call, however
long they sit on it, and the 24-hour sweep stays the safety net for everything
else (games_system_spec.md, Non-goals — this is a pre-game close of a lobby
that never filled, not a mid-game inactivity timeout).

**The Game Night ping** (discovery-2 / clapback-11, decision D4). A member
launch used to post nothing but the board, so a lobby was invisible outside
its channel. The sweep now posts **one** ping line per lobby the first tick it
sees the lobby's board (``message_id`` written — every DB-backed launcher does
that before returning) mentioning the guild's opt-in **Game Night** role
(``feature_roles.GAME_NIGHT_PING``, config key ``game_night_ping_role_id``,
provisioned on first use like the other ping roles; a stored "(none)" keeps
the sweep silent). ``content=`` with ``AllowedMentions(roles=[role])`` and a
jump link, never inside an embed. Platform-level on purpose: every ``/games
play`` launch and every scheduled launch gets it without a per-cog edit. A
schedule that announces itself claims the lobby before announcing so the two
never stack; ``game_night_pinged`` in the payload is the once-only flag, and
both senders take it as a **claim** (``claim_game_night_ping`` — an UPDATE
that matches only while the flag is unset) before they send, so the loser of a
race between a sweep tick and a launch announcement stays silent rather than
posting the second ping.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.core.role_provision import ensure_config_role
from bot_modules.core.utils import jump_url, resolve_bot_channel
from bot_modules.games.constants import (
    GAME_NAMES,
    LOBBY_GAME_TYPES,
    LOBBY_MIN_PLAYERS,
    LOBBY_START_BUTTON,
)
from bot_modules.games.utils.game_manager import resolve_guild_id
from bot_modules.services import ping_tracker_service
from bot_modules.services.feature_roles import GAME_NIGHT_PING

log = logging.getLogger(__name__)

# How often the loop sweeps open lobbies. The nudge lands within this many
# seconds of the advertised start.
POLL_SECONDS = 15

# Bounds on the host-supplied `start_in` (minutes). The slash params declare the
# same range, but a scheduled row's stored options bypass that validation.
START_IN_MAX_MINUTES = 60

# Idle-lobby dials: config-table keys (per guild) and the defaults an unset
# guild runs on. Minutes on the dashboard, seconds in the code.
IDLE_NUDGE_KEY = "games_lobby_idle_nudge_minutes"
IDLE_CANCEL_KEY = "games_lobby_idle_cancel_minutes"
IDLE_NUDGE_DEFAULT_MINUTES = 20
IDLE_CANCEL_DEFAULT_MINUTES = 60
IDLE_MAX_MINUTES = 24 * 60

LOBBY_TIMEOUT_REASON = "lobby_timeout"

# Once-only payload flags the sweep claims before acting (see
# ``set_payload_flag`` for why a targeted json_set and not a read-modify-write).
START_PING_SENT_FLAG = "start_ping_sent"
GAME_NIGHT_PINGED_FLAG = "game_night_pinged"
AUTO_START_FAILED_FLAG = "auto_start_failed"
_PAYLOAD_FLAGS = frozenset({START_PING_SENT_FLAG, GAME_NIGHT_PINGED_FLAG, AUTO_START_FAILED_FLAG})

# The countdown a scheduled lobby gets when its game can start itself and the
# schedule named no start_in — "scheduled" has to mean "runs" (clapback-8).
SCHEDULED_AUTO_START_MINUTES = 10


@dataclass(frozen=True)
class IdleLobbyDials:
    """The two idle-lobby windows in seconds; 0 means that step is off."""

    nudge_seconds: int
    cancel_seconds: int


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


def auto_starter_for(bot, game_type: str):
    """The cog-registered countdown auto-starter for ``game_type``, or None
    when the game keeps the press-the-button contract."""
    starters = getattr(bot, "lobby_auto_starters", None)
    if not isinstance(starters, dict):
        return None
    return starters.get(game_type)


def auto_start_due(game_type: str, payload: dict, now: float) -> bool:
    """True when a countdown lobby should be started by the sweep on this tick.

    Due means: a countdown was advertised, the moment has arrived, the roster
    is at or above the game's floor, and no earlier attempt blew up. Unlike
    the nudge this is **not** gated on ``start_ping_sent`` — a lobby that was
    short at the advertised moment (and got its nudge) still starts itself
    the tick the floor is reached. The caller checks that the game actually
    registered an auto-starter; a game that didn't is nudged, never started.
    """
    if payload.get(AUTO_START_FAILED_FLAG):
        return False
    epoch = extract_start_epoch(payload)
    if epoch is None or now < epoch:
        return False
    return lobby_roster_size(payload) >= lobby_min_players(game_type, payload)


def _minutes(raw, default: int) -> int:
    """A dial's stored string → whole minutes, clamped; junk reads as the default."""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(0, min(value, IDLE_MAX_MINUTES))


def parse_idle_dials(nudge_raw, cancel_raw) -> IdleLobbyDials:
    """Turn the two stored dial values (or None) into windows in seconds."""
    return IdleLobbyDials(
        nudge_seconds=_minutes(nudge_raw, IDLE_NUDGE_DEFAULT_MINUTES) * 60,
        cancel_seconds=_minutes(cancel_raw, IDLE_CANCEL_DEFAULT_MINUTES) * 60,
    )


async def read_idle_dials(db, guild_id: int) -> IdleLobbyDials:
    """The guild's idle-lobby dials from the config table, defaults when unset.

    A guild's own row wins; a legacy ``guild_id = 0`` row is the fallback, the
    way every other config key reads (``db_utils.get_config_value``).
    """

    async def _read(key: str):
        row = await db.fetchone(
            "SELECT value FROM config WHERE guild_id = ? AND key = ?", (int(guild_id), key)
        )
        if row is None and guild_id != 0:
            row = await db.fetchone(
                "SELECT value FROM config WHERE guild_id = 0 AND key = ?", (key,)
            )
        return row[0] if row else None

    return parse_idle_dials(await _read(IDLE_NUDGE_KEY), await _read(IDLE_CANCEL_KEY))


def _ints(values: Any) -> list[int]:
    out: list[int] = []
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
        return out
    for raw in values:
        try:
            uid = int(raw)
        except (TypeError, ValueError):
            continue
        if uid not in out:
            out.append(uid)
    return out


def lobby_roster_size(payload: dict) -> int:
    """How many have joined. The six lobby games keep their roster under
    ``players`` (clapback, mlt, rushmore, story) or ``participants``
    (compliment, mfk); anything unreadable counts as nobody."""
    roster = payload.get("players")
    if roster is None:
        roster = payload.get("participants")
    return len(_ints(roster))


def lobby_min_players(game_type: str, payload: dict) -> int:
    """The floor this lobby's start button would hold the roster to.

    mlt (``min_players``) and rushmore (``settings.min_players``) can raise
    theirs per game and store it in the payload; everything else uses the
    registry. An unknown type assumes two — a lobby with two people in it is
    never closed on a guess.
    """
    raw = payload.get("min_players")
    if raw is None:
        settings = payload.get("settings")
        if isinstance(settings, dict):
            raw = settings.get("min_players")
    if raw is not None:
        try:
            floor = int(raw)
            if floor > 0:
                return floor
        except (TypeError, ValueError):
            pass
    return LOBBY_MIN_PLAYERS.get(game_type, 2)


def opened_epoch(row) -> float | None:
    """When the lobby opened, from the row's ``created_at`` (SQLite's own
    ``CURRENT_TIMESTAMP`` text, UTC). None when unreadable — an unknown age
    is never treated as idle."""
    raw = row["created_at"]
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def idle_nudge_due(payload: dict, opened_at: float | None, now: float, dials: IdleLobbyDials) -> bool:
    """A countdown-less lobby, open for the nudge window, not yet nudged.

    Countdown lobbies are left to ``start_ping_due`` — their host chose when
    to be tapped, and tapping them earlier would contradict the board.
    """
    if dials.nudge_seconds <= 0 or opened_at is None:
        return False
    if payload.get("start_ping_sent") or extract_start_epoch(payload) is not None:
        return False
    return now - opened_at >= dials.nudge_seconds


def idle_cancel_due(
    game_type: str, payload: dict, opened_at: float | None, now: float, dials: IdleLobbyDials
) -> bool:
    """Sat for the cancel window with fewer joined than the game can start with.

    The clock starts at open, or at the advertised start for a countdown
    lobby — people were told to turn up then, so the hour of grace runs from
    then. Enough players to start means it is the host's lobby to run or
    abandon; the sweep never closes one that could have begun.
    """
    if dials.cancel_seconds <= 0 or opened_at is None:
        return False
    if lobby_roster_size(payload) >= lobby_min_players(game_type, payload):
        return False
    since = opened_at
    start_epoch = extract_start_epoch(payload)
    if start_epoch is not None:
        since = max(since, float(start_epoch))
    return now - since >= dials.cancel_seconds


def build_idle_nudge(
    game_type: str, host_id: int, *, idle_minutes: int, dials: IdleLobbyDials, min_players: int
) -> str:
    """The idle nudge: how long the lobby has waited, which button, and — when
    the close is on — the deadline it is heading for."""
    game_label = GAME_NAMES.get(game_type, game_type)
    button = LOBBY_START_BUTTON.get(game_type)
    button_str = f"**{button}**" if button else "the start button"
    text = (
        f"⏰ <@{host_id}> — **{game_label}** has been waiting {idle_minutes} minutes. "
        f"Hit {button_str} when everyone's in"
    )
    if dials.cancel_seconds > 0:
        players = "1 player" if min_players == 1 else f"{min_players} players"
        text += (
            f" — with fewer than {players} joined it closes on its own "
            f"after {dials.cancel_seconds // 60} minutes."
        )
    else:
        text += "."
    return text


def build_idle_cancel_notice(game_type: str, *, dials: IdleLobbyDials, min_players: int) -> str:
    """What the retired lobby message says, in place of its dead buttons."""
    game_label = GAME_NAMES.get(game_type, game_type)
    players = "1 player" if min_players == 1 else f"{min_players} players"
    return (
        f"⌛ **Lobby timed out** — {game_label} wasn't started within "
        f"{dials.cancel_seconds // 60} minutes and fewer than {players} had joined. "
        f"Run `/games play {game_type}` to open a new one."
    )


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


def build_short_roster_nudge(
    game_type: str, host_id: int, *, joined: int, min_players: int, dials: IdleLobbyDials | None = None
) -> str:
    """The countdown-up nudge for a game that starts itself: the roster is
    short, so say what it is waiting on rather than pointing at a button that
    would refuse — and, when the close is on, the deadline it is heading for."""
    game_label = GAME_NAMES.get(game_type, game_type)
    have = "has" if joined == 1 else "have"
    text = (
        f"⏰ <@{host_id}> — **{game_label}**'s countdown is up, but only "
        f"{joined} of the {min_players} it needs {have} joined. "
        f"It starts on its own the moment {min_players} are in"
    )
    if dials is not None and dials.cancel_seconds > 0:
        text += f" — with fewer than that it closes after {dials.cancel_seconds // 60} minutes."
    else:
        text += "."
    return text


def build_game_night_ping(
    game_type: str,
    *,
    role_id: int,
    guild_id: int,
    channel_id: int,
    message_id: int,
    start_epoch: int | None = None,
) -> str:
    """The one Game Night line: the role, the game, when it starts, a jump
    link to the board. Content, never an embed — a role mention only
    notifies from message content."""
    game_label = GAME_NAMES.get(game_type, game_type)
    when = f" — it starts <t:{start_epoch}:R>" if start_epoch else ""
    return (
        f"<@&{role_id}> 🎮 A **{game_label}** lobby just opened{when}! "
        f"Jump in: {jump_url(guild_id, channel_id, message_id)}"
    )


def role_only_mentions(role_id: int) -> discord.AllowedMentions:
    """Allow-list exactly the one role (docs/embed_style_guide.md)."""
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role_id)],
        replied_user=False,
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


async def set_payload_flag(db, game_id: str, flag: str) -> bool:
    """Claim one once-only flag on the game's payload. True if this call set it.

    A targeted ``json_set``, deliberately **not** a read-modify-write. Several
    lobby writers (mlt join/leave, story, clapback) mutate the payload without
    taking ``payload_lock``, so a read-modify-write here could interleave with a
    join and either lose that join or lose this flag — the latter costing a
    duplicate nudge 15s later. One UPDATE touching one key can't lose either.
    ``flag`` is one of the module's own constants, never caller text.

    The UPDATE only matches a row where the flag is *unset*, so it is a claim
    and not just a write: two senders racing for the same once-only line (the
    sweep's Game Night ping and a schedule's own announcement, which are
    separate loops and can be mid-flight together) both call this first, and
    exactly one is told it won. A row that has gone away, or whose payload is
    malformed enough for ``json_extract`` to refuse it, answers False — the
    safe direction, since a caller that can't claim doesn't send.
    """
    if flag not in _PAYLOAD_FLAGS:
        raise ValueError(f"unknown payload flag {flag!r}")
    try:
        cur = await db.execute(
            "UPDATE games_active_games "
            f"SET payload = json_set(COALESCE(NULLIF(payload, ''), '{{}}'), '$.{flag}', json('true')) "
            f"WHERE game_id = ? AND json_extract(COALESCE(NULLIF(payload, ''), '{{}}'), '$.{flag}') IS NULL",
            (game_id,),
        )
    except Exception:
        # Malformed payload JSON — json_set refuses it. The nudge is already
        # sent (or unsendable); log rather than let the sweep retry forever.
        log.warning("start ping: could not flag game %s as %s", game_id, flag, exc_info=True)
        return False
    return bool(getattr(cur, "rowcount", 0) > 0)


async def mark_start_ping_sent(db, game_id: str) -> bool:
    """Flag the nudge as delivered so the next tick skips this lobby."""
    return await set_payload_flag(db, game_id, START_PING_SENT_FLAG)


async def claim_game_night_ping(db, game_id: str, *, no_row_wins: bool = False) -> bool:
    """Claim the one Game Night line this game is allowed. True for the
    winner only — the sweep and an announcing schedule both ask, and the
    loser stays quiet rather than stacking a second ping on one game opening.

    ``no_row_wins`` is for a launcher announcing its own launch: a game that
    keeps its round in memory (risky_roll) has no ``games_active_games`` row
    to flag, and the sweep only ever pings rows it can see — so "no row" is a
    win there, while for the sweep itself a row that has gone is a game that
    ended and gets nothing.
    """
    if await set_payload_flag(db, game_id, GAME_NIGHT_PINGED_FLAG):
        return True
    if not no_row_wins:
        return False
    return await db.fetchone(
        "SELECT 1 FROM games_active_games WHERE game_id = ?", (game_id,)
    ) is None


async def resolve_game_night_role(bot, guild_id: int) -> tuple[bool, int | None]:
    """``(resolved, role_id)`` for the guild's Game Night dial.

    Provisions the role on a guild that never set the dial — the same
    first-use contract as every other ping role (``ensure_config_role``); a
    stored "(none)" resolves to ``(True, None)`` and the sweep stays silent.
    ``resolved`` is False when the guild is not reachable from this bot (or
    provisioning failed), so the caller leaves the lobby unflagged and tries
    again next tick rather than recording a ping that never went out.
    """
    get_guild = getattr(bot, "get_guild", None)
    ctx = getattr(bot, "ctx", None)
    guild: Any = get_guild(guild_id) if callable(get_guild) and guild_id else None
    if guild is None or ctx is None:
        return False, None
    try:
        role = await ensure_config_role(
            ctx, guild, GAME_NIGHT_PING.key, GAME_NIGHT_PING.spec,
            feature=GAME_NIGHT_PING.feature,
            allow_legacy_fallback=GAME_NIGHT_PING.legacy_fallback,
        )
    except Exception:
        log.warning("game night ping: role lookup failed for guild %s", guild_id, exc_info=True)
        return False, None
    return True, (int(role.id) if role is not None else None)


async def _record_game_night_ping(db, message, *, guild_id: int, channel_id: int, role_id: int, game_id: str) -> None:
    """Tie the ping to the lobby it advertised, so the Ping Response report
    can say how many actually joined. Best effort; never fails the sweep."""
    message_id = getattr(message, "id", None)
    author = getattr(message, "author", None)
    created = getattr(message, "created_at", None)
    if message_id is None or author is None or created is None:
        return

    def _write() -> None:
        with open_db(db.db_path) as conn:
            ping_tracker_service.record_game_start_ping(
                conn,
                message_id=int(message_id),
                guild_id=guild_id,
                channel_id=channel_id,
                author_id=int(author.id),
                role_ids=[role_id],
                game_id=game_id,
                ts=created.timestamp(),
            )

    try:
        await asyncio.to_thread(_write)
    except Exception:
        log.warning("game night ping: tracking failed for %s", game_id, exc_info=True)


async def _game_night_ping(bot, db, row, payload: dict, guild_id: int, roles: dict[int, tuple[bool, int | None]]) -> None:
    """Post the Game Night line for a lobby whose board now exists."""
    if guild_id not in roles:
        roles[guild_id] = await resolve_game_night_role(bot, guild_id)
    resolved, role_id = roles[guild_id]
    if not resolved:
        return
    game_id = row["game_id"]
    # Claim first: a lost flag write after a successful send is a second ping,
    # and the row was read at the top of the tick — a schedule that announces
    # its own launch may have claimed the line since, in which case this sweep
    # says nothing rather than pinging the same lobby twice.
    if not await claim_game_night_ping(db, game_id):
        return
    if role_id is None:
        return
    channel_id = int(row["channel_id"])
    channel = await resolve_bot_channel(bot, channel_id)
    if channel is None:
        return
    text = build_game_night_ping(
        str(row["game_type"]),
        role_id=role_id, guild_id=guild_id, channel_id=channel_id,
        message_id=int(row["message_id"]),
        start_epoch=extract_start_epoch(payload),
    )
    try:
        message = await channel.send(
            text, allowed_mentions=role_only_mentions(role_id), suppress_embeds=True,
        )
    except Exception:
        log.warning("game night ping failed for %s in channel %s", game_id, channel_id, exc_info=True)
        return
    await _record_game_night_ping(
        db, message, guild_id=guild_id, channel_id=channel_id, role_id=role_id, game_id=game_id,
    )



async def _retire_lobby_message(bot, channel, row, text: str) -> None:
    """Replace the dead lobby's buttons with the timeout notice; best effort."""
    message_id = row["message_id"]
    if not message_id:
        return
    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(content=text, view=None)
    except Exception:
        log.info(
            "idle lobby: could not retire message %s for game %s",
            message_id, row["game_id"], exc_info=True,
        )


async def _cancel_idle_lobby(bot, db, row, channel, dials: IdleLobbyDials, payload: dict) -> None:
    """Close a lobby that never filled: archive without pay, stop its view,
    retire its message. ``end_game``'s DELETE-first claim means a host pressing
    Start on the same tick wins or loses cleanly, never both."""
    from bot_modules.games.utils.game_manager import end_game  # noqa: PLC0415

    game_id = row["game_id"]
    game_type = str(row["game_type"])
    ended = await end_game(db, game_id, reason=LOBBY_TIMEOUT_REASON)
    if ended is None:
        return  # someone started or ended it between the read and now
    views = getattr(bot, "active_views", None)
    view = views.pop(game_id, None) if isinstance(views, dict) else None
    stop = getattr(view, "stop", None)
    if callable(stop):
        try:
            stop()
        except Exception:
            log.debug("idle lobby: view stop failed for %s", game_id, exc_info=True)
    log.info("Idle lobby %s (%s) closed after %ss with %d joined",
             game_id, game_type, dials.cancel_seconds, lobby_roster_size(payload))
    if channel is not None:
        await _retire_lobby_message(
            bot, channel, row,
            build_idle_cancel_notice(
                game_type, dials=dials, min_players=lobby_min_players(game_type, payload),
            ),
        )


async def _process_lobby(
    bot, db, row, now: float, *,
    dials: IdleLobbyDials | None = None,
    guild_id: int | None = None,
    roles: dict[int, tuple[bool, int | None]] | None = None,
) -> None:
    payload = json.loads(row["payload"]) if row["payload"] else {}
    game_id = row["game_id"]
    game_type = str(row["game_type"])
    opened_at = opened_epoch(row)

    if dials is not None and idle_cancel_due(game_type, payload, opened_at, now, dials):
        channel = await resolve_bot_channel(bot, int(row["channel_id"]))
        await _cancel_idle_lobby(bot, db, row, channel, dials, payload)
        return

    # The Game Night ping: once, the first tick the board exists. A row whose
    # launcher hasn't written message_id yet is simply looked at again next
    # tick — there is nothing to link at until then.
    if not payload.get(GAME_NIGHT_PINGED_FLAG) and row["message_id"]:
        if guild_id is None:
            guild_id = await resolve_guild_id(db, row)
        await _game_night_ping(bot, db, row, payload, guild_id, {} if roles is None else roles)

    # Countdown auto-start (clapback-8): the sweep starts the game itself when
    # the game can be, and only nudges when it can't.
    starter = auto_starter_for(bot, game_type)
    if starter is not None and auto_start_due(game_type, payload, now):
        channel = await resolve_bot_channel(bot, int(row["channel_id"]))
        if channel is not None:
            try:
                started = bool(await starter(row, payload, channel))
            except Exception:
                log.exception("auto-start: %s lobby %s failed to start", game_type, game_id)
                await set_payload_flag(db, game_id, AUTO_START_FAILED_FLAG)
                started = False
            if started:
                return
        # Not startable as it stands (or unreachable): the host is told the
        # ordinary way below, once.

    countdown_due = start_ping_due(payload, now)
    idle_due = dials is not None and idle_nudge_due(payload, opened_at, now, dials)
    if not countdown_due and not idle_due:
        return

    channel = await resolve_bot_channel(bot, int(row["channel_id"]))
    if channel is None:
        # Unreachable channel is terminal for this lobby — mark it sent so we
        # don't re-attempt every tick for the life of the lobby.
        log.warning("start ping: channel %s unreachable for game %s", row["channel_id"], game_id)
        await mark_start_ping_sent(db, game_id)
        return

    # Claim before sending: a send that succeeds but whose flag write is lost
    # would double-ping on the next tick, which is the louder failure.
    await mark_start_ping_sent(db, game_id)
    host_id = int(row["host_id"])
    if countdown_due:
        joined = lobby_roster_size(payload)
        floor = lobby_min_players(game_type, payload)
        if starter is not None and joined < floor and not payload.get(AUTO_START_FAILED_FLAG):
            # The game would have started itself; the roster is what's short.
            try:
                await channel.send(
                    build_short_roster_nudge(
                        game_type, host_id, joined=joined, min_players=floor, dials=dials,
                    ),
                    allowed_mentions=host_only_mentions(host_id),
                )
            except Exception:
                log.warning("short-roster nudge failed for %s in channel %s", game_type, row["channel_id"], exc_info=True)
            return
        await send_start_ping(channel, game_type, host_id)
        return
    assert dials is not None and opened_at is not None
    try:
        await channel.send(
            build_idle_nudge(
                game_type, host_id,
                idle_minutes=int((now - opened_at) // 60),
                dials=dials,
                min_players=lobby_min_players(game_type, payload),
            ),
            allowed_mentions=host_only_mentions(host_id),
        )
    except Exception:
        log.warning("idle nudge failed for %s in channel %s", game_type, row["channel_id"], exc_info=True)


async def _dials_for(db, guild_id: int, cache: dict[int, IdleLobbyDials]) -> IdleLobbyDials:
    """The idle dials for a guild, read once per guild per tick."""
    if guild_id not in cache:
        cache[guild_id] = await read_idle_dials(db, guild_id)
    return cache[guild_id]


async def game_start_ping_loop(bot) -> None:
    """Poll open lobbies: ping the Game Night role for each new board, start
    the games that start themselves, nudge hosts whose start time has arrived,
    nudge idle countdown-less lobbies, and close the ones that never filled.

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
            dials_cache: dict[int, IdleLobbyDials] = {}
            roles_cache: dict[int, tuple[bool, int | None]] = {}
            for row in rows:
                try:
                    guild_id = await resolve_guild_id(db, row)
                    dials = await _dials_for(db, guild_id, dials_cache)
                    await _process_lobby(
                        bot, db, row, now, dials=dials, guild_id=guild_id, roles=roles_cache,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("start ping: lobby %s failed to process", row["game_id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("game_start_ping_loop iteration error")
        await asyncio.sleep(POLL_SECONDS)
