"""Utility functions for the Discord bot."""

from __future__ import annotations

import logging
from typing import Any, Literal, TypeAlias

import discord
from discord import app_commands

log = logging.getLogger(__name__)

GuildTextLike: TypeAlias = discord.TextChannel | discord.Thread


def disable_all_items(view: discord.ui.View) -> None:
    """Disable every button and select on ``view`` — used to retire a
    message's controls when the interaction it drove is finished."""
    for item in view.children:
        if isinstance(item, (discord.ui.Button, discord.ui.Select)):
            item.disabled = True


async def safe_ephemeral(
    interaction: discord.Interaction, text: str, *, log_label: str = "ephemeral"
) -> None:
    """Send ``text`` back to the clicker privately, whatever state we're in.

    Answers the two ways a view can find itself needing to say something: if
    the interaction was already responded to (deferred, or a modal handled
    first) the reply has to go through ``followup``, otherwise through
    ``response``. Getting that wrong raises, and a raise inside a button
    callback surfaces to the member as "This interaction failed" — so the
    send is best-effort and an HTTP failure is swallowed.

    ``log_label`` names the caller in the debug line; the traceback would
    otherwise point at this module rather than the view that failed.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.debug("%s: failed to send ephemeral", log_label, exc_info=True)


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


def has_mod_or_admin_permissions(perms: Any) -> bool:
    """Return True if perms grant admin, manage_guild, or manage_channels.

    Matches the cog's ``is_mod_or_admin`` rule: any one of the three
    elevated perms qualifies a user to run mod-tier game commands.
    """
    if not perms:
        return False
    return bool(
        getattr(perms, "administrator", False)
        or getattr(perms, "manage_guild", False)
        or getattr(perms, "manage_channels", False)
    )


def is_mod_or_admin():
    """``app_commands.check`` gating the /games admin commands.

    A third rule again, wider than ``is_host_or_mod`` above: it accepts
    ``manage_channels`` as well, because the commands it guards are about
    which channels games may run in — the perm that says "you arrange this
    server's rooms" is the one that ought to say where games live. Kept
    separate from the host gate on purpose; widening that to match this
    would hand any channel manager control of other people's live games.
    """
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return has_mod_or_admin_permissions(interaction.user.guild_permissions)

    return app_commands.check(predicate)


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


#: Channel types the bot can post a panel or a doc into.
POSTABLE_CHANNELS = (discord.TextChannel, discord.Thread, discord.VoiceChannel)


async def resolve_postable_channel_in_guild(
    bot, channel_id: int, guild: discord.Guild | None = None
):
    """A postable channel by id, refusing anything outside ``guild``.

    This is a permission check, not a convenience. ``channel_id`` arrives from
    a dashboard route, and both ``get_channel`` and ``fetch_channel`` search
    every guild the bot is in — so without the ownership test a moderator of
    one guild could post, edit and pin into a co-tenant guild's channels. The
    docs sync and the role-menu sync each carried this verbatim; a rule about
    who may write where should have exactly one statement.

    ``guild`` is None only on paths acting on an already-stored placement,
    where the channel was vetted when it was first chosen.
    """
    channel = await resolve_bot_channel(bot, channel_id)
    if not isinstance(channel, POSTABLE_CHANNELS):
        return None
    owner = getattr(channel, "guild", None)
    if guild is not None and owner is not None and owner.id != guild.id:
        return None
    return channel


def jump_url(guild_id: int | Literal["@me"], channel_id: int, message_id: int) -> str:
    """A permalink to one message.

    Prefer ``message.jump_url`` when you have the message object; this is for
    callers working from stored ids, which would otherwise have to fetch a
    message purely to read the property off it. A channel-only link loses the
    message and lands the reader at the bottom of the channel instead — see
    the style guide's "Pointing at things" for when that is nonetheless right.

    ``guild_id`` takes the literal ``"@me"`` for a message in a DM, which is
    the form Discord itself uses. That one string is the whole widening — ids
    still arrive as ``int``, so a stringified snowflake from the web layer is
    a type error rather than a link that happens to work.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def channel_url(guild_id: int | Literal["@me"], channel_id: int) -> str:
    """A deep link to a channel, for when the channel *is* the subject.

    Its sibling ``jump_url`` is the default — see the style guide's "Pointing
    at things". Reach for this one only where there is no message to land on
    (a voice room to join, a channel to go start something in), or as the
    degraded form when a stored message id is missing.
    """
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


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
