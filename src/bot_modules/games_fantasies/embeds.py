"""Embed builders for the Fantasies & Dealbreakers cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games_fantasies.logic import compute_recap_summary


def build_lobby_embed(host_name: str, color: "discord.Color | None" = None) -> discord.Embed:
    """Build the lobby embed shown when ``/fantasies`` is invoked."""
    if color is None:
        color = discord.Color(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['fantasies']} Fantasies & Dealbreakers",
        description="Submit anonymously each round, then vote!",
        color=color,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.set_footer(text=f"{GAME_ICONS['fantasies']} Fantasies & Dealbreakers")
    return embed


def build_round_submit_embed(round_num: int, color: "discord.Color | None" = None) -> discord.Embed:
    """Build the embed shown above the submit-entry button each round."""
    if color is None:
        color = discord.Color(BRAND_COLOR)
    return discord.Embed(
        title=f"{GAME_ICONS['fantasies']} Round {round_num}",
        description="Submit your fantasy or dealbreaker anonymously!",
        color=color,
    )


def build_vote_embed(
    *,
    entry_text: str,
    entry_num: int,
    category: str,
    same_votes: list[int],
    nope_votes: list[int],
    total_entries: int = 0,
    closed: bool = False,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the per-entry voting embed shown alongside the vote buttons.

    Renders the entry text, a horizontal bar chart of "Same" vs "Not
    for me" votes, and an optional progress indicator. ``closed`` flips
    the title suffix to ``— VOTE CLOSED`` so the message can be edited
    in place when the round ends.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    total = len(same_votes) + len(nope_votes)
    bar_s, pct_s = build_bar(len(same_votes), total)
    bar_n, pct_n = build_bar(len(nope_votes), total)

    title = f"{GAME_ICONS['fantasies']} {category} #{entry_num}"
    if closed:
        title += " — Vote Closed"
    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="Entry",
        value=discord.utils.escape_markdown(entry_text),
        inline=False,
    )
    embed.add_field(
        name="Votes",
        value=(
            f"✅ Same\n{bar_s} {pct_s} ({len(same_votes)})\n"
            f"❌ Not for me\n{bar_n} {pct_n} ({len(nope_votes)})"
        ),
        inline=False,
    )
    if total_entries:
        embed.add_field(
            name="Progress",
            value=f"Entry {entry_num}/{total_entries}",
            inline=False,
        )
    embed.set_footer(text=f"{GAME_ICONS['fantasies']} Fantasies & Dealbreakers")
    return embed


def build_recap_embed(results: list[dict[str, Any]], color: "discord.Color | None" = None) -> discord.Embed | None:
    """Build the final recap embed for Fantasies & Dealbreakers.

    Returns ``None`` when ``results`` is empty so the cog can skip
    sending an empty embed — matching the old early-return in
    ``_post_recap``.
    """
    if color is None:
        color = discord.Color(BRAND_COLOR)
    summary = compute_recap_summary(results)
    if summary is None:
        return None

    embed = discord.Embed(
        title=f"{GAME_ICONS['fantasies']} Fantasies & Dealbreakers — Results",
        color=color,
    )

    most_shared = summary["most_shared"]
    embed.add_field(
        name="🌟 Most Universally Shared",
        value=f'"{most_shared["text"]}" ({most_shared["same_pct"]:.0%} Same)',
        inline=False,
    )

    most_polar = summary["most_polar"]
    embed.add_field(
        name="⚡ Most Polarizing",
        value=f'"{most_polar["text"]}"',
        inline=False,
    )

    biggest_outlier = summary["biggest_outlier"]
    embed.add_field(
        name="🏔️ Biggest Outlier",
        value=f'"{biggest_outlier["text"]}" ({biggest_outlier["same_pct"]:.0%} Same)',
        inline=False,
    )

    embed.add_field(
        name="Total Submissions",
        value=str(summary["total_results"]),
        inline=True,
    )
    embed.add_field(
        name="Total Voters",
        value=str(len(summary["total_voters"])),
        inline=True,
    )

    return embed
