"""Tests for bot_modules.services.inactive_report_service.

The merged Inactive Report replaces four member-list panels (Inactive,
Inactive Role, List Role, Oldest SFW); each old workflow maps to a scope
or threshold here, so every branch gets a row.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.xp_system import MemberActivity
from bot_modules.services.inactive_report_service import (
    MemberScope,
    build_inactive_report,
    channel_activity_map,
    scope_members,
)
from tests.db_template import migrated_db


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "ir.db"
    migrated_db(path)
    with open_db(path) as conn:
        yield conn

NOW = 1_785_000_000.0
DAY = 86400.0

ROLE = 555


def _member(uid, name=None, bot=False, roles=()):
    return MemberScope(
        user_id=uid, display_name=name or f"user{uid}", is_bot=bot, role_ids=tuple(roles)
    )


def _activity(uid, days_ago, channel_id=42):
    return MemberActivity(
        user_id=uid, channel_id=channel_id, message_id=1, created_at=NOW - days_ago * DAY
    )


MEMBERS = [
    _member(1, roles=(ROLE,)),
    _member(2, roles=(ROLE,)),
    _member(3),
    _member(4, bot=True, roles=(ROLE,)),
]


@pytest.mark.parametrize(
    "role_id, role_mode, expected_ids",
    [
        pytest.param(None, "with", [1, 2, 3], id="no-role-everyone"),
        pytest.param(ROLE, "with", [1, 2], id="with-role-holders-only"),
        pytest.param(ROLE, "without", [3], id="without-role-non-holders"),
    ],
)
def test_scope_members(role_id, role_mode, expected_ids):
    scoped = scope_members(MEMBERS, role_id=role_id, role_mode=role_mode)
    assert [m.user_id for m in scoped] == expected_ids


def test_scope_members_always_drops_bots():
    scoped = scope_members(MEMBERS, role_id=ROLE, role_mode="with")
    assert all(not m.is_bot for m in scoped)


def test_build_report_days_zero_lists_everyone_oldest_first():
    scoped = [_member(1), _member(2), _member(3)]
    activities = {1: _activity(1, days_ago=1), 2: _activity(2, days_ago=30)}
    out = build_inactive_report(scoped, activities, now_ts=NOW, days=0)
    # Never-tracked member 3 sorts first (ts None → 0), then oldest activity.
    assert [r["user_id"] for r in out["members"]] == ["3", "2", "1"]
    assert out["total"] == 3
    assert out["total_scoped"] == 3
    assert out["tracking_coverage"] == 2


def test_build_report_days_filter_keeps_idle_and_never_tracked():
    scoped = [_member(1), _member(2), _member(3)]
    activities = {1: _activity(1, days_ago=1), 2: _activity(2, days_ago=30)}
    out = build_inactive_report(scoped, activities, now_ts=NOW, days=7)
    # 1 posted yesterday → excluded; 2 idle 30d and 3 never tracked → included.
    assert [r["user_id"] for r in out["members"]] == ["3", "2"]
    assert out["members"][0]["days_since_last"] is None
    assert out["members"][1]["days_since_last"] == 30.0


def test_build_report_row_shape_and_limit():
    scoped = [_member(1), _member(2)]
    activities = {1: _activity(1, days_ago=10, channel_id=77)}
    out = build_inactive_report(scoped, activities, now_ts=NOW, days=0, limit=1)
    assert out["total"] == 2  # total counts matches, limit only trims rows
    assert len(out["members"]) == 1
    row = out["members"][0]
    assert row == {
        "user_id": "2",
        "display_name": "user2",
        "last_message_ts": None,
        "last_message_channel_id": None,
        "days_since_last": None,
    }


def test_channel_activity_map_scopes_to_channel(db_conn):
    for uid, ch, days_ago in ((1, 42, 20), (1, 43, 1), (2, 43, 2)):
        db_conn.execute(
            "INSERT INTO xp_events (guild_id, user_id, channel_id, amount, source, created_at)"
            " VALUES (10, ?, ?, 5, 'message', ?)",
            (uid, ch, NOW - days_ago * DAY),
        )
    act = channel_activity_map(db_conn, 10, [1, 2, 3], 42)
    # Only channel-42 activity counts: user 1's newer post in 43 is invisible,
    # user 2 (only in 43) and user 3 (nowhere) are absent.
    assert set(act) == {1}
    assert act[1].created_at == NOW - 20 * DAY
    assert act[1].channel_id == 42


def test_channel_activity_map_empty_member_list(db_conn):
    assert channel_activity_map(db_conn, 10, [], 42) == {}
