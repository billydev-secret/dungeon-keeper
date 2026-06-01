"""Embed builders for the Free For All cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

The single embed shape :func:`build_ffa_embed` is rendered twice: once
when the host posts the question (``reply_count=0``) and again every
time an anonymous reply lands (``reply_count`` increments). The footer
flips to include the running count once at least one reply is in so an
empty game doesn't show "0 anonymous replies".
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR


def build_ffa_embed(question: str, reply_count: int = 0) -> discord.Embed:
    """Build the main FFA embed shown alongside the reply button.

    ``question`` is rendered as an H1 inside the field value (escaped
    against markdown injection). ``reply_count`` controls whether the
    footer shows just the game name or appends a running count of
    anonymous replies — the cog calls this every time the count
    changes so the embed stays in sync.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['ffa']} FREE FOR ALL",
        color=BRAND_COLOR,
    )
    embed.add_field(
        name="Question",
        value=f"# {discord.utils.escape_markdown(question)}",
        inline=False,
    )
    footer_parts = [f"{GAME_ICONS['ffa']} Free For All"]
    if reply_count > 0:
        footer_parts.append(f"📊 {reply_count} anonymous replies")
    embed.set_footer(text=" • ".join(footer_parts))
    return embed
