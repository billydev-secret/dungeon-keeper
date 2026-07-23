"""Tests for bot_modules.services.voice_follow.record_voice_follow.

Covers the directed-follow accounting and the three noise guards: empty
channel, crowd cap, and flap debounce.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.voice_follow import record_voice_follow
from migrations import apply_migrations_sync

GUILD = 1000
CHAN = 42


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "vf.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        yield conn


def _weight(conn, frm, to):
    row = conn.execute(
        "SELECT weight FROM voice_follow WHERE guild_id=? AND from_user_id=? AND to_user_id=?",
        (GUILD, frm, to),
    ).fetchone()
    return row[0] if row else 0


def _log_count(conn):
    return conn.execute("SELECT COUNT(*) FROM voice_follow_log").fetchone()[0]


def test_join_occupied_channel_records_directed_follow(db_conn):
    n = record_voice_follow(db_conn, GUILD, from_user_id=1, present_user_ids=[2], channel_id=CHAN, ts=100)
    assert n == 1
    assert _weight(db_conn, 1, 2) == 1
    # Direction matters: nothing recorded the other way.
    assert _weight(db_conn, 2, 1) == 0


def test_empty_channel_records_nothing(db_conn):
    assert record_voice_follow(db_conn, GUILD, 1, [], CHAN, ts=100) == 0
    assert _log_count(db_conn) == 0


def test_joiner_excluded_from_targets(db_conn):
    # Present list containing only the joiner → no one to follow.
    assert record_voice_follow(db_conn, GUILD, 1, [1], CHAN, ts=100) == 0
    assert _log_count(db_conn) == 0


def test_multiple_present_members_each_get_an_edge(db_conn):
    n = record_voice_follow(db_conn, GUILD, 1, [2, 3, 4], CHAN, ts=100)
    assert n == 3
    assert _weight(db_conn, 1, 2) == 1
    assert _weight(db_conn, 1, 3) == 1
    assert _weight(db_conn, 1, 4) == 1


def test_crowd_is_ignored(db_conn):
    # Seven already present exceeds MAX_PRESENT (6) → party, not pursuit.
    present = [2, 3, 4, 5, 6, 7, 8]
    assert record_voice_follow(db_conn, GUILD, 1, present, CHAN, ts=100) == 0
    assert _log_count(db_conn) == 0


def test_crowd_cap_is_inclusive_at_the_limit(db_conn):
    present = [2, 3, 4, 5, 6, 7]  # exactly MAX_PRESENT
    assert record_voice_follow(db_conn, GUILD, 1, present, CHAN, ts=100) == 6


def test_flapping_into_same_channel_is_debounced(db_conn):
    assert record_voice_follow(db_conn, GUILD, 1, [2], CHAN, ts=100) == 1
    # Rejoin 5 min later, inside the 600s debounce → not re-counted.
    assert record_voice_follow(db_conn, GUILD, 1, [2], CHAN, ts=400) == 0
    assert _weight(db_conn, 1, 2) == 1
    # After the window, it counts again and bumps the weight + last_ts.
    assert record_voice_follow(db_conn, GUILD, 1, [2], CHAN, ts=800) == 1
    assert _weight(db_conn, 1, 2) == 2
    last_ts = db_conn.execute(
        "SELECT last_ts FROM voice_follow WHERE from_user_id=1 AND to_user_id=2"
    ).fetchone()[0]
    assert last_ts == 800


def test_debounce_is_per_channel(db_conn):
    assert record_voice_follow(db_conn, GUILD, 1, [2], channel_id=CHAN, ts=100) == 1
    # Same pair, different channel, same instant → a distinct follow.
    assert record_voice_follow(db_conn, GUILD, 1, [2], channel_id=CHAN + 1, ts=100) == 1
    assert _weight(db_conn, 1, 2) == 2
