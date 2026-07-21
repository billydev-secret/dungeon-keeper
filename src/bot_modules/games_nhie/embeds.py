"""Embed builders for the Never Have I Ever cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Three distinct embed shapes are exposed:

* :func:`build_round_embed` — the active round and the "ROUND OVER"
  state share a builder; only the title suffix and color differ
  (driven by the ``closed`` flag).
* :func:`build_closed_embed` — the "CLOSED" state when a host stops a
  game mid-round (a thin reskin of the round embed).
* :func:`build_recap_embed` — the game-over recap with the winner and
  final guilt scores.

``guild`` is passed through to :func:`resolve_name`; callers can pass
``None`` (e.g. tests) and IDs render as their string form.
"""

from __future__ import annotations

from typing import Any

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games.utils.game_manager import resolve_name
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games_nhie.logic import DEFAULT_LIVES


def build_round_embed(
    statement: str,
    guilty: list[int],
    innocent: list[int],
    round_num: int,
    closed: bool = False,
    lives: dict[int, int] | None = None,
    eliminated: set[int] | None = None,
    guild: Any = None,
    max_lives: int = DEFAULT_LIVES,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the main round embed (active OR finished round).

    When ``closed=True`` the title gains a "ROUND OVER" suffix. All
    other fields render identically — the cog re-uses this same embed
    when editing the message after the round resolves.

    The embed color follows the guild accent (``color``) when supplied;
    absent a guild it falls back to the phase palette (blue while
    playing, green once the round is over).

    When ``lives`` is supplied (non-empty) a "Still Standing" section
    is rendered with heart pips per player and a separate "Eliminated"
    section listing knocked-out players.
    """
    total = len(guilty) + len(innocent)
    bar_g, pct_g = build_bar(len(guilty), total)
    bar_i, pct_i = build_bar(len(innocent), total)

    title = f"{GAME_ICONS['nhie']} NEVER HAVE I EVER"
    if closed:
        title += " — ROUND OVER"
    embed = discord.Embed(
        title=title,
        color=color or discord.Color(PHASE_RESULTS if closed else PHASE_PLAYING),
    )
    embed.add_field(name="Round", value=str(round_num), inline=False)
    embed.add_field(
        name="Statement",
        value=discord.utils.escape_markdown(statement),
        inline=False,
    )
    embed.add_field(
        name="Votes",
        value=(
            f"😈 {bar_g} {pct_g} ({len(guilty)})\n"
            f"😇 {bar_i} {pct_i} ({len(innocent)})"
        ),
        inline=False,
    )

    if lives:
        elim = eliminated or set()
        alive_lines: list[str] = []
        for uid, hp in sorted(lives.items(), key=lambda x: -x[1]):
            if uid in elim:
                continue
            name = resolve_name(guild, uid) if guild else str(uid)
            hearts = "❤️" * hp + "🖤" * (max_lives - hp)
            alive_lines.append(
                f"{hearts} **{discord.utils.escape_markdown(name)}**"
            )
        if alive_lines:
            embed.add_field(
                name=f"Still Standing ({len(alive_lines)})",
                value="\n".join(alive_lines),
                inline=False,
            )

        elim_names: list[str] = []
        for uid in elim:
            name = resolve_name(guild, uid) if guild else str(uid)
            elim_names.append(discord.utils.escape_markdown(name))
        if elim_names:
            embed.add_field(
                name="💀 Eliminated", value=", ".join(elim_names), inline=False
            )

    embed.set_footer(
        text=f"{GAME_ICONS['nhie']} Never Have I Ever • Round {round_num}"
    )
    return embed


def build_closed_embed(
    statement: str,
    guilty: list[int],
    innocent: list[int],
    round_num: int,
    lives: dict[int, int] | None = None,
    eliminated: set[int] | None = None,
    guild: Any = None,
    max_lives: int = DEFAULT_LIVES,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the "host stopped the game" embed.

    This is a thin reskin of :func:`build_round_embed` with a
    "CLOSED" title. The color follows the guild accent (``color``) when
    supplied, otherwise falls back to the recap palette (dark gold).
    The cog edits the live message with this embed when the
    close-confirmation flow fires.
    """
    embed = build_round_embed(
        statement=statement,
        guilty=guilty,
        innocent=innocent,
        round_num=round_num,
        closed=True,
        lives=lives,
        eliminated=eliminated,
        guild=guild,
        max_lives=max_lives,
        color=color,
    )
    embed.title = f"{GAME_ICONS['nhie']} NEVER HAVE I EVER — CLOSED"
    embed.color = color or discord.Color(PHASE_RECAP)
    return embed


def build_recap_embed(
    winner_id: int | None,
    guilt_scores: dict[str, int],
    guild: Any = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the game-over recap embed.

    Shows the winner (or a tombstone for the "everyone eliminated"
    case) and the final per-player guilty vote tally sorted from worst
    to best. ``guilt_scores`` keys are stringified user IDs to match
    the persisted payload.
    """
    if winner_id is not None:
        winner_name = resolve_name(guild, winner_id) if guild else str(winner_id)
        description = (
            f"🏆 **{discord.utils.escape_markdown(winner_name)}** "
            f"is the last one standing!"
        )
    else:
        description = "Everyone's been eliminated! No winner this time."

    embed = discord.Embed(
        title=f"{GAME_ICONS['nhie']} NEVER HAVE I EVER — GAME OVER",
        description=description,
        color=color or discord.Color(PHASE_RECAP),
    )
    if guilt_scores:
        lines = [
            f"**{resolve_name(guild, int(uid)) if guild else str(uid)}** — "
            f"{score} guilty votes"
            for uid, score in sorted(guilt_scores.items(), key=lambda x: -x[1])
        ]
        value = "\n".join(lines)
    else:
        value = "—"
    embed.add_field(name="Final Guilt Scores", value=value, inline=False)
    return embed
