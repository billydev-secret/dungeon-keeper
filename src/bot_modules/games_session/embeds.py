"""Embed builders for the Session Recap cog.

These functions accept plain dicts/primitives and return
``discord.Embed`` objects. They never call out to Discord — testable
with no network and no mocks of the Bot/Guild API.

The one embed shape :func:`build_session_recap_embed` is the recap
view shown by ``/session-recap``. The cog resolves player mentions and
highlight strings against the live guild before calling so this layer
stays pure.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import BRAND_COLOR


def build_session_recap_embed(
    game_count: int,
    player_ids: list[int],
    duration_str: str,
    highlights: list[str],
) -> discord.Embed:
    """Build the ``/session-recap`` embed.

    ``player_ids`` are rendered as raw ``<@id>`` mentions inside the
    field value — Discord resolves them at display time. ``highlights``
    are the pre-formatted strings from :func:`build_highlights`; only
    the first 8 are shown to keep the embed under field-length limits.

    Empty ``player_ids`` and empty ``highlights`` both skip their
    respective fields so a sparse session doesn't render dashes.
    """
    embed = discord.Embed(
        title="📋 GAME NIGHT SESSION RECAP",
        color=BRAND_COLOR,
    )
    embed.add_field(name="🎮 Games Played", value=str(game_count), inline=True)
    embed.add_field(name="👥 Unique Players", value=str(len(player_ids)), inline=True)
    embed.add_field(name="⏱️ Total Duration", value=duration_str, inline=True)

    if player_ids:
        embed.add_field(
            name="🏆 Players",
            value=", ".join(f"<@{uid}>" for uid in player_ids[:10]),
            inline=False,
        )

    if highlights:
        embed.add_field(
            name="Game Highlights",
            value="\n".join(f"• {h}" for h in highlights[:8]),
            inline=False,
        )

    embed.set_footer(text="Community Games • Session Recap")
    return embed
