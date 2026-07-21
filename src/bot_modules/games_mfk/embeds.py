"""Embed builders for the Marry/Fornicate/Kiss cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Two embed shapes are exposed:

* :func:`build_lobby_embed` — the join-the-pool embed shown alongside
  the host/player buttons. Same shape on first render and after every
  Join toggle (only the participant list changes).
* :func:`build_assignments_embed` — the post-close embed listing each
  player's three randomly-drawn targets with the call-to-action
  ("Reply with your <Marry, Fornicate, Kiss> picks!").

The slot-line formatter (:func:`format_assignment_value`) is split out
so tests can assert on the bold-name layout without constructing a
Discord embed.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.games_mfk.logic import DEFAULT_LABELS


def _title_for(labels: list[str]) -> str:
    """Build the embed title from the labels list (e.g. ``"Marry, Fornicate, Kiss"``)."""
    return ", ".join(labels)


def build_lobby_embed(
    host_name: str,
    participants: list[str],
    labels: list[str] | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the lobby embed shown while players are joining.

    ``participants`` is a list of pre-resolved display names (the cog
    runs the resolution against the guild before calling). Empty list
    renders as ``"—"`` so the field always has a value.

    ``labels`` overrides the default categories — when supplied, both
    the title and the "Categories" field swap to the host's choices.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    labels = labels or DEFAULT_LABELS
    title_str = _title_for(labels)
    embed = discord.Embed(
        title=f"{GAME_ICONS['mfk']} {title_str}",
        color=color,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(
        name="Categories",
        value=" · ".join(f"**{lbl}**" for lbl in labels),
        inline=True,
    )
    pool_str = ", ".join(participants) if participants else "—"
    embed.add_field(
        name=f"Pool ({len(participants)})", value=pool_str, inline=False
    )
    embed.set_footer(text=f"{GAME_ICONS['mfk']} {title_str}")
    return embed


def format_assignment_value(target_names: list[str]) -> str:
    """Render the three target names as a bold ``·``-separated list.

    The cog assembles one of these per player and drops it in a field
    value. Split out so tests can assert on the layout without
    constructing the surrounding embed.
    """
    return " · ".join(f"**{name}**" for name in target_names)


def build_assignments_embed(
    player_assignments: list[tuple[str, list[str]]],
    labels: list[str] | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the post-close embed announcing each player's three targets.

    ``player_assignments`` is a list of ``(player_mention,
    [target_name_a, target_name_b, target_name_c])`` tuples — the cog
    resolves both the mentions and the target display names against
    the live guild before calling.

    ``labels`` matches the call sent to :func:`build_lobby_embed`; the
    embed title/footer track the host's chosen categories.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    labels = labels or DEFAULT_LABELS
    title_str = _title_for(labels)
    embed = discord.Embed(
        title=f"{GAME_ICONS['mfk']} {title_str} — Your Three Names",
        description=f"Reply with your {title_str} picks!",
        color=color,
    )
    for player_mention, target_names in player_assignments:
        embed.add_field(
            name=player_mention,
            value=format_assignment_value(target_names),
            inline=False,
        )
    embed.set_footer(text=f"{GAME_ICONS['mfk']} {title_str}")
    return embed
