"""Embed builder for the Free For All (Truth-or-Dare card) cog.

The host posts a rendered prompt card; the embed wraps that image so the
running anonymous-reply count and the CLOSED state can be updated in
place by editing the embed (a bare image attachment has nothing to edit).

:func:`build_ffa_embed` is rendered once when the card is posted
(``reply_count=0``) and again every time an anonymous reply lands so the
footer count stays in sync. The image is referenced via the
``attachment://`` scheme — the cog sends the PNG as ``ffa.png`` alongside
this embed.
"""

from __future__ import annotations

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR

CARD_FILENAME = "ffa.png"


def build_ffa_embed(label: str, number: int, reply_count: int = 0) -> discord.Embed:
    """Build the embed that frames the prompt card.

    ``label`` is "TRUTH" or "DARE" and ``number`` the per-channel count
    (e.g. ``TRUTH #5``). ``reply_count`` controls whether the footer
    appends a running tally of anonymous replies — the cog calls this
    every time the count changes so the embed stays in sync.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['ffa']} {label} #{number}",
        color=BRAND_COLOR,
    )
    embed.set_image(url=f"attachment://{CARD_FILENAME}")
    footer_parts = [f"{GAME_ICONS['ffa']} Truth or Dare"]
    if reply_count > 0:
        footer_parts.append(f"📊 {reply_count} anonymous replies")
    embed.set_footer(text=" • ".join(footer_parts))
    return embed
