"""Utility functions for the Discord bot."""

from __future__ import annotations

from typing import TypeAlias

import discord

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def get_interaction_member(interaction: discord.Interaction) -> discord.Member | None:
    """Get the member from an interaction, resolving from guild if needed."""
    user = interaction.user
    if isinstance(user, discord.Member):
        return user
    guild = interaction.guild
    if guild is None:
        return None
    return guild.get_member(user.id)


def get_bot_member(guild: discord.Guild) -> discord.Member | None:
    """Get the bot's member object for a guild."""
    return guild.me


def format_user_for_log(
    user: discord.abc.User | discord.Member | None = None,
    user_id: int | None = None,
) -> str:
    """Format a user for logging with display name, username, and ID."""
    if user is not None:
        resolved_id = getattr(user, "id", user_id)
        display_name = getattr(user, "display_name", None)
        username = getattr(user, "name", None)
        if display_name and username and display_name != username:
            return f"{display_name} [{username}] ({resolved_id})"
        label = display_name or username or str(user)
        return f"{label} ({resolved_id})" if resolved_id is not None else label

    if user_id is None:
        return "unknown user"

    return f"user {user_id}"


def resolve_user_for_log(guild: discord.Guild | None, user_id: int) -> str:
    """Resolve and format a user ID for logging."""
    member = guild.get_member(user_id) if guild is not None else None
    return format_user_for_log(member, user_id)


def format_guild_for_log(
    guild: discord.Guild | None = None,
    guild_id: int | None = None,
) -> str:
    """Format a guild for logging with name and ID."""
    if guild is not None:
        resolved_id = getattr(guild, "id", guild_id)
        name = getattr(guild, "name", None)
        if name:
            return f"{name} ({resolved_id})" if resolved_id is not None else name
        return f"guild {resolved_id}" if resolved_id is not None else "unknown guild"

    if guild_id is None:
        return "unknown guild"
    return f"guild {guild_id}"


def resolve_guild_for_log(bot: discord.Client | None, guild_id: int) -> str:
    """Resolve and format a guild ID for logging."""
    guild = bot.get_guild(guild_id) if bot is not None else None
    return format_guild_for_log(guild, guild_id)


def get_guild_channel_or_thread(
    guild: discord.Guild,
    channel_id: int,
) -> GuildTextLike | None:
    """Get a text channel or thread from a guild by ID."""
    resolver = getattr(guild, "get_channel_or_thread", None)
    if callable(resolver):
        channel = resolver(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    thread = guild.get_thread(channel_id)
    if isinstance(thread, discord.Thread):
        return thread

    return None


async def resolve_reply_target(message: discord.Message) -> discord.Message | None:
    """Resolve the target message of a reply, fetching if necessary."""
    if not message.reference:
        return None

    if isinstance(message.reference.resolved, discord.Message):
        return message.reference.resolved

    if not message.reference.message_id:
        return None

    ref_channel: GuildTextLike | None = None
    if message.guild is not None and message.reference.channel_id is not None:
        candidate_channel = message.guild.get_channel(message.reference.channel_id)
        if isinstance(candidate_channel, discord.TextChannel):
            ref_channel = candidate_channel
    if ref_channel is None and isinstance(
        message.channel,
        (discord.TextChannel, discord.Thread),
    ):
        ref_channel = message.channel

    if not isinstance(ref_channel, (discord.TextChannel, discord.Thread)):
        return None

    try:
        return await ref_channel.fetch_message(message.reference.message_id)
    except (discord.NotFound, discord.Forbidden):
        return None
