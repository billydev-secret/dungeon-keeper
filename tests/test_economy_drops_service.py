"""Coin Drops service tests — scheduling math, the claim race, and expiry."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_drops_service import (
    create_drop,
    discard_drop,
    drops_configured,
    expire_due_drops,
    has_open_drop,
    next_drop_delay,
    roll_amount,
    set_drop_message,
    try_claim_drop,
)
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 1234
CHANNEL = 5678
ALICE = 111
BOB = 222

LIVE = replace(
    DEFAULT_ECON_SETTINGS,
    enabled=True,
    drops_channel_id=CHANNEL,
    drops_min_coins=5,
    drops_max_coins=25,
    drops_per_day=4,
    drops_expire_minutes=60,
)


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _drop(conn, *, now_ts=1000.0, expire_minutes=60, amount=10, guild_id=GUILD):
    return create_drop(
        conn,
        guild_id,
        CHANNEL,
        amount,
        now_ts=now_ts,
        expire_minutes=expire_minutes,
    )


def _row(conn, drop_id):
    return conn.execute(
        "SELECT * FROM econ_drops WHERE id = ?", (drop_id,)
    ).fetchone()


# ── configuration gate ────────────────────────────────────────────────


def test_drops_off_by_default():
    assert not drops_configured(DEFAULT_ECON_SETTINGS)


def test_drops_configured_when_enabled_and_channel_set():
    assert drops_configured(LIVE)


@pytest.mark.parametrize(
    "override",
    [
        {"enabled": False},  # economy master switch wins
        {"drops_channel_id": 0},  # the channel picker is the toggle
        {"drops_per_day": 0},  # cadence 0 = off
        {"drops_min_coins": 0, "drops_max_coins": 0},  # nothing to pay
    ],
)
def test_drops_configured_gates(override):
    assert not drops_configured(replace(LIVE, **override))


# ── randomness helpers ────────────────────────────────────────────────


def test_roll_amount_stays_in_bounds():
    rng = random.Random(42)
    rolls = {roll_amount(LIVE, rng) for _ in range(300)}
    assert min(rolls) >= 5
    assert max(rolls) <= 25


def test_roll_amount_tolerates_swapped_and_zero_bounds():
    rng = random.Random(42)
    swapped = replace(LIVE, drops_min_coins=25, drops_max_coins=5)
    assert 5 <= roll_amount(swapped, rng) <= 25
    zero_min = replace(LIVE, drops_min_coins=0, drops_max_coins=3)
    assert all(1 <= roll_amount(zero_min, rng) <= 3 for _ in range(50))
    fixed = replace(LIVE, drops_min_coins=7, drops_max_coins=7)
    assert roll_amount(fixed, rng) == 7


def test_next_drop_delay_jitters_around_the_average():
    rng = random.Random(42)
    period = 86400.0 / 4  # drops_per_day = 4
    delays = [next_drop_delay(LIVE, rng) for _ in range(300)]
    assert min(delays) >= 0.5 * period
    assert max(delays) <= 1.5 * period


# ── create / backfill / discard ───────────────────────────────────────


def test_create_backfill_and_open_flag(db):
    with open_db(db) as conn:
        assert not has_open_drop(conn, GUILD)
        drop_id = _drop(conn, now_ts=1000.0, expire_minutes=30)
        row = _row(conn, drop_id)
        assert row["status"] == "open"
        assert int(row["message_id"]) == 0  # backfilled after the send
        assert float(row["expires_at"]) == pytest.approx(1000.0 + 30 * 60)
        set_drop_message(conn, drop_id, 424242)
        assert int(_row(conn, drop_id)["message_id"]) == 424242
        assert has_open_drop(conn, GUILD)
        assert not has_open_drop(conn, GUILD + 1)  # guild-scoped


def test_discard_removes_only_open_rows(db):
    with open_db(db) as conn:
        failed_send = _drop(conn)
        discard_drop(conn, failed_send)
        assert _row(conn, failed_send) is None
        claimed = _drop(conn)
        assert try_claim_drop(
            conn, LIVE, claimed, GUILD, ALICE, now_ts=1500.0, booster=False
        )
        discard_drop(conn, claimed)  # settled history must survive
        assert _row(conn, claimed)["status"] == "claimed"


# ── the claim race ────────────────────────────────────────────────────


def test_claim_pays_and_settles_the_row(db):
    with open_db(db) as conn:
        drop_id = _drop(conn, amount=10)
        credited = try_claim_drop(
            conn, LIVE, drop_id, GUILD, ALICE, now_ts=1500.0, booster=False
        )
        assert credited == 10
        assert get_balance(conn, GUILD, ALICE) == 10
        ledger = conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? AND user_id = ?",
            (GUILD, ALICE),
        ).fetchone()
        assert ledger["kind"] == "drop"
        assert int(ledger["amount"]) == 10
        row = _row(conn, drop_id)
        assert row["status"] == "claimed"
        assert int(row["claimed_by"]) == ALICE
        # A settled drop no longer blocks the next pouch.
        assert not has_open_drop(conn, GUILD)


def test_second_claim_loses_the_race(db):
    with open_db(db) as conn:
        drop_id = _drop(conn)
        assert try_claim_drop(
            conn, LIVE, drop_id, GUILD, ALICE, now_ts=1500.0, booster=False
        )
        assert (
            try_claim_drop(conn, LIVE, drop_id, GUILD, BOB, now_ts=1501.0, booster=False)
            is None
        )
        assert get_balance(conn, GUILD, BOB) == 0


def test_claim_fires_drop_claim_quest(db):
    # Winning the race pays the drop AND the drop_claim quest trigger; the
    # loser gets neither. Settings must be enabled in the DB — the inline
    # fire re-reads them there (the prod path).
    from bot_modules.services.economy_quests_service import (
        create_quest,
        set_quest_active,
    )
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        qid = create_quest(
            conn, GUILD, title="Catch a drop", description="", qtype="event",
            reward=7, signoff=0, criteria="", starts_at=None, ends_at=None,
            rotate_tag="", community_target=None, created_by=1,
            trigger_kind="drop_claim",
        )
        set_quest_active(conn, GUILD, qid, True)
        drop_id = _drop(conn, amount=10)
        credited = try_claim_drop(
            conn, LIVE, drop_id, GUILD, ALICE, now_ts=1500.0, booster=False
        )
        assert credited == 10
        quest_row = conn.execute(
            "SELECT amount FROM econ_ledger "
            "WHERE guild_id = ? AND user_id = ? AND kind = 'quest'",
            (GUILD, ALICE),
        ).fetchone()
        # Reward left loose: the weekly ⚡ spotlight can double it.
        assert quest_row is not None and int(quest_row["amount"]) >= 7
        # The race loser gets no quest credit either.
        assert try_claim_drop(
            conn, LIVE, drop_id, GUILD, BOB, now_ts=1501.0, booster=False
        ) is None
        assert conn.execute(
            "SELECT 1 FROM econ_ledger "
            "WHERE guild_id = ? AND user_id = ? AND kind = 'quest'",
            (GUILD, BOB),
        ).fetchone() is None


def test_claim_after_expiry_pays_nothing(db):
    with open_db(db) as conn:
        drop_id = _drop(conn, now_ts=1000.0, expire_minutes=1)
        assert (
            try_claim_drop(
                conn, LIVE, drop_id, GUILD, ALICE, now_ts=1061.0, booster=False
            )
            is None
        )
        assert get_balance(conn, GUILD, ALICE) == 0


def test_booster_multiplier_applies(db):
    with open_db(db) as conn:
        drop_id = _drop(conn, amount=10)
        credited = try_claim_drop(
            conn, LIVE, drop_id, GUILD, ALICE, now_ts=1500.0, booster=True
        )
        assert credited == 15  # ceil(10 * 1.5)
        assert get_balance(conn, GUILD, ALICE) == 15


# ── expiry sweep ──────────────────────────────────────────────────────


def test_expiry_sweep_settles_only_overdue_open_drops(db):
    with open_db(db) as conn:
        overdue = _drop(conn, now_ts=1000.0, expire_minutes=1)
        set_drop_message(conn, overdue, 424242)
        fresh = _drop(conn, now_ts=1000.0, expire_minutes=60)
        claimed = _drop(conn, now_ts=1000.0, expire_minutes=1)
        assert try_claim_drop(
            conn, LIVE, claimed, GUILD, ALICE, now_ts=1010.0, booster=False
        )
        swept = expire_due_drops(conn, now_ts=2000.0)
        # Only the overdue open drop is swept, carrying its message id for
        # the embed edit.
        assert [(int(r["id"]), int(r["message_id"])) for r in swept] == [
            (overdue, 424242)
        ]
        assert _row(conn, overdue)["status"] == "expired"
        assert _row(conn, fresh)["status"] == "open"
        # Idempotent: a second sweep finds nothing left to settle.
        assert expire_due_drops(conn, now_ts=2000.0) == []
        assert has_open_drop(conn, GUILD)  # the fresh pouch is still out