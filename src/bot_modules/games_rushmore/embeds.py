"""Embed builders for the Mt. Rushmore Draft cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects (or, for :func:`render_draft_board`, a formatted string). They
never call out to Discord — testable with no network and no mocks of
the Bot/Guild API.

The deadline-style embeds (:func:`build_draft_embed`,
:func:`build_vote_embed`) call
:mod:`bot_modules.games.utils.timer` for the "ends in" text. Tests
should assert structure / substrings, not the exact deadline string,
since it bakes in ``time.time()`` at render.
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
from bot_modules.games_rushmore.logic import DRAFT_ROUNDS, SKIPPED_MARKER
from bot_modules.services.embeds import COLOR_GREEN


def _footer(host_name: str) -> str:
    """Standard footer used by every embed in the game.

    Pulled out so the icon and title text only live in one place.
    """
    return f"{GAME_ICONS['rushmore']} Mt. Rushmore Draft • Hosted by {host_name}"


def render_draft_board(
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
    active_player_id: int | None,
) -> str:
    """Render the multi-line monospaced "Draft Board" string.

    Each player gets one row: their (left-padded) name followed by their
    4 picks, with the active picker marked by a target emoji. Names are
    capped at 16 chars and pick text at 18 chars (with ``...``) so the
    board stays readable on mobile.
    """
    max_name = max((len(n) for _, n in players), default=6)
    max_name = min(max_name, 16)

    lines: list[str] = []
    for uid, name in players:
        board = boards.get(str(uid), [None] * DRAFT_ROUNDS)
        picks: list[str] = []
        for i, pick in enumerate(board):
            if pick is None:
                picks.append(f"{i+1}. —")
            elif pick == SKIPPED_MARKER:
                picks.append(f"{i+1}. *Skipped*")
            else:
                display = pick if len(pick) <= 18 else pick[:15] + "..."
                picks.append(f"{i+1}. {display}")

        truncated_name = (
            name if len(name) <= max_name else name[: max_name - 1] + "."
        )
        line = f"`{truncated_name:<{max_name}}` {' | '.join(picks)}"
        if uid == active_player_id:
            line += "  ← \U0001f3af"
        lines.append(line)

    return "**Draft Board:**\n" + "\n".join(lines)


def build_join_embed(
    host_name: str,
    player_names: list[str],
    topic: str | None = None,
    mode: str = "snake",
    color: discord.Color | None = None,
) -> discord.Embed:
    """Lobby embed shown while players are joining.

    When ``topic`` is set (host-supplied), the description teases the
    upcoming draft topic. A one-line how-to always renders — the ❓ Help
    button exists but live games showed nobody presses it, so the rules
    ride in the embed itself. ``mode`` flips the line between snake
    ("when it's your turn") and blitz ("everyone picks at once").
    """
    title = f"{GAME_ICONS['rushmore']} Mt. Rushmore Draft"
    desc = f"Hosted by: **{discord.utils.escape_markdown(host_name)}**"
    if topic:
        desc += (
            "\n\nBuild your Mt. Rushmore of "
            f"**{discord.utils.escape_markdown(topic)}**!"
        )
    if mode == "blitz":
        desc += (
            "\n\n⚡ **How it works:** each round, everyone picks at once — hit "
            "🗿 **Make Your Pick** and type one. Top 4, no duplicates "
            "(fastest fingers win), then the room votes on the best board."
        )
    else:
        desc += (
            "\n\n**How it works:** when it's your turn, hit 🗿 **Make Your "
            "Pick** and type one. Snake draft, top 4, no duplicates, then "
            "the room votes on the best board."
        )
    embed = discord.Embed(
        title=title, description=desc,
        color=color or discord.Color(PHASE_JOINING),
    )
    pool_str = ", ".join(player_names) if player_names else "(nobody yet)"
    embed.add_field(
        name=f"Players ({len(player_names)})", value=pool_str, inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_draft_embed(
    host_name: str,
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
    active_player_id: int | None,
    active_player_name: str | None,
    round_num: int,
    timer_secs: int,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Active-round draft embed.

    Imports the timer formatter lazily so test code that exercises the
    string-based builders below doesn't have to mock the timer module.
    The "Now Picking" field is dropped when there's no active player
    (used during the final "draft complete" frame).
    """
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=(
            f"{GAME_ICONS['rushmore']} Mt. Rushmore of: "
            f"{discord.utils.escape_markdown(topic)}"
        ),
        color=color or discord.Color(PHASE_PLAYING),
    )
    embed.add_field(
        name="Timer",
        value=(
            f"Round {round_num}/{DRAFT_ROUNDS} | "
            f"{format_deadline(now_plus(timer_secs))}"
        ),
        inline=False,
    )
    if active_player_name:
        embed.add_field(
            name="Now Picking",
            value=(
                "\U0001f3af "
                f"**{discord.utils.escape_markdown(active_player_name)}**'s turn!"
            ),
            inline=False,
        )
    board_text = render_draft_board(players, boards, active_player_id)
    embed.add_field(name="​", value=board_text, inline=False)
    embed.set_footer(text=_footer(host_name))
    return embed


def build_final_boards_embed(
    host_name: str,
    topic: str,
    players: list[tuple[int, str]],
    boards: dict[str, list[Any]],
    color: discord.Color | None = None,
) -> discord.Embed:
    """Final-boards embed shown after the snake draft completes.

    Each player's Mt. Rushmore renders as an inline field so the room
    can compare side-by-side before voting.
    """
    embed = discord.Embed(
        title=(
            f"{GAME_ICONS['rushmore']} Mt. Rushmore of: "
            f"{discord.utils.escape_markdown(topic)} — Final Boards"
        ),
        color=color or discord.Color(PHASE_RESULTS),
    )
    for uid, name in players:
        board = boards.get(str(uid), [None] * DRAFT_ROUNDS)
        lines: list[str] = []
        for i, pick in enumerate(board):
            if pick is None:
                lines.append(f"{i+1}. —")
            elif pick == SKIPPED_MARKER:
                lines.append(f"{i+1}. *Skipped*")
            else:
                lines.append(f"{i+1}. {discord.utils.escape_markdown(pick)}")
        esc_name = discord.utils.escape_markdown(name)
        embed.add_field(
            name=f"{GAME_ICONS['rushmore']} {esc_name}'s Mt. Rushmore",
            value="\n".join(lines),
            inline=True,
        )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_vote_embed(
    host_name: str, topic: str, timer_secs: int,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Voting-phase embed; the dropdown is attached by the cog's view."""
    from bot_modules.games.utils.timer import format_deadline, now_plus
    embed = discord.Embed(
        title=(
            f"{GAME_ICONS['rushmore']} Vote — Best Mt. Rushmore of "
            f"{discord.utils.escape_markdown(topic)}"
        ),
        color=color or discord.Color(PHASE_PLAYING),
    )
    embed.add_field(
        name="Timer", value=format_deadline(now_plus(timer_secs)), inline=False,
    )
    embed.add_field(
        name="Vote", value="Who built the best Mt. Rushmore?", inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_winner_embed(
    host_name: str,
    topic: str,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list[Any]],
    all_results: list[tuple[str, int]],
    color: discord.Color | None = None,
) -> discord.Embed:
    """Winner reveal embed shown right after the vote phase.

    Handles ties by joining ``winner_names`` with ``&`` and stacking
    each winner's board (with a blank line between when there's more
    than one). ``all_results`` is the sorted ``(name, votes)`` list the
    cog hands in.

    This is the game's genuine *win* state, so it stays semantic green
    (``COLOR_GREEN``) rather than following the guild accent — per the
    2026-07-21 ruling, true win=green survives the accent migration.
    ``color`` is accepted for uniformity/testability but callers should
    normally leave it unset so the semantic green applies.
    """
    embed = discord.Embed(
        title=(
            f"{GAME_ICONS['rushmore']} Winner — Mt. Rushmore of "
            f"{discord.utils.escape_markdown(topic)}"
        ),
        color=color or discord.Color(COLOR_GREEN),
    )
    winner_label = " & ".join(
        discord.utils.escape_markdown(n) for n in winner_names
    )
    board_lines: list[str] = []
    for board in winner_boards:
        for i, pick in enumerate(board):
            if pick and pick != SKIPPED_MARKER:
                board_lines.append(
                    f"{i+1}. {discord.utils.escape_markdown(pick)}"
                )
            else:
                board_lines.append(f"{i+1}. —")
        if len(winner_boards) > 1:
            board_lines.append("")
    embed.add_field(
        name=(
            f"\U0001f3c6 {winner_label} wins! — "
            f"{winner_votes} vote{'s' if winner_votes != 1 else ''}"
        ),
        value="\n".join(board_lines) or "—",
        inline=False,
    )
    results_lines: list[str] = []
    for name, votes in all_results:
        results_lines.append(
            f"**{discord.utils.escape_markdown(name)}** — "
            f"{votes} vote{'s' if votes != 1 else ''}"
        )
    embed.add_field(
        name="Full Results",
        value="\n".join(results_lines) or "—",
        inline=False,
    )
    embed.set_footer(text=_footer(host_name))
    return embed


def build_recap_embed(
    host_name: str,
    topic: str,
    player_count: int,
    duration_secs: float,
    winner_names: list[str],
    winner_votes: int,
    winner_boards: list[list[Any]],
    stats: dict[str, Any],
    color: discord.Color | None = None,
) -> discord.Embed:
    """Final game-over embed.

    ``stats`` must come from
    :func:`bot_modules.games_rushmore.logic.compute_recap_stats`; any
    key listed in that function's docstring may be present (or absent).
    Absent keys collapse silently so a game with no completed picks
    still renders cleanly.
    """
    mins = int(duration_secs // 60)
    secs = int(duration_secs % 60)
    embed = discord.Embed(
        title=f"{GAME_ICONS['rushmore']} Mt. Rushmore Draft — Game Over",
        color=color or discord.Color(PHASE_RECAP),
    )
    winner_label = " & ".join(
        discord.utils.escape_markdown(n) for n in winner_names
    )
    board_lines: list[str] = []
    for board in winner_boards:
        for i, pick in enumerate(board):
            if pick and pick != SKIPPED_MARKER:
                board_lines.append(
                    f"  {i+1}. {discord.utils.escape_markdown(pick)}"
                )
            else:
                board_lines.append(f"  {i+1}. —")

    summary = (
        f"\U0001f4cb Topic: **{discord.utils.escape_markdown(topic)}**\n"
        f"\U0001f465 Players: **{player_count}**\n"
        f"⏱️ Draft duration: **{mins}m {secs}s**\n\n"
        f"\U0001f3c6 Winner: **{winner_label}** — "
        f"{winner_votes} vote{'s' if winner_votes != 1 else ''}\n"
    )
    summary += "\n".join(board_lines)
    embed.add_field(name="Summary", value=summary, inline=False)

    stat_lines: list[str] = []
    if stats.get("first_pick"):
        fp = stats["first_pick"]
        stat_lines.append(
            "\U0001f947 First Overall Pick: "
            f"**{discord.utils.escape_markdown(fp['pick'])}** "
            f"({discord.utils.escape_markdown(fp['player'])}, Round 1)"
        )
    if stats.get("skipped_count") is not None:
        sc = stats["skipped_count"]
        skipped_names = stats.get("skipped_names", [])
        extra = (
            " ("
            + ", ".join(
                discord.utils.escape_markdown(n) for n in skipped_names
            )
            + ")"
            if skipped_names
            else ""
        )
        stat_lines.append(f"⏭️ Skipped Picks: **{sc}**{extra}")
    if stats.get("fastest"):
        f = stats["fastest"]
        stat_lines.append(
            "⚡ Fastest Pick: "
            f"**{discord.utils.escape_markdown(f['pick'])}** by "
            f"{discord.utils.escape_markdown(f['player'])} "
            f"({f['time']:.1f}s)"
        )
    if stats.get("slowest"):
        s = stats["slowest"]
        stat_lines.append(
            "\U0001f422 Slowest Pick: "
            f"**{discord.utils.escape_markdown(s['pick'])}** by "
            f"{discord.utils.escape_markdown(s['player'])} "
            f"({s['time']:.1f}s)"
        )
    if stats.get("unanimous"):
        stat_lines.append(
            "\U0001f3af Unanimous Vote: Yes — everyone voted for "
            f"**{discord.utils.escape_markdown(winner_label)}**"
        )
    elif stats.get("vote_split"):
        stat_lines.append(
            f"\U0001f3af Vote: {stats['vote_split']}-way split"
        )

    if stat_lines:
        embed.add_field(
            name="\U0001f4ca Draft Stats",
            value="\n".join(stat_lines),
            inline=False,
        )

    embed.set_footer(text=_footer(host_name))
    return embed
