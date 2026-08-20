"""Utility functions for the Discord bot."""

from __future__ import annotations

from typing import TypeAlias

import discord

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def disable_all_items(view: discord.ui.View) -> None:
    """Disable every button and select on ``view`` — used to retire a
    message's controls when the interaction it drove is finished."""
    for item in view.children:
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            item.disabled = True


def is_host_or_mod(interaction: discord.Interaction, host_id: int) -> bool:
    """Is the clicker the game's host, or a server mod who can override?

    The gate on every game view's host-only control — close the round, skip,
    end early. One definition for all of them: the host always passes; anyone
    else needs ``administrator`` or ``manage_guild`` *in a guild*, so a DM or
    a non-member user never qualifies.

    Deliberately **not** ``AppContext.is_mod``. That one reads
    ``interaction.permissions`` and additionally honours the guild's
    configured mod roles, so it answers a wider question ("is this person
    staff?"). Routing game overrides through it would hand a configured
    mod-role holder without ``manage_guild`` control over other people's
    games — a permission widening, not a refactor. ``is_mod_or_admin`` below
    is a third rule again (it also accepts ``manage_channels``). Three gates,
    kept apart on purpose.
    """
    if interaction.user.id == host_id:
        return True
    if interaction.guild and isinstance(interaction.user, discord.Member):
        perms = interaction.user.guild_permissions
        return perms.administrator or perms.manage_guild
    return False


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


async def resolve_bot_channel(bot, channel_id: int):
    """A channel by id from the cache, falling back to one API fetch.

    Every background loop that posts somewhere configured needs this — the
    cache misses after a restart, or for a channel the bot has never seen —
    and it had been copy-pasted into four of them (game_start_ping,
    scheduled_games, announcements, event_echo) before landing here. Returns
    None rather than raising: a loop must not die because one destination
    went away.
    """
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


def jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    """A permalink to one message.

    Prefer ``message.jump_url`` when you have the message object; this is for
    callers working from stored ids, which would otherwise have to fetch a
    message purely to read the property off it. A channel-only link loses the
    message and lands the reader at the bottom of the channel instead.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


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
