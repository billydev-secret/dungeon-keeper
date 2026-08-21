"""Shared ledger mechanics under the economy's paid-submission queues.

Pins, sponsored QOTDs and bounties each let a member spend coins on something
a mod approves or denies. The refund path is the part worth real tests: it
moves money, and it runs on paths that can fire twice.
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
            id INTEGER PRIMARY KEY, guild_id INTEGER, user_id INTEGER,
            price INTEGER, state TEXT, created_at REAL, refunded_at REAL
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
