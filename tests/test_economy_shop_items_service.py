"""Tests for services/economy_shop_items_service.py — custom shop items.

The money-critical paths: all four kind×billing flows, escrow at purchase and
refund on every non-delivered exit, refund exactly-once under replay, stock as
a guarded decrement that survives a race, the per-member limit ignoring
refunded orders, delivery riding todo completion, and a refused order closing
its todo as *missed* rather than done.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import WEEK_SECONDS
from bot_modules.economy.shop_items import Refusal
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from bot_modules.services.economy_shop_items_service import (
    _take_stock,
    cancel_own_order,
    create_item,
    delete_item,
    end_rental_order,
    expire_orders,
    get_purchase,
    list_items,
    owned_count,
    pending_orders,
    purchase,
    refund_order,
    shop_items_for,
    update_item,
)
from bot_modules.services.todo_service import complete_todo, list_todos
from tests.db_template import migrated_db

GUILD = 800
USER = 3001
USER_2 = 3002
MOD = 9001
ROLE = 4242
NOW = 1_800_000_000.0
DAY = 86400.0

SETTINGS = EconSettings(enabled=True, shop_item_expire_days=14)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


@pytest.fixture
def conn(db):
    with open_db(db) as c:
        yield c


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant", actor_id=MOD)


def _item(conn, **kw):
    base = {"name": "Custom Emoji", "price": 100, "created_by": MOD, "now": NOW}
    return create_item(conn, GUILD, **{**base, **kw})


def _ledger(conn, user_id=USER):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger"
            " WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (GUILD, user_id),
        )
    ]


def _rentals(conn, user_id=USER):
    return list(
        conn.execute(
            "SELECT * FROM econ_rentals WHERE guild_id = ? AND user_id = ?"
            " ORDER BY id",
            (GUILD, user_id),
        )
    )


# ── defining items ─────────────────────────────────────────────────


def test_create_and_list(conn):
    _item(conn, name="Shoutout", price=50)
    _item(conn, name="Plaque", price=200)
    names = [r["name"] for r in list_items(conn, GUILD)]
    assert names == ["Shoutout", "Plaque"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        pytest.param({"name": "  "}, "needs a name", id="blank-name"),
        pytest.param({"price": -1}, "negative", id="negative-price"),
        pytest.param({"kind": "wat"}, "unknown kind", id="bad-kind"),
        pytest.param({"billing": "daily"}, "unknown billing", id="bad-billing"),
        pytest.param({"kind": "role"}, "needs a role", id="role-item-without-role"),
        pytest.param(
            {"available_from": NOW + DAY, "available_until": NOW},
            "end after it starts",
            id="backwards-window",
        ),
    ],
)
def test_create_validation(conn, kwargs, message):
    with pytest.raises(ValueError, match=message):
        _item(conn, **kwargs)


def test_update_rejects_unknown_field(conn):
    item_id = _item(conn)
    with pytest.raises(KeyError):
        update_item(conn, GUILD, item_id, {"sold": 99})


def test_update_cannot_orphan_a_role_item(conn):
    item_id = _item(conn, kind="role", role_id=ROLE)
    with pytest.raises(ValueError, match="needs a role"):
        update_item(conn, GUILD, item_id, {"role_id": None})


def test_delete_refuses_while_an_order_is_open(conn):
    """Deleting would strand escrowed money — disable is the retirement path."""
    _fund(conn, 500)
    item_id = _item(conn)
    purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    with pytest.raises(ValueError, match="disable the item instead"):
        delete_item(conn, GUILD, item_id)


def test_delete_allowed_once_orders_are_settled(conn):
    _fund(conn, 500)
    item_id = _item(conn)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    )
    assert delete_item(conn, GUILD, item_id) is True


# ── the four flows ─────────────────────────────────────────────────


def test_role_once_is_delivered_on_the_spot(conn):
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert out.ok
    assert out.state == "fulfilled"
    assert out.grant_role_id == ROLE
    assert out.todo_id is None
    assert get_balance(conn, GUILD, USER) == 400
    assert ("shop_item", -100) in _ledger(conn)


def test_role_weekly_opens_a_rental(conn):
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, billing="weekly", price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert out.state == "live"
    rentals = _rentals(conn)
    assert len(rentals) == 1
    assert rentals[0]["perk"] == "custom_item"
    assert rentals[0]["catalog_item_id"] == item_id
    assert rentals[0]["next_bill_at"] == pytest.approx(NOW + WEEK_SECONDS)
    assert get_balance(conn, GUILD, USER) == 400


def test_manual_once_escrows_and_files_a_todo(conn):
    _fund(conn, 500)
    item_id = _item(conn, price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert out.state == "pending"
    assert out.todo_id
    # Money is taken now, not at delivery.
    assert get_balance(conn, GUILD, USER) == 400
    tasks = [t["task"] for t in list_todos(conn, GUILD)]
    assert tasks == ["Deliver Custom Emoji"]


def test_the_todo_never_names_the_buyer(conn):
    """The privacy ground for anonymising rather than deleting a todos row."""
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    todo = next(t for t in list_todos(conn, GUILD) if t["id"] == out.todo_id)
    assert str(USER) not in todo["task"]


def test_manual_weekly_opens_its_rental_at_delivery(conn):
    """The escrow paid week one, so the first anniversary is a week out."""
    _fund(conn, 500)
    item_id = _item(conn, billing="weekly", price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert out.state == "pending"
    assert not _rentals(conn)

    later = NOW + 2 * DAY
    assert complete_todo(conn, out.todo_id, GUILD, MOD, now_ts=later) is True

    rentals = _rentals(conn)
    assert len(rentals) == 1
    assert rentals[0]["next_bill_at"] == pytest.approx(later + WEEK_SECONDS)
    assert str(get_purchase(conn, out.purchase_id)["state"]) == "live"
    # No second charge at delivery.
    assert _ledger(conn) == [("grant", 500), ("shop_item", -100)]


# ── delivery through the todo board ────────────────────────────────


def test_ticking_the_todo_delivers_the_order(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert complete_todo(conn, out.todo_id, GUILD, MOD, now_ts=NOW) is True
    row = get_purchase(conn, out.purchase_id)
    assert str(row["state"]) == "fulfilled"
    assert int(row["resolver_id"]) == MOD


def test_delivery_is_exactly_once_under_a_replayed_tick(conn):
    """The board button and the dashboard race; only one may settle."""
    _fund(conn, 500)
    item_id = _item(conn, billing="weekly", price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert complete_todo(conn, out.todo_id, GUILD, MOD, now_ts=NOW) is True
    assert complete_todo(conn, out.todo_id, GUILD, USER_2, now_ts=NOW) is False
    assert len(_rentals(conn)) == 1


def test_an_ordinary_todo_still_completes(conn):
    """The hook must not disturb a row no purchase spawned."""
    from bot_modules.services.todo_service import create_todo

    todo_id = create_todo(conn, GUILD, MOD, "Sweep the floor", now_ts=NOW)
    assert complete_todo(conn, todo_id, GUILD, MOD, now_ts=NOW) is True


# ── refunds ────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["denied", "cancelled", "expired"])
def test_every_refund_path_returns_the_money(conn, state):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert get_balance(conn, GUILD, USER) == 400
    assert refund_order(
        conn, GUILD, out.purchase_id, state=state, resolver_id=MOD, now=NOW
    ) == 100
    assert get_balance(conn, GUILD, USER) == 500
    assert ("shop_item_refund", 100) in _ledger(conn)


def test_refund_is_exactly_once(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    ) == 100
    assert refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    ) is None
    assert get_balance(conn, GUILD, USER) == 500


def test_a_refused_order_leaves_the_board_as_missed_not_done(conn):
    """A refunded order must never render as delivered."""
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    )
    todo = conn.execute(
        "SELECT completed_at, missed_at FROM todos WHERE id = ?", (out.todo_id,)
    ).fetchone()
    assert todo["completed_at"] is None
    assert todo["missed_at"] is not None


def test_refunding_cannot_be_undone_by_ticking_the_todo(conn):
    """mark_missed closes the row, so a late tick can't pay the order twice."""
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    )
    assert complete_todo(conn, out.todo_id, GUILD, MOD, now_ts=NOW) is False
    assert str(get_purchase(conn, out.purchase_id)["state"]) == "denied"
    assert get_balance(conn, GUILD, USER) == 500


def test_refund_rejects_a_non_refund_state(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    with pytest.raises(ValueError, match="not a refund state"):
        refund_order(conn, GUILD, out.purchase_id, state="fulfilled", now=NOW)


def test_a_delivered_order_cannot_be_refunded(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    complete_todo(conn, out.todo_id, GUILD, MOD, now_ts=NOW)
    assert refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    ) is None
    assert get_balance(conn, GUILD, USER) == 400


# ── member self-cancel ─────────────────────────────────────────────


def test_member_cancels_their_own_pending_order(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert cancel_own_order(
        conn, GUILD, out.purchase_id, user_id=USER, now=NOW
    ) == 100
    assert get_balance(conn, GUILD, USER) == 500


def test_cancelling_someone_elses_order_reads_as_an_ordinary_miss(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert cancel_own_order(
        conn, GUILD, out.purchase_id, user_id=USER_2, now=NOW
    ) is None
    assert str(get_purchase(conn, out.purchase_id)["state"]) == "pending"


# ── expiry sweep ───────────────────────────────────────────────────


def test_stale_orders_expire_and_refund(conn):
    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert expire_orders(conn, GUILD, SETTINGS, now=NOW + 15 * DAY) == [
        out.purchase_id
    ]
    assert get_balance(conn, GUILD, USER) == 500


def test_a_fresh_order_survives_the_sweep(conn):
    _fund(conn, 500)
    purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    assert expire_orders(conn, GUILD, SETTINGS, now=NOW + 13 * DAY) == []


def test_expiry_days_zero_disables_the_sweep(conn):
    """0 must not expire every open order at once."""
    _fund(conn, 500)
    purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    off = EconSettings(enabled=True, shop_item_expire_days=0)
    assert expire_orders(conn, GUILD, off, now=NOW + 999 * DAY) == []


# ── stock ──────────────────────────────────────────────────────────


def test_stock_runs_out(conn):
    _fund(conn, 500)
    _fund(conn, 500, USER_2)
    item_id = _item(conn, price=100, stock=1)
    assert purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW).ok
    second = purchase(conn, SETTINGS, GUILD, USER_2, item_id, now=NOW)
    assert second.refusal is Refusal.SOLD_OUT


def test_the_last_unit_can_only_be_taken_once(conn):
    """The decrement itself is the race anchor, not the read-only verdict.

    ``evaluate_purchase`` reads stock a moment before the write, so two
    genuinely concurrent buyers can both pass it; only the guarded UPDATE
    decides. Exercised directly, because two interleaved transactions can't be
    staged through the public call on one connection.
    """
    item_id = _item(conn, price=100, stock=1)
    assert _take_stock(conn, GUILD, item_id) is True
    assert _take_stock(conn, GUILD, item_id) is False
    sold = conn.execute(
        "SELECT sold FROM econ_shop_items WHERE id = ?", (item_id,)
    ).fetchone()["sold"]
    assert sold == 1


def test_an_unlimited_item_never_runs_out(conn):
    item_id = _item(conn, price=100)
    assert all(_take_stock(conn, GUILD, item_id) for _ in range(50))


def test_a_refund_puts_the_unit_back_on_the_shelf(conn):
    _fund(conn, 500)
    _fund(conn, 500, USER_2)
    item_id = _item(conn, price=100, stock=1)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    )
    assert purchase(conn, SETTINGS, GUILD, USER_2, item_id, now=NOW).ok


def test_an_unaffordable_purchase_consumes_no_stock(conn):
    """The debit and the decrement land together or not at all."""
    item_id = _item(conn, price=100, stock=1)
    assert purchase(
        conn, SETTINGS, GUILD, USER, item_id, now=NOW
    ).refusal is Refusal.INSUFFICIENT
    sold = conn.execute(
        "SELECT sold FROM econ_shop_items WHERE id = ?", (item_id,)
    ).fetchone()["sold"]
    assert sold == 0


# ── per-member limit ───────────────────────────────────────────────


def test_per_member_limit_blocks_the_second_buy(conn):
    _fund(conn, 500)
    item_id = _item(conn, price=100, per_member_limit=1)
    assert purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW).ok
    assert purchase(
        conn, SETTINGS, GUILD, USER, item_id, now=NOW
    ).refusal is Refusal.LIMIT_REACHED


def test_a_refused_order_does_not_consume_the_members_one_allowed_buy(conn):
    """A mod's refusal must not quietly spend the member's limit."""
    _fund(conn, 500)
    item_id = _item(conn, price=100, per_member_limit=1)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    refund_order(
        conn, GUILD, out.purchase_id, state="denied", resolver_id=MOD, now=NOW
    )
    assert owned_count(conn, GUILD, USER, item_id) == 0
    assert purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW).ok


def test_the_limit_is_per_member_not_per_guild(conn):
    _fund(conn, 500)
    _fund(conn, 500, USER_2)
    item_id = _item(conn, price=100, per_member_limit=1)
    assert purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW).ok
    assert purchase(conn, SETTINGS, GUILD, USER_2, item_id, now=NOW).ok


def test_renting_the_same_item_twice_is_refused(conn):
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, billing="weekly", price=100)
    assert purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW).ok
    assert purchase(
        conn, SETTINGS, GUILD, USER, item_id, now=NOW
    ).refusal is Refusal.ALREADY_RENTED


def test_two_different_items_can_be_rented_at_once(conn):
    """The COALESCE in the live-rental index is what allows this."""
    _fund(conn, 500)
    first = _item(conn, name="A", kind="role", role_id=ROLE, billing="weekly", price=50)
    second = _item(conn, name="B", kind="role", role_id=99, billing="weekly", price=50)
    assert purchase(conn, SETTINGS, GUILD, USER, first, now=NOW).ok
    assert purchase(conn, SETTINGS, GUILD, USER, second, now=NOW).ok
    assert len(_rentals(conn)) == 2


# ── window and visibility, through the database ────────────────────


def test_the_shop_hides_what_cannot_be_bought(conn):
    _item(conn, name="Live")
    _item(conn, name="Off", enabled=0)
    _item(conn, name="Later", available_from=NOW + DAY)
    names = [i.name for i in shop_items_for(conn, GUILD, now=NOW)]
    assert names == ["Live"]


def test_a_renter_keeps_seeing_a_disabled_item(conn):
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, billing="weekly", price=100)
    purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    update_item(conn, GUILD, item_id, {"enabled": 0})
    assert shop_items_for(conn, GUILD, now=NOW) == []
    mine = shop_items_for(conn, GUILD, now=NOW, user_id=USER)
    assert [i.item_id for i in mine] == [item_id]


# ── queue and bookkeeping ──────────────────────────────────────────


def test_pending_orders_lists_oldest_first_with_item_names(conn):
    _fund(conn, 500)
    _fund(conn, 500, USER_2)
    first = purchase(conn, SETTINGS, GUILD, USER, _item(conn, name="A"), now=NOW)
    purchase(conn, SETTINGS, GUILD, USER_2, _item(conn, name="B"), now=NOW + 1)
    rows = pending_orders(conn, GUILD)
    assert [r["item_name"] for r in rows] == ["A", "B"]
    assert int(rows[0]["id"]) == first.purchase_id


def test_a_lapsed_rental_marks_its_order_ended(conn):
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, billing="weekly", price=100)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    end_rental_order(conn, out.rental_id, now=NOW + DAY)
    assert str(get_purchase(conn, out.purchase_id)["state"]) == "lapsed"


def test_the_note_is_only_kept_when_the_item_asks_for_one(conn):
    _fund(conn, 500)
    quiet = purchase(
        conn, SETTINGS, GUILD, USER, _item(conn, name="A"), note="hi", now=NOW
    )
    asked = purchase(
        conn, SETTINGS, GUILD, USER, _item(conn, name="B", ask_note=1),
        note="  engrave BILLY  ", now=NOW,
    )
    assert str(get_purchase(conn, quiet.purchase_id)["note"]) == ""
    assert str(get_purchase(conn, asked.purchase_id)["note"]) == "engrave BILLY"


# ── nothing is left behind by a refusal (code review, 2026-08-25) ──


def test_a_lost_funds_race_on_a_rental_leaves_no_rental_row(conn, monkeypatch):
    """rent_perk inserts the rental and *then* debits, raising on failure so the
    caller's transaction unwinds. purchase() reports that as a refusal instead
    of propagating, so without a savepoint the 'active' row stays pending — and
    open_db commits on normal exit, handing out a free, silently-billing rental.
    """
    _fund(conn, 500)
    item_id = _item(conn, kind="role", role_id=ROLE, billing="weekly", price=100)
    monkeypatch.setattr(
        "bot_modules.services.economy_rentals_service.apply_debit",
        lambda *a, **k: False,
    )
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    assert out.refusal is Refusal.INSUFFICIENT
    assert _rentals(conn) == []
    assert get_balance(conn, GUILD, USER) == 500


def test_a_lost_funds_race_returns_the_stock(conn, monkeypatch):
    """The guarded decrement runs before the money moves, so a refusal after it
    must put the unit back or a stock-1 item becomes unbuyable forever."""
    _fund(conn, 500)
    item_id = _item(conn, price=100, stock=1)
    monkeypatch.setattr(
        "bot_modules.services.economy_shop_items_service.apply_debit",
        lambda *a, **k: False,
    )
    assert purchase(
        conn, SETTINGS, GUILD, USER, item_id, now=NOW
    ).refusal is Refusal.INSUFFICIENT
    sold = conn.execute(
        "SELECT sold FROM econ_shop_items WHERE id = ?", (item_id,)
    ).fetchone()["sold"]
    assert sold == 0


def test_a_lost_funds_race_writes_no_purchase_row(conn, monkeypatch):
    _fund(conn, 500)
    item_id = _item(conn, price=100)
    monkeypatch.setattr(
        "bot_modules.services.economy_shop_items_service.apply_debit",
        lambda *a, **k: False,
    )
    purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    rows = conn.execute("SELECT COUNT(*) AS n FROM econ_shop_purchases").fetchone()
    assert rows["n"] == 0


def test_two_pending_orders_for_one_weekly_item_both_resolve(conn):
    """A manual weekly item opens no rental until delivery, so the live-rental
    gate can't see the first order. Both todos must still tick off — the bare
    INSERT raised IntegrityError out of complete_todo, rolling back every other
    completion in the mod's multi-select batch."""
    _fund(conn, 500)
    item_id = _item(conn, billing="weekly", price=100)
    first = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)
    second = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW + 1)
    assert first.ok and second.ok

    assert complete_todo(conn, first.todo_id, GUILD, MOD, now_ts=NOW + 2) is True
    assert complete_todo(conn, second.todo_id, GUILD, MOD, now_ts=NOW + 3) is True


@pytest.mark.parametrize("field", ["kind", "billing"])
def test_update_rejects_an_explicit_null(conn, field):
    """A present-but-None value wrote NULL into a NOT NULL column — an
    IntegrityError where every other bad input raises ValueError."""
    item_id = _item(conn)
    with pytest.raises(ValueError):
        update_item(conn, GUILD, item_id, {field: None})


def test_purging_a_buyer_closes_their_open_order(conn):
    """Erasure deletes the purchase row, so an untouched pending order would
    leave a todo pointing at nothing — a mod ticks it off and silently delivers
    nothing — and would strand its unit of stock forever."""
    from bot_modules.services.economy_service import econ_purge_user

    _fund(conn, 500)
    item_id = _item(conn, price=100, stock=1)
    out = purchase(conn, SETTINGS, GUILD, USER, item_id, now=NOW)

    econ_purge_user(conn, GUILD, USER)

    sold = conn.execute(
        "SELECT sold FROM econ_shop_items WHERE id = ?", (item_id,)
    ).fetchone()["sold"]
    assert sold == 0
    todo = conn.execute(
        "SELECT purchase_id, missed_at FROM todos WHERE id = ?", (out.todo_id,)
    ).fetchone()
    assert todo["purchase_id"] is None
    assert todo["missed_at"] is not None


def test_the_board_query_joins_the_buyer_back_onto_the_order(conn):
    """The task text can't name the buyer, so the board has to join for them."""
    from bot_modules.services.todo_service import pending_todos

    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    row = next(r for r in pending_todos(conn, GUILD) if r["id"] == out.todo_id)
    assert row["purchase_id"] == out.purchase_id
    assert row["buyer_id"] == USER


def test_an_ordinary_task_has_no_buyer(conn):
    from bot_modules.services.todo_service import create_todo, pending_todos

    todo_id = create_todo(conn, GUILD, MOD, "Sweep up", now_ts=NOW)
    row = next(r for r in pending_todos(conn, GUILD) if r["id"] == todo_id)
    assert row["purchase_id"] is None
    assert row["buyer_id"] is None


def test_purging_the_buyer_leaves_the_board_row_anonymous(conn):
    """The work stands; the person disappears from it."""
    from bot_modules.services.economy_service import econ_purge_user
    from bot_modules.services.todo_service import pending_todos

    _fund(conn, 500)
    out = purchase(conn, SETTINGS, GUILD, USER, _item(conn), now=NOW)
    econ_purge_user(conn, GUILD, USER)
    # The order closed as missed, so it leaves the pending list entirely.
    assert all(r["id"] != out.todo_id for r in pending_todos(conn, GUILD))
