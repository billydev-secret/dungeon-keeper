"""Embed builders for the music cog's slash commands.

The now-playing embed already lives in
``services/music_now_playing.py`` (it's coupled to the persistent
``NowPlayingView``); these are the standalone embeds that the cog
assembles inside one-shot slash commands and which are tedious to
unit-test through Discord.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

# Warm gold matches services/music_now_playing.EMBED_COLOR -- intentionally
# duplicated rather than imported to keep this module free of cog deps.
_EMBED_COLOR = 0xC9A961


def build_queue_embed(
    *,
    current_summary: str | None,
    item_summaries: Sequence[str],
    start_index: int,
    total_in_queue: int,
    page: int,
    total_pages: int,
    loop_mode_value: str,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the embed for ``/queue``.

    Caller supplies pre-formatted track summary strings -- this keeps
    the embed builder free of wavelink imports and makes
    ``format_track_summary`` the single source of truth for the
    rendering of a track line.

    * ``current_summary`` -- None when nothing is playing (field is
      skipped, matching the cog's existing behavior of only adding the
      field when ``queue.current is not None``).
    * ``item_summaries`` -- the current page's upcoming tracks. Empty
      list renders ``(empty)`` so the embed never has no fields.
    * ``start_index`` -- zero-based index of the first item in
      ``item_summaries`` within the full queue (for the ``` 1.``` line
      numbering).
    """
    if color is None:
        color = discord.Color(_EMBED_COLOR)
    embed = discord.Embed(title="🎶 Music queue", color=color)

    if current_summary is not None:
        embed.add_field(name="Now playing", value=current_summary, inline=False)

    if item_summaries:
        lines = [
            f"`{start_index + i + 1:>2}.` {summary}"
            for i, summary in enumerate(item_summaries)
        ]
        embed.add_field(
            name=f"Up next ({total_in_queue} total)",
            value="\n".join(lines),
            inline=False,
        )
    else:
        embed.add_field(name="Up next", value="(empty)", inline=False)

    embed.set_footer(text=f"Page {page}/{total_pages} · loop: {loop_mode_value}")
    return embed


def build_247_status_embed(
    lines: Sequence[str], color: "discord.Color | None" = None
) -> discord.Embed:
    """Build the embed for ``/247_status``.

    ``lines`` is a sequence of pre-formatted bullet strings (one per
    24/7 channel) -- ``format_247_status_line`` produces them. Empty
    sequences are still rendered (the caller decides whether to short
    out on no entries with a plain message instead).
    """
    if color is None:
        color = discord.Color(_EMBED_COLOR)
    return discord.Embed(
        title="📻 24/7 channels",
        description="\n".join(lines) if lines else "(none)",
        color=color,
    )
