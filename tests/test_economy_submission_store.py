"""Shared ledger mechanics under the economy's paid-submission queues.

Pins, sponsored QOTDs, sponsored emoji and flash themes each let a member
spend coins on something a mod approves or denies. The refund path is the part
worth real tests: it moves money, and it runs on paths that can fire twice.

The second half covers the product-shaped layer the four services now sit on.
Its two load-bearing guarantees are that ``charge_and_insert`` never leaves a
debit without a row, and that ``move_state`` lets exactly one of two racing
resolvers through — every product's concurrency story reduces to that.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.services import economy_submission_store as store


@pytest.fixture
def conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE econ_pin_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER,
            user_id INTEGER, price INTEGER, state TEXT, created_at REAL,
            refunded_at REAL, message TEXT, resolver_id INTEGER,
            deny_reason TEXT, resolved_at REAL, expires_at REAL,
            card_channel_id INTEGER, card_message_id INTEGER
        )
        """
    )
    return conn


def _row(conn, *, sub_id=1, price=100, state="pending", created_at=1.0, refunded=None):
    conn.execute(
        "INSERT INTO econ_pin_submissions "
        "(id, guild_id, user_id, price, state, created_at, refunded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sub_id, 7, 42, price, state, created_at, refunded),
    )
    return conn.execute(
        "SELECT * FROM econ_pin_submissions WHERE id = ?", (sub_id,)
    ).fetchone()


@pytest.fixture
def credits(monkeypatch):
    """Record ledger credits instead of writing them."""
    calls: list[dict] = []

    def _apply(conn, guild_id, user_id, amount, kind, meta=None, booster=False):
        calls.append(
            dict(guild_id=guild_id, user_id=user_id, amount=amount, kind=kind, meta=meta)
        )

    monkeypatch.setattr(store, "apply_credit", _apply)
    return calls


# ── refund_once ───────────────────────────────────────────────────────


def test_a_refund_credits_the_price_back(conn, credits):
    row = _row(conn, price=250)
    assert store.refund_once(conn, "econ_pin_submissions", row, "denied",
                             refund_kind="pin_sponsor_refund") == 250
    assert credits == [
        dict(guild_id=7, user_id=42, amount=250, kind="pin_sponsor_refund",
             meta={"submission_id": 1, "reason": "denied"})
    ]


def test_a_second_refund_pays_nothing(conn, credits):
    """Two mods denying the same submission at once must pay out once.

    The guard is the UPDATE's own predicate, not a prior read, so this holds
    even when both callers saw the same un-refunded row.
    """
    row = _row(conn, price=250)
    first = store.refund_once(conn, "econ_pin_submissions", row, "denied",
                              refund_kind="k")
    second = store.refund_once(conn, "econ_pin_submissions", row, "denied",
                               refund_kind="k")
    assert (first, second) == (250, 0)
    assert len(credits) == 1


def test_an_already_refunded_row_pays_nothing(conn, credits):
    row = _row(conn, price=250, refunded=123.0)
    assert store.refund_once(conn, "econ_pin_submissions", row, "x", refund_kind="k") == 0
    assert credits == []


def test_the_refund_is_stamped_on_the_row(conn, credits):
    row = _row(conn, price=250)
    store.refund_once(conn, "econ_pin_submissions", row, "denied", refund_kind="k")
    stamped = conn.execute(
        "SELECT refunded_at FROM econ_pin_submissions WHERE id = 1"
    ).fetchone()
    assert stamped["refunded_at"] is not None


@pytest.mark.parametrize("price", [0, -5])
def test_a_free_submission_is_not_credited(conn, credits, price):
    """Crediting zero would put a meaningless row in the ledger."""
    row = _row(conn, price=price)
    assert store.refund_once(conn, "econ_pin_submissions", row, "x", refund_kind="k") == 0
    assert credits == []
    unstamped = conn.execute(
        "SELECT refunded_at FROM econ_pin_submissions WHERE id = 1"
    ).fetchone()
    assert unstamped["refunded_at"] is None


def test_the_refund_kind_is_the_callers(conn, credits):
    """Pins and QOTD sponsorships file under different ledger kinds."""
    row = _row(conn)
    store.refund_once(conn, "econ_pin_submissions", row, "x",
                      refund_kind="qotd_sponsor_refund")
    assert credits[0]["kind"] == "qotd_sponsor_refund"


# ── list_rows ─────────────────────────────────────────────────────────


def test_a_filtered_queue_reads_oldest_first(conn):
    """A queue is a work list — the longest wait is handled next."""
    _row(conn, sub_id=1, state="pending", created_at=300.0)
    _row(conn, sub_id=2, state="pending", created_at=100.0)
    _row(conn, sub_id=3, state="live", created_at=200.0)
    rows = store.list_rows(conn, "econ_pin_submissions", 7, "pending")
    assert [r["id"] for r in rows] == [2, 1]


def test_an_unfiltered_list_reads_newest_first(conn):
    """Unfiltered this view is a history, not a queue."""
    _row(conn, sub_id=1, created_at=100.0)
    _row(conn, sub_id=2, created_at=300.0)
    rows = store.list_rows(conn, "econ_pin_submissions", 7)
    assert [r["id"] for r in rows] == [2, 1]


@pytest.mark.parametrize("state", [None, "pending"], ids=["unfiltered", "filtered"])
def test_ties_break_on_id_so_paging_is_stable(conn, state):
    """Equal timestamps must not shuffle between requests. Bounties used to
    omit this on the unfiltered branch, so their history could reorder."""
    for i in (1, 2, 3):
        _row(conn, sub_id=i, state="pending", created_at=100.0)
    ids = [r["id"] for r in store.list_rows(conn, "econ_pin_submissions", 7, state)]
    assert ids == ([1, 2, 3] if state else [3, 2, 1])


def test_other_guilds_are_not_listed(conn):
    _row(conn, sub_id=1)
    conn.execute(
        "INSERT INTO econ_pin_submissions (id, guild_id, user_id, price, state, created_at) "
        "VALUES (2, 999, 42, 100, 'pending', 1.0)"
    )
    assert [r["id"] for r in store.list_rows(conn, "econ_pin_submissions", 7)] == [1]


@pytest.mark.parametrize(
    ("asked", "served"),
    [
        pytest.param(0, 1, id="zero-clamps-up"),
        pytest.param(-10, 1, id="negative-clamps-up"),
        pytest.param(9999, 500, id="huge-clamps-down"),
    ],
)
def test_the_page_size_is_clamped_not_rejected(conn, asked, served):
    """This backs a dashboard fetch; an odd page size is worth serving."""
    for i in range(1, 4):
        _row(conn, sub_id=i, created_at=float(i))
    rows = store.list_rows(conn, "econ_pin_submissions", 7, None, asked)
    assert len(rows) == min(served, 3)


# ── the identifier guard ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda c, r: store.refund_once(c, "x; DROP TABLE y", r, "z", refund_kind="k"),
            id="refund",
        ),
        pytest.param(lambda c, r: store.list_rows(c, "x; DROP TABLE y", 7), id="list"),
    ],
)
def test_a_bad_table_name_never_reaches_the_database(conn, credits, call):
    row = _row(conn)
    with pytest.raises(ValueError):
        call(conn, row)
    assert credits == []


# ── the product-shaped layer ──────────────────────────────────────────

PRODUCT = store.SubmissionProduct(
    table="econ_pin_submissions",
    spend_kind="pin_sponsor",
    refund_kind="pin_sponsor_refund",
    open_states=("pending", "live"),
)


@pytest.fixture
def debits(monkeypatch):
    """Record ledger debits, and let a test make the member too poor."""
    calls: list[dict] = []
    state = {"afford": True}

    def _apply(conn, guild_id, user_id, amount, kind, meta=None, booster=False):
        if not state["afford"]:
            return False
        calls.append(dict(user_id=user_id, amount=amount, kind=kind, meta=meta))
        return True

    monkeypatch.setattr(store, "apply_debit", _apply)
    monkeypatch.setattr(
        "bot_modules.services.economy_quests_service.fire_trigger_inline",
        lambda *a, **k: None,
    )
    return state, calls


def test_an_in_flight_submission_is_found_by_state(conn):
    _row(conn, sub_id=1, state="denied")
    _row(conn, sub_id=2, state="live")
    found = store.open_submission(conn, PRODUCT, 7, 42)
    assert found is not None and found["id"] == 2


def test_a_member_with_only_terminal_rows_has_nothing_in_flight(conn):
    """The one-per-member rule must free up once a submission runs its course."""
    _row(conn, sub_id=1, state="expired")
    assert store.open_submission(conn, PRODUCT, 7, 42) is None


def test_charging_inserts_the_row_and_debits_once(conn, debits):
    _state, calls = debits
    sub_id = store.charge_and_insert(
        conn, PRODUCT, 7, 42, 250, {"message": "hello"}, now=99.0
    )
    row = store.get(conn, PRODUCT, sub_id)
    assert row is not None
    assert (row["state"], row["price"], row["message"], row["created_at"]) == (
        "pending", 250, "hello", 99.0
    )
    assert calls == [
        dict(user_id=42, amount=250, kind="pin_sponsor", meta={"message": "hello"})
    ]


def test_a_member_who_cannot_pay_gets_no_row(conn, debits):
    """Returning None rather than raising keeps the 'you have X' sentence
    at the product, where the member-facing wording lives."""
    state, _calls = debits
    state["afford"] = False
    assert store.charge_and_insert(conn, PRODUCT, 7, 42, 250, {"message": "hi"}) is None
    assert conn.execute("SELECT COUNT(*) FROM econ_pin_submissions").fetchone()[0] == 0


def test_a_guarded_move_advances_the_state(conn, credits):
    _row(conn, sub_id=1, state="pending")
    fresh = store.move_state(
        conn, PRODUCT, 1, from_state="pending", to_state="approved",
        resolver_id=900, now=50.0,
    )
    assert fresh is not None
    assert (fresh["state"], fresh["resolver_id"], fresh["resolved_at"]) == (
        "approved", 900, 50.0
    )
    assert credits == []


def test_the_loser_of_a_race_gets_none_not_an_exception(conn, credits):
    """Two mods clicking Approve both run this; exactly one may win. None
    rather than a raise, because the apology sentence differs per product."""
    _row(conn, sub_id=1, state="pending")
    first = store.move_state(conn, PRODUCT, 1, from_state="pending", to_state="denied",
                             refund_reason="denied")
    second = store.move_state(conn, PRODUCT, 1, from_state="pending", to_state="denied",
                              refund_reason="denied")
    assert first is not None and second is None
    assert len(credits) == 1


def test_a_move_on_a_missing_row_is_none(conn):
    assert store.move_state(conn, PRODUCT, 404, from_state="pending",
                            to_state="denied") is None


def test_a_refunding_move_pays_the_price_back(conn, credits):
    _row(conn, sub_id=1, price=175, state="pending")
    store.move_state(conn, PRODUCT, 1, from_state="pending", to_state="denied",
                     refund_reason="denied")
    assert credits[0]["amount"] == 175
    assert credits[0]["kind"] == "pin_sponsor_refund"


def test_extra_columns_land_in_the_same_update(conn, credits):
    """Going live sets the clock and the pinned-message ids atomically with
    the state — a half-applied go-live would strand a paid pin."""
    _row(conn, sub_id=1, state="pending")
    fresh = store.move_state(
        conn, PRODUCT, 1, from_state="pending", to_state="live",
        now=10.0, extra={"expires_at": 10.0 + 86400},
    )
    assert fresh is not None and fresh["expires_at"] == 10.0 + 86400


def test_a_long_deny_reason_is_truncated(conn, credits):
    _row(conn, sub_id=1, state="pending")
    fresh = store.move_state(conn, PRODUCT, 1, from_state="pending",
                             to_state="denied", deny_reason="x" * 900)
    assert fresh is not None and len(fresh["deny_reason"]) == 500


def test_the_stale_sweep_expires_and_refunds(conn, credits):
    _row(conn, sub_id=1, price=60, state="pending", created_at=0.0)
    expired = store.expire_stale_pending(conn, PRODUCT, 7, days=3, now=10 * 86400.0)
    assert [r["id"] for r in expired] == [1]
    assert credits[0]["amount"] == 60
    assert store.get(conn, PRODUCT, 1)["state"] == "expired"


def test_the_stale_sweep_leaves_fresh_rows_alone(conn, credits):
    _row(conn, sub_id=1, state="pending", created_at=10 * 86400.0)
    assert store.expire_stale_pending(conn, PRODUCT, 7, days=3,
                                      now=10 * 86400.0 + 60) == []
    assert credits == []


def test_the_stale_sweep_never_touches_an_accepted_submission(conn, credits):
    """Timing out an approved row would charge a member for staff latency."""
    _row(conn, sub_id=1, state="live", created_at=0.0)
    assert store.expire_stale_pending(conn, PRODUCT, 7, days=1, now=99 * 86400.0) == []
    assert credits == []


@pytest.mark.parametrize("days", [0, -1], ids=["zero", "negative"])
def test_a_disabled_sweep_expires_nothing(conn, credits, days):
    """0 is how a guild with a slow queue keeps submissions alive."""
    _row(conn, sub_id=1, state="pending", created_at=0.0)
    assert store.expire_stale_pending(conn, PRODUCT, 7, days=days,
                                      now=99 * 86400.0) == []
    assert credits == []


def test_the_sweep_is_scoped_to_one_guild(conn, credits):
    _row(conn, sub_id=1, state="pending", created_at=0.0)
    conn.execute(
        "INSERT INTO econ_pin_submissions (id, guild_id, user_id, price, state, "
        "created_at) VALUES (2, 999, 42, 100, 'pending', 0.0)"
    )
    assert [r["id"] for r in
            store.expire_stale_pending(conn, PRODUCT, 7, days=1, now=99 * 86400.0)
            ] == [1]


def test_the_approval_card_location_is_recorded(conn):
    """Without this the card can't be edited when the queue is resolved."""
    _row(conn, sub_id=1)
    store.set_card(conn, PRODUCT, 1, 111, 222)
    row = store.get(conn, PRODUCT, 1)
    assert (row["card_channel_id"], row["card_message_id"]) == (111, 222)
