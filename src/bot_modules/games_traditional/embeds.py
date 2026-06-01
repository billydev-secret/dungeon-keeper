"""Embed builders for the Truth-or-Dare cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.games_traditional.logic import CAT_LABELS, summarize_asked_by_category

_RESULTS_GREY = 0x808080


def build_tod_embed(
    host_name: str, payload: dict[str, Any], closed: bool = False
) -> discord.Embed:
    """Build the main lobby embed shown alongside the host/player buttons.

    ``closed`` flips the title suffix to ``— GAME OVER``; the rest of the
    rendering is unchanged so the game message can be edited in place
    once the round ends.
    """
    title = f"{GAME_ICONS['traditional']} TRUTH OR DARE"
    if closed:
        title += " — GAME OVER"
    embed = discord.Embed(title=title, color=BRAND_COLOR)
    embed.add_field(name="Host", value=host_name, inline=True)

    participants: list = payload.get("participants", [])
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)

    asked: dict[str, str] = payload.get("asked", {})
    embed.add_field(name="Questions Asked", value=str(len(asked)), inline=True)

    embed.set_footer(text=f"{GAME_ICONS['traditional']} Truth or Dare")
    return embed


def build_recap_embed(payload: dict[str, Any]) -> discord.Embed:
    """Build the game-over recap embed.

    Shows totals plus a per-category breakdown for any non-zero
    category — categories with zero questions are skipped to keep the
    embed compact.
    """
    participants: list = payload.get("participants", [])
    asked: dict[str, str] = payload.get("asked", {})
    total_q = len(asked)
    by_cat = summarize_asked_by_category(asked)

    embed = discord.Embed(
        title=f"{GAME_ICONS['traditional']} TRUTH OR DARE — GAME OVER",
        color=_RESULTS_GREY,
    )
    embed.add_field(name="Total Questions Asked", value=str(total_q), inline=True)
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)
    for cat, count in by_cat.items():
        if count:
            label = CAT_LABELS.get(cat, cat)
            embed.add_field(name=label, value=str(count), inline=True)
    return embed


def build_lobby_embed(host_name: str) -> discord.Embed:
    """Build the initial lobby embed shown when ``/traditional`` is invoked.

    Distinct from :func:`build_tod_embed` only in the description and
    the hard-coded zero counts (no payload required yet).
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['traditional']} TRUTH OR DARE",
        description="Select your preferences below to join!",
        color=BRAND_COLOR,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Participants", value="0", inline=True)
    embed.add_field(name="Questions Asked", value="0", inline=True)
    embed.set_footer(text=f"{GAME_ICONS['traditional']} Truth or Dare")
    return embed


def format_question_post(category: str, target_mention: str, question: str) -> str:
    """Format the public message announcing a question to the channel.

    Centralized so the wording can be tested and so future variants
    (DM mode, anonymous mode) can share the same template.
    """
    label = CAT_LABELS.get(category, category)
    return (
        f"**{GAME_ICONS['traditional']} {label}** for {target_mention}\n"
        f"**{question}**"
    )
