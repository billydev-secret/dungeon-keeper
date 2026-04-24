"""Tests for services/birthday_service.py."""

from __future__ import annotations

import pytest

from db_utils import open_db
from migrations import apply_migrations_sync
from services.birthday_service import (
    MAX_DAYS,
    delete_birthday,
    list_all_birthdays,
    mark_announced,
    todays_unannounced,
    upsert_birthday,
)

GUILD = 123
USER_A = 1001
USER_B = 1002
MOD = 9001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── MAX_DAYS ──────────────────────────────────────────────────────────


def test_max_days_length():
    assert len(MAX_DAYS) == 13  # index 0 unused


def test_max_days_spot_checks():
    assert MAX_DAYS[1] == 31   # January
    assert MAX_DAYS[2] == 28   # February capped
    assert MAX_DAYS[4] == 30   # April
    assert MAX_DAYS[12] == 31  # December


# ── upsert_birthday ───────────────────────────────────────────────────


def test_upsert_inserts_new_birthday(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 7, 15)]


def test_upsert_overwrites_existing(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_A, 3, 1, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_A, 3, 1)]


def test_upsert_multiple_users(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 6, 10, MOD)
        upsert_birthday(conn, GUILD, USER_B, 6, 5, MOD)
        rows = list_all_birthdays(conn, GUILD)
    # Ordered by month then day
    assert rows == [(USER_B, 6, 5), (USER_A, 6, 10)]


# ── delete_birthday ───────────────────────────────────────────────────


def test_delete_existing_birthday(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        removed = delete_birthday(conn, GUILD, USER_A)
        assert removed is True
        assert list_all_birthdays(conn, GUILD) == []


def test_delete_nonexistent_birthday(db):
    with open_db(db) as conn:
        removed = delete_birthday(conn, GUILD, USER_A)
    assert removed is False


def test_delete_only_affects_target_user(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_B, 8, 20, MOD)
        delete_birthday(conn, GUILD, USER_A)
        rows = list_all_birthdays(conn, GUILD)
    assert rows == [(USER_B, 8, 20)]


# ── list_all_birthdays ────────────────────────────────────────────────


def test_list_empty_guild(db):
    with open_db(db) as conn:
        assert list_all_birthdays(conn, GUILD) == []


def test_list_ordered_by_month_then_day(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, 1001, 12, 25, MOD)
        upsert_birthday(conn, GUILD, 1002, 1, 1, MOD)
        upsert_birthday(conn, GUILD, 1003, 1, 15, MOD)
        rows = list_all_birthdays(conn, GUILD)
    assert [(m, d) for _, m, d in rows] == [(1, 1), (1, 15), (12, 25)]


def test_list_isolated_by_guild(db):
    OTHER_GUILD = 999
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, OTHER_GUILD, USER_B, 8, 1, MOD)
        assert len(list_all_birthdays(conn, GUILD)) == 1
        assert len(list_all_birthdays(conn, OTHER_GUILD)) == 1


# ── todays_unannounced ────────────────────────────────────────────────


def test_unannounced_returns_todays_birthdays(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_A]


def test_unannounced_excludes_already_announced(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == []


def test_unannounced_same_user_different_date_counts_again(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2025-07-15")
        # New year — should appear again
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_A]


def test_unannounced_empty_when_no_birthdays_today(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        result = todays_unannounced(conn, GUILD, 7, 16, "2026-07-16")
    assert result == []


def test_unannounced_mixed_announced_and_not(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        upsert_birthday(conn, GUILD, USER_B, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        result = todays_unannounced(conn, GUILD, 7, 15, "2026-07-15")
    assert result == [USER_B]


# ── mark_announced ────────────────────────────────────────────────────


def test_mark_announced_returns_true_on_first_call(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is True


def test_mark_announced_idempotent(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        mark_announced(conn, GUILD, USER_A, "2026-07-15")
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is False


def test_mark_announced_allows_same_user_different_dates(db):
    with open_db(db) as conn:
        upsert_birthday(conn, GUILD, USER_A, 7, 15, MOD)
        assert mark_announced(conn, GUILD, USER_A, "2025-07-15") is True
        assert mark_announced(conn, GUILD, USER_A, "2026-07-15") is True
