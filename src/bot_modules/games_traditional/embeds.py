"""Embed builders for the Truth-or-Dare cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.games_traditional.logic import (
    CATEGORIES,
    CAT_LABELS,
    question_pool_size,
    summarize_asked_by_category,
)

# Per-category card styling: (emoji, accent color). Truths are cool-toned
# and inquisitive; dares are warm-toned and bold; the NSFW variants get
# spicier glyphs and deeper colors so the card reads at a glance.
_CARD_STYLE: dict[str, tuple[str, int]] = {
    "sfw_truth":  ("💭", 0x4E9AF1),  # blue
    "sfw_dare":   ("🔥", 0xFF6B35),  # orange
    "nsfw_truth": ("💋", 0x9B59B6),  # purple
    "nsfw_dare":  ("😈", 0xED4245),  # red
}
_CARD_FALLBACK: tuple[str, int] = ("🎲", BRAND_COLOR)


def build_tod_embed(
    host_name: str,
    payload: dict[str, Any],
    closed: bool = False,
    names: dict[str, str] | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the main lobby embed shown alongside the host/player buttons."""
    if color is None:
        color = discord.Color(BRAND_COLOR)
    title = f"{GAME_ICONS['traditional']} Truth or Dare"
    if closed:
        title += " — Game Over"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Host", value=host_name, inline=True)

    participants: list = payload.get("participants", [])
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)

    asked: dict[str, str] = payload.get("asked", {})
    prefs: dict[str, list[str]] = payload.get("prefs", {})

    total_pool = question_pool_size(prefs, asked)
    asked_value = f"{len(asked)} / {total_pool}" if total_pool else str(len(asked))
    embed.add_field(name="Questions Asked", value=asked_value, inline=True)

    for cat in CATEGORIES:
        members_in_cat = [
            (names.get(uid, uid) if names else uid)
            for uid, cats in prefs.items()
            if cat in cats
        ]
        value = "\n".join(members_in_cat) if members_in_cat else "—"
        embed.add_field(name=CAT_LABELS[cat], value=value, inline=True)

    embed.set_footer(text=_footer_text(payload.get("single_choice", False)))
    return embed


def _footer_text(single_choice: bool) -> str:
    """Footer line, tagged with the pick mode when single-choice is on."""
    base = f"{GAME_ICONS['traditional']} Truth or Dare"
    return f"{base} • One category each" if single_choice else base


def build_recap_embed(
    payload: dict[str, Any],
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the game-over recap embed.

    Shows totals plus a per-category breakdown for any non-zero
    category — categories with zero questions are skipped to keep the
    embed compact.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    participants: list = payload.get("participants", [])
    asked: dict[str, str] = payload.get("asked", {})
    total_q = len(asked)
    by_cat = summarize_asked_by_category(asked)

    embed = discord.Embed(
        title=f"{GAME_ICONS['traditional']} Truth or Dare — Game Over",
        color=color,
    )
    embed.add_field(name="Total Questions Asked", value=str(total_q), inline=True)
    embed.add_field(name="Participants", value=str(len(participants)), inline=True)
    bank_asked = payload.get("bank_asked", 0)
    if bank_asked:
        embed.add_field(name="Bank Round Questions", value=str(bank_asked), inline=True)
    for cat, count in by_cat.items():
        if count:
            label = CAT_LABELS.get(cat, cat)
            embed.add_field(name=label, value=str(count), inline=True)
    return embed


def build_lobby_embed(
    host_name: str,
    color: "discord.Color | None" = None,
    single_choice: bool = False,
) -> discord.Embed:
    """Build the initial lobby embed shown when ``/traditional`` is invoked.

    Distinct from :func:`build_tod_embed` only in the description and
    the hard-coded zero counts (no payload required yet). When
    ``single_choice`` is on, the prompt and footer say so up front.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    description = (
        "Pick the one category you're up for below to join!"
        if single_choice
        else "Select your preferences below to join!"
    )
    embed = discord.Embed(
        title=f"{GAME_ICONS['traditional']} Truth or Dare",
        description=description,
        color=color,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Participants", value="0", inline=True)
    embed.add_field(name="Questions Asked", value="0", inline=True)
    embed.set_footer(text=_footer_text(single_choice))
    return embed


def build_question_embed(
    category: str,
    question: str,
    target_name: str | None = None,
) -> discord.Embed:
    """Build the question "card" posted to the channel when a host asks.

    Each category gets its own emoji + accent color so the card reads at
    a glance. The target's ping lives in the message ``content`` (embeds
    never fire notifications), so this card carries the display name in
    its author line instead.

    Unknown category keys (stale payloads) fall back to a neutral style
    rather than crashing — callers can pass legacy keys safely.

    The question's own markdown is left intact so embeds render it (bold,
    italic, etc.). Newlines are indented so a multi-line question stays
    inside the blockquote instead of spilling out after the first line.
    """
    emoji, color = _CARD_STYLE.get(category, _CARD_FALLBACK)
    label = CAT_LABELS.get(category, category)

    quoted = "> " + question.replace("\n", "\n> ")
    embed = discord.Embed(
        title=f"{emoji} {label.upper()}",
        description=quoted,
        color=color,
    )
    if target_name:
        embed.set_author(name=f"For {target_name}")
    embed.set_footer(text=f"{GAME_ICONS['traditional']} Truth or Dare")
    return embed
