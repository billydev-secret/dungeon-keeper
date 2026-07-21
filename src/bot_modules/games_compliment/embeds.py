"""Embed builders for the Spin-the-Compliment cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Two embed shapes are exposed:

* :func:`build_lobby_embed` — the join-the-pool embed shown alongside
  the host/player buttons. Same shape on first render and after every
  Add-Me toggle (only the participant list changes).
* :func:`build_pairings_embed` — the post-close embed listing each
  ``giver → receiver`` mapping with the closing "deliver your
  compliment" call-to-action.

The line-formatter (:func:`format_pairing_line`) is split out so the
pairings text can be assembled deterministically in tests without
constructing a Discord embed.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR


def build_lobby_embed(host_name: str, participants: list[str], color: "discord.Color | None" = None) -> discord.Embed:
    """Build the lobby embed shown while players are joining.

    ``participants`` is a list of pre-resolved display names (the cog
    runs the resolution against the guild before calling). Empty list
    renders as ``"—"`` so the field always has a value.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} Spin the Compliment",
        color=color,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    pool_str = ", ".join(participants) if participants else "—"
    embed.add_field(
        name=f"Pool ({len(participants)})", value=pool_str, inline=False
    )
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
    return embed


def format_pairing_line(giver_mention: str, receiver_mention: str) -> str:
    """Format a single ``giver → receiver`` line for the pairings embed.

    Centralised so the arrow symbol/spacing can be tweaked in one place
    and so tests can assert on the line shape without rebuilding an
    embed.
    """
    return f"{giver_mention} → {receiver_mention}"


def build_pairings_embed(pairing_lines: list[str], color: "discord.Color | None" = None) -> discord.Embed:
    """Build the post-close embed announcing the pairings.

    ``pairing_lines`` are the pre-formatted ``giver → receiver`` strings
    from :func:`format_pairing_line` — the cog renders the mentions
    against the live guild before assembling the list.

    The trailing call-to-action ("Reply to deliver your compliment!") is
    appended unconditionally so even a 2-player game has the prompt.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['compliment']} Compliment Pairings",
        color=color,
    )
    body = "\n".join(pairing_lines)
    embed.description = f"{body}\n\n💛 Reply to deliver your compliment!"
    embed.set_footer(text=f"{GAME_ICONS['compliment']} Spin the Compliment")
    return embed
