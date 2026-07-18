"""Embed builders for the Name Your Price cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Per-round flow uses four embeds:

* :func:`build_start_embed` — initial "starting up" placeholder
* :func:`build_scenario_embed` — submission phase with live count
* :func:`build_reveal_embed` — sorted price ladder + summary stats
* :func:`build_vote_embed` — voting phase prompt
* :func:`build_round_results_embed` — round winners after voting
* :func:`build_recap_embed` — game-over summary with awards
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_JOINING,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games.utils.timer import format_deadline, now_plus
from bot_modules.games_price.logic import (
    format_price,
    ladder_stats,
    price_label,
)


def _footer(host_name: str) -> str:
    return f"{GAME_ICONS['price']} Name Your Price • Hosted by {host_name}"


def build_start_embed(
    host_name: str, round_num: int, total_rounds: int
) -> discord.Embed:
    """Initial "starting up" embed shown the instant ``/price`` is invoked.

    Holds the message slot while the first scenario is fetched.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} NAME YOUR PRICE",
        description=(
            f"Hosted by: **{discord.utils.escape_markdown(host_name)}** | "
            f"Round {round_num}/{total_rounds}"
        ),
        color=PHASE_JOINING,
    )
    embed.add_field(
        name="Status",
        value="Starting up — first scenario incoming...",
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_scenario_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    timer_secs: int,
    submitted: int,
    total_players: int | None = None,
) -> discord.Embed:
    """Per-round submission embed shown alongside the Name-Your-Price button.

    ``timer_secs`` is rendered as a Discord countdown timestamp.
    ``submitted`` / ``total_players`` drive the live submission counter
    that the cog refreshes after every modal submit.
    """
    embed = discord.Embed(
        title=(
            f"{GAME_ICONS['price']} NAME YOUR PRICE — "
            f"Round {round_num}/{total_rounds}"
        ),
        color=PHASE_PLAYING,
    )
    embed.add_field(
        name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False
    )
    embed.add_field(
        name="Scenario",
        value=f'# "{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )
    sub_text = f"💵 Submitted: **{submitted}**"
    if total_players is not None:
        sub_text += f"/{total_players}"
    embed.add_field(name="Submissions", value=sub_text, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_reveal_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    ladder: list[tuple[str, int]],
) -> discord.Embed:
    """Reveal embed shown after the submission timer expires.

    ``ladder`` is a list of ``(display_name, amount)`` already sorted
    by the cog (using :func:`logic.build_ladder` then resolving uids).
    Builds the price-ladder column and an optional stats footer when
    at least one price was submitted.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} REVEAL — Round {round_num}/{total_rounds}",
        color=PHASE_RESULTS,
    )
    embed.add_field(
        name="Scenario",
        value=f'"{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )

    lines: list[str] = []
    for name, amount in ladder:
        label = price_label(amount)
        # price_label embeds the flavor text already; for the ladder we
        # want a fixed-width left column, so format the price without the
        # flavor and tack the flavor suffix on after the name.
        base_price = format_price(amount)
        suffix = label[len(base_price):]
        lines.append(
            f"`{base_price:>12}` — **{discord.utils.escape_markdown(name)}**{suffix}"
        )
    embed.add_field(
        name="💵 Price Ladder", value="\n".join(lines) or "—", inline=False
    )

    amounts = [a for _, a in ladder]
    stats = ladder_stats(amounts)
    if stats is not None:
        spread = f"{format_price(stats['low'])} — {format_price(stats['high'])}"
        median = format_price(stats["median"])
        avg = format_price(stats["mean"])
        embed.add_field(
            name="📊 Stats",
            value=f"Spread: {spread}\nMedian: {median}\nAverage: {avg}",
            inline=False,
        )

    embed.set_footer(text=_footer(host_name))
    return embed


def build_vote_embed(
    host_name: str,
    scenario: str,
    round_num: int,
    total_rounds: int,
    timer_secs: int,
) -> discord.Embed:
    """Voting embed shown alongside the Reasonable/Unhinged select menus.

    Recaps the scenario and prompts voters to pick from the two
    categories.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} VOTE — Round {round_num}/{total_rounds}",
        color=PHASE_PLAYING,
    )
    embed.add_field(
        name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False
    )
    embed.add_field(
        name="Scenario",
        value=f'"{discord.utils.escape_markdown(scenario)}"',
        inline=False,
    )
    embed.add_field(
        name="Vote",
        value="Who had the **Most Reasonable** price? Who was the **Most Unhinged**?",
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_round_results_embed(
    host_name: str,
    round_num: int,
    total_rounds: int,
    reasonable_winner: str,
    reasonable_price: int,
    reasonable_votes: int,
    unhinged_winner: str,
    unhinged_price: int,
    unhinged_votes: int,
) -> discord.Embed:
    """Round-results embed shown after voting closes."""
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} ROUND {round_num} RESULTS",
        color=PHASE_RESULTS,
    )
    embed.add_field(
        name="🎯 Most Reasonable",
        value=(
            f"**{discord.utils.escape_markdown(reasonable_winner)}** "
            f"({format_price(reasonable_price)}) — "
            f"{reasonable_votes} vote{'s' if reasonable_votes != 1 else ''}"
        ),
        inline=False,
    )
    embed.add_field(
        name="🤯 Most Unhinged",
        value=(
            f"**{discord.utils.escape_markdown(unhinged_winner)}** "
            f"({format_price(unhinged_price)}) — "
            f"{unhinged_votes} vote{'s' if unhinged_votes != 1 else ''}"
        ),
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_recap_embed(
    host_name: str,
    rounds_played: int,
    player_count: int,
    awards: dict[str, tuple[str, str, str]],
    highlight: str | None,
) -> discord.Embed:
    """Game-over recap embed.

    ``awards`` is the cog-level dict of ``slug -> (label, name, detail)``
    — note the second element is already a resolved display name (the
    cog turns :func:`logic.compute_recap_awards`'s uid lists into names
    before calling this builder). ``highlight`` is the prebuilt
    sentence or ``None`` to omit the field.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['price']} NAME YOUR PRICE — GAME OVER",
        color=PHASE_RECAP,
    )
    embed.add_field(
        name="Summary",
        value=f"🎮 Rounds played: **{rounds_played}**\n👥 Players: **{player_count}**",
        inline=False,
    )
    award_lines: list[str] = []
    for _key, (label, name, detail) in awards.items():
        if name:
            award_lines.append(
                f"{label} **{discord.utils.escape_markdown(name)}** — {detail}"
            )
    if award_lines:
        embed.add_field(name="🏆 Awards", value="\n".join(award_lines), inline=False)
    if highlight:
        embed.add_field(name="💡 Highlight", value=highlight, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


__all__: list[str] = [
    "build_start_embed",
    "build_scenario_embed",
    "build_reveal_embed",
    "build_vote_embed",
    "build_round_results_embed",
    "build_recap_embed",
]


# Convenience type alias used by tests that want to peek at the
# typed-shape of an awards row (label, name, detail).
AwardRow = tuple[str, str, str]
