"""Embed builders for the Most Likely To cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

Four distinct embed shapes are exposed:

* :func:`build_join_embed` — the lobby embed shown while players
  click Join/Leave before the host starts the game.
* :func:`build_round_embed` — the active round embed (and its
  ``closed=True`` variant, which only changes the title suffix and
  color). The cog reuses the same builder when editing the message
  after the round resolves.
* :func:`build_closed_embed` — a thin reskin of the round embed for
  the "host stopped the game mid-round" path.
* :func:`build_results_embed` — the per-round results embed shown
  publicly after the votes are tallied; the top-voted player(s) get
  a crown prefix.

``guild`` is passed through to :func:`resolve_name`; callers can
pass ``None`` (e.g. tests) and IDs render as their string form.
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
from bot_modules.games.utils.game_manager import resolve_name


def build_join_embed(host_name: str, players: list[str]) -> discord.Embed:
    """Build the lobby embed shown above the Join/Leave/Start buttons.

    ``players`` is a list of pre-resolved display names (the cog calls
    ``resolve_names`` first so this embed builder stays Discord-API
    free). An em-dash is shown when no one has joined yet so the field
    never renders empty.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['mlt']} MOST LIKELY TO",
        color=PHASE_JOINING,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(
        name=f"Players ({len(players)})",
        value=", ".join(players) if players else "—",
        inline=False,
    )
    embed.set_footer(text=f"{GAME_ICONS['mlt']} Most Likely To")
    return embed


def build_round_embed(
    prompt: str,
    round_num: int,
    vote_count: int,
    closed: bool = False,
) -> discord.Embed:
    """Build the active-round (or finished-round) vote embed.

    When ``closed=True`` the title gains a ``— ROUND OVER`` suffix
    and the color shifts from "playing" blue to "results" green.
    All other fields render identically — the cog reuses this builder
    when editing the message after the round resolves.
    """
    title = f"{GAME_ICONS['mlt']} MOST LIKELY TO..."
    if closed:
        title += " — ROUND OVER"
    embed = discord.Embed(
        title=title, color=PHASE_RESULTS if closed else PHASE_PLAYING
    )
    embed.add_field(
        name="Prompt",
        value=discord.utils.escape_markdown(prompt),
        inline=False,
    )
    embed.add_field(
        name="Round",
        value=f"{round_num} — {vote_count} votes",
        inline=False,
    )
    embed.set_footer(
        text=f"{GAME_ICONS['mlt']} Most Likely To  •  Round {round_num}"
    )
    return embed


def build_closed_embed(
    prompt: str,
    round_num: int,
    vote_count: int,
) -> discord.Embed:
    """Build the "host stopped the game" embed.

    Thin reskin of :func:`build_round_embed`: the title flips to
    ``— CLOSED`` and the color shifts to the recap dark gold so the
    closed state is visually distinct from a regular round-over.
    """
    embed = build_round_embed(
        prompt=prompt, round_num=round_num, vote_count=vote_count, closed=True
    )
    embed.title = f"{GAME_ICONS['mlt']} MOST LIKELY TO — CLOSED"
    embed.color = discord.Color(PHASE_RECAP)
    return embed


def build_results_embed(
    prompt: str,
    round_num: int,
    tally: dict[int, int],
    guild: Any = None,
) -> discord.Embed:
    """Build the per-round results embed shown after votes are tallied.

    Lines are sorted by descending vote count; the top-voted player(s)
    get a 👑 crown prefix (multiple crowns appear on a tie). When
    ``tally`` is empty (no votes were cast) the description renders a
    placeholder so the embed is never blank.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['mlt']} MOST LIKELY TO {prompt}",
        color=PHASE_RESULTS,
    )
    sorted_tally = sorted(tally.items(), key=lambda x: -x[1])
    max_votes = sorted_tally[0][1] if sorted_tally else 0
    lines: list[str] = []
    for uid, count in sorted_tally:
        name = resolve_name(guild, uid) if guild is not None else str(uid)
        crown = "👑 " if count == max_votes and count > 0 else "   "
        lines.append(
            f"{crown}**{discord.utils.escape_markdown(name)}** — {count} votes"
        )
    embed.description = "\n".join(lines) if lines else "No votes cast."
    embed.set_footer(
        text=f"{GAME_ICONS['mlt']} Most Likely To  •  Round {round_num} Results"
    )
    return embed


def build_final_standings_embed(crowns: dict, guild: Any = None) -> discord.Embed:
    """Final cumulative-crown standings shown when a game ends.

    ``crowns`` maps ``str(user_id) -> crown count``. Players are ranked by
    crown count; ties at the top all get 👑.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['mlt']} Most Likely To — Final Standings",
        color=PHASE_RECAP,
    )
    items = sorted(
        ((int(uid), int(c)) for uid, c in (crowns or {}).items() if int(c) > 0),
        key=lambda x: -x[1],
    )
    if not items:
        embed.description = "No crowns were awarded this game."
        return embed
    top = items[0][1]
    lines: list[str] = []
    for rank, (uid, count) in enumerate(items, start=1):
        name = resolve_name(guild, uid) if guild is not None else str(uid)
        prefix = "👑 " if count == top else f"{rank}. "
        plural = "crown" if count == 1 else "crowns"
        lines.append(f"{prefix}**{discord.utils.escape_markdown(name)}** — {count} {plural}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"{GAME_ICONS['mlt']} Most Likely To  •  Final crown tally")
    return embed
