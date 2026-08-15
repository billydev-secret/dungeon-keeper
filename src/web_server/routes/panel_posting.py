"""Shared guards for routes that make the bot post a panel into a channel.

Every "repost this panel" endpoint needs the same three checks in the same
order — the bot is online and in the guild, the target really is a text channel
here, and the bot may post *the kind of message this panel sends*. They lived
private in ``routes/config.py`` until the colour palette's admin moved to
``routes/economy.py`` and needed them too; a second copy would have been a
second thing to keep in step with the first.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel


class ChannelIdBody(BaseModel):
    """A body carrying just the target channel for a panel post."""

    channel_id: str


def guild_or_503(ctx, guild_id: int):
    """The active guild, or the 503 the dashboard shows as "bot offline".

    Routes that act on Discord rather than just the DB all open this way.
    Callers that must report a *different* failure first (a missing cog, say)
    keep their own inline checks so the error precedence does not move.
    """
    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(503, "Bot not available")
    guild = bot.get_guild(guild_id)
    if guild is None:
        raise HTTPException(503, "Discord guild not available")
    return guild


def text_channel_or_400(guild, raw_channel_id):
    """Resolve a body's ``channel_id`` to a text channel in this guild."""
    import discord

    try:
        channel_id = int(raw_channel_id)
    except (TypeError, ValueError):
        raise HTTPException(400, "Invalid channel_id") from None
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(400, "Channel must be a text channel in this guild")
    return channel


_PERM_LABELS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "attach_files": "Attach Files",
}


def require_post_permissions(guild, channel, *required: str) -> None:
    """Refuse up front if the bot cannot post what this panel actually posts.

    Worth doing *before* calling a panel-posting service rather than letting
    discord.Forbidden escape as a 500: a panel poster deletes the existing panel
    messages before it sends the new ones, and those sends are unguarded, so
    failing partway through leaves the guild with no panel at all and a repost
    that fails the same way. A 400 naming the missing permission is also simply
    actionable — the admin can go and fix it.

    The flags differ per panel and must be passed explicitly: the DM perms
    panel sends an embed, while the colour-palette showroom sends image
    *attachments* and no embed. Checking the wrong set is worse than not
    checking, because it both rejects channels that would have worked and waves
    through the one failure this exists to prevent.
    """
    perms = channel.permissions_for(guild.me)
    missing = [
        _PERM_LABELS[name] for name in required if not getattr(perms, name)
    ]
    if missing:
        raise HTTPException(
            400,
            f"The bot can't post in #{channel.name} — missing permissions: "
            f"{', '.join(missing)}",
        )
