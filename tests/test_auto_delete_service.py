from __future__ import annotations

import pytest

from db_utils import open_db
from services.auto_delete_service import (
    auto_delete_rule_exists,
    format_duration_seconds,
    init_auto_delete_tables,
    list_auto_delete_rules_for_guild,
    parse_duration_seconds,
    pop_due_auto_delete_message_ids,
    remove_auto_delete_rule,
    remove_tracked_auto_delete_message,
    remove_tracked_auto_delete_messages,
    touch_auto_delete_rule_run,
    track_auto_delete_message,
    upsert_auto_delete_rule,
)


# ── parse_duration_seconds ────────────────────────────────────────────

def test_named_intervals():
    assert parse_duration_seconds("hourly") == 3600
    assert parse_duration_seconds("daily") == 86400
    assert parse_duration_seconds("weekly") == 7 * 86400


@pytest.mark.parametrize("s,expected", [
    ("30s", 30), ("30 seconds", 30),
    ("5m", 300), ("5 minutes", 300),
    ("2h", 7200), ("2 hours", 7200),
    ("1d", 86400), ("1w", 7 * 86400),
])
def test_unit_variants(s, expected):
    assert parse_duration_seconds(s) == expected


def test_compound_durations():
    assert parse_duration_seconds("1h30m") == 5400
    assert parse_duration_seconds("2d12h") == 2 * 86400 + 12 * 3600
    assert parse_duration_seconds("1h30m15s") == 5415


def test_case_insensitive():
    assert parse_duration_seconds("1H") == 3600
    assert parse_duration_seconds("1D") == 86400


@pytest.mark.parametrize("s", ("", "abc", "0h", "1h abc", "abc 1h"))
def test_invalid_returns_none(s):
    assert parse_duration_seconds(s) is None


# ── format_duration_seconds ───────────────────────────────────────────

def test_format_zero():
    assert format_duration_seconds(0) == "0s"


@pytest.mark.parametrize("secs,expected", [
    (60, "1 minute"), (120, "2 minutes"),
    (3600, "1 hour"), (7200, "2 hours"),
    (86400, "1 day"), (172800, "2 days"),
])
def test_singular_and_plural(secs, expected):
    assert format_duration_seconds(secs) == expected


@pytest.mark.parametrize("secs", (90, 3661))
def test_non_round_falls_back_to_seconds(secs):
    assert "seconds" in format_duration_seconds(secs)


# ── auto_delete DB tests ──────────────────────────────────────────────

@pytest.fixture
def ad_db(tmp_path):
    db_path = tmp_path / "test.db"
    with open_db(db_path) as conn:
        init_auto_delete_tables(conn)
    return db_path


def test_upsert_creates_and_lists_rules(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 1, 200, 43200, 7200)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert len(rules) == 2
    assert int(rules[0]["channel_id"]) == 100
    assert int(rules[1]["channel_id"]) == 200


def test_upsert_updates_existing_rule(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 1, 100, 43200, 7200)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert len(rules) == 1
    assert int(rules[0]["max_age_seconds"]) == 43200
    assert int(rules[0]["interval_seconds"]) == 7200


def test_rules_are_scoped_by_guild(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    upsert_auto_delete_rule(ad_db, 2, 100, 86400, 3600)
    assert len(list_auto_delete_rules_for_guild(ad_db, 1)) == 1
    assert len(list_auto_delete_rules_for_guild(ad_db, 2)) == 1
    assert len(list_auto_delete_rules_for_guild(ad_db, 3)) == 0


def test_remove_rule_returns_true_when_found(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    assert remove_auto_delete_rule(ad_db, 1, 100) is True
    assert list_auto_delete_rules_for_guild(ad_db, 1) == []


def test_remove_rule_returns_false_when_missing(ad_db):
    assert remove_auto_delete_rule(ad_db, 1, 999) is False


def test_auto_delete_rule_exists(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    with open_db(ad_db) as conn:
        assert auto_delete_rule_exists(conn, 1, 100) is True
        assert auto_delete_rule_exists(conn, 1, 999) is False
        assert auto_delete_rule_exists(conn, 2, 100) is False


def test_touch_rule_updates_last_run_ts(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600, last_run_ts=0.0)
    touch_auto_delete_rule_run(ad_db, 1, 100, 9999.0)
    rules = list_auto_delete_rules_for_guild(ad_db, 1)
    assert abs(float(rules[0]["last_run_ts"]) - 9999.0) < 0.001


def test_track_and_pop_due_messages_respects_cutoff(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1002, 200.0)
        track_auto_delete_message(conn, 1, 100, 1003, 300.0)
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=250.0)
    assert [mid for mid, _ in due] == [1001, 1002]


def test_track_message_is_idempotent(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1001]


def test_remove_tracked_message(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_tracked_auto_delete_message(ad_db, 1, 100, 1001)
    with open_db(ad_db) as conn:
        assert pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0) == []


def test_remove_tracked_messages_bulk(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
        track_auto_delete_message(conn, 1, 100, 1002, 200.0)
        track_auto_delete_message(conn, 1, 100, 1003, 300.0)
    remove_tracked_auto_delete_messages(ad_db, 1, 100, {1001, 1002})
    with open_db(ad_db) as conn:
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1003]


def test_remove_tracked_messages_bulk_empty_set_is_noop(ad_db):
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_tracked_auto_delete_messages(ad_db, 1, 100, set())
    with open_db(ad_db) as conn:
        due = pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0)
    assert [mid for mid, _ in due] == [1001]


def test_remove_rule_also_clears_tracked_messages(ad_db):
    upsert_auto_delete_rule(ad_db, 1, 100, 86400, 3600)
    with open_db(ad_db) as conn:
        track_auto_delete_message(conn, 1, 100, 1001, 100.0)
    remove_auto_delete_rule(ad_db, 1, 100)
    with open_db(ad_db) as conn:
        assert pop_due_auto_delete_message_ids(conn, 1, 100, cutoff_ts=9999.0) == []
