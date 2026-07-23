"""Tests for the Greeting Watch monitor loop — settings load + multi-notify.

Focus is the behavior the loop adds on top of the service helpers: reading the
notify subscriber list (with legacy fallback) and DMing *every* subscriber when
a greeting goes unanswered.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.greeting_watch_loop import _load_settings, _process_guild
from bot_modules.services.greeting_watch_service import record_greeting
from bot_modules.services.interaction_graph import record_interactions
from migrations import apply_migrations_sync

GUILD = 1000
CHANNEL = 2000
GREETER = 3000
NOTIFY_A = 4001
NOTIFY_B = 4002


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "gw.db"
    apply_migrations_sync(path)
    return path


def _configure(db_path, *, enabled=True, notify_ids="", legacy="", window=10):
    with open_db(db_path) as conn:
        set_config_value(
            conn, "greeting_watch_enabled", "1" if enabled else "0", GUILD
        )
        set_config_value(
            conn, "greeting_watch_notify_user_ids", notify_ids, GUILD
        )
        if legacy:
            set_config_value(
                conn, "greeting_watch_notify_user_id", legacy, GUILD
            )
        set_config_value(
            conn, "greeting_watch_window_minutes", str(window), GUILD
        )


# ── _load_settings ───────────────────────────────────────────────────


def test_load_settings_reads_notify_csv(db_path):
    _configure(db_path, notify_ids="4001,4002", window=15)
    enabled, window, notify_ids = _load_settings(db_path, GUILD)
    assert enabled is True
    assert window == 15
    assert notify_ids == [NOTIFY_A, NOTIFY_B]


def test_load_settings_falls_back_to_legacy_single(db_path):
    # CSV empty but the old single-notify key is set → keep that subscriber.
    _configure(db_path, notify_ids="", legacy="4001")
    _, _, notify_ids = _load_settings(db_path, GUILD)
    assert notify_ids == [NOTIFY_A]


# ── _process_guild fan-out ───────────────────────────────────────────


class FakeUser:
    def __init__(self, uid: int) -> None:
        self.id = uid
        self.send = AsyncMock()


class FakeBot:
    def __init__(self, users: dict[int, FakeUser]) -> None:
        self._users = users

    def get_user(self, uid: int):
        return self._users.get(uid)

    def get_guild(self, gid: int):
        return SimpleNamespace(
            get_channel=lambda cid: SimpleNamespace(name="general"),
            get_member=lambda mid: SimpleNamespace(display_name="Greeter"),
        )


def _resolved_rows(db_path):
    with open_db(db_path) as conn:
        return conn.execute(
            "SELECT outcome FROM greeting_watch WHERE resolved_at IS NOT NULL"
        ).fetchall()


async def test_process_guild_dms_every_subscriber(db_path):
    _configure(db_path, notify_ids="4001,4002", window=10)
    with open_db(db_path) as conn:
        # Greeting posted at ts=100, window 10 min → due once now is well past.
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)

    users = {NOTIFY_A: FakeUser(NOTIFY_A), NOTIFY_B: FakeUser(NOTIFY_B)}
    bot = FakeBot(users)
    await _process_guild(bot, db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    users[NOTIFY_A].send.assert_awaited_once()
    users[NOTIFY_B].send.assert_awaited_once()
    rows = _resolved_rows(db_path)
    assert [r["outcome"] for r in rows] == ["unanswered"]


async def test_process_guild_acknowledged_skips_all_dms(db_path):
    _configure(db_path, notify_ids="4001,4002", window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        # Someone answered the greeter inside the window.
        record_interactions(conn, GUILD, 9999, [GREETER], ts=150, message_id=7)

    users = {NOTIFY_A: FakeUser(NOTIFY_A), NOTIFY_B: FakeUser(NOTIFY_B)}
    bot = FakeBot(users)
    await _process_guild(bot, db_path, GUILD, now_ts=100 + 10 * 60 + 5)

    users[NOTIFY_A].send.assert_not_awaited()
    users[NOTIFY_B].send.assert_not_awaited()
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["acknowledged"]


async def test_process_guild_no_subscribers_skips_row(db_path):
    # Enabled but nobody to notify → straggler retired as 'skipped', no DMs.
    _configure(db_path, notify_ids="", window=10)
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)

    bot = FakeBot({})
    await _process_guild(bot, db_path, GUILD, now_ts=100 + 10 * 60 + 5)
    assert [r["outcome"] for r in _resolved_rows(db_path)] == ["skipped"]
