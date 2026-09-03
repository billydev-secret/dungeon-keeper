"""Tests for services/economy_approvals_service.py — one queue over three products.

Flash themes, sponsored questions and pins are three features with three
tables, but to a moderator they are one job: somebody paid, and somebody has
to say yes or no. This module is what lets the todo board show them as one
list, so the tests here are about the *merge* — ordering across tables, the
guild scope, the overflow sentinel — not about any product's own mechanics,
which their own service tests own.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_approvals_service import (
    QUEUES,
    QUEUES_BY_KEY,
    get_approval_row,
    pending_approval_count,
    pending_approvals,
)
from bot_modules.services.economy_pin_service import submit_pin
from bot_modules.services.economy_qotd_sponsor_service import submit_sponsor
from bot_modules.services.economy_service import EconSettings, apply_credit
from bot_modules.services.economy_theme_service import approve as approve_theme
from bot_modules.services.economy_theme_service import submit_theme
from tests.db_template import migrated_db

GUILD = 700
OTHER_GUILD = 701
MOD = 9001
NOW = 1_800_000_000.0

SETTINGS = EconSettings(
    enabled=True,
    flash_theme_enabled=True,
    price_flash_theme=300,
    theme_channel_id=6666,
    price_qotd_sponsor=40,
    price_pin_of_day=150,
    pin_channel_id=6668,
)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    with open_db(path) as c:
        yield c


def _fund(conn, user_id, amount=5000, guild_id=GUILD):
    apply_credit(conn, guild_id, user_id, amount, "grant", actor_id=MOD)


def _theme(conn, user_id, title="Cursed Cooking", guild_id=GUILD):
    _fund(conn, user_id, guild_id=guild_id)
    return submit_theme(
        conn, SETTINGS, guild_id, user_id, title, "The Idea"
    ).submission_id


def _sponsor(conn, user_id, question="What is your comfort food?", guild_id=GUILD):
    _fund(conn, user_id, guild_id=guild_id)
    return submit_sponsor(conn, SETTINGS, guild_id, user_id, question).submission_id


def _pin(conn, user_id, message="Raid at eight", guild_id=GUILD):
    _fund(conn, user_id, guild_id=guild_id)
    return submit_pin(conn, SETTINGS, guild_id, user_id, message).submission_id


def _stamp(conn, table, row_id, created_at):
    conn.execute(
        f"UPDATE {table} SET created_at = ? WHERE id = ?", (created_at, row_id)
    )


# ── the descriptor table ────────────────────────────────────────────────


def test_every_queue_has_a_distinct_key_and_is_reachable_by_it():
    keys = [q.key for q in QUEUES]
    assert len(keys) == len(set(keys))
    assert set(QUEUES_BY_KEY) == set(keys)


def test_the_three_paid_approval_products_are_all_covered():
    assert {q.product.table for q in QUEUES} == {
        "econ_theme_submissions",
        "econ_qotd_submissions",
        "econ_pin_submissions",
    }


# ── the merged queue ────────────────────────────────────────────────────


def test_an_empty_queue_is_empty(conn):
    assert pending_approvals(conn, GUILD) == []
    assert pending_approval_count(conn, GUILD) == 0


def test_a_pending_submission_from_each_product_lands_in_one_list(conn):
    _theme(conn, 1)
    _sponsor(conn, 2)
    _pin(conn, 3)
    rows = pending_approvals(conn, GUILD)
    assert {r["kind"] for r in rows} == {"theme", "sponsor", "pin"}
    assert pending_approval_count(conn, GUILD) == 3


def test_a_row_carries_who_paid_what_and_for_what(conn):
    _theme(conn, 42, title="Cursed Cooking")
    (row,) = pending_approvals(conn, GUILD)
    assert row["kind"] == "theme"
    assert row["user_id"] == 42
    assert row["price"] == 300
    assert row["summary"] == "Cursed Cooking"


def test_the_oldest_request_is_offered_first(conn):
    theme_id = _theme(conn, 1)
    pin_id = _pin(conn, 2)
    _stamp(conn, "econ_theme_submissions", theme_id, NOW)
    _stamp(conn, "econ_pin_submissions", pin_id, NOW - 60)
    assert [r["kind"] for r in pending_approvals(conn, GUILD)] == ["pin", "theme"]


def test_only_pending_rows_are_waiting(conn):
    sid = _theme(conn, 1)
    approve_theme(conn, sid, resolver_id=MOD)
    assert pending_approvals(conn, GUILD) == []
    assert pending_approval_count(conn, GUILD) == 0


def test_another_guilds_queue_is_not_this_guilds(conn):
    _theme(conn, 1, guild_id=OTHER_GUILD)
    assert pending_approvals(conn, GUILD) == []
    assert pending_approval_count(conn, GUILD) == 0
    assert len(pending_approvals(conn, OTHER_GUILD)) == 1


def test_the_limit_bounds_the_list_but_not_the_count(conn):
    for user_id in range(1, 5):
        _theme(conn, user_id)
    assert len(pending_approvals(conn, GUILD, limit=2)) == 2
    assert pending_approval_count(conn, GUILD) == 4


# ── reading one back ────────────────────────────────────────────────────


def test_a_row_can_be_read_back_by_its_kind_and_id(conn):
    sid = _sponsor(conn, 7, question="What is your comfort food?")
    row = get_approval_row(conn, "sponsor", sid)
    assert row is not None
    assert row["question"] == "What is your comfort food?"


def test_an_unknown_kind_or_id_reads_as_nothing(conn):
    sid = _sponsor(conn, 7)
    assert get_approval_row(conn, "nope", sid) is None
    assert get_approval_row(conn, "sponsor", sid + 999) is None
