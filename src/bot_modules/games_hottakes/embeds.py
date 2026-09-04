"""Embed builders for the Hot Takes cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_JOINING,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.round_pacing import TIMER_FIELD_NAME, timer_field_value
from bot_modules.games_hottakes.logic import (
    MIN_TAKES,
    VOTE_LABELS,
    compute_recap_summary,
)
from bot_modules.core.branding import apply_section_spacing

# The lobby says how the game goes and what "anonymous" means here, in the
# embed itself rather than behind the Help button (anon-tail-72/75). The
# mod-visibility line is the one FFA's reply help has always carried.
LOBBY_DESCRIPTION = (
    "Submit your spiciest take — all entries are anonymous.\n\n"
    "**How to play:** press **Submit Hot Take** (as many as you like); when "
    f"at least {MIN_TAKES} are in, the host presses **Start Voting** and the room rates "
    "each take 🧊 → 🔥, one at a time. The hottest take wins.\n"
    "Your name is never shown with your take — mods can still see who sent it."
)


def build_lobby_embed(
    host_name: str,
    submission_count: int = 0,
    color: discord.Color | None = None,
    start_at: int | None = None,
) -> discord.Embed:
    """Build the initial lobby embed shown when ``/hottakes`` is invoked.

    ``submission_count`` is rendered in the Submissions field; the cog
    keeps this value live by editing the field in place as takes arrive.
    ``color`` is the resolved guild accent; it falls back to the old
    :data:`PHASE_JOINING` constant when no guild is in scope. ``start_at``
    is the host's advertised start (``start_in``), shown as a live relative
    timestamp — advertising, not automation: the host still presses Start
    Voting.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['hottakes']} Hot Takes",
        description=LOBBY_DESCRIPTION,
        color=color or discord.Color(PHASE_JOINING),
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Submissions", value=str(submission_count), inline=True)
    if start_at:
        embed.add_field(name="⏰ Starting", value=f"<t:{int(start_at)}:R>", inline=True)
    embed.set_footer(text=f"{GAME_ICONS['hottakes']} Hot Takes • 👁 Anonymous")
    apply_section_spacing(embed)
    return embed


def build_vote_embed(
    take_text: str,
    take_num: int,
    total_takes: int,
    votes_by_user: dict[int, int],
    closed: bool = False,
    color: discord.Color | None = None,
    advance_at: int | None = None,
) -> discord.Embed:
    """Build the per-round voting embed shown alongside the vote buttons.

    Renders the take text, a horizontal bar chart of vote counts per
    option, and a progress indicator. ``closed`` flips the title
    suffix to ``— ROUND OVER``. ``color`` is the resolved guild accent,
    used for both the open and closed states (voting rounds are not a
    win/loss, so both are accent); it falls back to the old
    :data:`PHASE_PLAYING` / :data:`PHASE_RESULTS` constants when no
    guild is in scope. ``advance_at`` (epoch) adds the shared live
    countdown field while the take's timer is running.
    """
    title = f"{GAME_ICONS['hottakes']} Hot Take #{take_num}"
    if closed:
        title += " — Round Over"
    embed = discord.Embed(
        title=title,
        color=color or discord.Color(PHASE_RESULTS if closed else PHASE_PLAYING),
    )
    embed.add_field(
        name="Take", value=discord.utils.escape_markdown(take_text), inline=False
    )

    vote_counts = [0] * len(VOTE_LABELS)
    for v in votes_by_user.values():
        if 0 <= v < len(VOTE_LABELS):
            vote_counts[v] += 1
    total = sum(vote_counts)

    bars = []
    for i, label in enumerate(VOTE_LABELS):
        bar, pct = build_bar(vote_counts[i], total)
        bars.append(f"{label}\n{bar} {pct} ({vote_counts[i]})")
    embed.add_field(name="Votes", value="\n".join(bars), inline=False)
    embed.add_field(
        name="Progress",
        value=f"Take {take_num}/{total_takes}",
        inline=False,
    )
    if advance_at and not closed:
        embed.add_field(name=TIMER_FIELD_NAME, value=timer_field_value(advance_at), inline=False)
    embed.set_footer(text=f"{GAME_ICONS['hottakes']} Hot Takes • 👁 Anonymous")
    apply_section_spacing(embed)
    return embed


def build_recap_embed(
    results: list[dict[str, Any]],
    color: discord.Color | None = None,
) -> discord.Embed | None:
    """Build the game-over recap embed for Hot Takes.

    Shows the hottest take, the coldest take, an optional Most Divisive
    pick (only when 2+ takes were voted on), plus Total Takes and Total
    Voters tallies. Returns ``None`` when ``results`` is empty so the
    cog can skip sending an empty embed — matching the old early-return
    in ``_post_recap``.

    ``color`` is the resolved guild accent. Hot Takes crowns a hottest
    take but declares no win/loss, so the recap is accent (not semantic
    green); it falls back to the old :data:`PHASE_RECAP` constant when
    no guild is in scope.
    """
    summary = compute_recap_summary(results)
    if summary is None:
        return None

    embed = discord.Embed(
        title=f"{GAME_ICONS['hottakes']} Hot Takes — Final Results",
        color=color or discord.Color(PHASE_RECAP),
    )
    hottest = summary["hottest"]
    coldest = summary["coldest"]
    embed.add_field(
        name="🔥 Hottest Take",
        value=f'"{discord.utils.escape_markdown(hottest["text"])}" (avg {hottest["avg"]:.1f}/5)',
        inline=False,
    )
    embed.add_field(
        name="🧊 Coldest Take",
        value=f'"{discord.utils.escape_markdown(coldest["text"])}" (avg {coldest["avg"]:.1f}/5)',
        inline=False,
    )

    most_divisive = summary["most_divisive"]
    if most_divisive is not None:
        embed.add_field(
            name="⚡ Most Divisive",
            value=f'"{most_divisive["text"]}"',
            inline=False,
        )

    embed.add_field(name="Total Takes", value=str(summary["total_takes"]), inline=True)
    embed.add_field(
        name="Total Voters", value=str(len(summary["total_voters"])), inline=True
    )
    apply_section_spacing(embed)
    return embed
