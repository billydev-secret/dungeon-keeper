"""Tests for services/inactivity_prune_service.compute_prune_targets."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from bot_modules.services.inactivity_prune_service import compute_prune_targets


@dataclass
class FakeActivity:
    created_at: float


CUTOFF = 1000.0  # ts; anything before this is "inactive"


# ── empty / no-op cases ───────────────────────────────────────────────


def test_empty_roster_returns_empty():
    assert compute_prune_targets([], set(), {}, CUTOFF) == []


def test_no_activity_records_prunes_nobody():
    # User in the role but no activity record — skipped, not pruned.
    result = compute_prune_targets([(1001, False)], set(), {}, CUTOFF)
    assert result == []


# ── activity window ───────────────────────────────────────────────────


def test_activity_before_cutoff_is_pruned():
    activity = {1001: FakeActivity(created_at=CUTOFF - 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == [1001]


def test_activity_after_cutoff_is_kept():
    activity = {1001: FakeActivity(created_at=CUTOFF + 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == []


def test_activity_exactly_at_cutoff_is_kept():
    # Boundary: strict `<` means exact match is kept
    activity = {1001: FakeActivity(created_at=CUTOFF)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == []


# ── bot / exception filters ───────────────────────────────────────────


def test_bots_never_pruned():
    # Bot with ancient activity — still excluded
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, True)], set(), activity, CUTOFF)
    assert result == []


def test_exempted_user_never_pruned():
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, False)], {1001}, activity, CUTOFF)
    assert result == []


def test_exception_wins_over_bot_flag():
    # Both a bot AND exempted — still not pruned
    activity = {1001: FakeActivity(created_at=0.0)}
    result = compute_prune_targets([(1001, True)], {1001}, activity, CUTOFF)
    assert result == []


# ── mixed roster ──────────────────────────────────────────────────────


def test_mixed_roster_selects_correctly():
    roster = [
        (1001, False),  # inactive human → prune
        (1002, False),  # active human → keep
        (1003, True),   # inactive bot → keep (bot)
        (1004, False),  # inactive, exempted → keep
        (1005, False),  # no activity → keep
    ]
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 100),
        1002: FakeActivity(created_at=CUTOFF + 100),
        1003: FakeActivity(created_at=CUTOFF - 100),
        1004: FakeActivity(created_at=CUTOFF - 100),
    }
    exceptions = {1004}
    result = compute_prune_targets(roster, exceptions, activity, CUTOFF)
    assert result == [1001]


def test_preserves_roster_order():
    roster = [(1003, False), (1001, False), (1002, False)]
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 1),
        1002: FakeActivity(created_at=CUTOFF - 1),
        1003: FakeActivity(created_at=CUTOFF - 1),
    }
    result = compute_prune_targets(roster, set(), activity, CUTOFF)
    assert result == [1003, 1001, 1002]


# ── regression guards ─────────────────────────────────────────────────


def test_activity_record_for_non_roster_user_ignored():
    # activity_map may contain users not in the role; they should be ignored.
    activity = {
        1001: FakeActivity(created_at=CUTOFF - 1),
        9999: FakeActivity(created_at=CUTOFF - 1),  # not in role
    }
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert result == [1001]


def test_returns_list_not_set():
    # Callers may rely on list semantics (order, duplicates impossible via filter)
    activity = {1001: FakeActivity(created_at=CUTOFF - 1)}
    result = compute_prune_targets([(1001, False)], set(), activity, CUTOFF)
    assert isinstance(result, list)


# ── run_prune_for_guild writes the durable ledger ─────────────────────


async def test_run_prune_records_role_prune_events(tmp_path):
    import time
    from unittest.mock import AsyncMock, MagicMock

    from bot_modules.core.db_utils import open_db
    from bot_modules.services.inactivity_prune_service import run_prune_for_guild
    from bot_modules.services.role_grant_audit_service import get_open_prune_events
    from migrations import apply_migrations_sync

    guild_id, role_id, days = 12345, 555, 30
    db_path = tmp_path / "prune.db"
    apply_migrations_sync(db_path)

    def make_member(uid):
        m = MagicMock()
        m.id = uid
        m.bot = False
        m.display_name = f"member-{uid}"
        m.remove_roles = AsyncMock()
        return m

    stale = make_member(201)   # inactive past the window → pruned
    fresh = make_member(202)   # active yesterday → kept

    role = MagicMock()
    role.name = "NSFW"
    role.members = [stale, fresh]
    guild = MagicMock()
    guild.name = "Test Guild"
    guild.get_role = MagicMock(return_value=role)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)

    now = time.time()
    with open_db(db_path) as conn:
        for uid, last_at in ((201, now - (days + 10) * 86400), (202, now - 86400)):
            conn.execute(
                "INSERT INTO member_activity (guild_id, user_id, last_channel_id, "
                "last_message_id, last_message_at) VALUES (?, ?, 1, 1, ?)",
                (guild_id, uid, last_at),
            )

    await run_prune_for_guild(bot, db_path, guild_id, role_id, days)

    stale.remove_roles.assert_awaited_once()
    fresh.remove_roles.assert_not_awaited()
    with open_db(db_path) as conn:
        events = get_open_prune_events(conn, guild_id, role_id)
        assert [int(e["user_id"]) for e in events] == [201]
        assert float(events[0]["pruned_at"]) == pytest.approx(now, abs=120)
