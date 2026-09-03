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
    DASHBOARD_KINDS,
    DASHBOARD_QUEUES,
    QUEUES,
    QUEUES_BY_KEY,
    card_location,
    get_approval_row,
    pending_approval_count,
    pending_approvals,
    pending_for_dashboard,
    set_approval_card,
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


# ── the card ledger ─────────────────────────────────────────────────────
#
# The two columns that make the approvals channel and the todo board one
# surface rather than two: whoever resolves a request has to be able to find
# and close the card the other surface is showing.


@pytest.mark.parametrize(
    ("kind", "make"),
    [
        pytest.param("theme", _theme, id="theme"),
        pytest.param("sponsor", _sponsor, id="sponsor"),
        pytest.param("pin", _pin, id="pin"),
    ],
)
def test_a_cards_location_round_trips_for_every_product(conn, kind, make):
    sid = make(conn, 11)
    assert set_approval_card(conn, kind, sid, 4242, 9999) is True
    row = get_approval_row(conn, kind, sid)
    assert row is not None
    assert card_location(row) == (4242, 9999)


def test_an_uncarded_request_reports_no_location(conn):
    """Every request submitted while the channel dial was unset looks like this."""
    sid = _pin(conn, 12)
    row = get_approval_row(conn, "pin", sid)
    assert row is not None
    assert card_location(row) == (0, 0)


def test_an_unknown_kind_records_nothing_rather_than_raising(conn):
    """A stale kind is a shrug, exactly as get_approval_row treats one."""
    assert set_approval_card(conn, "nope", 1, 4242, 9999) is False


def test_recording_a_card_does_not_disturb_the_pending_queue(conn):
    """Posting a card is not a resolution — the board must still show the row."""
    sid = _theme(conn, 13)
    set_approval_card(conn, "theme", sid, 4242, 9999)
    rows = pending_approvals(conn, GUILD)
    assert [r["id"] for r in rows] == [sid]
    assert pending_approval_count(conn, GUILD) == 1


def test_a_second_post_repoints_the_location(conn):
    """An admin can move the review channel; the newest card is the live one."""
    sid = _sponsor(conn, 14)
    set_approval_card(conn, "sponsor", sid, 1, 2)
    set_approval_card(conn, "sponsor", sid, 3, 4)
    row = get_approval_row(conn, "sponsor", sid)
    assert row is not None
    assert card_location(row) == (3, 4)


@pytest.mark.parametrize(
    "row",
    [
        pytest.param(None, id="no-row"),
        pytest.param({}, id="empty-mapping"),
        pytest.param({"card_channel_id": None, "card_message_id": None}, id="nulls"),
        pytest.param({"card_channel_id": "x", "card_message_id": "y"}, id="garbage"),
    ],
)
def test_card_location_reads_anything_unusable_as_uncarded(row):
    """"Don't know" and "not carded" collapse to the same do-nothing answer."""
    assert card_location(row) == (0, 0)


# ── the dashboard's wider queue ─────────────────────────────────────────
#
# The board's QUEUES answers "is anybody waiting on a yes/no?"; the dashboard
# answers "is anybody waiting on us at all?", which is two products bigger.


def _emoji(conn, user_id, name="sparkle", guild_id=GUILD):
    _fund(conn, user_id, guild_id=guild_id)
    conn.execute(
        "INSERT INTO econ_emoji_submissions (guild_id, user_id, name, image_path,"
        " state, price, created_at) VALUES (?, ?, ?, '/tmp/x.png', 'pending', ?, ?)",
        (guild_id, user_id, name, 250, 1000.0),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def _claim(conn, user_id, title="Host a hangout", reward=75, guild_id=GUILD):
    conn.execute(
        "INSERT INTO econ_quests (guild_id, title, qtype, reward, signoff, active,"
        " created_at) VALUES (?, ?, 'daily', ?, 1, 1, 1000.0)",
        (guild_id, title, reward),
    )
    quest_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO econ_quest_claims (quest_id, guild_id, user_id, period, state,"
        " created_at) VALUES (?, ?, ?, '2026-09-03', 'pending', 1000.0)",
        (quest_id, guild_id, user_id),
    )
    claim_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    return quest_id, claim_id


def _order(conn, user_id, item="Custom title", price=500, guild_id=GUILD):
    conn.execute(
        "INSERT INTO econ_shop_items (guild_id, name, kind, billing, price,"
        " created_at) VALUES (?, ?, 'manual', 'once', ?, 1000.0)",
        (guild_id, item, price),
    )
    item_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        "INSERT INTO econ_shop_purchases (guild_id, user_id, item_id, price, state,"
        " created_at) VALUES (?, ?, ?, ?, 'pending', 1000.0)",
        (guild_id, user_id, item_id, price),
    )
    return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])


def test_the_board_queue_is_not_widened_by_the_dashboard_one():
    """QUEUES keys ride in the Discord board's select values and signature.

    Widening it to serve a web page would change a Discord surface, so the
    dashboard gets its own tuple and the board's stays exactly three.
    """
    assert [q.key for q in QUEUES] == ["theme", "sponsor", "pin"]
    assert [q.key for q in DASHBOARD_QUEUES] == ["theme", "sponsor", "pin", "emoji"]
    assert set(DASHBOARD_KINDS) == {
        "theme", "sponsor", "pin", "emoji", "claim", "order",
    }


def test_an_empty_dashboard_queue_is_empty(conn):
    assert pending_for_dashboard(conn, GUILD) == []


def test_every_product_lands_in_one_list(conn):
    """The whole point of the merge: one look answers "is anyone waiting?"."""
    _theme(conn, 11)
    _sponsor(conn, 12)
    _pin(conn, 13)
    _emoji(conn, 14)
    _claim(conn, 15)
    _order(conn, 16)
    kinds = {r["kind"] for r in pending_for_dashboard(conn, GUILD)}
    assert kinds == {"theme", "sponsor", "pin", "emoji", "claim", "order"}


def test_the_list_is_oldest_first_across_products(conn):
    """It is a work list: the longest wait is handled next."""
    t = _theme(conn, 11)
    s = _sponsor(conn, 12)
    _stamp(conn, "econ_theme_submissions", t, 3000.0)
    _stamp(conn, "econ_qotd_submissions", s, 1000.0)
    e = _emoji(conn, 14)
    _stamp(conn, "econ_emoji_submissions", e, 2000.0)
    order = [r["kind"] for r in pending_for_dashboard(conn, GUILD)]
    assert order == ["sponsor", "emoji", "theme"]


def test_an_order_carries_its_item_name_and_price(conn):
    _order(conn, 16, item="Custom title", price=500)
    row = next(r for r in pending_for_dashboard(conn, GUILD) if r["kind"] == "order")
    assert row["summary"] == "Custom title"
    assert row["amount"] == 500


def test_a_claim_carries_its_quest_reward_as_the_amount(conn):
    """Nobody paid to claim — the coins flow the other way — but the number a
    reviewer wants on the row is still the amount, so it rides in that field."""
    _claim(conn, 15, title="Host a hangout", reward=75)
    row = next(r for r in pending_for_dashboard(conn, GUILD) if r["kind"] == "claim")
    assert row["summary"] == "Host a hangout"
    assert row["amount"] == 75


def test_a_claim_whose_quest_was_deleted_still_appears(conn):
    """The member is waiting either way; hiding the row would strand them."""
    quest_id, _ = _claim(conn, 15)
    conn.execute("DELETE FROM econ_quests WHERE id = ?", (quest_id,))
    row = next(r for r in pending_for_dashboard(conn, GUILD) if r["kind"] == "claim")
    assert row["summary"] == ""
    assert row["amount"] == 0


def test_only_pending_rows_are_listed(conn):
    t = _theme(conn, 11)
    approve_theme(conn, t, resolver_id=MOD)
    _claim(conn, 15)
    conn.execute("UPDATE econ_quest_claims SET state = 'paid'")
    _order(conn, 16)
    conn.execute("UPDATE econ_shop_purchases SET state = 'fulfilled'")
    assert pending_for_dashboard(conn, GUILD) == []


def test_another_guilds_rows_never_appear(conn):
    _theme(conn, 11, guild_id=GUILD)
    _theme(conn, 11, guild_id=OTHER_GUILD)
    _emoji(conn, 14, guild_id=OTHER_GUILD)
    _claim(conn, 15, guild_id=OTHER_GUILD)
    _order(conn, 16, guild_id=OTHER_GUILD)
    rows = pending_for_dashboard(conn, GUILD)
    assert len(rows) == 1


def test_the_limit_is_a_runaway_guard_not_pagination(conn):
    for i in range(5):
        _pin(conn, 20 + i, message=f"pin {i}")
    assert len(pending_for_dashboard(conn, GUILD, limit=3)) == 3
    assert len(pending_for_dashboard(conn, GUILD)) == 5
