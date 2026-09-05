"""The word cloud card.

Built by a pure function so the copy can be asserted without a Discord mock,
per docs/embed_style_guide.md. The colour is resolved by the caller and passed
in — resolving it needs a bot and a guild, which is exactly the Discord glue
this layer is meant to be free of.
"""

from __future__ import annotations

from datetime import timedelta

import discord

from .logic import span_label

#: Filename the attachment is sent under; the embed's image points at it.
FILENAME = "wordcloud.png"


def build_cloud_embed(
    *,
    message_count: int,
    member_name: str | None,
    scope_label: str,
    span: timedelta,
    source_label: str,
    by_sentiment: bool,
    notes: list[str],
    color: discord.Color | None,
) -> discord.Embed:
    """Assemble the card.

    ``member_name`` is escaped here rather than at the call site: a display
    name is member-supplied text, and an unescaped ``__Robin__`` would
    reformat the description around it.

    ``span`` must be the window actually covered, not the one asked for — on
    the live path a seven-day request covers ten minutes, and a headline
    saying otherwise is wrong even with a note underneath explaining it.
    """
    who = ""
    if member_name:
        who = f" from {discord.utils.escape_markdown(member_name)}"

    embed = discord.Embed(
        title="Word cloud",
        description=(
            f"**{message_count:,}** messages{who} in {scope_label}, "
            f"over the last {span_label(span)} — from {source_label}."
        ),
        color=color,
    )
    if by_sentiment:
        embed.add_field(
            name="Colour",
            value="Warm words came up in happier messages, cool ones in unhappier.",
            inline=False,
        )
    if notes:
        embed.add_field(name="Worth knowing", value="\n".join(notes), inline=False)
    embed.set_image(url=f"attachment://{FILENAME}")
    return embed
