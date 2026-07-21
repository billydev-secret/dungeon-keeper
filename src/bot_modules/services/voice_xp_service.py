"""Voice XP award service - handles XP from voice channel participation."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.core.utils import format_user_for_log
from bot_modules.core.xp_system import (
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
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_quests_service import fire_trigger_inline
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    process_login,
)

if TYPE_CHECKING:
    import sqlite3 as _sqlite3
    from pathlib import Path

    from bot_modules.core.xp_system import AwardResult

log = logging.getLogger("dungeonkeeper.voice_xp")

# Sustained qualified voice time (seconds) that earns the daily voice login —
# spec §3.1's 5-minute presence bar. econ_logins' PK keeps it once-per-day.
VOICE_LOGIN_MIN_SECONDS = 300


def _try_voice_login(
    conn: _sqlite3.Connection,
    guild_id: int,
    member: discord.Member,
    now_ts: float,
    settings: EconSettings,
    offset_hours: float,
) -> None:
    """Pay the daily voice login for a member past the presence bar.

    Idempotency lives in ``process_login`` (econ_logins PK); the caller's cheap
    existence check just spares the heavier path on repeat ticks. Fail-safe:
    swallow-and-log so an economy error never aborts the shared tick transaction.
    """
    today = local_day_for(now_ts, offset_hours)
    already = conn.execute(
        "SELECT 1 FROM econ_logins WHERE guild_id = ? AND user_id = ? AND local_day = ? LIMIT 1",
        (guild_id, member.id, today),
    ).fetchone()
    if already is not None:
        return
    try:
        process_login(
            conn,
            settings,
            guild_id,
            member.id,
            local_day=today,
            source="voice",
            booster=member.premium_since is not None,
        )
    except Exception:
        log.exception("voice login failed for member %s in guild %s", member.id, guild_id)


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
    settings_for: Callable[[int], XpSettings] | None = None,
) -> dict[tuple[int, int], tuple[discord.Member, AwardResult]]:
    """
    Process one voice XP tick, awarding XP to qualifying members.

    ``settings_for`` resolves per-guild XP settings (e.g.
    ``lambda gid: ctx.guild_config(gid).xp_settings``); when omitted the static
    ``settings`` snapshot is used for every guild.

    Returns a dict of (guild_id, member_id) -> (member, award_result) for level-up handling.
    """
    leveled_members: dict[tuple[int, int], tuple[discord.Member, AwardResult]] = {}
    active_members: set[tuple[int, int]] = set()
    now_ts = time.time()

    with open_db(db_path) as conn:
        # Lazily resolved per-guild economy config for the daily voice login:
        # None once we know a guild's economy is disabled, so we load at most
        # once per guild per tick and only when a member actually crosses the bar.
        econ_cache: dict[int, tuple[EconSettings, float] | None] = {}

        def _econ_for(gid: int) -> tuple[EconSettings, float] | None:
            if gid not in econ_cache:
                econ_settings = load_econ_settings(conn, gid)
                econ_cache[gid] = (
                    (econ_settings, get_tz_offset_hours(conn, gid))
                    if econ_settings.enabled
                    else None
                )
            return econ_cache[gid]

        for guild in bot.guilds:
            g_settings = settings_for(guild.id) if settings_for is not None else settings
            for channel in guild.voice_channels:
                human_members = [member for member in channel.members if not member.bot]
                if not human_members:
                    continue

                qualifies = is_qualifying_voice_channel(channel, g_settings)
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

                    # Daily voice login once sustained qualified time clears the bar.
                    if now_ts - session.qualified_since >= VOICE_LOGIN_MIN_SECONDS:
                        econ = _econ_for(guild.id)
                        if econ is not None:
                            _try_voice_login(
                                conn, guild.id, member, now_ts, econ[0], econ[1]
                            )

                    intervals_due = completed_voice_intervals(session, now_ts, g_settings)
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
                        intervals_due * g_settings.voice_award_xp,
                        event_source=XP_SOURCE_VOICE,
                        event_timestamp=now_ts,
                        channel_id=channel.id,
                        settings=g_settings,
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
                        # Voice-session quest trigger, keyed to the local day
                        # so repeated ticks collide silently (once/day even on
                        # event quests). Rides the voice-XP award so the
                        # anti-idle qualification rules gate it too.
                        econ = _econ_for(guild.id)
                        if econ is not None and econ[0].enabled:
                            fire_trigger_inline(
                                conn,
                                guild.id,
                                "voice_session",
                                member.id,
                                occurrence=local_day_for(now_ts, econ[1]),
                                booster=member.premium_since is not None,
                            )
                            # voice_partner: distinct co-present humans at an
                            # earning tick (occurrence = the partner, so a
                            # counted quest reads "share voice with N
                            # different people"). Deafened partners are
                            # skipped — parked, not hanging out.
                            for other in channel.members:
                                if other.bot or other.id == member.id:
                                    continue
                                voice = other.voice
                                if voice is not None and (
                                    voice.self_deaf or voice.deaf
                                ):
                                    continue
                                fire_trigger_inline(
                                    conn,
                                    guild.id,
                                    "voice_partner",
                                    member.id,
                                    occurrence=str(other.id),
                                    booster=member.premium_since is not None,
                                )

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
    settings_for: Callable[[int], XpSettings] | None = None,
) -> None:
    """Background task that periodically awards voice XP.

    Pass ``settings_for`` as a per-guild resolver (e.g.
    ``lambda gid: ctx.guild_config(gid).xp_settings``) so each guild uses its own
    XP config. ``settings_getter`` (a zero-arg callable) and the static
    ``settings`` snapshot remain supported as guild-agnostic fallbacks.
    """
    await bot.wait_until_ready()

    while not bot.is_closed():
        current_settings = settings_getter() if settings_getter is not None else settings
        try:
            leveled_members = await process_voice_xp_tick(
                bot, db_path, current_settings, settings_for=settings_for
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
