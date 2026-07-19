"""XP system service layer - business logic for XP awards and level progression."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import format_user_for_log, get_guild_channel_or_thread
from bot_modules.core.xp_system import (
    DEFAULT_XP_SETTINGS,
    AwardResult,
    XpSettings,
    is_channel_xp_eligible,
    role_grant_due,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import GuildConfig

    GuildTextLike = discord.TextChannel | discord.Thread

log = logging.getLogger("dungeonkeeper.xp_service")


class LevelRoleDecision(Enum):
    """Outcome of the level-role eligibility check."""

    GRANT = "grant"
    SKIP_NOT_CONFIGURED = "skip_not_configured"
    SKIP_BELOW_THRESHOLD = "skip_below_threshold"
    SKIP_ROLE_MISSING = "skip_role_missing"
    SKIP_ALREADY_HAS = "skip_already_has"


def nsfw_grant_role_id(grant_roles: dict) -> int:
    """The guild's configured NSFW/"spicy" access role id, or 0 if unset."""
    cfg = grant_roles.get("nsfw")
    return int(cfg["role_id"]) if cfg else 0


def candidates_missing_grant_check(
    level_by_user: dict[int, int], inactive_user_ids: set[int]
) -> list[tuple[int, int]]:
    """``(user_id, level)`` pairs worth checking for a missing grant role.

    Excludes members currently on an inactive-channel hold: their roles were
    stripped on purpose when they went inactive, not skipped by mistake, so
    they shouldn't show up as "missing" a grant. Sorted highest level first.
    """
    out = [
        (uid, lvl) for uid, lvl in level_by_user.items() if uid not in inactive_user_ids
    ]
    out.sort(key=lambda p: -p[1])
    return out


# A level-5 crossing this fresh reads as a burst, not a track record — the
# promotion-review post waits for the member to clear this tenure bar.
PROMOTION_REVIEW_MIN_TENURE = timedelta(days=2)


def record_pending_promotion_post(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    total_xp: float,
    eligible_at: float,
    *,
    now: float | None = None,
) -> None:
    """Park a level-5 promotion post until the member clears the tenure bar."""
    conn.execute(
        "INSERT INTO pending_promotion_posts "
        "(guild_id, user_id, total_xp, eligible_at, created_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (guild_id, user_id) DO UPDATE SET "
        "total_xp=excluded.total_xp, eligible_at=excluded.eligible_at",
        (guild_id, user_id, total_xp, eligible_at, now if now is not None else eligible_at),
    )
    conn.commit()


def list_due_pending_promotion_posts(
    conn: sqlite3.Connection, now: float
) -> list[sqlite3.Row]:
    """Pending promotion posts whose tenure bar has now cleared, across all guilds."""
    return conn.execute(
        "SELECT guild_id, user_id, total_xp FROM pending_promotion_posts WHERE eligible_at <= ?",
        (now,),
    ).fetchall()


def delete_pending_promotion_post(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> None:
    conn.execute(
        "DELETE FROM pending_promotion_posts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    conn.commit()


def should_grant_level_role(
    new_level: int,
    role_grant_level: int,
    level_role_id: int,
    role_exists: bool,
    member_already_has_role: bool,
) -> LevelRoleDecision:
    """Decide whether a member qualifies for the level-reward role.

    Checks run in fixed priority order so a skip reason stays stable even if
    later inputs change:
      1. Reward role not configured (id <= 0)   → SKIP_NOT_CONFIGURED
      2. Member's level below threshold         → SKIP_BELOW_THRESHOLD
      3. Configured role doesn't exist in guild → SKIP_ROLE_MISSING
      4. Member already has the role            → SKIP_ALREADY_HAS
      5. Otherwise                              → GRANT
    """
    if level_role_id <= 0:
        return LevelRoleDecision.SKIP_NOT_CONFIGURED
    if new_level < role_grant_level:
        return LevelRoleDecision.SKIP_BELOW_THRESHOLD
    if not role_exists:
        return LevelRoleDecision.SKIP_ROLE_MISSING
    if member_already_has_role:
        return LevelRoleDecision.SKIP_ALREADY_HAS
    return LevelRoleDecision.GRANT


def channel_is_xp_allowed(
    channel: GuildTextLike,
    excluded_channel_ids: frozenset[int] | set[int],
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
    role = (
        member.guild.get_role(level_role_id) if level_role_id > 0 else None
    )
    decision = should_grant_level_role(
        new_level=new_level,
        role_grant_level=settings.role_grant_level,
        level_role_id=level_role_id,
        role_exists=role is not None,
        member_already_has_role=role is not None and role in member.roles,
    )

    if decision is LevelRoleDecision.SKIP_NOT_CONFIGURED:
        log.debug(
            "Skipping level %s role grant for %s: reward role is not configured.",
            settings.role_grant_level,
            format_user_for_log(member),
        )
        return
    if decision is LevelRoleDecision.SKIP_BELOW_THRESHOLD:
        log.debug(
            "Skipping level %s role grant for %s: member level is %s.",
            settings.role_grant_level,
            format_user_for_log(member),
            new_level,
        )
        return
    if decision is LevelRoleDecision.SKIP_ROLE_MISSING:
        log.warning(
            "Level %s reward role %s was not found.",
            settings.role_grant_level,
            level_role_id,
        )
        return
    if decision is LevelRoleDecision.SKIP_ALREADY_HAS:
        assert role is not None  # Guaranteed by decision branch
        log.debug(
            "Skipping level %s role grant for %s: role %s is already assigned.",
            settings.role_grant_level,
            format_user_for_log(member),
            role.id,
        )
        return

    assert role is not None  # decision == GRANT implies role exists
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
            from bot_modules.core.db_utils import open_db
            from bot_modules.core.xp_system import log_role_event

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
    *,
    nsfw_role_id: int = 0,
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
    if nsfw_role_id > 0:
        has_nsfw = any(r.id == nsfw_role_id for r in member.roles)
        embed.add_field(
            name="Spicy access",
            value="✅ Granted" if has_nsfw else "❌ Not granted",
            inline=True,
        )
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
    announced_level: int,
    new_level: int,
    total_xp: float,
    level_up_log_channel_id: int,
    level_5_log_channel_id: int,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> int:
    """Announce every level the member has reached but not yet been told about.

    Starts from ``announced_level``, not from this award's starting level, so
    levels won through a silent path (a quest payout credits XP with no
    Discord handle in scope) are delivered here on the member's next ordinary
    award rather than lost.

    Returns the highest level now considered announced, for the caller to
    persist. A guild with no level-up channel returns ``new_level``: there is
    nothing to deliver, and holding a backlog would dump every member's whole
    history into the channel the day one is configured. A failed send returns
    the last level that did land, so the rest retry on the next award.
    """
    if level_up_log_channel_id <= 0:
        log.debug(
            "Skipping level-up announcements for %s: level-up log channel is not configured.",
            format_user_for_log(member),
        )
        return new_level

    if new_level <= announced_level:
        log.debug(
            "Skipping level-up announcements for %s: nothing new to announce (announced=%s current=%s).",
            format_user_for_log(member),
            announced_level,
            new_level,
        )
        return announced_level

    channel = get_guild_channel_or_thread(member.guild, level_up_log_channel_id)
    if channel is None:
        log.warning(
            "Level-up log channel %s was not found.",
            level_up_log_channel_id,
        )
        return new_level

    delivered = announced_level
    skip_special_level = level_up_log_channel_id == level_5_log_channel_id
    for level in range(announced_level + 1, new_level + 1):
        if skip_special_level and level == settings.role_grant_level:
            log.debug(
                "Skipping level %s in general level-up channel "
                "because it matches the dedicated level-%s channel.",
                level,
                settings.role_grant_level,
            )
            # Counts as delivered: maybe_log_level_5 posts this one.
            delivered = level
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
            delivered = level
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
            return delivered
        except discord.HTTPException:
            log.exception(
                "Discord API error while sending level-up announcement in channel %s for %s.",
                level_up_log_channel_id,
                format_user_for_log(member),
            )
            return delivered

    return delivered


async def handle_level_progress(
    member: discord.Member,
    award: AwardResult,
    source: str,
    *,
    level_5_role_id: int,
    level_up_log_channel_id: int,
    level_5_log_channel_id: int,
    db_path: Path,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    nsfw_role_id: int = 0,
) -> None:
    """Handle role grants and announcements when a member levels up.

    ``db_path`` is required: announcing without recording the mark would
    replay the same levels on every subsequent award.
    """
    log.debug(
        "Level progress check (source=%s) for %s: old_level=%s new_level=%s announced_level=%s "
        "total_xp=%.2f role_grant_due=%s (role_id=%s levelup_log_channel=%s level5_log_channel=%s).",
        source,
        format_user_for_log(member),
        award.old_level,
        award.new_level,
        award.announced_level,
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

    if award.new_level > award.announced_level:
        delivered = await maybe_log_level_ups(
            member,
            award.announced_level,
            award.new_level,
            award.total_xp,
            level_up_log_channel_id,
            level_5_log_channel_id,
            settings,
        )
        if delivered > award.announced_level:
            from bot_modules.core.db_utils import open_db
            from bot_modules.core.xp_system import mark_level_announced
            from bot_modules.services.economy_quests_service import (
                fire_trigger_inline,
            )

            with open_db(db_path) as conn:
                mark_level_announced(conn, member.guild.id, member.id, delivered)
                # Quest hook: level_up fires per level actually announced —
                # announce-time (not award-time) keeps it out of the quest-XP
                # payout path, so a quest reward's own XP can't recurse into
                # another quest claim mid-transaction. Occurrence = the level
                # number: reaching level 7 pays once, ever.
                for lvl in range(award.announced_level + 1, delivered + 1):
                    fire_trigger_inline(
                        conn, member.guild.id, "level_up", member.id,
                        occurrence=str(lvl),
                    )

        # Gate the promotion post on what was actually delivered, not on
        # award.role_grant_due: that flag is measured from announced_level, so
        # if the general channel send fails at the threshold level the mark
        # never advances and the flag stays true, re-posting the promotion on
        # every subsequent award. Keyed to `delivered`, the two channels retry
        # together and each level is posted once.
        if role_grant_due(award.announced_level, delivered, settings):
            log.info(
                "Level %s trigger fired for %s from source=%s (old_level=%s new_level=%s total_xp=%.2f).",
                settings.role_grant_level,
                format_user_for_log(member),
                source,
                award.old_level,
                award.new_level,
                award.total_xp,
            )
            eligible_at = (
                (member.joined_at + PROMOTION_REVIEW_MIN_TENURE).timestamp()
                if member.joined_at is not None
                else 0.0
            )
            if member.joined_at is None or eligible_at <= datetime.now(timezone.utc).timestamp():
                await maybe_log_level_5(
                    member,
                    award.total_xp,
                    level_5_log_channel_id,
                    level_5_role_id,
                    settings,
                    nsfw_role_id=nsfw_role_id,
                )
            else:
                log.info(
                    "Deferring level %s promotion post for %s until tenure clears (eligible_at=%s).",
                    settings.role_grant_level,
                    format_user_for_log(member),
                    eligible_at,
                )
                from bot_modules.core.db_utils import open_db

                with open_db(db_path) as conn:
                    record_pending_promotion_post(
                        conn, member.guild.id, member.id, award.total_xp, eligible_at
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


async def promotion_review_recheck_loop(
    bot: discord.Client,
    db_path: Path,
    guild_config_for: Callable[[int], GuildConfig],
    *,
    interval_seconds: int = 1800,
) -> None:
    """Fire promotion posts deferred by ``handle_level_progress`` once due.

    A pending row is dropped (not retried) once handled — the member left,
    the guild is gone, or the post was attempted. This matches the
    best-effort reliability of the immediate post path, which likewise
    doesn't retry a failed Discord send beyond logging it.
    """
    from bot_modules.core.db_utils import open_db

    await bot.wait_until_ready()

    while not bot.is_closed():
        try:
            with open_db(db_path) as conn:
                due = list_due_pending_promotion_posts(conn, datetime.now(timezone.utc).timestamp())

            for row in due:
                guild_id, user_id, total_xp = row["guild_id"], row["user_id"], row["total_xp"]
                guild = bot.get_guild(guild_id)
                member = guild.get_member(user_id) if guild is not None else None
                if guild is not None and member is None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except discord.NotFound:
                        member = None
                    except discord.HTTPException:
                        log.exception(
                            "Failed to fetch member %s in guild %s for a deferred promotion post.",
                            user_id, guild_id,
                        )
                        continue

                if guild is not None and member is not None:
                    cfg = guild_config_for(guild_id)
                    await maybe_log_level_5(
                        member,
                        total_xp,
                        cfg.level_5_log_channel_id,
                        cfg.level_5_role_id,
                        cfg.xp_settings,
                        nsfw_role_id=nsfw_grant_role_id(cfg.grant_roles),
                    )

                with open_db(db_path) as conn:
                    delete_pending_promotion_post(conn, guild_id, user_id)
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            log.exception("Promotion review recheck hit a SQLite operational error.")
        except Exception:
            log.exception("Promotion review recheck failed.")

        await asyncio.sleep(interval_seconds)
