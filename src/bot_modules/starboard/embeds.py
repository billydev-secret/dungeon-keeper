"""Embed builders for starboard posts and status output.

These functions take what Discord gives the cog (a ``discord.Message`` or a
pre-existing ``discord.Embed``) and return new embed objects. They never
touch the network — testable with mocked Discord objects.
"""

from __future__ import annotations

import discord

from bot_modules.services.embeds import STARBOARD_PRIMARY, footer_emoji


def build_starboard_embed(
    message: discord.Message, star_count: int, emoji: str
) -> discord.Embed:
    """Build the embed that gets posted to the starboard channel.

    Mirrors the message content (truncated), credits the author, links
    back to the original, and surfaces the first image attachment as the
    embed image so starred screenshots actually appear inline.
    """
    embed = discord.Embed(
        description=message.content[:2000] if message.content else None,
        color=STARBOARD_PRIMARY,
        timestamp=message.created_at,
    )
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    embed.set_author(
        name=f"{message.author.display_name} in #{channel_name}",
        icon_url=message.author.display_avatar.url,
    )
    embed.add_field(
        name="Original",
        value=f"[Jump to message]({message.jump_url})",
        inline=False,
    )
    # A custom star emoji renders as raw text in a footer; fall back to ⭐.
    embed.set_footer(text=f"{footer_emoji(emoji, '⭐')} {star_count}")

    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            embed.set_image(url=attachment.url)
            break

    return embed


def updated_starboard_embed(
    old_embed: discord.Embed, star_count: int, emoji: str
) -> discord.Embed:
    """Return a copy of ``old_embed`` with its star-count footer refreshed.

    Used when an existing starboard post's count changes; everything else
    (author, jump link, attachment) stays as it was so we don't refetch
    the original message on every reaction.
    """
    new_embed = old_embed.copy()
    new_embed.set_footer(text=f"{emoji} {star_count}")
    return new_embed
