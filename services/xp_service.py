"""XP system service layer - business logic for XP awards and level progression."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from utils import format_user_for_log, get_guild_channel_or_thread
from xp_system import (
    DEFAULT_XP_SETTINGS,
    AwardResult,
    XpSettings,
    is_channel_xp_eligible,
)

if TYPE_CHECKING:
    GuildTextLike = discord.TextChannel | discord.Thread

log = logging.getLogger("dungeonkeeper.xp_service")


def channel_is_xp_allowed(
    channel: GuildTextLike,
    excluded_channel_ids: set[int],
) -> bool:
    """Check if XP should be awarded in a channel."""
    channel_id = getattr(channel, "id", None)
    if channel_id is None:
        return False
    parent_id = getattr(channel, "parent_id", None)
    return is_channel_xp_eligible(channel_id, parent_id, excluded_channel_ids)


async def maybe_grant_level_role(
    member: discord.Member,
    new_level: int,
    level_role_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    db_path: Path | None = None,
) -> None:
    """Grant a level reward role to a member if they qualify."""
    if level_role_id <= 0:
        log.debug(
            "Skipping level %s role grant for %s: reward role is not configured.",
            settings.role_grant_level,
            format_user_for_log(member),
        )
        return

    if new_level < settings.role_grant_level:
        log.debug(
            "Skipping level %s role grant for %s: member level is %s.",
            settings.role_grant_level,
            format_user_for_log(member),
            new_level,
        )
        return

    role = member.guild.get_role(level_role_id)
    if role is None:
        log.warning(
            "Level %s reward role %s was not found.",
            settings.role_grant_level,
            level_role_id,
        )
        return

    if role in member.roles:
        log.debug(
            "Skipping level %s role grant for %s: role %s is already assigned.",
            settings.role_grant_level,
            format_user_for_log(member),
            role.id,
        )
        return

    try:
        await member.add_roles(
            role, reason=f"Reached level {settings.role_grant_level}"
        )
        log.info(
            "Granted level %s reward role %s to %s.",
            settings.role_grant_level,
            role.id,
            format_user_for_log(member),
        )
        if db_path is not None:
            from db_utils import open_db
            from xp_system import log_role_event

            with open_db(db_path) as conn:
                log_role_event(conn, member.guild.id, member.id, role.name, "grant")
    except discord.Forbidden:
        log.warning(
            "Missing permission to grant level reward role %s to %s.",
            role.id,
            member,
        )


async def maybe_log_level_5(
    member: discord.Member,
    total_xp: float,
    level_5_log_channel_id: int,
    level_5_role_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> None:
    """Log a level 5 achievement announcement."""
    if level_5_log_channel_id <= 0:
        log.debug(
            "Skipping level %s announcement for %s: level-5 log channel is not configured.",
            settings.role_grant_level,
            format_user_for_log(member),
        )
        return

    channel = get_guild_channel_or_thread(member.guild, level_5_log_channel_id)
    if channel is None:
        log.warning(
            "Level %s log channel %s was not found.",
            settings.role_grant_level,
            level_5_log_channel_id,
        )
        return

    reward_role = (
        member.guild.get_role(level_5_role_id) if level_5_role_id > 0 else None
    )
    embed = discord.Embed(
        title=f"Level {settings.role_grant_level} reached",
        description=f"{member.mention} just reached level {settings.role_grant_level}.",
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Total XP", value=f"{total_xp:.2f}", inline=True)
    if reward_role is not None:
        embed.add_field(name="Reward Role", value=reward_role.mention, inline=True)
    if member.joined_at is not None:
        joined_ts = int(member.joined_at.timestamp())
        embed.add_field(
            name="Joined",
            value=f"<t:{joined_ts}:F> (<t:{joined_ts}:R>)",
            inline=False,
        )
    embed.set_thumbnail(url=member.display_avatar.url)

    log.info(
        "Attempting level %s announcement for %s in channel %s (total_xp=%.2f).",
        settings.role_grant_level,
        format_user_for_log(member),
        level_5_log_channel_id,
        total_xp,
    )
    try:
        await channel.send(embed=embed)
        log.info(
            "Sent level %s announcement for %s to channel %s.",
            settings.role_grant_level,
            format_user_for_log(member),
            level_5_log_channel_id,
        )
    except discord.Forbidden:
        log.warning(
            "Missing permission to send level %s announcements in channel %s.",
            settings.role_grant_level,
            level_5_log_channel_id,
        )
    except discord.HTTPException:
        log.exception(
            "Discord API error while sending level %s announcement in channel %s for %s.",
            settings.role_grant_level,
            level_5_log_channel_id,
            format_user_for_log(member),
        )


async def maybe_log_level_ups(
    member: discord.Member,
    old_level: int,
    new_level: int,
    total_xp: float,
    level_up_log_channel_id: int,
    level_5_log_channel_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> None:
    """Log level-up announcements for all levels between old and new."""
    if level_up_log_channel_id <= 0:
        log.debug(
            "Skipping level-up announcements for %s: level-up log channel is not configured.",
            format_user_for_log(member),
        )
        return

    if new_level <= old_level:
        log.debug(
            "Skipping level-up announcements for %s: no level change (%s -> %s).",
            format_user_for_log(member),
            old_level,
            new_level,
        )
        return

    channel = get_guild_channel_or_thread(member.guild, level_up_log_channel_id)
    if channel is None:
        log.warning(
            "Level-up log channel %s was not found.",
            level_up_log_channel_id,
        )
        return

    skip_special_level = level_up_log_channel_id == level_5_log_channel_id
    for level in range(old_level + 1, new_level + 1):
        if skip_special_level and level == settings.role_grant_level:
            log.debug(
                "Skipping level %s in general level-up channel "
                "because it matches the dedicated level-%s channel.",
                level,
                settings.role_grant_level,
            )
            continue

        embed = discord.Embed(
            title=f"Level {level} reached",
            description=f"{member.mention} leveled up to level {level}.",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Total XP", value=f"{total_xp:.2f}", inline=True)
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed)
            log.debug(
                "Sent level-up announcement for %s at level %s to channel %s.",
                format_user_for_log(member),
                level,
                level_up_log_channel_id,
            )
        except discord.Forbidden:
            log.warning(
                "Missing permission to send level-up announcements in channel %s.",
                level_up_log_channel_id,
            )
            return
        except discord.HTTPException:
            log.exception(
                "Discord API error while sending level-up announcement in channel %s for %s.",
                level_up_log_channel_id,
                format_user_for_log(member),
            )
            return


async def handle_level_progress(
    member: discord.Member,
    award: AwardResult,
    source: str,
    level_5_role_id: int,
    level_up_log_channel_id: int,
    level_5_log_channel_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    db_path: Path | None = None,
) -> None:
    """Handle role grants and announcements when a member levels up."""
    log.debug(
        "Level progress check (source=%s) for %s: old_level=%s new_level=%s total_xp=%.2f role_grant_due=%s "
        "(role_id=%s levelup_log_channel=%s level5_log_channel=%s).",
        source,
        format_user_for_log(member),
        award.old_level,
        award.new_level,
        award.total_xp,
        award.role_grant_due,
        level_5_role_id,
        level_up_log_channel_id,
        level_5_log_channel_id,
    )

    if award.new_level >= settings.role_grant_level:
        await maybe_grant_level_role(
            member, award.new_level, level_5_role_id, settings, db_path
        )

    if award.new_level > award.old_level:
        await maybe_log_level_ups(
            member,
            award.old_level,
            award.new_level,
            award.total_xp,
            level_up_log_channel_id,
            level_5_log_channel_id,
            settings,
        )
        if award.role_grant_due:
            log.info(
                "Level %s trigger fired for %s from source=%s (old_level=%s new_level=%s total_xp=%.2f).",
                settings.role_grant_level,
                format_user_for_log(member),
                source,
                award.old_level,
                award.new_level,
                award.total_xp,
            )
            await maybe_log_level_5(
                member,
                award.total_xp,
                level_5_log_channel_id,
                level_5_role_id,
                settings,
            )
        else:
            log.debug(
                "No level %s trigger for %s from source=%s (old_level=%s new_level=%s).",
                settings.role_grant_level,
                format_user_for_log(member),
                source,
                award.old_level,
                award.new_level,
            )
