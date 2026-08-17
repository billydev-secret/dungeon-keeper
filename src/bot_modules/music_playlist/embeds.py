"""Embed builders for the music playlist member panel.

Pure functions: store rows (mappings) in, ``discord.Embed`` out, no Discord
or network calls — testable offline. The cog resolves the guild accent and
passes it as ``color`` (see docs/embed_style_guide.md); a builder never
resolves the accent itself.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import discord
from discord.utils import escape_markdown

# One concept, one glyph, matching the source-message reactions: 🎶 the
# window, 🎧 your posts, ❓ in review (the same ❓ the bot reacts with).
_MAX_LISTED = 30


def _track_line(index: int, row: Mapping[str, Any], *, with_poster: bool) -> str:
    title = escape_markdown(str(row["title"] or "Unknown title"))
    artist = escape_markdown(str(row["artist"] or "Unknown artist"))
    line = f"{index}. **{title}** — {artist}"
    if with_poster:
        line += f" • <@{row['added_by']}>"
    return line


def build_window_embed(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_size: int,
    color: discord.Color,
) -> discord.Embed:
    """The live rolling window, newest first (the order the store returns)."""
    if rows:
        lines = [
            _track_line(i, row, with_poster=True)
            for i, row in enumerate(rows[:_MAX_LISTED], start=1)
        ]
        description = "\n".join(lines)
    else:
        description = "Nothing in the playlist yet — post a song link to start it."
    embed = discord.Embed(
        title="🎶 Rolling Playlist", description=description, color=color
    )
    embed.set_footer(
        text=f"{len(rows)}/{window_size} songs • newest first • oldest rolls off"
    )
    return embed


def build_my_posts_embed(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_size: int,
    color: discord.Color,
) -> discord.Embed:
    """The member's own tracks currently in the window (pre-filtered rows)."""
    if rows:
        lines = [
            _track_line(i, row, with_poster=False)
            for i, row in enumerate(rows[:_MAX_LISTED], start=1)
        ]
        description = "\n".join(lines)
    else:
        description = "None of your posts are in the current window."
    embed = discord.Embed(
        title="🎧 Your Songs", description=description, color=color
    )
    embed.set_footer(
        text=f"{len(rows)} of the current {window_size}-song window"
    )
    return embed


def build_my_review_embed(
    items: Sequence[Mapping[str, Any]],
    *,
    color: discord.Color,
) -> discord.Embed:
    """The member's pending review-queue items (pre-filtered, oldest first)."""
    if items:
        lines = []
        for item in items[:_MAX_LISTED]:
            label = escape_markdown(
                str(item["extracted_title"] or item["source_url"])
            )
            candidate = item["candidate_name"]
            if candidate:
                artist = item["candidate_artist"]
                guess = escape_markdown(
                    f"{candidate} — {artist}" if artist else str(candidate)
                )
                lines.append(f"• **{label}** (best guess: {guess})")
            else:
                lines.append(f"• **{label}** (no match found)")
        description = "\n".join(lines)
    else:
        description = "Nothing of yours is waiting for review."
    embed = discord.Embed(
        title="❓ Your Songs in Review", description=description, color=color
    )
    embed.set_footer(text="A mod reviews these on the dashboard.")
    return embed
