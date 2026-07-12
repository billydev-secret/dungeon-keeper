"""Tests for message_xp_service — award split + reaction-given XP."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import DEFAULT_XP_SETTINGS, XP_SOURCE_REACTION_GIVEN
from bot_modules.services.message_xp_service import (
    award_reaction_given_xp,
    split_award_into_text_and_reply,
)
from migrations import apply_migrations_sync


# ── no reply bonus ────────────────────────────────────────────────────


def test_no_reply_bonus_all_text():
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=0.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 10.0
    assert reply == 0.0


def test_negative_reply_bonus_treated_as_none():
    # Defensive: breakdown should never have negative reply_bonus_xp, but
    # if it did we'd still want the split to stay consistent.
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=-5.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 10.0
    assert reply == 0.0


def test_zero_total_zero_result():
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=0.0,
        reply_bonus_xp=0.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 0.0
    assert reply == 0.0


# ── with reply bonus, no multiplier penalties ────────────────────────


def test_reply_bonus_at_full_multipliers():
    # Total was 10 (8 text + 2 reply), no penalties
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 2.0
    assert text == 8.0


# ── with multipliers applied ──────────────────────────────────────────


def test_reply_scaled_by_cooldown():
    # reply_bonus=2.0, cooldown=0.5 → reply_award=1.0
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=5.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=0.5,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 1.0
    assert text == 4.0


def test_reply_scaled_by_all_multipliers():
    # reply_bonus=4.0, cooldown=0.5, duplicate=0.5, pair=0.5 → reply=0.5
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=3.0,
        reply_bonus_xp=4.0,
        cooldown_multiplier=0.5,
        duplicate_multiplier=0.5,
        pair_multiplier=0.5,
    )
    assert reply == 0.5
    assert text == 2.5


def test_zero_multiplier_kills_reply_award():
    # Duplicate multiplier of 0 means duplicate message → no reply XP
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=5.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=0.0,
        pair_multiplier=1.0,
    )
    assert reply == 0.0
    assert text == 5.0


# ── rounding and floor behavior ──────────────────────────────────────


def test_results_rounded_to_two_decimals():
    # 1/3 = 0.333... → rounded
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=1.0,
        reply_bonus_xp=1.0,
        cooldown_multiplier=1 / 3,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 0.33
    assert text == 0.67


def test_text_award_floored_at_zero():
    # If reply_award somehow exceeds total (rounding quirk / weird breakdown),
    # text should floor at 0, never go negative.
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=1.0,
        reply_bonus_xp=5.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 5.0
    assert text == 0.0
    assert text >= 0


# ── invariant: split always sums close to total ──────────────────────


@pytest.mark.parametrize(
    "total,reply_bonus,cd,dup,pair",
    [
        (10.0, 2.0, 1.0, 1.0, 1.0),
        (5.0, 2.0, 0.5, 1.0, 1.0),
        (8.5, 3.0, 0.75, 0.9, 0.8),
        (100.0, 20.0, 1.0, 1.0, 1.0),
    ],
)
def test_sum_within_rounding_of_total(total, reply_bonus, cd, dup, pair):
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=total,
        reply_bonus_xp=reply_bonus,
        cooldown_multiplier=cd,
        duplicate_multiplier=dup,
        pair_multiplier=pair,
    )
    # Sum matches total within a couple of cents (2x rounding tolerance)
    assert abs((text + reply) - total) <= 0.02


# ── award_reaction_given_xp ──────────────────────────────────────────

REACT_GID = 321
REACTOR_ID = 42
AUTHOR_ID = 7
CHANNEL_ID = 10
MESSAGE_ID = 555


@pytest.fixture
def react_db(tmp_path):
    db_path = tmp_path / "react.db"
    apply_migrations_sync(db_path)
    return db_path


def _member(uid: int, *, is_bot: bool = False) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.bot = is_bot
    m.display_name = f"user-{uid}"
    m.name = f"user-{uid}"
    return m


def _payload(*, reactor_id: int = REACTOR_ID) -> MagicMock:
    p = MagicMock(spec=discord.RawReactionActionEvent)
    p.guild_id = REACT_GID
    p.user_id = reactor_id
    p.channel_id = CHANNEL_ID
    p.message_id = MESSAGE_ID
    p.emoji = "🔥"
    return p


def _bot(*, reactor: MagicMock, author: MagicMock) -> tuple[MagicMock, MagicMock]:
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 1
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL_ID
    channel.parent_id = None
    message = MagicMock(spec=discord.Message)
    message.id = MESSAGE_ID
    message.author = author
    channel.fetch_message = AsyncMock(return_value=message)
    guild = MagicMock()
    guild.id = REACT_GID
    guild.get_channel_or_thread = MagicMock(return_value=channel)
    guild.get_member = MagicMock(return_value=reactor)
    bot.get_guild = MagicMock(return_value=guild)
    return bot, channel


async def _award(bot, db, payload, *, message=None):
    return await award_reaction_given_xp(
        payload,
        bot=bot,
        db_path=db,
        excluded_channel_ids=set(),
        settings=DEFAULT_XP_SETTINGS,
        message=message,
    )


async def test_reaction_given_awards_reactor_and_dedups(react_db):
    reactor = _member(REACTOR_ID)
    author = _member(AUTHOR_ID)
    bot, _ = _bot(reactor=reactor, author=author)

    result = await _award(bot, react_db, _payload())
    assert result is not None
    who, award = result
    assert who is reactor
    assert award.awarded_xp == DEFAULT_XP_SETTINGS.reaction_given_xp

    with open_db(react_db) as conn:
        events = conn.execute(
            "SELECT source, user_id FROM xp_events WHERE source = ?",
            (XP_SOURCE_REACTION_GIVEN,),
        ).fetchall()
        dedup = conn.execute("SELECT COUNT(*) c FROM xp_reaction_awards").fetchone()["c"]
    assert len(events) == 1
    assert events[0]["user_id"] == REACTOR_ID
    assert dedup == 1

    # A second reaction on the same message by the same reactor earns nothing.
    again = await _award(bot, react_db, _payload())
    assert again is None
    with open_db(react_db) as conn:
        events2 = conn.execute(
            "SELECT COUNT(*) c FROM xp_events WHERE source = ?",
            (XP_SOURCE_REACTION_GIVEN,),
        ).fetchone()["c"]
    assert events2 == 1


async def test_reaction_given_no_self_award(react_db):
    # Reactor is the message author → no XP for reacting to your own message.
    reactor = _member(AUTHOR_ID)
    author = _member(AUTHOR_ID)
    bot, _ = _bot(reactor=reactor, author=author)
    result = await _award(bot, react_db, _payload(reactor_id=AUTHOR_ID))
    assert result is None
    with open_db(react_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM xp_reaction_awards").fetchone()["c"] == 0


async def test_reaction_given_skips_bot_reactor(react_db):
    reactor = _member(REACTOR_ID, is_bot=True)
    author = _member(AUTHOR_ID)
    bot, _ = _bot(reactor=reactor, author=author)
    assert await _award(bot, react_db, _payload()) is None


async def test_reaction_given_skips_bot_author(react_db):
    reactor = _member(REACTOR_ID)
    author = _member(AUTHOR_ID, is_bot=True)
    bot, _ = _bot(reactor=reactor, author=author)
    assert await _award(bot, react_db, _payload()) is None


async def test_reaction_given_respects_channel_exclusion(react_db):
    reactor = _member(REACTOR_ID)
    author = _member(AUTHOR_ID)
    bot, _ = _bot(reactor=reactor, author=author)
    result = await award_reaction_given_xp(
        _payload(),
        bot=bot,
        db_path=react_db,
        excluded_channel_ids={CHANNEL_ID},
        settings=DEFAULT_XP_SETTINGS,
    )
    assert result is None


async def test_reaction_given_uses_prefetched_message_no_fetch(react_db):
    reactor = _member(REACTOR_ID)
    author = _member(AUTHOR_ID)
    bot, channel = _bot(reactor=reactor, author=author)
    message = MagicMock(spec=discord.Message)
    message.id = MESSAGE_ID
    message.author = author
    result = await _award(bot, react_db, _payload(), message=message)
    assert result is not None
    channel.fetch_message.assert_not_called()  # reused the pre-fetched message
