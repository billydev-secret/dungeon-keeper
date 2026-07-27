"""Tests for the shared sweep candidate gathering.

``select_sweep_candidates`` decides *who qualifies* and is pinned in
test_inactive_logic.py. This file covers the step before it — building the
last-seen map and the exclusion set out of guild + DB state — because that is
where the safety rules actually live: bots, the owner, admins, mods, exempted
members and existing holds are kept out of a destructive mass role-strip here,
not in the pure selector.

Both the real sweeps and the dashboard's dry-run preview go through this
module, so these tests are also what stops the preview from drifting away from
the sweep it claims to predict.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.core.app_context import AppContext
from bot_modules.inactive.store import add_sweep_exemption, create_inactive
from bot_modules.inactive.sweep_service import compute_candidates
from tests.db_template import migrated_db

GUILD_ID = 100
OWNER_ID = 1
DAY = 86400.0


def _make_ctx(db_path) -> AppContext:
    migrated_db(db_path)
    return AppContext(
        bot=MagicMock(),
        log=logging.getLogger("test"),
        db_path=db_path,
        guild_id=GUILD_ID,
        debug=True,
    )


def _member(
    member_id: int,
    *,
    is_bot: bool = False,
    admin: bool = False,
    manage_guild: bool = False,
    joined_days_ago: float | None = 400.0,
) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = member_id
    m.bot = is_bot
    m.name = f"u{member_id}"
    m.display_name = m.name
    perms = MagicMock()
    perms.administrator = admin
    perms.manage_guild = manage_guild
    m.guild_permissions = perms
    m.joined_at = (
        None
        if joined_days_ago is None
        else datetime.now(timezone.utc) - timedelta(days=joined_days_ago)
    )
    m.roles = []
    return m


def _guild(members: list) -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.id = GUILD_ID
    g.owner_id = OWNER_ID
    g.members = members
    return g


def _seed_message(ctx: AppContext, user_id: int, *, days_ago: float) -> None:
    """Record one tracked message for a member at a given age."""
    import time

    ts = time.time() - days_ago * DAY
    with ctx.open_db() as conn:
        conn.execute(
            "INSERT INTO processed_messages "
            "(guild_id, message_id, channel_id, user_id, created_at, processed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (GUILD_ID, user_id * 1000 + int(days_ago), 7, user_id, ts, ts),
        )


async def test_idle_member_is_selected(tmp_path):
    ctx = _make_ctx(tmp_path / "a.db")
    _seed_message(ctx, 5, days_ago=90)
    selection = await compute_candidates(ctx, _guild([_member(5)]))
    assert [c.user_id for c in selection.candidates] == [5]
    assert selection.threshold_days == 30  # default


async def test_recently_active_member_is_skipped(tmp_path):
    ctx = _make_ctx(tmp_path / "b.db")
    _seed_message(ctx, 5, days_ago=90)
    _seed_message(ctx, 5, days_ago=2)  # most recent message wins
    selection = await compute_candidates(ctx, _guild([_member(5)]))
    assert selection.candidates == []


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        pytest.param({"is_bot": True}, "bots are never swept", id="bot"),
        pytest.param({"admin": True}, "admins are never swept", id="admin"),
        pytest.param({"manage_guild": True}, "mods are never swept", id="manage-guild"),
    ],
)
async def test_privileged_members_are_excluded(tmp_path, kwargs, reason):
    ctx = _make_ctx(tmp_path / f"c-{kwargs}.db")
    _seed_message(ctx, 5, days_ago=90)
    selection = await compute_candidates(ctx, _guild([_member(5, **kwargs)]))
    assert selection.candidates == [], reason


async def test_owner_is_excluded(tmp_path):
    ctx = _make_ctx(tmp_path / "d.db")
    _seed_message(ctx, OWNER_ID, days_ago=90)
    selection = await compute_candidates(ctx, _guild([_member(OWNER_ID)]))
    assert selection.candidates == []


async def test_already_inactive_member_is_excluded(tmp_path):
    ctx = _make_ctx(tmp_path / "e.db")
    _seed_message(ctx, 5, days_ago=90)
    with ctx.open_db() as conn:
        create_inactive(
            conn, guild_id=GUILD_ID, user_id=5, moderator_id=2,
            reason="", stored_roles=[], source="command",
        )
    selection = await compute_candidates(ctx, _guild([_member(5)]))
    assert selection.candidates == []


async def test_exempt_member_is_excluded_from_selection(tmp_path):
    """The dashboard exemption has to hold against the sweeps themselves."""
    ctx = _make_ctx(tmp_path / "f.db")
    _seed_message(ctx, 5, days_ago=90)
    _seed_message(ctx, 6, days_ago=90)
    with ctx.open_db() as conn:
        add_sweep_exemption(conn, guild_id=GUILD_ID, user_id=5, added_by=2)
    selection = await compute_candidates(ctx, _guild([_member(5), _member(6)]))
    assert [c.user_id for c in selection.candidates] == [6]


async def test_member_with_no_messages_is_aged_from_join(tmp_path):
    ctx = _make_ctx(tmp_path / "g.db")
    guild = _guild([_member(5, joined_days_ago=400), _member(6, joined_days_ago=3)])
    selection = await compute_candidates(ctx, guild)
    # The long-standing member qualifies on join date alone; the fresh one is
    # not treated as ancient just because they have never posted.
    assert [c.user_id for c in selection.candidates] == [5]
    assert selection.tracked_user_ids == set()


async def test_member_without_cached_join_time_is_skipped(tmp_path):
    ctx = _make_ctx(tmp_path / "h.db")
    selection = await compute_candidates(ctx, _guild([_member(5, joined_days_ago=None)]))
    assert selection.candidates == []


async def test_tracked_user_ids_reports_who_has_history(tmp_path):
    ctx = _make_ctx(tmp_path / "i.db")
    _seed_message(ctx, 5, days_ago=90)
    guild = _guild([_member(5), _member(6)])
    selection = await compute_candidates(ctx, guild)
    assert {c.user_id for c in selection.candidates} == {5, 6}
    assert selection.tracked_user_ids == {5}


async def test_threshold_override_is_honoured(tmp_path):
    """The preview passes an unsaved threshold so it can be tried before saving."""
    ctx = _make_ctx(tmp_path / "j.db")
    _seed_message(ctx, 5, days_ago=45)
    guild = _guild([_member(5, joined_days_ago=45)])

    assert (await compute_candidates(ctx, guild, threshold_days=90)).candidates == []
    loose = await compute_candidates(ctx, guild, threshold_days=10)
    assert [c.user_id for c in loose.candidates] == [5]
    assert loose.threshold_days == 10


async def test_saved_cap_truncates_and_reports_overflow(tmp_path):
    ctx = _make_ctx(tmp_path / "k.db")
    members = []
    for uid in range(2, 8):
        _seed_message(ctx, uid, days_ago=100 + uid)
        members.append(_member(uid))
    ctx.set_config_value("inactive_sweep_cap", "2", GUILD_ID)

    selection = await compute_candidates(ctx, _guild(members))
    assert len(selection.candidates) == 2
    assert selection.overflow == 4
    assert selection.saved_cap == 2


async def test_uncapped_lists_everyone_for_the_preview(tmp_path):
    """The dashboard listing shows every eligible member, not one run's worth."""
    ctx = _make_ctx(tmp_path / "l.db")
    members = []
    for uid in range(2, 8):
        _seed_message(ctx, uid, days_ago=100 + uid)
        members.append(_member(uid))
    ctx.set_config_value("inactive_sweep_cap", "2", GUILD_ID)

    selection = await compute_candidates(ctx, _guild(members), cap=None)
    assert len(selection.candidates) == 6
    assert selection.overflow == 0
    # The configured cap still comes back, so the caller can say what one run
    # would reach even though the listing lifted it.
    assert selection.saved_cap == 2
