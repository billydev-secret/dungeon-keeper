"""Tests for services/xp_service.should_grant_level_role."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bot_modules.core.xp_system import AwardResult, init_xp_tables
from bot_modules.services.xp_service import (
    LevelRoleDecision,
    handle_level_progress,
    should_grant_level_role,
)

THRESHOLD = 5
ROLE_ID = 777

_GRANT_OK: dict[str, Any] = dict(
    new_level=THRESHOLD,
    role_grant_level=THRESHOLD,
    level_role_id=ROLE_ID,
    role_exists=True,
    member_already_has_role=False,
)


# ── happy path ────────────────────────────────────────────────────────


def test_grants_when_all_conditions_met():
    assert should_grant_level_role(**_GRANT_OK) is LevelRoleDecision.GRANT


def test_grants_when_above_threshold():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": 10})
        is LevelRoleDecision.GRANT
    )


# ── SKIP_NOT_CONFIGURED ───────────────────────────────────────────────


def test_skip_not_configured_when_role_id_zero():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "level_role_id": 0})
        is LevelRoleDecision.SKIP_NOT_CONFIGURED
    )


def test_skip_not_configured_when_role_id_negative():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "level_role_id": -1})
        is LevelRoleDecision.SKIP_NOT_CONFIGURED
    )


# ── SKIP_BELOW_THRESHOLD ──────────────────────────────────────────────


def test_skip_below_threshold_when_level_lower():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": THRESHOLD - 1})
        is LevelRoleDecision.SKIP_BELOW_THRESHOLD
    )


def test_skip_below_threshold_at_level_zero():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": 0})
        is LevelRoleDecision.SKIP_BELOW_THRESHOLD
    )


def test_threshold_is_inclusive():
    # Exactly at threshold → grant (not below)
    assert (
        should_grant_level_role(**{**_GRANT_OK, "new_level": THRESHOLD})
        is LevelRoleDecision.GRANT
    )


# ── SKIP_ROLE_MISSING ─────────────────────────────────────────────────


def test_skip_role_missing_when_configured_but_absent():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "role_exists": False})
        is LevelRoleDecision.SKIP_ROLE_MISSING
    )


# ── SKIP_ALREADY_HAS ──────────────────────────────────────────────────


def test_skip_already_has_when_member_has_role():
    assert (
        should_grant_level_role(**{**_GRANT_OK, "member_already_has_role": True})
        is LevelRoleDecision.SKIP_ALREADY_HAS
    )


# ── priority ordering ─────────────────────────────────────────────────


def test_not_configured_beats_below_threshold():
    # id=0 AND below threshold → id check wins
    result = should_grant_level_role(
        **{**_GRANT_OK, "level_role_id": 0, "new_level": 0}
    )
    assert result is LevelRoleDecision.SKIP_NOT_CONFIGURED


def test_below_threshold_beats_role_missing():
    # below threshold AND role missing → level check wins
    result = should_grant_level_role(
        **{**_GRANT_OK, "new_level": 0, "role_exists": False}
    )
    assert result is LevelRoleDecision.SKIP_BELOW_THRESHOLD


def test_role_missing_beats_already_has():
    # role missing AND already_has flag somehow true → role-missing wins
    # (in practice already_has cannot be true when role doesn't exist, but
    #  the ordering guard is important anyway)
    result = should_grant_level_role(
        **{**_GRANT_OK, "role_exists": False, "member_already_has_role": True}
    )
    assert result is LevelRoleDecision.SKIP_ROLE_MISSING


# ── all skip reasons surface distinctly ──────────────────────────────


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, LevelRoleDecision.GRANT),
        ({"level_role_id": 0}, LevelRoleDecision.SKIP_NOT_CONFIGURED),
        ({"new_level": THRESHOLD - 1}, LevelRoleDecision.SKIP_BELOW_THRESHOLD),
        ({"role_exists": False}, LevelRoleDecision.SKIP_ROLE_MISSING),
        ({"member_already_has_role": True}, LevelRoleDecision.SKIP_ALREADY_HAS),
    ],
)
def test_each_decision_reachable(overrides, expected):
    assert should_grant_level_role(**{**_GRANT_OK, **overrides}) is expected


# ── handle_level_progress: deliver owed levels + persist the mark ─────────
#
# These cover the announce path that fixes the silent quest level-up: a level
# owed (new_level > announced_level) must be announced and the mark advanced,
# and a member with nothing owed must stay quiet.

LOG_CHANNEL = 4242
GUILD_ID = 900
MEMBER_ID = 111


def _seed_member(db_path: Path, *, total_xp: float, level: int, announced_level: int):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_xp_tables(conn)
    conn.execute(
        "INSERT INTO member_xp (guild_id, user_id, total_xp, level, announced_level) "
        "VALUES (?, ?, ?, ?, ?)",
        (GUILD_ID, MEMBER_ID, total_xp, level, announced_level),
    )
    conn.commit()
    conn.close()


def _announced_level(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT announced_level FROM member_xp WHERE guild_id = ? AND user_id = ?",
            (GUILD_ID, MEMBER_ID),
        ).fetchone()[0]
    finally:
        conn.close()


class _FakeMember:
    def __init__(self):
        self.guild = type("G", (), {"id": GUILD_ID, "get_role": lambda self, _: None})()
        self.id = MEMBER_ID
        self.mention = "<@111>"
        self.display_avatar = type("A", (), {"url": "http://x/a.png"})()
        self.joined_at = None
        self.roles = []


def _award(*, old_level, new_level, announced_level, total_xp):
    return AwardResult(
        awarded_xp=1.0,
        total_xp=total_xp,
        old_level=old_level,
        new_level=new_level,
        announced_level=announced_level,
        role_grant_due=False,
    )


@pytest.mark.asyncio
async def test_owed_quest_level_is_announced_and_marked(tmp_path):
    """A level won silently (announced_level lags new_level) is announced now.

    The award itself did not level up (old == new); the gap is announced_level.
    This is the exact quest case: the member is one level higher than they were
    told, and this ordinary award is what surfaces it.
    """
    db_path = tmp_path / "xp.db"
    _seed_member(db_path, total_xp=64.0, level=3, announced_level=2)
    member = _FakeMember()
    channel = AsyncMock()

    with patch(
        "bot_modules.services.xp_service.get_guild_channel_or_thread",
        return_value=channel,
    ):
        await handle_level_progress(
            member,
            _award(old_level=3, new_level=3, announced_level=2, total_xp=64.0),
            "text_message",
            level_5_role_id=0,
            level_up_log_channel_id=LOG_CHANNEL,
            level_5_log_channel_id=0,
            db_path=db_path,
        )

    assert channel.send.await_count == 1  # only the owed level 3
    assert _announced_level(db_path) == 3


@pytest.mark.asyncio
async def test_nothing_owed_stays_quiet(tmp_path):
    db_path = tmp_path / "xp.db"
    _seed_member(db_path, total_xp=64.0, level=3, announced_level=3)
    member = _FakeMember()
    channel = AsyncMock()

    with patch(
        "bot_modules.services.xp_service.get_guild_channel_or_thread",
        return_value=channel,
    ):
        await handle_level_progress(
            member,
            _award(old_level=3, new_level=3, announced_level=3, total_xp=64.0),
            "text_message",
            level_5_role_id=0,
            level_up_log_channel_id=LOG_CHANNEL,
            level_5_log_channel_id=0,
            db_path=db_path,
        )

    channel.send.assert_not_awaited()
    assert _announced_level(db_path) == 3


@pytest.mark.asyncio
async def test_second_award_after_announce_is_silent(tmp_path):
    """Once a level is announced, the next award over the same span says nothing."""
    db_path = tmp_path / "xp.db"
    _seed_member(db_path, total_xp=64.0, level=3, announced_level=2)
    member = _FakeMember()
    channel = AsyncMock()

    with patch(
        "bot_modules.services.xp_service.get_guild_channel_or_thread",
        return_value=channel,
    ):
        award = _award(old_level=3, new_level=3, announced_level=2, total_xp=64.0)
        await handle_level_progress(
            member, award, "text_message",
            level_5_role_id=0, level_up_log_channel_id=LOG_CHANNEL,
            level_5_log_channel_id=0, db_path=db_path,
        )
        assert channel.send.await_count == 1

        # The mark advanced to 3; a fresh award now reads announced_level=3.
        channel.send.reset_mock()
        await handle_level_progress(
            member,
            _award(old_level=3, new_level=3, announced_level=3, total_xp=64.5),
            "text_message",
            level_5_role_id=0, level_up_log_channel_id=LOG_CHANNEL,
            level_5_log_channel_id=0, db_path=db_path,
        )
        channel.send.assert_not_awaited()
        assert _announced_level(db_path) == 3


@pytest.mark.asyncio
async def test_failed_send_leaves_level_unmarked_for_retry(tmp_path):
    """A Discord send failure must not advance the mark past the failed level."""
    import discord

    db_path = tmp_path / "xp.db"
    _seed_member(db_path, total_xp=142.0, level=4, announced_level=2)
    member = _FakeMember()
    channel = AsyncMock()
    # Level 3 sends, level 4 fails.
    channel.send.side_effect = [None, discord.HTTPException(AsyncMock(), "boom")]

    with patch(
        "bot_modules.services.xp_service.get_guild_channel_or_thread",
        return_value=channel,
    ):
        await handle_level_progress(
            member,
            _award(old_level=4, new_level=4, announced_level=2, total_xp=142.0),
            "text_message",
            level_5_role_id=0,
            level_up_log_channel_id=LOG_CHANNEL,
            level_5_log_channel_id=0,
            db_path=db_path,
        )

    # 3 landed, 4 did not -> mark stops at 3 so 4 retries next award.
    assert _announced_level(db_path) == 3
