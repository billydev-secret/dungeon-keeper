"""Tests for services/role_grant_audit_service — the prune ledger + audit buckets."""

from __future__ import annotations

import time
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.role_grant_audit_service import (
    backfill_prune_events_from_role_events,
    compute_recent_inactive,
    compute_stripped_returned,
    compute_waiting_for_first_grant,
    get_ever_pruned_ids,
    get_hold_excluded_ids,
    get_open_prune_events,
    mark_restored,
    record_prune_events,
)
from migrations import apply_migrations_sync

GUILD_ID = 12345
ROLE_ID = 555
CUTOFF = 1_000_000.0  # activity at/after this counts as "active again"


@dataclass
class FakeActivity:
    created_at: float


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "audit.db"
    apply_migrations_sync(path)
    return path


# ── ledger roundtrip ──────────────────────────────────────────────────


def test_record_and_open_events_roundtrip(db_path):
    with open_db(db_path) as conn:
        assert record_prune_events(conn, GUILD_ID, [201, 202], ROLE_ID, 100.0) == 2
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert {int(e["user_id"]) for e in events} == {201, 202}
        assert all(float(e["pruned_at"]) == 100.0 for e in events)


def test_record_empty_user_list_is_noop(db_path):
    with open_db(db_path) as conn:
        assert record_prune_events(conn, GUILD_ID, [], ROLE_ID, 100.0) == 0
        assert get_open_prune_events(conn, GUILD_ID, ROLE_ID) == []


def test_mark_restored_closes_only_that_user(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201, 202], ROLE_ID, 100.0)
        assert mark_restored(conn, GUILD_ID, 201, ROLE_ID, 200.0) == 1
        remaining = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in remaining] == [202]
        # Restored users still count as ever-pruned (never "waiting for first grant").
        assert get_ever_pruned_ids(conn, GUILD_ID, ROLE_ID) == {201, 202}


def test_mark_restored_without_open_event_is_noop(db_path):
    with open_db(db_path) as conn:
        assert mark_restored(conn, GUILD_ID, 999, ROLE_ID, 200.0) == 0


def test_mark_restored_does_not_rewrite_closed_events(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        mark_restored(conn, GUILD_ID, 201, ROLE_ID, 200.0)
        assert mark_restored(conn, GUILD_ID, 201, ROLE_ID, 300.0) == 0
        row = conn.execute(
            "SELECT restored_at FROM role_prune_events WHERE user_id = 201"
        ).fetchone()
        assert float(row["restored_at"]) == 200.0


def test_open_events_scoped_to_role_and_guild(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        record_prune_events(conn, GUILD_ID, [202], ROLE_ID + 1, 100.0)
        record_prune_events(conn, GUILD_ID + 1, [203], ROLE_ID, 100.0)
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in events] == [201]


def test_open_events_report_latest_prune_per_user(db_path):
    with open_db(db_path) as conn:
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 100.0)
        record_prune_events(conn, GUILD_ID, [201], ROLE_ID, 500.0)
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert len(events) == 1
        assert float(events[0]["pruned_at"]) == 500.0


# ── hold exclusions ───────────────────────────────────────────────────


def test_hold_excluded_ids_covers_inactive_jail_and_config(db_path):
    from bot_modules.core.db_utils import set_config_value

    with open_db(db_path) as conn:
        conn.execute(
            "INSERT INTO inactive_members (guild_id, user_id, stored_roles, created_at, status) "
            "VALUES (?, 301, '[]', 0, 'active')",
            (GUILD_ID,),
        )
        conn.execute(
            "INSERT INTO jails (guild_id, user_id, moderator_id, stored_roles, created_at, status) "
            "VALUES (?, 302, 0, '[]', 0, 'active')",
            (GUILD_ID,),
        )
        set_config_value(conn, "inactive_role_id", "777", GUILD_ID)
        held_ids, hold_role_ids = get_hold_excluded_ids(conn, GUILD_ID)
        assert held_ids == {301, 302}
        assert hold_role_ids == {777}


def test_hold_excluded_ids_empty_when_nothing_configured(db_path):
    with open_db(db_path) as conn:
        held_ids, hold_role_ids = get_hold_excluded_ids(conn, GUILD_ID)
        assert held_ids == set()
        assert hold_role_ids == set()


# ── bucketing (pure) ──────────────────────────────────────────────────


def test_waiting_excludes_granted_and_ever_pruned_sorts_desc():
    levels = {1: 5, 2: 9, 3: 7, 4: 6}
    out = compute_waiting_for_first_grant(levels, granted_ids={1}, ever_pruned_ids={3})
    assert out == [(2, 9), (4, 6)]


def test_waiting_empty_inputs():
    assert compute_waiting_for_first_grant({}, set(), set()) == []


def _events(*pairs):
    return [{"user_id": uid, "pruned_at": ts} for uid, ts in pairs]


def test_stripped_returned_requires_resumed_activity():
    events = _events((10, 100.0), (11, 200.0), (12, 300.0), (13, 400.0))
    activity = {
        10: FakeActivity(CUTOFF + 5),  # resumed → in
        11: FakeActivity(CUTOFF - 5),  # still inactive → out
        # 12: no activity → out
        13: FakeActivity(CUTOFF),  # boundary: at cutoff counts as resumed
    }
    out = compute_stripped_returned(events, set(), activity, CUTOFF)
    assert [r["user_id"] for r in out] == [13, 10]  # newest prune first


def test_stripped_returned_excludes_regranted():
    events = _events((10, 100.0))
    activity = {10: FakeActivity(CUTOFF + 5)}
    assert compute_stripped_returned(events, {10}, activity, CUTOFF) == []


def test_recent_inactive_includes_stale_and_untracked():
    events = _events((10, 100.0), (11, 200.0), (12, 300.0))
    activity = {
        10: FakeActivity(CUTOFF + 5),  # resumed → out
        11: FakeActivity(CUTOFF - 5),  # stale → in
        # 12: no record → in
    }
    out = compute_recent_inactive(events, set(), activity, CUTOFF)
    assert [r["user_id"] for r in out] == [12, 11]


def test_recent_inactive_excludes_regranted():
    events = _events((11, 200.0))
    assert compute_recent_inactive(events, {11}, {}, CUTOFF) == []


def test_recent_inactive_caps_at_limit_newest_first():
    events = _events(*[(100 + i, float(i)) for i in range(15)])
    out = compute_recent_inactive(events, set(), {}, CUTOFF, limit=10)
    assert len(out) == 10
    assert [r["pruned_at"] for r in out] == [float(i) for i in range(14, 4, -1)]


# ── backfill from role_events ─────────────────────────────────────────

INACTIVITY_DAYS = 30
WINDOW = INACTIVITY_DAYS * 86400


def _seed_remove_event(conn, user_id, removed_at, role_name="NSFW"):
    conn.execute(
        "INSERT INTO role_events (guild_id, user_id, role_name, action, granted_at) "
        "VALUES (?, ?, ?, 'remove', ?)",
        (GUILD_ID, user_id, role_name, removed_at),
    )


def _seed_activity(conn, user_id, last_message_at):
    conn.execute(
        "INSERT INTO member_activity (guild_id, user_id, last_channel_id, last_message_id, last_message_at) "
        "VALUES (?, ?, 1, 1, ?)",
        (GUILD_ID, user_id, last_message_at),
    )


def _fake_role(holder_ids=()):
    role = MagicMock()
    role.id = ROLE_ID
    role.name = "NSFW"
    role.members = [MagicMock(id=uid) for uid in holder_ids]
    return role


def _fake_guild():
    guild = MagicMock()
    guild.id = GUILD_ID
    return guild


def test_backfill_inserts_prune_like_removals(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 401, removed_at)
        # Last activity long before removal — inactive well past the window.
        _seed_activity(conn, 401, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 1
        events = get_open_prune_events(conn, GUILD_ID, ROLE_ID)
        assert [int(e["user_id"]) for e in events] == [401]
        assert float(events[0]["pruned_at"]) == pytest.approx(removed_at)


def test_backfill_skips_removals_that_cannot_be_prunes(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        # Active the day before removal — a mod removal, not the prune loop.
        _seed_remove_event(conn, 402, removed_at)
        _seed_activity(conn, 402, removed_at - 86400)
        # No activity record at all — the prune loop never strips those.
        _seed_remove_event(conn, 403, removed_at)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 0


def test_backfill_includes_members_who_resumed_after_removal(db_path):
    # The known live cases (Phantom, Nate): pruned, then came back. Their
    # current last-activity is *after* the removal, which can't disprove a
    # prune — they must land in the ledger as open events.
    removed_at = time.time() - 20 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 404, removed_at)
        _seed_activity(conn, 404, removed_at + 5 * 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 1
        assert [int(e["user_id"]) for e in get_open_prune_events(conn, GUILD_ID, ROLE_ID)] == [404]


def test_backfill_marks_current_holders_restored(db_path):
    removed_at = time.time() - 20 * 86400
    now = time.time()
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 405, removed_at)
        _seed_activity(conn, 405, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(holder_ids=[405]), INACTIVITY_DAYS, now=now
        )
        assert inserted == 1
        assert get_open_prune_events(conn, GUILD_ID, ROLE_ID) == []
        row = conn.execute(
            "SELECT restored_at FROM role_prune_events WHERE user_id = 405"
        ).fetchone()
        assert float(row["restored_at"]) == pytest.approx(now)


def test_backfill_is_idempotent(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 406, removed_at)
        _seed_activity(conn, 406, removed_at - WINDOW - 86400)
        first = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        second = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert (first, second) == (1, 0)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM role_prune_events WHERE user_id = 406"
        ).fetchone()
        assert int(count["n"]) == 1


def test_backfill_ignores_other_role_names(db_path):
    removed_at = time.time() - 10 * 86400
    with open_db(db_path) as conn:
        _seed_remove_event(conn, 407, removed_at, role_name="Veteran")
        _seed_activity(conn, 407, removed_at - WINDOW - 86400)
        inserted = backfill_prune_events_from_role_events(
            conn, _fake_guild(), _fake_role(), INACTIVITY_DAYS
        )
        assert inserted == 0
