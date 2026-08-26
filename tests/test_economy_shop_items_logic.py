"""Tests for economy/shop_items.py — the pure purchasability verdict.

Every gate, in the order the module promises, plus the visibility rules that
decide whether a row appears in the shop at all. No database: this is the
table-testable half, and the service test exercises the money.
"""

from __future__ import annotations

import pytest

from bot_modules.economy.shop_items import (
    HOLDING_STATES,
    REFUNDED_STATES,
    ItemView,
    Refusal,
    evaluate_purchase,
    expiry_cutoff,
    refusal_text,
    todo_task_text,
    visible,
)

NOW = 1_800_000_000.0
DAY = 86400.0


def item(**kw) -> ItemView:
    base = {"item_id": 1, "name": "Custom Emoji", "price": 100}
    return ItemView(**{**base, **kw})


# ── the verdict, gate by gate ──────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param({}, Refusal.OK, id="plain-item-is-buyable"),
        pytest.param({"enabled": False}, Refusal.DISABLED, id="disabled"),
        pytest.param(
            {"available_from": NOW + DAY}, Refusal.NOT_YET, id="window-not-open"
        ),
        pytest.param(
            {"available_until": NOW - DAY}, Refusal.ENDED, id="window-closed"
        ),
        pytest.param({"stock": 3, "sold": 3}, Refusal.SOLD_OUT, id="sold-out"),
        pytest.param({"stock": 3, "sold": 1}, Refusal.OK, id="stock-remaining"),
        pytest.param(
            {"available_from": NOW - DAY, "available_until": NOW + DAY},
            Refusal.OK,
            id="inside-window",
        ),
    ],
)
def test_item_gates(kwargs, expected):
    assert evaluate_purchase(item(**kwargs), now=NOW, balance=500) is expected


def test_unknown_item():
    assert evaluate_purchase(None, now=NOW, balance=500) is Refusal.UNKNOWN


def test_window_boundaries_are_half_open():
    """On sale AT available_from; off sale AT available_until."""
    opens = item(available_from=NOW)
    assert evaluate_purchase(opens, now=NOW, balance=500) is Refusal.OK
    closes = item(available_until=NOW)
    assert evaluate_purchase(closes, now=NOW, balance=500) is Refusal.ENDED


@pytest.mark.parametrize(
    ("limit", "owned", "expected"),
    [
        pytest.param(1, 0, Refusal.OK, id="under-limit"),
        pytest.param(1, 1, Refusal.LIMIT_REACHED, id="at-limit"),
        pytest.param(2, 1, Refusal.OK, id="one-of-two"),
        pytest.param(None, 99, Refusal.OK, id="no-limit-set"),
    ],
)
def test_per_member_limit(limit, owned, expected):
    verdict = evaluate_purchase(
        item(per_member_limit=limit), now=NOW, balance=500, owned_count=owned
    )
    assert verdict is expected


@pytest.mark.parametrize(
    ("balance", "expected"),
    [
        pytest.param(100, Refusal.OK, id="exact-price-affords"),
        pytest.param(99, Refusal.INSUFFICIENT, id="one-short"),
        pytest.param(0, Refusal.INSUFFICIENT, id="broke"),
    ],
)
def test_funds(balance, expected):
    assert evaluate_purchase(item(), now=NOW, balance=balance) is expected


def test_free_item_is_affordable_at_zero_balance():
    assert evaluate_purchase(item(price=0), now=NOW, balance=0) is Refusal.OK


def test_already_renting_beats_the_limit_message():
    """The member who simply has the thing is told that, not 'limit reached'."""
    verdict = evaluate_purchase(
        item(billing="weekly", per_member_limit=1),
        now=NOW, balance=500, owned_count=1, holds_rental=True,
    )
    assert verdict is Refusal.ALREADY_RENTED


def test_gate_order_prefers_the_deadline_over_the_wallet():
    """A member who is both broke and too late hears about the deadline.

    Telling them to go earn coins for something they can no longer buy sends
    them off to waste a week.
    """
    verdict = evaluate_purchase(
        item(available_until=NOW - DAY), now=NOW, balance=0
    )
    assert verdict is Refusal.ENDED


def test_sold_out_beats_insufficient():
    verdict = evaluate_purchase(item(stock=1, sold=1), now=NOW, balance=0)
    assert verdict is Refusal.SOLD_OUT


# ── refusal copy ───────────────────────────────────────────────────


@pytest.mark.parametrize("refusal", [r for r in Refusal if r is not Refusal.OK])
def test_every_refusal_has_member_facing_text(refusal):
    assert refusal_text(refusal).strip()


def test_ok_has_no_refusal_text():
    with pytest.raises(ValueError, match="not a refusal"):
        refusal_text(Refusal.OK)


# ── visibility ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "shown"),
    [
        pytest.param({}, True, id="ordinary-item-shows"),
        pytest.param({"enabled": False}, False, id="disabled-hidden"),
        pytest.param({"available_from": NOW + DAY}, False, id="not-yet-hidden"),
        pytest.param({"available_until": NOW - DAY}, False, id="ended-hidden"),
        pytest.param({"stock": 2, "sold": 2}, True, id="sold-out-still-shown"),
    ],
)
def test_visibility(kwargs, shown):
    assert visible(item(**kwargs), NOW) is shown


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"enabled": False}, id="disabled"),
        pytest.param({"available_until": NOW - DAY}, id="past-its-window"),
    ],
)
def test_a_renter_always_sees_their_own_row(kwargs):
    """Never bill someone weekly for a perk with no row anywhere in the shop."""
    assert visible(item(**kwargs), NOW, owned=True) is True


# ── stock arithmetic ───────────────────────────────────────────────


def test_remaining_is_none_when_unlimited():
    assert item().remaining is None


def test_remaining_never_goes_negative():
    """An admin can lower the stock under live orders; '-2 left' is nonsense."""
    assert item(stock=1, sold=3).remaining == 0


# ── state sets ─────────────────────────────────────────────────────


def test_refunded_and_holding_states_are_disjoint():
    """A refunded order must not also hold stock or a limit slot."""
    assert not (REFUNDED_STATES & HOLDING_STATES)


# ── todo text and the expiry cutoff ────────────────────────────────


def test_todo_task_names_the_item_only():
    """data_register.md anonymises a todos row rather than deleting it, on the
    ground that its text is server work product — a buyer's name baked in
    would survive the erasure inside that half."""
    assert todo_task_text("Custom Emoji") == "Deliver Custom Emoji"


def test_expiry_cutoff_is_days_back():
    assert expiry_cutoff(NOW, 14) == NOW - 14 * DAY


@pytest.mark.parametrize("days", [0, -1])
def test_expiry_disabled_matches_nothing(days):
    """0 turns the sweep off — it must not expire every open order at once."""
    assert expiry_cutoff(NOW, days) == float("-inf")
