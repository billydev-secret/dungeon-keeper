"""Static lookup tables and pure helpers for the Games Help panel.

These back the ``/games help`` slash command — one entry per game key in
:data:`bot_modules.games.constants.GAME_ICONS`. They live in their own
module so the cog stays a thin Discord-glue shim and so tests can assert
key alignment without spinning up Discord.

The alignment is load-bearing: if a game gets added to ``GAME_ICONS``
without a matching ``GAME_COMMANDS`` and ``GAME_DESCRIPTIONS`` entry,
the help panel will fall back to ``"/<key>"`` and an empty description —
silent UX rot. The test in ``tests/test_games_help_logic.py`` catches that.

The panel (2026-09-04, platform-25 / discovery-7) is a grouped overview —
party games, duels & group games, rooms & tables — with a select menu that
opens one game's rules, floor, pacing and length, and a Start button that
goes through the shared launch guard. :func:`help_groups`,
:func:`select_options` and :func:`game_detail` are the pure halves the
embeds and view render from.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from bot_modules.games.constants import (
    DUEL_GAME_KEYS,
    GAME_ICONS,
    GAME_NAMES,
    GAME_TYPICAL_LENGTH,
    HOSTING_LABEL,
    HOW_TO_PLAY,
    hosting_kind,
    play_floor,
    play_pacing,
)

SUPPORT_INVITE_URL = "https://discord.gg/7gfbYYkH"

# Discord's ceilings the panel builds against: fields per embed, options per
# select menu, characters per field value.
EMBED_FIELD_LIMIT = 25
SELECT_OPTION_LIMIT = 25
FIELD_VALUE_LIMIT = 1024

# Slash command name for each game. The party games launch under the
# ``/games play`` group; the duel/lobby/standalone games (pressure, quickdraw,
# chicken, hot potato, musical chairs, risky_roll) have their own entry points.
GAME_COMMANDS: dict[str, str] = {
    "ffa": "/games play ffa",
    "ffa_banner": "/games play ffa_banner",
    "traditional": "/games play traditional",
    "compliment": "/games play compliment",
    "mfk": "/games play mfk",
    "wyr": "/games play wyr",
    "nhie": "/games play nhie",
    "mlt": "/games play mlt",
    "ttl": "/games play twotruths",
    "hottakes": "/games play hottakes",
    "story": "/games play story",
    "ama": "/games play ama",
    "fantasies": "/games play fantasies",
    "price": "/games play price",
    "rushmore": "/games play rushmore",
    "clapback": "/games play clapback",
    "legitlibs": "/games play legitlibs",
    "pressure": "/games pressure challenge",
    "quickdraw": "/games quickdraw challenge",
    "chicken": "/games chicken start",
    "hot_potato": "/games hotpotato challenge",
    "hot_potato_group": "/games hotpotatogroup start",
    "musical_chairs": "/games musicalchairs start",
    "risky_roll": "/risky start",
}

# Short one-line descriptions for the help list.
GAME_DESCRIPTIONS: dict[str, str] = {
    "ffa": "A Truth or Dare prompt drops — reply anonymously right in the channel.",
    "ffa_banner": "Just drops a Truth or Dare prompt card in the channel for open chat.",
    "traditional": "Classic truth or dare with SFW/NSFW categories.",
    "compliment": "Random pairings — give your match a compliment.",
    "mfk": "Assign three names to each player. You know the rest.",
    "wyr": "Vote between two options each round.",
    "nhie": "Guilty or innocent? Find out who has done what.",
    "mlt": "Vote on who fits each prompt the best.",
    "ttl": "Submit two truths and one lie. Fool the group.",
    "hottakes": "Submit anonymous opinions, rate them 🧊 to 🔥.",
    "story": "Take turns writing one sentence to build a story.",
    "ama": "Ask the hot seat player anything — anonymously.",
    "fantasies": "Submit anonymously, then vote Same or Not for me.",
    "price": "Name your price for absurd scenarios — vote on the most unhinged.",
    "rushmore": "Snake-draft your top 4 picks — no duplicates allowed.",
    "clapback": "Write the funniest answer head-to-head, vote for the best.",
    "legitlibs": (
        "Fill in the blanks to complete a story — everyone gets their "
        "own unhinged version."
    ),
    "pressure": "1v1 pressure duel — pump the gauge, don't be the one who pops it.",
    "quickdraw": "1v1 fastest-finger duel — draw on the signal, but fire early and you lose.",
    "chicken": "Duel or group — a meter climbs to a crash; bail before it blows.",
    "hot_potato": "1v1 — pass the bomb back and forth; whoever's holding it at zero loses.",
    "hot_potato_group": "Group lobby — the bomb hops the circle until it detonates on someone.",
    "musical_chairs": "3+ players — when the music stops, hit Sit fast; slowest out each round.",
    "risky_roll": "Roll the dice — the highest and lowest rolls face off with a question.",
}

# ``/support`` is a top-level command; the ``/games support`` subcommand it
# replaced never existed under that name (platform-25).
OTHER_COMMANDS_VALUE: str = (
    "`/games help` — This panel\n"
    "`/support` — Join the support Discord server\n"
    "`/recap` — Recap of the current game night\n"
    "`/games end` — End the game running in this channel\n"
    "`/games join` · `/games leave` — Hop into or out of a running game"
)

# Display variants folded into their base game's listing: the banner card is
# FFA's ``kind`` option, not a game of its own (discovery-7).
FOLDED_VARIANTS: dict[str, str] = {"ffa_banner": "ffa"}

# The standalone games with no ``/games play`` door — always listed, with
# the channel appended by ``room_help_lines`` when the guild has one wired.
WHISPER_ROOM_LINE = (
    "💌 **Whisper** — `/whisper optin`, then send anonymous whispers to "
    "members who opted in"
)


def party_keys() -> list[str]:
    """GAME_ICONS order, minus the duels and the folded variants."""
    return [
        k for k in GAME_ICONS
        if k not in DUEL_GAME_KEYS and k not in FOLDED_VARIANTS
    ]


def duel_keys() -> list[str]:
    return [k for k in GAME_ICONS if k in DUEL_GAME_KEYS]


def listing_line(key: str) -> str:
    """``emoji Name — /command`` for the grouped overview."""
    return f"{GAME_ICONS.get(key, '🎮')} **{GAME_NAMES.get(key, key)}** — `{GAME_COMMANDS.get(key, f'/{key}')}`"


def help_groups(extra_lines: list[str] | tuple[str, ...] = ()) -> list[tuple[str, list[str]]]:
    """The overview's groups as ``(field name, lines)`` in display order.

    ``extra_lines`` are the channel-native rooms (Survivor, Mahjong, Guess
    Who, the casino — whatever the cog found open on this server); Whisper
    is always there since its door is a command, not a channel.
    """
    return [
        ("🎉 Party Games", [listing_line(k) for k in party_keys()]),
        ("⚔️ Duels & Group Games", [listing_line(k) for k in duel_keys()]),
        ("🏠 Rooms & Tables", [*extra_lines, WHISPER_ROOM_LINE]),
    ]


def chunk_lines(lines: list[str], limit: int = FIELD_VALUE_LIMIT) -> list[str]:
    """Join *lines* into as few strings as fit under *limit* characters
    each — a group longer than one field value spills into a continuation
    field rather than a 400 from Discord."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if current and len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def select_options() -> list[tuple[str, str, str]]:
    """``(key, label, description)`` for the game picker — one per game,
    folded variants excluded so the menu stays under Discord's 25."""
    out = []
    for key in GAME_ICONS:
        if key in FOLDED_VARIANTS:
            continue
        label = f"{GAME_ICONS[key]} {GAME_NAMES.get(key, key)}"
        out.append((key, label[:100], play_floor(key)))
    assert len(out) <= SELECT_OPTION_LIMIT, "game picker over Discord's option ceiling"
    return out


@dataclass(frozen=True)
class GameDetail:
    key: str
    icon: str
    name: str
    rules: str
    floor: str
    pacing: str
    hosting_label: str
    length: str
    command: str
    startable: bool
    variant_lines: tuple[str, ...]


def game_detail(key: str) -> GameDetail:
    """Everything the detail embed shows for one game.

    ``startable`` is whether the panel's Start button can launch it — only the
    ``/games play`` games have a headless ``launch()`` registered in
    ``bot.game_launchers``; a duel needs an opponent picked at the command.
    """
    variants = tuple(
        f"`{GAME_COMMANDS[v]}` — {GAME_DESCRIPTIONS.get(v, '')}"
        for v, base in FOLDED_VARIANTS.items()
        if base == key
    )
    return GameDetail(
        key=key,
        icon=GAME_ICONS.get(key, "🎮"),
        name=GAME_NAMES.get(key, key),
        rules=HOW_TO_PLAY.get(key) or GAME_DESCRIPTIONS.get(key, ""),
        floor=play_floor(key),
        pacing=play_pacing(key),
        hosting_label=HOSTING_LABEL[hosting_kind(key)],
        length=GAME_TYPICAL_LENGTH.get(key, "Varies"),
        command=GAME_COMMANDS.get(key, f"/{key}"),
        startable=GAME_COMMANDS.get(key, "").startswith("/games play "),
        variant_lines=variants,
    )


def survivor_help_line(
    conn: sqlite3.Connection, guild_id: int, now: float
) -> str | None:
    """One pointer at the Survivor channel while its door is open.

    Survivor is channel-native — no ``/games play`` entry, and its main-chat
    echo was removed on purpose (2026-08-20: the panel is the advertisement)
    — so ``/games help`` was the one place a member could look and not find
    it (2026-09-02 review). Returns None with no live season, no wired
    channel, or a ``closed`` late-entry season past Week 1 kickoff.
    """
    from bot_modules.services.survivor_service import get_active_season
    from bot_modules.survivor.logic import elapsed_weeks

    season = get_active_season(conn, guild_id)
    if season is None:
        return None
    channel_id = int(season["config"].get("channel_id") or 0)
    if not channel_id:
        return None
    if season["config"]["late_entry"] == "closed" and elapsed_weeks(
        conn, season["season_year"], now
    ):
        return None
    return (
        f"🏈 **Survivor** — the NFL pick'em season is open in <#{channel_id}>: "
        "join from the panel there, one team a week, no team twice"
    )


def room_help_lines(conn: sqlite3.Connection, guild_id: int) -> list[str]:
    """Pointers at the rooms that live in a configured channel — Guess Who
    and the casino — for the guilds that have one wired. A room with no
    channel is closed, so it gets no line rather than a dead pointer."""
    from bot_modules.services.casino_service import load_casino_settings
    from bot_modules.services.guess_repo import get_guess_config

    lines: list[str] = []
    guess_channel = get_guess_config(conn, guild_id).guess_channel_id
    if guess_channel:
        lines.append(
            f"🖼️ **Guess Who** — `/guess submit` posts a cropped photo of you in "
            f"<#{guess_channel}>; the room guesses who it is"
        )
    casino_channel = load_casino_settings(conn, guild_id).channel_id
    if casino_channel:
        lines.append(
            f"🎰 **Casino** — coinflip, slots, roulette and the derby from the "
            f"panel in <#{casino_channel}>"
        )
    return lines
