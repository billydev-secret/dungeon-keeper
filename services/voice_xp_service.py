"""Voice XP award service - handles XP from voice channel participation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

import discord

from db_utils import open_db
from utils import format_user_for_log
from xp_system import (
    DEFAULT_XP_SETTINGS,
    XP_SOURCE_VOICE,
    XpSettings,
    apply_xp_award,
    completed_voice_intervals,
    delete_voice_session,
    get_voice_session,
    list_voice_sessions,
    set_voice_session,
)

if TYPE_CHECKING:
    from pathlib import Path

    from xp_system import AwardResult

log = logging.getLogger("dungeonkeeper.voice_xp")


def is_qualifying_voice_channel(
    channel: discord.VoiceChannel,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> bool:
    """Check if a voice channel qualifies for XP awards."""
    afk_channel = channel.guild.afk_channel
    if afk_channel and channel.id == afk_channel.id:
        return False

    human_count = sum(1 for member in channel.members if not member.bot)
    return human_count >= settings.voice_min_humans


async def process_voice_xp_tick(
    bot: discord.Client,
    db_path: Path,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
) -> dict[tuple[int, int], tuple[discord.Member, AwardResult]]:
    """
    Process one voice XP tick, awarding XP to qualifying members.

    Returns a dict of (guild_id, member_id) -> (member, award_result) for level-up handling.
    """
    leveled_members: dict[tuple[int, int], tuple[discord.Member, AwardResult]] = {}
    active_members: set[tuple[int, int]] = set()
    now_ts = time.time()

    with open_db(db_path) as conn:
        for guild in bot.guilds:
            for channel in guild.voice_channels:
                human_members = [member for member in channel.members if not member.bot]
                if not human_members:
                    continue

                qualifies = is_qualifying_voice_channel(channel, settings)
                for member in human_members:
                    active_members.add((guild.id, member.id))
                    session = get_voice_session(conn, guild.id, member.id)

                    if session is None or session.channel_id != channel.id:
                        set_voice_session(
                            conn,
                            guild.id,
                            member.id,
                            channel.id,
                            session_started_at=now_ts,
                            qualified_since=now_ts if qualifies else None,
                            awarded_intervals=0,
                        )
                        continue

                    if not qualifies:
                        if (
                            session.qualified_since is not None
                            or session.awarded_intervals != 0
                        ):
                            set_voice_session(
                                conn,
                                guild.id,
                                member.id,
                                channel.id,
                                session_started_at=now_ts,
                                qualified_since=None,
                                awarded_intervals=0,
                            )
                        continue

                    if session.qualified_since is None:
                        set_voice_session(
                            conn,
                            guild.id,
                            member.id,
                            channel.id,
                            session_started_at=now_ts,
                            qualified_since=now_ts,
                            awarded_intervals=0,
                        )
                        continue

                    intervals_due = completed_voice_intervals(session, now_ts, settings)
                    if intervals_due <= 0:
                        continue

                    set_voice_session(
                        conn,
                        guild.id,
                        member.id,
                        channel.id,
                        session_started_at=session.session_started_at,
                        qualified_since=session.qualified_since,
                        awarded_intervals=session.awarded_intervals + intervals_due,
                    )
                    award = apply_xp_award(
                        conn,
                        guild.id,
                        member.id,
                        intervals_due * settings.voice_award_xp,
                        event_source=XP_SOURCE_VOICE,
                        event_timestamp=now_ts,
                        channel_id=channel.id,
                        settings=settings,
                    )
                    if award.awarded_xp > 0:
                        log.debug(
                            "Awarded %.2f voice XP to %s in voice channel %s (total=%.2f level=%s).",
                            award.awarded_xp,
                            format_user_for_log(member),
                            channel.id,
                            award.total_xp,
                            award.new_level,
                        )
                        leveled_members[(guild.id, member.id)] = (member, award)

        for session in list_voice_sessions(conn):
            if (session.guild_id, session.user_id) not in active_members:
                delete_voice_session(conn, session.guild_id, session.user_id)

    return leveled_members


async def voice_xp_loop(
    bot: discord.Client,
    db_path: Path,
    handle_level_progress_callback,
    settings: XpSettings = DEFAULT_XP_SETTINGS,
    settings_getter=None,
) -> None:
    """Background task that periodically awards voice XP.

    Pass ``settings_getter`` as a zero-argument callable (e.g. ``lambda: ctx.xp_settings``)
    so each tick picks up live config changes without restarting the loop.
    If omitted, the static ``settings`` snapshot is used for every tick.
    """
    await bot.wait_until_ready()

    while not bot.is_closed():
        current_settings = settings_getter() if settings_getter is not None else settings
        try:
            leveled_members = await process_voice_xp_tick(bot, db_path, current_settings)
            if leveled_members:
                log.info(
                    "Voice XP tick awarded XP to %d member(s): %s",
                    len(leveled_members),
                    ", ".join(
                        f"{m.display_name} (+{a.awarded_xp:.1f} XP, total={a.total_xp:.1f}, lvl {a.new_level})"
                        for m, a in leveled_members.values()
                    ),
                )
            for member, award in leveled_members.values():
                await handle_level_progress_callback(member, award, source="voice_tick")
        except asyncio.CancelledError:
            raise
        except sqlite3.OperationalError:
            log.exception("Voice XP tick hit a SQLite operational error.")
        except Exception:
            log.exception("Voice XP tick failed.")

        await asyncio.sleep(current_settings.voice_poll_seconds)
