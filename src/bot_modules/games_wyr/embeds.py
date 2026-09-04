"""Embed builders for the Would-You-Rather cog.

These functions accept plain primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.

WYR's main embed (:func:`build_wyr_embed`) shows the current question,
both options, and live vote bars. The cog edits the same message across
states (open → round-over → closed) by re-calling this builder with the
right ``closed`` flag. :func:`build_closed_embed` is a small wrapper
that produces the final "CLOSED" variant used by the close-game flow.

A revealed round names its voters through a ``name_fn``
(``services/name_resolver.build_name_fn``): a ``<@id>`` inside an embed
is resolved by the *reading* client from its own cache, so it renders as
a bare number for any viewer who hasn't seen that member. The default
``mention`` keeps an un-wired caller rendering; the cog is required to
pass a real resolver (a test walks its render sites).
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_PLAYING,
    PHASE_RECAP,
)
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games.utils.round_pacing import (
    TIMER_FIELD_NAME,
    timer_field_value,
    waiting_notice,
)
from bot_modules.games_wyr.logic import count_votes, most_divisive_round, played_rounds
from bot_modules.core.branding import apply_section_spacing
from bot_modules.services.name_resolver import NameFn, mention


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
    *,
    name_fn: NameFn = mention,
    waiting: bool = False,
    advance_at: int | None = None,
) -> discord.Embed:
    """Build the main WYR round embed.

    ``closed`` flips the title suffix to ``— ROUND OVER``; ``revealed``
    lists each option's voters by name (via ``name_fn``) under its bar.
    Both flags can combine. ``waiting`` renders the round with no question
    yet (the bank had nothing to serve — a posed question starts it), and
    ``advance_at`` adds the live countdown of a timed round.

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

    title = f"{GAME_ICONS['wyr']} Would You Rather"
    if closed:
        title += " — Round Over"
    embed = discord.Embed(title=title, color=color or discord.Color(PHASE_PLAYING))
    embed.add_field(name="Round", value=str(round_num), inline=False)
    if waiting:
        embed.description = waiting_notice("✍️ Pose Question", "question")
        embed.set_footer(text=f"{GAME_ICONS['wyr']} Would You Rather • Round {round_num}")
        apply_section_spacing(embed)
        return embed
    esc = discord.utils.escape_markdown
    embed.add_field(name="🅰️", value=esc(option_a), inline=True)
    embed.add_field(name="🅱️", value=esc(option_b), inline=True)
    embed.add_field(name="​", value="​", inline=True)

    a_label = f"🅰️ {bar_a} {pct_a} ({len(votes_a)})"
    b_label = f"🅱️ {bar_b} {pct_b} ({len(votes_b)})"

    if revealed:
        a_names = ", ".join(name_fn(uid) for uid in votes_a) if votes_a else "—"
        b_names = ", ".join(name_fn(uid) for uid in votes_b) if votes_b else "—"
        a_label += f"\n{a_names}"
        b_label += f"\n{b_names}"

    embed.add_field(name="Votes", value=f"{a_label}\n{b_label}", inline=False)
    if advance_at and not closed:
        embed.add_field(name=TIMER_FIELD_NAME, value=timer_field_value(advance_at), inline=False)
    anon_badge = " • 👁 Anonymous" if anonymous else ""
    embed.set_footer(text=f"{GAME_ICONS['wyr']} Would You Rather • Round {round_num}{anon_badge}")
    apply_section_spacing(embed)
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
    *,
    name_fn: NameFn = mention,
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
        name_fn=name_fn,
    )
    embed.title = f"{GAME_ICONS['wyr']} Would You Rather — Closed"
    return embed


def build_wyr_recap_embed(
    rounds: dict,
    *,
    color: discord.Color | None = None,
    reason: str | None = None,
) -> discord.Embed:
    """The game-over card: rounds played, total votes, the most divisive
    question and its split.

    Posted by the host's **🏁 End Game**, the round cap and ``/games end``
    (vote-games-52 / discovery-3 — until 2026-09-04 the only ending WYR had
    was the red Force-Closed card or the 24h sweep). Vote counts only; who
    voted stays behind the round's own Reveal Voters button.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['wyr']} Would You Rather — Game Over",
        color=color or discord.Color(PHASE_RECAP),
    )
    played = played_rounds(rounds)
    if reason == "round_cap":
        embed.description = "That's the last round — thanks for playing!"
    embed.add_field(name="Rounds Played", value=str(len(played)), inline=True)
    embed.add_field(name="Total Votes", value=str(count_votes(rounds)), inline=True)
    most_div = most_divisive_round(rounds)
    if most_div is not None:
        a, b = len(most_div.get("a") or []), len(most_div.get("b") or [])
        question = discord.utils.escape_markdown(str(most_div.get("q", "")))[:200]
        embed.add_field(
            name="Most Divisive",
            value=f"{question}\n🅰️ {a} — 🅱️ {b}",
            inline=False,
        )
    else:
        embed.add_field(name="Most Divisive", value="No votes were cast.", inline=False)
    embed.set_footer(text=f"{GAME_ICONS['wyr']} Would You Rather • Final tally")
    apply_section_spacing(embed)
    return embed
