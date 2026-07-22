"""Tests for services/casino_service.py — the casino's money choke point.

The money-critical properties: the guard cascade in take_stake (economy →
casino open → table open → limits → daily cap → funds), payouts/refunds
that never mint through the booster, blackjack/roulette settlement that is
exactly-once under replays, and conservation — every stake either settles
or refunds, never both, never twice.
"""

from __future__ import annotations

import sqlite3

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import casino_service as svc
from bot_modules.services.economy_service import (
    apply_credit,
    get_balance,
    save_econ_settings,
)
from migrations import apply_migrations_sync

GUILD = 800
CHAN = 9100
A, B = 3001, 3002
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        svc.save_casino_settings(conn, GUILD, {"channel_id": CHAN})
    return path


def _fund(conn, user_id, amount):
    apply_credit(conn, GUILD, user_id, amount, "grant")


def _kinds(conn, user_id):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? "
            "AND user_id = ? ORDER BY id",
            (GUILD, user_id),
        )
    ]


# ── settings ───────────────────────────────────────────────────────────


def test_settings_default_dark(tmp_path):
    path = tmp_path / "fresh.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        s = svc.load_casino_settings(conn, GUILD)
    assert s == svc.DEFAULT_CASINO_SETTINGS
    assert s.channel_id == 0  # the master switch ships off


def test_settings_roundtrip_partial(db):
    with open_db(db) as conn:
        svc.save_casino_settings(
            conn, GUILD, {"max_bet": 250, "slots_enabled": False}
        )
        s = svc.load_casino_settings(conn, GUILD)
    assert s.max_bet == 250
    assert s.slots_enabled is False
    assert s.min_bet == svc.DEFAULT_CASINO_SETTINGS.min_bet  # untouched


def test_settings_unknown_key_raises(db):
    with open_db(db) as conn, pytest.raises(KeyError):
        svc.save_casino_settings(conn, GUILD, {"house_edge": 50})


def test_settings_garbage_int_falls_back(db):
    with open_db(db) as conn:
        set_config_value(conn, "casino_max_bet", "lots", GUILD)
        s = svc.load_casino_settings(conn, GUILD)
    assert s.max_bet == svc.DEFAULT_CASINO_SETTINGS.max_bet


# ── take_stake guard cascade ───────────────────────────────────────────


def test_stake_requires_economy_enabled(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": False})
        _fund(conn, A, 100)
        err = svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW)
        assert err is not None and "economy" in err
        assert get_balance(conn, GUILD, A) == 100


def test_stake_requires_casino_channel(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"channel_id": 0})
        _fund(conn, A, 100)
        err = svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW)
        assert err == "The casino is closed."


def test_stake_requires_table_enabled(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"blackjack_enabled": False})
        _fund(conn, A, 100)
        err = svc.take_stake(conn, GUILD, A, 10, "blackjack", now=NOW)
        assert err == "That table is closed right now."
        assert svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW) is None


def test_stake_enforces_bet_limits(db):
    with open_db(db) as conn:
        _fund(conn, A, 10_000)
        assert "Minimum" in (svc.take_stake(conn, GUILD, A, 2, "slots", now=NOW) or "")
        assert "Maximum" in (
            svc.take_stake(conn, GUILD, A, 500, "slots", now=NOW) or ""
        )
        # max_bet 0 = uncapped bets (the cap still applies, so lift it too)
        svc.save_casino_settings(conn, GUILD, {"max_bet": 0, "daily_wager_cap": 0})
        assert svc.take_stake(conn, GUILD, A, 5_000, "slots", now=NOW) is None


def test_stake_rejects_nonpositive(db):
    with open_db(db) as conn, pytest.raises(ValueError):
        svc.take_stake(conn, GUILD, A, 0, "slots", now=NOW)


def test_stake_requires_funds_and_moves_nothing_short(db):
    with open_db(db) as conn:
        _fund(conn, A, 8)
        err = svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW)
        assert err is not None and "you have 8" in err
        assert get_balance(conn, GUILD, A) == 8
        assert svc.wagered_today(conn, GUILD, A, "2027-01-15") == 0


def test_stake_ledger_row(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        assert svc.take_stake(conn, GUILD, A, 25, "coinflip", now=NOW) is None
        assert get_balance(conn, GUILD, A) == 75
        assert _kinds(conn, A)[-1] == ("casino_stake", -25)


def test_daily_cap_accumulates_and_blocks(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 50})
        _fund(conn, A, 1_000)
        assert svc.take_stake(conn, GUILD, A, 30, "slots", now=NOW) is None
        err = svc.take_stake(conn, GUILD, A, 30, "slots", now=NOW)
        assert err is not None and "20 left today" in err
        # the failed bet consumed nothing
        assert get_balance(conn, GUILD, A) == 970
        # a bet that fits still lands, reaching the cap exactly
        assert svc.take_stake(conn, GUILD, A, 20, "slots", now=NOW) is None
        # other members are untouched
        _fund(conn, B, 100)
        assert svc.take_stake(conn, GUILD, B, 50, "slots", now=NOW) is None


def test_daily_cap_resets_next_local_day(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 50})
        _fund(conn, A, 1_000)
        assert svc.take_stake(conn, GUILD, A, 50, "slots", now=NOW) is None
        assert "cap" in (svc.take_stake(conn, GUILD, A, 5, "slots", now=NOW) or "")
        assert svc.take_stake(conn, GUILD, A, 50, "slots", now=NOW + 86_400) is None


def test_daily_cap_zero_keeps_no_books(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 0})
        _fund(conn, A, 1_000)
        assert svc.take_stake(conn, GUILD, A, 50, "slots", now=NOW) is None
        assert conn.execute("SELECT COUNT(*) AS c FROM casino_daily").fetchone()["c"] == 0


def test_unlimited_flag_skips_bet_limits_not_cap_or_funds(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 60})
        _fund(conn, A, 1_000)
        # over max_bet but allowed (the double-down path)
        assert (
            svc.take_stake(
                conn, GUILD, A, 55, "blackjack", now=NOW, enforce_bet_limits=False
            )
            is None
        )
        # still capped
        err = svc.take_stake(
            conn, GUILD, A, 10, "blackjack", now=NOW, enforce_bet_limits=False
        )
        assert err is not None and "cap" in err


# ── payouts / refunds ──────────────────────────────────────────────────


def test_pay_out_and_refund_kinds_and_no_boost(db):
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"booster_multiplier": 3.0})
        svc.pay_out(conn, GUILD, A, 40, "slots", meta={"reels": "x"})
        svc.refund(conn, GUILD, A, 10, "roulette")
        assert _kinds(conn, A) == [("casino_payout", 40), ("casino_refund", 10)]
        assert get_balance(conn, GUILD, A) == 50  # never ×3


def test_zero_payout_writes_nothing(db):
    with open_db(db) as conn:
        svc.pay_out(conn, GUILD, A, 0, "slots")
        svc.refund(conn, GUILD, A, 0, "slots")
        assert _kinds(conn, A) == []


# ── blackjack hand lifecycle ───────────────────────────────────────────


def _deal(conn, user_id=A, stake=20):
    assert svc.take_stake(conn, GUILD, user_id, stake, "blackjack", now=NOW) is None
    state = svc.serialize_blackjack(["2♣"], ["A♠", "K♦"], ["9♥", "5♦"])
    return svc.create_blackjack_hand(
        conn, GUILD, CHAN, user_id, stake, state, now=NOW
    )


def test_blackjack_hand_roundtrip(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn)
        row = svc.live_blackjack_hand(conn, GUILD, A)
        assert row is not None and int(row["id"]) == hand_id
        deck, player, dealer = svc.deserialize_blackjack(str(row["state_json"]))
        assert (deck, player, dealer) == (["2♣"], ["A♠", "K♦"], ["9♥", "5♦"])


def test_one_live_hand_per_member(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _deal(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _deal(conn)


def test_double_folds_into_stake_and_books_the_cap(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 120})
        _fund(conn, A, 200)
        hand_id = _deal(conn, stake=60)
        assert svc.double_blackjack_stake(conn, GUILD, hand_id, A, 60, now=NOW) is None
        row = svc.get_blackjack_hand(conn, hand_id)
        assert row is not None
        assert (int(row["stake"]), int(row["doubled"])) == (120, 1)
        # the doubled 120 total lands on the daily books and exhausts the cap
        assert "cap" in (svc.take_stake(conn, GUILD, A, 5, "slots", now=NOW) or "")


def test_double_failure_leaves_hand_intact(db):
    with open_db(db) as conn:
        _fund(conn, A, 25)
        hand_id = _deal(conn, stake=20)  # 5 left
        err = svc.double_blackjack_stake(conn, GUILD, hand_id, A, 20, now=NOW)
        assert err is not None
        row = svc.get_blackjack_hand(conn, hand_id)
        assert row is not None
        assert (int(row["stake"]), int(row["doubled"])) == (20, 0)


def test_settle_hand_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn, stake=20)
        assert svc.settle_blackjack_hand(conn, hand_id, 40, "win", now=NOW)
        assert not svc.settle_blackjack_hand(conn, hand_id, 40, "win", now=NOW)
        assert get_balance(conn, GUILD, A) == 120  # 100 − 20 + 40, once
        assert svc.live_blackjack_hand(conn, GUILD, A) is None


def test_settle_loss_credits_nothing(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn, stake=20)
        assert svc.settle_blackjack_hand(conn, hand_id, 0, "bust", now=NOW)
        assert get_balance(conn, GUILD, A) == 80
        assert _kinds(conn, A)[-1] == ("casino_stake", -20)


def test_boot_sweep_refunds_live_hands_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        _deal(conn, A, stake=20)
        hand_b = _deal(conn, B, stake=30)
        svc.settle_blackjack_hand(conn, hand_b, 60, "win", now=NOW)  # already done
        swept = svc.refund_live_blackjack_hands(conn, now=NOW)
        assert [int(r["user_id"]) for r in swept] == [A]
        assert get_balance(conn, GUILD, A) == 100  # made whole
        assert _kinds(conn, A)[-1] == ("casino_refund", 20)
        assert svc.refund_live_blackjack_hands(conn, now=NOW) == []


def test_idle_sweep_finds_only_stale_live_hands(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn)  # last_action_at = NOW
        assert svc.idle_live_blackjack_hands(conn, NOW - 1) == []
        stale = svc.idle_live_blackjack_hands(conn, NOW + 1)
        assert [int(r["id"]) for r in stale] == [hand_id]
        svc.update_blackjack_state(conn, hand_id, "{}", now=NOW + 500)
        assert svc.idle_live_blackjack_hands(conn, NOW + 1) == []


# ── roulette rounds ────────────────────────────────────────────────────


def _open_round(conn, channel=CHAN, now=NOW):
    round_id = svc.open_roulette_round(conn, GUILD, channel, 45, now=now)
    assert round_id is not None
    return round_id


def test_one_open_round_per_channel(db):
    with open_db(db) as conn:
        _open_round(conn)
        assert svc.open_roulette_round(conn, GUILD, CHAN, 45, now=NOW) is None
        assert svc.open_roulette_round(conn, GUILD, CHAN + 1, 45, now=NOW) is not None


def test_bets_debit_and_close_with_the_window(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_round(conn)
        assert (
            svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
            is None
        )
        assert get_balance(conn, GUILD, A) == 90
        err = svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 46)
        assert err == "Betting on that round has closed."
        with pytest.raises(ValueError):
            svc.place_roulette_bet(conn, round_id, A, "corner", 0, 10, now=NOW + 2)


def test_settle_round_pays_winners_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_round(conn)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        svc.place_roulette_bet(conn, round_id, A, "number", 3, 10, now=NOW + 2)
        svc.place_roulette_bet(conn, round_id, B, "black", 0, 20, now=NOW + 3)
        svc.place_roulette_bet(conn, round_id, B, "dozen", 1, 10, now=NOW + 4)

        bets = svc.settle_roulette_round(conn, round_id, 3, now=NOW + 45)  # 3 = red
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [20, 360, 0, 30]
        assert get_balance(conn, GUILD, A) == 100 - 20 + 20 + 360
        assert get_balance(conn, GUILD, B) == 100 - 30 + 30
        # replay pays nothing again
        assert svc.settle_roulette_round(conn, round_id, 3, now=NOW + 46) is None
        assert get_balance(conn, GUILD, A) == 460
        # a settled round takes no more bets
        err = svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 2)
        assert err == "Betting on that round has closed."


def test_void_round_refunds_totals_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_round(conn)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        svc.place_roulette_bet(conn, round_id, A, "number", 7, 15, now=NOW + 2)
        assert svc.void_roulette_round(conn, round_id, now=NOW + 5) == {A: 25}
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)
        assert svc.void_roulette_round(conn, round_id, now=NOW + 6) == {}


def test_boot_sweep_lists_open_rounds(db):
    with open_db(db) as conn:
        r1 = _open_round(conn)
        r2 = _open_round(conn, channel=CHAN + 1)
        svc.settle_roulette_round(conn, r2, 0, now=NOW + 45)
        assert [int(r["id"]) for r in svc.open_roulette_rounds(conn)] == [r1]
