"""Embed builders for the Games Help panel.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Three embeds:

* :func:`build_help_embed` — the ``/games help`` overview: every game
  grouped (party / duels & group / rooms & tables), one line each.
* :func:`build_game_detail_embed` — one game's rules, floor, pacing and
  length, shown when it is picked from the panel's select menu.
* :func:`build_support_embed` — the ``/support`` invite card.
"""

from __future__ import annotations

from collections.abc import Iterable

import discord

from bot_modules.games.constants import BRAND_COLOR
from bot_modules.games_help.logic import (
    EMBED_FIELD_LIMIT,
    OTHER_COMMANDS_VALUE,
    SUPPORT_INVITE_URL,
    chunk_lines,
    game_detail,
    help_groups,
)
from bot_modules.core.branding import apply_section_spacing


def build_help_embed(
    color: "discord.Color | None" = None,
    *,
    extra_lines: Iterable[str] = (),
) -> discord.Embed:
    """Build the ``/games help`` overview embed.

    Groups come from :func:`help_groups` (which iterates ``GAME_ICONS``, the
    canonical registry, so a new game shows up on its own). A group longer
    than one field value spills into a continuation field; the whole thing
    is asserted under Discord's 25-field ceiling so the next game added
    fails a test here rather than a send in prod (discovery-7).

    ``extra_lines`` are the channel-native rooms the cog found open on this
    server (Survivor, Mahjong, Guess Who, the casino); they land in the
    Rooms & Tables group.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title="🌸 Community Games",
        description=(
            "Pick a game below to read the rules, how many players it needs and "
            "who keeps it moving — then press **Start Here** to launch it in this "
            "channel, or use the command shown."
        ),
        color=color,
    )

    for name, lines in help_groups(list(extra_lines)):
        for i, chunk in enumerate(chunk_lines(lines)):
            embed.add_field(
                name=name if i == 0 else f"{name} (cont.)",
                value=chunk,
                inline=False,
            )

    embed.add_field(name="⚙️ Other Commands", value=OTHER_COMMANDS_VALUE, inline=False)
    assert len(embed.fields) <= EMBED_FIELD_LIMIT, "help overview over Discord's field ceiling"

    embed.set_footer(text="Community Games • /games help")
    apply_section_spacing(embed)
    return embed


def build_game_detail_embed(
    key: str, color: "discord.Color | None" = None
) -> discord.Embed:
    """One game's card: the in-game ❓ Help text, the floor and pacing from
    the play registry, a rough length, and the command to type."""
    if color is None:
        color = discord.Color(BRAND_COLOR)
    d = game_detail(key)
    embed = discord.Embed(
        title=f"{d.icon} {d.name}",
        description=d.rules[:4096],
        color=color,
    )
    embed.add_field(name="👥 Players", value=d.floor, inline=True)
    embed.add_field(name="⏱️ Typical Length", value=d.length, inline=True)
    embed.add_field(
        name="🎛️ Pacing", value=f"{d.hosting_label} — {d.pacing}", inline=False,
    )
    start_lines = [f"`{d.command}`", *d.variant_lines]
    embed.add_field(name="▶️ Start With", value="\n".join(start_lines), inline=False)
    assert len(embed.fields) <= EMBED_FIELD_LIMIT
    embed.set_footer(text="Community Games • /games help")
    apply_section_spacing(embed)
    return embed


def build_support_embed(color: "discord.Color | None" = None) -> discord.Embed:
    """Build the ``/support`` invite embed."""
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title="🛟 Support Server",
        description=(
            f"Need help, want to report a bug, or share feedback?\n"
            f"Join us here: {SUPPORT_INVITE_URL}"
        ),
        color=color,
    )
    embed.set_footer(text="Dungeon Keeper • /support")
    return embed
