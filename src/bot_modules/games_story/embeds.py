"""Embed builders for the Story Builder cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.

The turn / reveal embeds need to render Discord member display names
from raw uids; rather than depend on a ``Guild`` object, they take a
``name_resolver`` callable (``int -> str``) so tests can pass a dict.
"""

from __future__ import annotations

from typing import Callable

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR

NameResolver = Callable[[int], str]


def build_lobby_embed(
    host_name: str, visibility: str, max_sentences: int,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the ``/story`` join-lobby embed.

    Players field starts at 0 / "—"; the join button updates it in
    place by editing field index 0. Host + mode summary fields are
    static for the life of the lobby.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['story']} Story Builder",
        description="Join to contribute to the story!",
        color=color,
    )
    embed.add_field(name="Writers (0)", value="—", inline=False)
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(
        name="Mode",
        value=f"{visibility} | {max_sentences} sentences",
        inline=True,
    )
    embed.set_footer(text=f"{GAME_ICONS['story']} Story Builder")
    return embed


def build_turn_embed(
    sentence_count: int,
    max_sentences: int,
    current_player_id: int,
    turn_order: list[int],
    name_resolver: NameResolver,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the per-turn "story in progress" embed.

    Renders three fields: a Progress badge (``sentence_count+1/max``),
    a Currently-writing badge, and a multi-line Turn Order showing all
    writers with the active one highlighted (``▸ name ✍️``). All
    rendered names go through ``discord.utils.escape_markdown`` so a
    writer whose nick contains markdown can't break the embed.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['story']} Story in Progress",
        color=color,
    )
    current_name = name_resolver(current_player_id)
    embed.add_field(
        name="Progress",
        value=f"Sentence {sentence_count + 1}/{max_sentences}",
        inline=True,
    )
    embed.add_field(
        name="Currently writing",
        value=f"**{current_name}** ✍️",
        inline=True,
    )

    order_lines: list[str] = []
    for pid in turn_order:
        name = discord.utils.escape_markdown(name_resolver(pid))
        if pid == current_player_id:
            order_lines.append(f"**▸ {name}** ✍️")
        else:
            order_lines.append(f"  {name}")
    embed.add_field(name="Turn Order", value="\n".join(order_lines), inline=False)
    embed.set_footer(text=f"{GAME_ICONS['story']} Story Builder")
    return embed


def build_complete_story_embed(
    story_text: str, player_count: int, sentence_count: int,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the full-story reveal embed.

    ``story_text`` is expected to already be escaped and truncated by
    :func:`bot_modules.games_story.logic.assemble_story_text`. The
    "Community Original" footer-field summarises the participant
    and sentence counts.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['story']} The Complete Story",
        color=color,
    )
    embed.description = f"*{story_text}*"
    embed.add_field(
        name="A Community Original",
        value=f"{player_count} writers | {sentence_count} sentences",
        inline=False,
    )
    return embed


def build_attribution_embed(
    chunks: list[list[str]],
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the "Who Wrote What" attribution embed.

    ``chunks`` is the output of
    :func:`bot_modules.games_story.logic.chunk_attribution_lines` —
    each chunk becomes one field whose value is the lines joined with
    newlines. When there are multiple chunks the field names get a
    ``(pt. N)`` suffix so readers can follow long stories spilled
    across fields.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['story']} Who Wrote What",
        color=color,
    )
    for i, chunk in enumerate(chunks, start=1):
        name = "Sentences" if len(chunks) == 1 else f"Sentences (pt. {i})"
        embed.add_field(name=name, value="\n".join(chunk), inline=False)
    embed.set_footer(text=f"{GAME_ICONS['story']} Story Builder")
    return embed
