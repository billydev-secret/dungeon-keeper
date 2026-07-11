"""Tests for services/economy_service.py."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from migrations import apply_migrations_sync
from bot_modules.services.economy_service import (
    DEFAULT_ECON_SETTINGS,
    ECON_PREFIX,
    apply_credit,
    apply_debit,
    get_balance,
    get_ledger,
    get_notify_muted,
    load_econ_settings,
    save_econ_settings,
    set_notify_muted,
)

GUILD = 123
USER = 1001
OTHER = 1002


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


# ── settings ──────────────────────────────────────────────────────────


def test_defaults_when_unconfigured(db):
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings == DEFAULT_ECON_SETTINGS
    assert settings.enabled is False
    assert settings.currency_name == "Coin"


def test_save_load_roundtrip(db):
    values = {
        "enabled": True,
        "transfers_enabled": False,
        "bank_channel_id": 555,
        "manager_role_id": 777,
        "currency_name": "Gem",
        "currency_plural": "Gems",
        "currency_emoji": "💎",
        "booster_multiplier": 2.0,
        "xp_per_coin": 20.0,
        "reward_qotd": 42,
    }
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, values)
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is True
    assert settings.transfers_enabled is False
    assert settings.bank_channel_id == 555
    assert settings.manager_role_id == 777
    assert settings.currency_name == "Gem"
    assert settings.currency_plural == "Gems"
    assert settings.currency_emoji == "💎"
    assert settings.booster_multiplier == 2.0
    assert settings.xp_per_coin == 20.0
    assert settings.reward_qotd == 42
    # Untouched fields keep defaults.
    assert settings.wallet_name == DEFAULT_ECON_SETTINGS.wallet_name


def test_enabled_stays_bool_not_int(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is True
    assert isinstance(settings.enabled, bool)


def test_no_legacy_guild0_fallback(db):
    # Write legacy guild_id=0 rows directly; a real guild must ignore them.
    with open_db(db) as conn:
        set_config_value(conn, f"{ECON_PREFIX}enabled", "1", 0)
        set_config_value(conn, f"{ECON_PREFIX}currency_name", "Legacy", 0)
    with open_db(db) as conn:
        settings = load_econ_settings(conn, GUILD)
    assert settings.enabled is False
    assert settings.currency_name == "Coin"


def test_save_rejects_unknown_key(db):
    with open_db(db) as conn:
        with pytest.raises(KeyError):
            save_econ_settings(conn, GUILD, {"not_a_field": 1})


# ── wallet + ledger ───────────────────────────────────────────────────


def test_get_balance_zero_when_no_wallet(db):
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, USER) == 0


def test_credit_creates_wallet_and_ledger(db):
    with open_db(db) as conn:
        credited = apply_credit(conn, GUILD, USER, 10, "grant", actor_id=9)
        assert credited == 10
        assert get_balance(conn, GUILD, USER) == 10
        rows = get_ledger(conn, GUILD, USER)
    assert len(rows) == 1
    assert rows[0]["amount"] == 10
    assert rows[0]["kind"] == "grant"
    assert rows[0]["actor_id"] == 9


def test_credit_accumulates(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        apply_credit(conn, GUILD, USER, 5, "grant")
        assert get_balance(conn, GUILD, USER) == 15


def test_credit_meta_serialized_as_json(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 3, "grant", meta={"reason": "hi"})
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["meta"] == '{"reason": "hi"}'


def test_credit_no_meta_is_null(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 3, "grant")
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["meta"] is None


def test_credit_booster_ceil_rounding(db):
    with open_db(db) as conn:
        credited = apply_credit(
            conn, GUILD, USER, 5, "grant", booster=True, multiplier=1.5
        )
    # ceil(5 * 1.5) == ceil(7.5) == 8
    assert credited == 8


def test_credit_no_booster_ignores_multiplier(db):
    with open_db(db) as conn:
        credited = apply_credit(
            conn, GUILD, USER, 5, "grant", booster=False, multiplier=1.5
        )
    assert credited == 5


def test_credit_rejects_amount_below_one(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError):
            apply_credit(conn, GUILD, USER, 0, "grant")
        with pytest.raises(ValueError):
            apply_credit(conn, GUILD, USER, -3, "grant")
        # No writes happened.
        assert get_balance(conn, GUILD, USER) == 0
        assert get_ledger(conn, GUILD, USER) == []


def test_debit_success(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 20, "grant")
        ok = apply_debit(conn, GUILD, USER, 8, "spend")
        assert ok is True
        assert get_balance(conn, GUILD, USER) == 12
        rows = get_ledger(conn, GUILD, USER)
    assert rows[0]["amount"] == -8
    assert rows[0]["kind"] == "spend"


def test_debit_insufficient_leaves_no_writes(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 5, "grant")
        ok = apply_debit(conn, GUILD, USER, 10, "spend")
        assert ok is False
        assert get_balance(conn, GUILD, USER) == 5
        rows = get_ledger(conn, GUILD, USER)
    # Only the initial credit row exists — no debit ledger row.
    assert len(rows) == 1
    assert rows[0]["amount"] == 5


def test_debit_no_wallet_returns_false(db):
    with open_db(db) as conn:
        ok = apply_debit(conn, GUILD, USER, 1, "spend")
        assert ok is False
        assert get_ledger(conn, GUILD, USER) == []


def test_debit_exact_balance_to_zero(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 7, "grant")
        ok = apply_debit(conn, GUILD, USER, 7, "spend")
        assert ok is True
        assert get_balance(conn, GUILD, USER) == 0


def test_debit_rejects_amount_below_one(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 5, "grant")
        with pytest.raises(ValueError):
            apply_debit(conn, GUILD, USER, 0, "spend")
        assert get_balance(conn, GUILD, USER) == 5


def test_wallets_isolated_per_user(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        assert get_balance(conn, GUILD, OTHER) == 0


def test_ledger_newest_first_and_limit(db):
    with open_db(db) as conn:
        for i in range(1, 6):
            apply_credit(conn, GUILD, USER, i, "grant")
        rows = get_ledger(conn, GUILD, USER, limit=3)
    assert len(rows) == 3
    # Newest (last inserted, amount 5) first.
    assert [r["amount"] for r in rows] == [5, 4, 3]


def test_ledger_scoped_to_user(db):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10, "grant")
        apply_credit(conn, GUILD, OTHER, 20, "grant")
        rows = get_ledger(conn, GUILD, USER)
    assert len(rows) == 1
    assert rows[0]["amount"] == 10


# ── notify prefs ──────────────────────────────────────────────────────


def test_notify_muted_defaults_false(db):
    with open_db(db) as conn:
        assert get_notify_muted(conn, GUILD, USER) is False


def test_notify_muted_roundtrip(db):
    with open_db(db) as conn:
        set_notify_muted(conn, GUILD, USER, True)
        assert get_notify_muted(conn, GUILD, USER) is True
        set_notify_muted(conn, GUILD, USER, False)
        assert get_notify_muted(conn, GUILD, USER) is False
