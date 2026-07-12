"""Tests for the daily voice login wired into process_voice_xp_tick."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import DEFAULT_XP_SETTINGS, set_voice_session
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.voice_xp_service import (
    VOICE_LOGIN_MIN_SECONDS,
    process_voice_xp_tick,
)
from migrations import apply_migrations_sync

VOICE_GID = 555
VOICE_UID = 91
CHANNEL_ID = 300


@pytest.fixture
def voice_db(tmp_path):
    db_path = tmp_path / "voice.db"
    apply_migrations_sync(db_path)
    return db_path


def _enable(voice_db) -> None:
    with open_db(voice_db) as conn:
        save_econ_settings(conn, VOICE_GID, {"enabled": True})


def _vmember(uid: int, *, is_bot: bool = False, premium: object | None = None) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.bot = is_bot
    m.premium_since = premium
    m.display_name = f"u{uid}"
    m.name = f"u{uid}"
    return m


def _bot(members: list[MagicMock]) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id = CHANNEL_ID
    channel.members = members
    guild = MagicMock()
    guild.id = VOICE_GID
    guild.afk_channel = None
    guild.voice_channels = [channel]
    channel.guild = guild
    bot = MagicMock()
    bot.guilds = [guild]
    return bot


def _seed_session(voice_db, *, qualified_ago: float) -> None:
    now = time.time()
    with open_db(voice_db) as conn:
        set_voice_session(
            conn,
            VOICE_GID,
            VOICE_UID,
            CHANNEL_ID,
            session_started_at=now - qualified_ago,
            qualified_since=now - qualified_ago,
            awarded_intervals=100,  # high → no interval XP interferes with login
        )


async def test_voice_login_fires_past_the_five_minute_bar(voice_db):
    _enable(voice_db)
    member = _vmember(VOICE_UID)
    other = _vmember(VOICE_UID + 1)  # second human so the channel qualifies
    bot = _bot([member, other])
    _seed_session(voice_db, qualified_ago=VOICE_LOGIN_MIN_SECONDS + 100)

    await process_voice_xp_tick(bot, voice_db, DEFAULT_XP_SETTINGS)

    with open_db(voice_db) as conn:
        login = conn.execute(
            "SELECT source, paid FROM econ_logins WHERE guild_id=? AND user_id=?",
            (VOICE_GID, VOICE_UID),
        ).fetchone()
        balance = get_balance(conn, VOICE_GID, VOICE_UID)
    assert login is not None
    assert login["source"] == "voice"
    assert balance > 0


async def test_voice_login_waits_for_the_bar(voice_db):
    _enable(voice_db)
    bot = _bot([_vmember(VOICE_UID), _vmember(VOICE_UID + 1)])
    _seed_session(voice_db, qualified_ago=VOICE_LOGIN_MIN_SECONDS - 100)

    await process_voice_xp_tick(bot, voice_db, DEFAULT_XP_SETTINGS)

    with open_db(voice_db) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM econ_logins").fetchone()["c"]
    assert count == 0


async def test_voice_login_skips_disabled_guild(voice_db):
    # economy left disabled — no login even well past the bar
    bot = _bot([_vmember(VOICE_UID), _vmember(VOICE_UID + 1)])
    _seed_session(voice_db, qualified_ago=VOICE_LOGIN_MIN_SECONDS + 100)

    await process_voice_xp_tick(bot, voice_db, DEFAULT_XP_SETTINGS)

    with open_db(voice_db) as conn:
        count = conn.execute("SELECT COUNT(*) c FROM econ_logins").fetchone()["c"]
    assert count == 0


async def test_voice_login_is_once_per_day(voice_db):
    _enable(voice_db)
    bot = _bot([_vmember(VOICE_UID), _vmember(VOICE_UID + 1)])
    _seed_session(voice_db, qualified_ago=VOICE_LOGIN_MIN_SECONDS + 100)

    await process_voice_xp_tick(bot, voice_db, DEFAULT_XP_SETTINGS)
    with open_db(voice_db) as conn:
        first = get_balance(conn, VOICE_GID, VOICE_UID)
    # Re-seed the session (the tick advanced it) and tick again the same day.
    _seed_session(voice_db, qualified_ago=VOICE_LOGIN_MIN_SECONDS + 100)
    await process_voice_xp_tick(bot, voice_db, DEFAULT_XP_SETTINGS)
    with open_db(voice_db) as conn:
        second = get_balance(conn, VOICE_GID, VOICE_UID)
        count = conn.execute("SELECT COUNT(*) c FROM econ_logins").fetchone()["c"]
    assert first == second  # econ_logins PK blocks a second same-day payout
    assert count == 1
