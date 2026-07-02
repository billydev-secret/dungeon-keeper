"""Embed builders for the Games Help cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Two embeds:

* :func:`build_help_embed` — the ``/games-help`` lobby listing every
  game with its slash command and one-line description.
* :func:`build_support_embed` — the ``/games-support`` invite card.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, GAME_NAMES, BRAND_COLOR
from bot_modules.games_help.logic import (
    GAME_COMMANDS,
    GAME_DESCRIPTIONS,
    OTHER_COMMANDS_VALUE,
    SUPPORT_INVITE_URL,
)


def build_help_embed(colour: "discord.Colour | None" = None) -> discord.Embed:
    """Build the ``/games-help`` embed.

    Iterates ``GAME_ICONS`` (the canonical game registry) so any game
    added there shows up automatically. The slash command and
    description come from :mod:`bot_modules.games_help.logic`; missing
    entries fall back to ``"/<key>"`` and an empty description rather
    than crashing — but the alignment test in
    ``tests/test_games_help_logic.py`` ensures they're always present.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    embed = discord.Embed(
        title="🌸 Community Games",
        description="All available game modes. Start one with `/games play <game>` (or the command shown).",
        color=colour,
    )

    for key in GAME_ICONS:
        icon = GAME_ICONS[key]
        name = GAME_NAMES.get(key, key)
        cmd = GAME_COMMANDS.get(key, f"/{key}")
        desc = GAME_DESCRIPTIONS.get(key, "")
        embed.add_field(
            name=f"{icon} {name}",
            value=f"`{cmd}` — {desc}",
            inline=False,
        )

    embed.add_field(
        name="⚙️ Other Commands",
        value=OTHER_COMMANDS_VALUE,
        inline=False,
    )

    embed.set_footer(text="Community Games • /games help")
    return embed


def build_support_embed(colour: "discord.Colour | None" = None) -> discord.Embed:
    """Build the ``/games-support`` invite embed."""
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    embed = discord.Embed(
        title="🛟 Support Server",
        description=(
            f"Need help, want to report a bug, or share feedback?\n"
            f"Join us here: {SUPPORT_INVITE_URL}"
        ),
        color=colour,
    )
    embed.set_footer(text="Community Games • /games support")
    return embed
