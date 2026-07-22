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
from bot_modules.services import casino_logic as logic
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


# ── review-fix regressions (docs/reviews round, 2026-07-22) ────────────


def test_stale_precheck_cannot_strand_a_roulette_stake(db, monkeypatch):
    """The buzzer-beater race: the autocommit pre-check saw an open round
    but the settler claimed it before our debit. The in-transaction claim
    must refuse the bet — money moved for a settled round is unrecoverable."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_round(conn)
        assert svc.settle_roulette_round(conn, round_id, 3, now=NOW + 45) is not None
        stale = {
            "id": round_id, "status": "open",
            "closes_at": NOW + 45, "guild_id": GUILD,
        }
        monkeypatch.setattr(svc, "get_roulette_round", lambda *_: stale)
        err = svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 2)
        assert err == "Betting on that round has closed."
        assert get_balance(conn, GUILD, A) == 100  # nothing debited
        monkeypatch.undo()
        assert all(int(b["user_id"]) != A for b in svc.roulette_bets(conn, round_id))


def test_refunds_restore_daily_cap_headroom(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 100})
        _fund(conn, A, 200)
        _deal(conn, A, stake=80)
        day = "2027-01-15"  # NOW's local day at offset 0
        assert svc.wagered_today(conn, GUILD, A, day) == 80
        swept = svc.refund_live_blackjack_hands(conn, now=NOW)
        assert len(swept) == 1
        assert svc.wagered_today(conn, GUILD, A, day) == 0
        # the full cap is available again
        assert svc.take_stake(conn, GUILD, A, 100, "slots", now=NOW) is None


def test_void_round_restores_cap_headroom_and_clamps_at_zero(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 100})
        _fund(conn, A, 200)
        round_id = _open_round(conn)
        assert svc.place_roulette_bet(conn, round_id, A, "red", 0, 60, now=NOW + 1) is None
        day = "2027-01-15"
        assert svc.wagered_today(conn, GUILD, A, day) == 60
        assert svc.void_roulette_round(conn, round_id, now=NOW + 5) == {A: 60}
        assert svc.wagered_today(conn, GUILD, A, day) == 0
        # a refund with no counter row never goes negative
        svc.refund(conn, GUILD, A, 50, "roulette", now=NOW)
        assert svc.wagered_today(conn, GUILD, A, day) == 0


def test_member_leave_refunds_live_stakes_and_spares_the_round(db):
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _fund(conn, B, 100)
        _deal(conn, A, stake=20)
        round_id = _open_round(conn)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        svc.place_roulette_bet(conn, round_id, A, "number", 7, 15, now=NOW + 2)
        svc.place_roulette_bet(conn, round_id, B, "black", 0, 20, now=NOW + 3)

        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 4)
        assert out == {"blackjack": 20, "roulette": 25}
        assert get_balance(conn, GUILD, A) == 200  # made whole
        assert svc.live_blackjack_hand(conn, GUILD, A) is None
        # A's bets are gone so the spin can't pay a ghost; B's bet survives
        remaining = svc.roulette_bets(conn, round_id)
        assert [int(b["user_id"]) for b in remaining] == [B]
        bets = svc.settle_roulette_round(conn, round_id, 2, now=NOW + 45)  # 2 = black
        assert bets is not None and [int(b["payout"]) for b in bets] == [40]
        # a second leave call finds nothing live
        assert svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 5) == {
            "blackjack": 0, "roulette": 0,
        }


def _deal_state(conn, user_id, stake, deck, player, dealer):
    assert svc.take_stake(conn, GUILD, user_id, stake, "blackjack", now=NOW) is None
    return svc.create_blackjack_hand(
        conn, GUILD, CHAN, user_id, stake,
        svc.serialize_blackjack(deck, player, dealer), now=NOW,
    )


def test_resolve_action_hit_to_bust(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(conn, A, 20, ["5♣"], ["10♠", "9♦"], ["10♥", "7♥"])
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "hit", now=NOW)
        assert (step.err, step.outcome, step.payout) == (None, "bust", 0)
        assert get_balance(conn, GUILD, A) == 80
        assert svc.live_blackjack_hand(conn, GUILD, A) is None


def test_resolve_action_stand_win_pays_double(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(conn, A, 20, [], ["10♠", "9♦"], ["10♥", "7♥"])
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "stand", now=NOW)
        assert (step.outcome, step.payout) == ("win", 40)
        assert get_balance(conn, GUILD, A) == 120


def test_resolve_action_hit_to_21_auto_stands(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(
            conn, A, 20, ["2♣", "6♣"], ["10♠", "5♦"], ["10♥", "6♥"]
        )
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "hit", now=NOW)
        # player drew to 21, dealer drew 2 to 18 — resolved without a stand press
        assert (step.outcome, step.payout) == ("win", 40)


def test_resolve_action_plain_hit_stays_live_and_resets_idle_clock(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(
            conn, A, 20, ["2♣", "2♦"], ["5♠", "5♦"], ["10♥", "7♥"]
        )
        step = svc.resolve_blackjack_action(
            conn, GUILD, hand_id, A, "hit", now=NOW + 100
        )
        assert step.err is None and step.outcome is None
        assert step.player == ["5♠", "5♦", "2♦"]
        # the press bumped last_action_at, so the idle sweep no longer sees it
        assert svc.idle_live_blackjack_hands(conn, NOW + 50) == []


def test_resolve_action_double_derives_stake_from_the_row(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(
            conn, A, 20, ["9♥", "10♣"], ["5♠", "6♦"], ["10♥", "7♥"]
        )
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "double", now=NOW)
        assert (step.doubled, step.stake) == (True, 40)
        assert (step.outcome, step.payout) == ("win", 80)
        assert get_balance(conn, GUILD, A) == 100 - 40 + 80


def test_resolve_action_double_needs_two_cards(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(
            conn, A, 20, ["2♣"], ["5♠", "5♦", "3♣"], ["10♥", "7♥"]
        )
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "double", now=NOW)
        assert step.err == "You can only double on your first two cards."
        assert get_balance(conn, GUILD, A) == 80  # no second debit


def test_resolve_action_double_short_funds_leaves_hand_live(db):
    with open_db(db) as conn:
        _fund(conn, A, 25)
        hand_id = _deal_state(conn, A, 20, ["9♥"], ["5♠", "6♦"], ["10♥", "7♥"])
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "double", now=NOW)
        assert step.err is not None and "you have 5" in step.err
        assert svc.live_blackjack_hand(conn, GUILD, A) is not None


def test_resolve_action_owner_and_settled_guards(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(conn, A, 20, [], ["10♠", "9♦"], ["10♥", "7♥"])
        with pytest.raises(ValueError):
            svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "split", now=NOW)
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, B, "stand", now=NOW)
        assert step.err == "That's not your hand — deal your own!"
        # settle it out from under the press (the boot-sweep race)
        assert svc.settle_blackjack_hand(conn, hand_id, 20, "push", now=NOW)
        balance = get_balance(conn, GUILD, A)
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "stand", now=NOW)
        assert step.err == "That hand is already finished."
        assert get_balance(conn, GUILD, A) == balance  # nothing paid twice


def test_double_stake_refused_on_settled_hand(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn, stake=20)
        assert svc.settle_blackjack_hand(conn, hand_id, 0, "bust", now=NOW)
        err = svc.double_blackjack_stake(conn, GUILD, hand_id, A, 20, now=NOW)
        assert err == "That hand is already finished."
        assert get_balance(conn, GUILD, A) == 80  # the double debited nothing


def test_stand_idle_hand_settles_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal_state(conn, A, 20, [], ["10♠", "8♦"], ["10♥", "8♥"])
        step = svc.stand_idle_blackjack_hand(conn, hand_id, now=NOW)
        assert step is not None and (step.outcome, step.payout) == ("push", 20)
        assert svc.stand_idle_blackjack_hand(conn, hand_id, now=NOW) is None
        assert get_balance(conn, GUILD, A) == 100  # push paid exactly once


def test_stake_refuses_the_wrong_channel(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        err = svc.take_stake(
            conn, GUILD, A, 10, "slots", now=NOW, channel_id=CHAN + 99
        )
        assert err is not None and "moved" in err
        assert get_balance(conn, GUILD, A) == 100
        assert (
            svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW, channel_id=CHAN)
            is None
        )


def test_casino_kinds_economy_accounting_registrations():
    """The feed skips bet spam, the faucet mix ignores gross winnings, and
    the spenders board ignores gross turnover — the accounting decisions
    from the review, pinned."""
    from bot_modules.economy.metrics import FAUCET_GROUPS
    from bot_modules.economy.register import SKIP_KINDS
    from bot_modules.economy.stats import BURN_EXCLUDED_KINDS

    assert "casino_stake" in SKIP_KINDS
    assert "casino_payout" in SKIP_KINDS
    assert "casino_refund" not in SKIP_KINDS
    assert "casino_stake" in BURN_EXCLUDED_KINDS
    assert not any(k.startswith("casino_") for k in FAUCET_GROUPS)


# ── fancy round: jackpot + stats (stage 2) ─────────────────────────────


def test_jackpot_feeds_only_on_full_losses(db):
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        # a lost slots spin feeds 25% of the stake
        r = svc.settle_slots(conn, GUILD, A, 40, ("🌻", "🍀", "🐝"), now=NOW)
        assert r.payout == 0
        assert svc.get_jackpot(conn, GUILD) == svc.DEFAULT_CASINO_SETTINGS.jackpot_seed + 10
        # a winning spin feeds nothing
        pot = svc.get_jackpot(conn, GUILD)
        r = svc.settle_slots(conn, GUILD, A, 40, ("🌻", "🌻", "🍀"), now=NOW)
        assert r.payout == 60 and svc.get_jackpot(conn, GUILD) == pot
        # cut that floors to zero feeds nothing (3-coin stake, 25% = 0)
        svc.feed_jackpot(conn, GUILD, 3, now=NOW)
        assert svc.get_jackpot(conn, GUILD) == pot


def test_jackpot_disabled_pays_flat_and_keeps_no_pot(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_enabled": False})
        _fund(conn, A, 1_000)
        r = svc.settle_slots(conn, GUILD, A, 10, ("🌻", "🍀", "🐝"), now=NOW)
        assert r.payout == 0
        assert svc.get_jackpot(conn, GUILD) == 0  # nothing fed
        r = svc.settle_slots(conn, GUILD, A, 10, (logic.SEVEN,) * 3, now=NOW)
        assert (r.payout, r.jackpot_won) == (1200, 0)  # flat 120×, no pot claim


def test_triple_sevens_takes_pot_with_flat_floor(db):
    with open_db(db) as conn:
        _fund(conn, A, 10_000)
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 0, "max_bet": 0})
        # small pot, big bet → the flat 120× floor wins out (pot still resets)
        svc.feed_jackpot(conn, GUILD, 100, now=NOW)  # pot = seed 100 + 25
        r = svc.settle_slots(conn, GUILD, A, 10, (logic.SEVEN,) * 3, now=NOW)
        assert (r.payout, r.jackpot_won) == (1200, 1200)
        assert svc.get_jackpot(conn, GUILD) == 100  # reseeded
        # fat pot, small bet → the pot wins out
        conn.execute("UPDATE casino_jackpot SET pot = 5000 WHERE guild_id = ?", (GUILD,))
        before = get_balance(conn, GUILD, A)
        r = svc.settle_slots(conn, GUILD, A, 10, (logic.SEVEN,) * 3, now=NOW)
        assert (r.payout, r.jackpot_won) == (5000, 5000)
        assert get_balance(conn, GUILD, A) == before + 5000
        assert svc.get_jackpot(conn, GUILD) == 100


def test_claim_jackpot_is_exactly_once_per_pot(db):
    with open_db(db) as conn:
        svc.feed_jackpot(conn, GUILD, 1_000, now=NOW)  # 100 seed + 250
        assert svc.claim_jackpot(conn, GUILD, A, now=NOW) == 350
        assert svc.claim_jackpot(conn, GUILD, B, now=NOW) == 100  # just the reseed


def test_blackjack_and_roulette_losses_feed_the_pot(db):
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        hand_id = _deal(conn, stake=40)
        assert svc.settle_blackjack_hand(conn, hand_id, 0, "bust", now=NOW)
        assert svc.get_jackpot(conn, GUILD) == 110  # seed 100 + 10
        round_id = _open_round(conn)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 40, now=NOW + 1)
        assert svc.settle_roulette_round(conn, round_id, 2, now=NOW + 45) is not None
        assert svc.get_jackpot(conn, GUILD) == 120  # black landed, red fed
        # refunds never feed: a fresh hand swept at boot leaves the pot alone
        hand2 = _deal(conn, stake=40)
        assert hand2 and svc.refund_live_blackjack_hands(conn, now=NOW)
        assert svc.get_jackpot(conn, GUILD) == 120


def test_record_play_tracks_streaks_stats_and_weekly(db):
    with open_db(db) as conn:
        assert svc.record_play(conn, GUILD, A, "coinflip", 10, 19, now=NOW) == 1
        assert svc.record_play(conn, GUILD, A, "slots", 10, 0, now=NOW) == -1
        assert svc.record_play(conn, GUILD, A, "slots", 10, 0, now=NOW) == -2
        assert svc.record_play(conn, GUILD, A, "roulette", 10, 360, now=NOW) == 1
        assert svc.record_play(conn, GUILD, A, "blackjack", 10, 10, now=NOW) == 0
        row = svc.member_casino_stats(conn, GUILD, A)
        assert row is not None
        assert (int(row["plays"]), int(row["wins"])) == (5, 2)
        assert (int(row["wagered"]), int(row["returned"])) == (50, 389)
        assert int(row["biggest_win"]) == 360
        assert str(row["biggest_win_game"]) == "roulette"
        assert (int(row["streak"]), int(row["best_streak"])) == (0, 1)
        from bot_modules.economy.quests import iso_week_for
        from bot_modules.economy.logic import local_day_for
        week = iso_week_for(local_day_for(NOW, 0.0))
        biggest, luckiest = svc.weekly_table_highlights(conn, GUILD, week)
        assert biggest is not None and int(biggest["biggest_win"]) == 360
        assert luckiest is not None and int(luckiest["biggest_mult_x100"]) == 3600
        assert svc.weekly_table_highlights(conn, GUILD, "1999-W01") == (None, None)


def test_settled_games_land_in_stats_via_their_settle_paths(db):
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        svc.settle_coinflip(conn, GUILD, A, 10, "heads", "heads", now=NOW)
        hand_id = _deal(conn, stake=20)
        svc.settle_blackjack_hand(conn, hand_id, 40, "win", now=NOW)
        round_id = _open_round(conn)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        svc.settle_roulette_round(conn, round_id, 3, now=NOW + 45)
        row = svc.member_casino_stats(conn, GUILD, A)
        assert row is not None and int(row["plays"]) == 3
        assert int(row["streak"]) == 3  # three wins in a row
        # a boot-sweep refund is NOT a play
        hand2 = _deal(conn, stake=20)
        assert hand2 and svc.refund_live_blackjack_hands(conn, now=NOW)
        row = svc.member_casino_stats(conn, GUILD, A)
        assert row is not None and int(row["plays"]) == 3
