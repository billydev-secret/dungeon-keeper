"""Embed builders for the Would-You-Rather cog.

These functions accept plain primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.

WYR's main embed (:func:`build_wyr_embed`) shows the current question,
both options, and live vote bars. The cog edits the same message across
states (open → round-over → closed) by re-calling this builder with the
right ``closed`` flag. :func:`build_closed_embed` is a small wrapper
that produces the final "CLOSED" variant used by the close-game flow.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_PLAYING,
)
from bot_modules.games.utils.live_bar import build_bar


def build_wyr_embed(
    host_name: str,
    option_a: str,
    option_b: str,
    votes_a: list,
    votes_b: list,
    anonymous: bool,
    round_num: int,
    closed: bool = False,
    revealed: bool = False,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the main WYR round embed.

    ``closed`` flips the title suffix to ``— ROUND OVER``; ``revealed``
    appends voter mentions under each option's bar. Both flags can
    combine.

    Per the 2026-07-21 embed-color ruling, WYR (a voting game with no
    single winner) always uses the guild accent — pass it via ``color``.
    When ``color`` is ``None`` (no guild in scope, or accent resolution
    failed) the embed falls back to the ``PHASE_PLAYING`` blue.

    ``host_name`` is currently accepted but not rendered — kept in the
    signature for parity with the other game embeds in this cluster.
    """
    total = len(votes_a) + len(votes_b)
    bar_a, pct_a = build_bar(len(votes_a), total)
    bar_b, pct_b = build_bar(len(votes_b), total)

    title = f"{GAME_ICONS['wyr']} WOULD YOU RATHER"
    if closed:
        title += " — ROUND OVER"
    embed = discord.Embed(title=title, color=color or discord.Color(PHASE_PLAYING))
    embed.add_field(name="Round", value=str(round_num), inline=False)
    esc = discord.utils.escape_markdown
    embed.add_field(name="🅰️", value=esc(option_a), inline=True)
    embed.add_field(name="🅱️", value=esc(option_b), inline=True)
    embed.add_field(name="​", value="​", inline=True)

    a_label = f"🅰️ {bar_a} {pct_a} ({len(votes_a)})"
    b_label = f"🅱️ {bar_b} {pct_b} ({len(votes_b)})"

    if revealed:
        a_names = ", ".join(f"<@{uid}>" for uid in votes_a) if votes_a else "—"
        b_names = ", ".join(f"<@{uid}>" for uid in votes_b) if votes_b else "—"
        a_label += f"\n{a_names}"
        b_label += f"\n{b_names}"

    embed.add_field(name="Votes", value=f"{a_label}\n{b_label}", inline=False)
    anon_badge = "  •  👁 Anonymous" if anonymous else ""
    embed.set_footer(text=f"{GAME_ICONS['wyr']} Would You Rather  •  Round {round_num}{anon_badge}")
    return embed


def build_closed_embed(
    host_name: str,
    option_a: str,
    option_b: str,
    votes_a: list,
    votes_b: list,
    anonymous: bool,
    round_num: int,
    revealed: bool = False,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the final ``CLOSED`` embed used by the close-game flow.

    Starts from :func:`build_wyr_embed` with ``closed=True`` (so the
    bars and labels match the round-over state), then rewrites the title
    suffix to ``— CLOSED``. The guild accent (``color``) is threaded
    through unchanged — the CLOSED variant no longer overrides to a
    distinct recap color. Centralized so the cog doesn't need to mutate
    Embed fields directly.
    """
    embed = build_wyr_embed(
        host_name=host_name,
        option_a=option_a,
        option_b=option_b,
        votes_a=votes_a,
        votes_b=votes_b,
        anonymous=anonymous,
        round_num=round_num,
        closed=True,
        revealed=revealed,
        color=color,
    )
    embed.title = f"{GAME_ICONS['wyr']} WOULD YOU RATHER — CLOSED"
    return embed
