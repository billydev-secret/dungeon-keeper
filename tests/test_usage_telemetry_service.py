"""Tests for services/usage_telemetry_service.py."""

from __future__ import annotations

import json
import time

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import usage_telemetry_service as svc

GUILD = 123
OTHER_GUILD = 456
USER = 1001
OTHER_USER = 1002
BOT_USER = 9001


@pytest.fixture
def db(sync_db_path):
    """Alias for the shared `sync_db_path` fixture (conftest.py) — kept only so
    the many `db` parameters below stay readable."""
    return sync_db_path


def _mark_bot(conn, user_id, guild_id=GUILD):
    conn.execute(
        "INSERT OR REPLACE INTO known_users "
        "(guild_id, user_id, username, display_name, is_bot, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?)",
        (guild_id, user_id, f"bot_{user_id}", f"Bot {user_id}", 1000.0),
    )


def _cmd(conn, name, user_id=USER, *, ok=True, ago_days=0.0, guild_id=GUILD):
    svc.record_event(
        conn,
        guild_id,
        svc.KIND_COMMAND,
        name,
        user_id,
        ok=ok,
        ts=time.time() - ago_days * 86400,
    )


# ── record_event ─────────────────────────────────────────────────────────


def test_record_event_persists_all_fields(db):
    with open_db(db) as conn:
        svc.record_event(
            conn, GUILD, svc.KIND_COMMAND, "bank", USER,
            channel_id=777, ok=True, extra={"src": "test"}, ts=1234.5,
        )
        row = conn.execute(
            "SELECT guild_id, kind, name, user_id, channel_id, ok, extra, ts "
            "FROM usage_events"
        ).fetchone()
    assert row[0] == GUILD
    assert row[1] == svc.KIND_COMMAND
    assert row[2] == "bank"
    assert row[3] == USER
    assert row[4] == 777
    assert row[5] == 1
    assert json.loads(row[6]) == {"src": "test"}
    assert row[7] == 1234.5


def test_record_event_marks_failures(db):
    with open_db(db) as conn:
        svc.record_event(conn, GUILD, svc.KIND_COMMAND, "bank", USER, ok=False)
        assert conn.execute("SELECT ok FROM usage_events").fetchone()[0] == 0


@pytest.mark.parametrize("name", ["", "   ", None])
def test_record_event_drops_empty_names(db, name):
    """Telemetry must never raise into the command it measures."""
    with open_db(db) as conn:
        svc.record_event(conn, GUILD, svc.KIND_COMMAND, name, USER)  # type: ignore[arg-type]
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0


def test_record_event_truncates_overlong_names(db):
    with open_db(db) as conn:
        svc.record_event(conn, GUILD, svc.KIND_PANEL, "x" * 500, USER)
        stored = conn.execute("SELECT name FROM usage_events").fetchone()[0]
    assert len(stored) == 100


# ── name_usage ───────────────────────────────────────────────────────────


def test_name_usage_aggregates_and_orders_by_uses(db):
    with open_db(db) as conn:
        for _ in range(3):
            _cmd(conn, "bank")
        _cmd(conn, "quest board")
        rows = svc.name_usage(conn, GUILD, svc.KIND_COMMAND)
    assert [r.name for r in rows] == ["bank", "quest board"]
    assert rows[0].uses == 3


def test_name_usage_counts_distinct_users_and_errors(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", USER)
        _cmd(conn, "bank", USER)
        _cmd(conn, "bank", OTHER_USER, ok=False)
        rows = svc.name_usage(conn, GUILD, svc.KIND_COMMAND)
    assert rows[0].uses == 3
    assert rows[0].users == 2
    assert rows[0].errors == 1


def test_name_usage_separates_kinds(db):
    with open_db(db) as conn:
        _cmd(conn, "bank")
        svc.record_event(conn, GUILD, svc.KIND_PANEL, "home", USER)
        commands = svc.name_usage(conn, GUILD, svc.KIND_COMMAND)
        panels = svc.name_usage(conn, GUILD, svc.KIND_PANEL)
    assert [r.name for r in commands] == ["bank"]
    assert [r.name for r in panels] == ["home"]


def test_name_usage_scopes_to_guild(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", guild_id=OTHER_GUILD)
        assert svc.name_usage(conn, GUILD, svc.KIND_COMMAND) == []


def test_name_usage_respects_window(db):
    with open_db(db) as conn:
        _cmd(conn, "old", ago_days=60)
        _cmd(conn, "recent", ago_days=1)
        rows = svc.name_usage(conn, GUILD, svc.KIND_COMMAND, days=30)
    assert [r.name for r in rows] == ["recent"]


# ── bot exclusion ────────────────────────────────────────────────────────


def test_bots_excluded_by_default(db):
    """The convention should hold here even though bots can't run commands."""
    with open_db(db) as conn:
        _mark_bot(conn, BOT_USER)
        _cmd(conn, "bank", BOT_USER)
        _cmd(conn, "bank", USER)
        rows = svc.name_usage(conn, GUILD, svc.KIND_COMMAND)
    assert rows[0].uses == 1
    assert rows[0].users == 1


def test_include_bots_opt_in(db):
    with open_db(db) as conn:
        _mark_bot(conn, BOT_USER)
        _cmd(conn, "bank", BOT_USER)
        _cmd(conn, "bank", USER)
        rows = svc.name_usage(conn, GUILD, svc.KIND_COMMAND, include_bots=True)
    assert rows[0].uses == 2


def test_bots_excluded_from_user_usage_and_totals(db):
    with open_db(db) as conn:
        _mark_bot(conn, BOT_USER)
        _cmd(conn, "bank", BOT_USER)
        _cmd(conn, "bank", USER)
        users = svc.user_usage(conn, GUILD, svc.KIND_COMMAND)
        agg = svc.totals(conn, GUILD)
    assert [u.user_id for u in users] == [USER]
    assert agg["commands"] == 1
    assert agg["distinct_users"] == 1


# ── unused_names (the headline output) ───────────────────────────────────


def test_unused_names_returns_registered_minus_seen():
    assert svc.unused_names({"bank", "quest", "help"}, {"bank"}) == ["help", "quest"]


def test_unused_names_empty_when_all_seen():
    assert svc.unused_names({"bank"}, {"bank", "quest"}) == []


def test_unused_names_ignores_stale_history():
    """A name in history but no longer registered is not a deletion candidate."""
    assert svc.unused_names(set(), {"deleted_command"}) == []


def test_used_names_all_history_by_default(db):
    with open_db(db) as conn:
        _cmd(conn, "ancient", ago_days=400)
        assert svc.used_names(conn, GUILD, svc.KIND_COMMAND) == {"ancient"}


# ── user_usage ───────────────────────────────────────────────────────────


def test_user_usage_ranks_and_counts_distinct_names(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", USER)
        _cmd(conn, "quest", USER)
        _cmd(conn, "bank", OTHER_USER)
        rows = svc.user_usage(conn, GUILD, svc.KIND_COMMAND)
    assert rows[0].user_id == USER
    assert rows[0].uses == 2
    assert rows[0].distinct_names == 2
    assert rows[1].user_id == OTHER_USER


def test_user_usage_spans_both_kinds_when_kind_is_none(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", USER)
        svc.record_event(conn, GUILD, svc.KIND_PANEL, "home", USER)
        rows = svc.user_usage(conn, GUILD, None)
    assert rows[0].uses == 2


# ── time series ──────────────────────────────────────────────────────────


def test_daily_series_fills_quiet_days_with_zero(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", ago_days=0)
        points = svc.daily_series(conn, GUILD, svc.KIND_COMMAND, days=7)
    assert len(points) == 7
    assert sum(p.total for p in points) == 1
    assert points[-1].total == 1  # today is last


def test_hour_histogram_has_24_buckets(db):
    with open_db(db) as conn:
        svc.record_event(
            conn, GUILD, svc.KIND_COMMAND, "bank", USER, ts=1700000000.0
        )
        hours = svc.hour_histogram(conn, GUILD, svc.KIND_COMMAND, days=0)
    assert len(hours) == 24
    assert sum(hours) == 1


def test_hour_histogram_shifts_with_timezone(db):
    with open_db(db) as conn:
        # 1700000000 == 2023-11-14 22:13:20 UTC
        svc.record_event(
            conn, GUILD, svc.KIND_COMMAND, "bank", USER, ts=1700000000.0
        )
        utc = svc.hour_histogram(conn, GUILD, svc.KIND_COMMAND, days=0)
        shifted = svc.hour_histogram(
            conn, GUILD, svc.KIND_COMMAND, days=0, tz_offset_hours=-7
        )
    assert utc.index(1) == 22
    assert shifted.index(1) == 15


# ── totals ───────────────────────────────────────────────────────────────


def test_totals_splits_kinds_and_errors(db):
    with open_db(db) as conn:
        _cmd(conn, "bank", USER)
        _cmd(conn, "bank", OTHER_USER, ok=False)
        svc.record_event(conn, GUILD, svc.KIND_PANEL, "home", USER)
        agg = svc.totals(conn, GUILD)
    assert agg == {
        "commands": 2,
        "panel_views": 1,
        "command_errors": 1,
        "distinct_users": 2,
    }


def test_totals_zero_on_empty_table(db):
    with open_db(db) as conn:
        agg = svc.totals(conn, GUILD)
    assert agg["commands"] == 0
    assert agg["distinct_users"] == 0
