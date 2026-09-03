"""Tests for services/casino_service.py — the casino's money choke point.

The money-critical properties: the guard cascade in take_stake (economy →
casino open → table open → limits → daily cap → funds), payouts/refunds
that never mint through the booster, blackjack/roulette settlement that is
exactly-once under replays, and conservation — every stake either settles
or refunds, never both, never twice.
"""

from __future__ import annotations

import json
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
from tests.db_template import migrated_db

GUILD = 800
CHAN = 9100
A, B = 3001, 3002
NOW = 1_800_000_000.0


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
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
    migrated_db(path)
    with open_db(path) as conn:
        s = svc.load_casino_settings(conn, GUILD)
    assert s == svc.DEFAULT_CASINO_SETTINGS
    assert s.channel_id == 0  # the master switch ships off
    # The jackpot cut escrows coins the house would otherwise destroy, so
    # the shipped share stays small — a quarter of every loss parked a
    # fifth of the live guild's float in the pot inside one day.
    assert s.jackpot_cut_pct == 5


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


def _open_round(conn, channel=CHAN, now=NOW, user_id=0):
    round_id = svc.open_roulette_round(
        conn, GUILD, channel, 45, user_id=user_id, now=now
    )
    assert round_id is not None
    return round_id


@pytest.mark.parametrize(
    "opener",
    ["open_roulette_round", "open_race_round", "open_baccarat_round",
     "open_dice_round", "open_keno_round"],
)
def test_one_open_round_per_player(db, opener):
    """Migration 158: a round belongs to a player, not to the channel.

    Two people must be able to have their own round open at the same time
    — under the old channel-scoped index the second player was simply
    refused, which is exactly the communal constraint being removed. All
    five games ride one implementation, so this is one contract with five
    rows rather than five copies of a test.
    """
    with open_db(db) as conn:
        open_round = getattr(svc, opener)
        assert open_round(conn, GUILD, CHAN, 600, user_id=A, now=NOW) is not None
        assert open_round(conn, GUILD, CHAN, 600, user_id=A, now=NOW) is None
        assert open_round(conn, GUILD, CHAN, 600, user_id=B, now=NOW) is not None


def test_live_player_round_reads_only_the_asking_player(db):
    with open_db(db) as conn:
        mine = _open_round(conn, user_id=A)
        _open_round(conn, user_id=B)
        row = svc.live_roulette_player_round(conn, GUILD, A)
        assert row is not None and int(row["id"]) == mine
        assert svc.live_roulette_player_round(conn, GUILD, 999) is None


def test_second_round_for_a_player_is_index_proof_not_just_precheck(db):
    """The pre-check is a courtesy; the partial unique index is the rule.

    Two presses that both clear the pre-check (the race the cog catches as
    IntegrityError) must not leave a player holding two live rounds — one
    of which nothing would ever resolve or refund.
    """
    with open_db(db) as conn:
        _open_round(conn, user_id=A)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO casino_roulette_rounds "
                "(guild_id, channel_id, user_id, opened_at, closes_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD, CHAN, A, NOW, NOW + 600),
            )


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
        r1 = _open_round(conn, user_id=A)
        r2 = _open_round(conn, user_id=B)
        svc.settle_roulette_round(conn, r2, 0, now=NOW + 45)
        assert [int(r["id"]) for r in svc.open_roulette_rounds(conn)] == [r1]


def test_abandoned_round_takes_no_more_bets(db):
    """closes_at is the abandonment TTL now, and still shuts betting.

    A player who wandered off and came back after the auto-resolve window
    must not be able to bet into a round the maintenance sweep is about to
    settle out from under them.
    """
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW
        )
        assert round_id is not None
        assert (
            svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 599)
            is None
        )
        assert (
            svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 601)
            == "Betting on that round has closed."
        )
        assert get_balance(conn, GUILD, A) == 90


# ── private-round state machine ────────────────────────────────────────


def test_idle_resolve_and_a_manual_spin_settle_once_between_them(db):
    """The abandonment TTL and the player's own resolve press race whenever
    someone comes back at the last second. The status='open' claim is the
    mutual exclusion — whichever lands first pays, and the loser is a
    no-op, so the stake can never be paid twice or paid and refunded.
    """
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW
        )
        assert round_id is not None
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)

        # The player presses Spin; the sweep then reaches the same round.
        bets = svc.settle_roulette_round(conn, round_id, 3, now=NOW + 5)
        assert bets is not None
        after = get_balance(conn, GUILD, A)
        assert svc.settle_roulette_round(conn, round_id, 0, now=NOW + 601) is None
        # The public void wrapper folds "claim lost" into {} (the private
        # _void_round distinguishes it with None), so the balance is what
        # actually proves the settled stake was not also handed back.
        assert svc.void_roulette_round(conn, round_id, now=NOW + 602) == {}
        assert get_balance(conn, GUILD, A) == after


def test_an_abandoned_round_still_pays_its_winner(db):
    """Auto-resolve is a resolve, not a void: the bets were placed, so the
    wheel turning without the player watching still pays what it owes."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW
        )
        assert round_id is not None
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        bets = svc.settle_roulette_round(conn, round_id, 3, now=NOW + 601)
        assert bets is not None and [int(b["payout"]) for b in bets] == [20]
        assert get_balance(conn, GUILD, A) == 110


def test_resolving_frees_the_player_to_open_another(db):
    """The partial index only constrains 'open' rows, so finishing a round
    is what lets the next one start — the play-again loop."""
    with open_db(db) as conn:
        first = svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW
        )
        assert first is not None
        assert svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW
        ) is None
        svc.settle_roulette_round(conn, first, 3, now=NOW + 5)
        assert svc.open_roulette_round(
            conn, GUILD, CHAN, 600, user_id=A, now=NOW + 6
        ) is not None


def test_two_players_rounds_settle_independently(db):
    """The whole point of the change: one player's spin must not touch
    anyone else's round, which the old one-per-channel model made
    impossible to even express."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        ra = svc.open_roulette_round(conn, GUILD, CHAN, 600, user_id=A, now=NOW)
        rb = svc.open_roulette_round(conn, GUILD, CHAN, 600, user_id=B, now=NOW)
        assert ra is not None and rb is not None
        svc.place_roulette_bet(conn, ra, A, "red", 0, 10, now=NOW + 1)
        svc.place_roulette_bet(conn, rb, B, "black", 0, 10, now=NOW + 1)

        svc.settle_roulette_round(conn, ra, 3, now=NOW + 5)  # 3 = red, A wins
        assert get_balance(conn, GUILD, A) == 110
        assert get_balance(conn, GUILD, B) == 90  # still staked, still open
        rnd_b = svc.get_roulette_round(conn, rb)
        assert rnd_b is not None and str(rnd_b["status"]) == "open"


# ── boot refund of private rounds ──────────────────────────────────────


def test_boot_refund_hands_back_every_private_round_once(db):
    """A restart kills the ephemeral message's webhook token, so the round
    can never be shown to its player again — refund rather than resolve
    against a result nobody can see."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        ra = svc.open_roulette_round(conn, GUILD, CHAN, 600, user_id=A, now=NOW)
        rb = svc.open_keno_round(conn, GUILD, CHAN, 600, user_id=B, now=NOW)
        assert ra is not None and rb is not None
        svc.place_roulette_bet(conn, ra, A, "red", 0, 10, now=NOW + 1)
        svc.place_roulette_bet(conn, ra, A, "number", 7, 15, now=NOW + 2)
        svc.place_keno_ticket(conn, rb, B, 4, 20, now=NOW + 1)
        assert get_balance(conn, GUILD, A) == 75

        swept = svc.refund_live_rounds(conn, now=NOW + 10)
        assert swept == {"roulette": {A: 25}, "keno": {B: 20}}
        assert get_balance(conn, GUILD, A) == 100
        assert get_balance(conn, GUILD, B) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)

        # Replaying the sweep is free — the status='open' claim is gone.
        assert svc.refund_live_rounds(conn, now=NOW + 11) == {}
        assert get_balance(conn, GUILD, A) == 100


def test_boot_refund_absorbs_an_unowned_pre_migration_round(db):
    """A round the pre-158 communal cog left open belongs to nobody, so no
    player-scoped path would ever reach it. The sweep still hands the
    stake back instead of stranding it."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        orphan = _open_round(conn, user_id=0)
        svc.place_roulette_bet(conn, orphan, A, "red", 0, 30, now=NOW + 1)
        assert svc.refund_live_rounds(conn, now=NOW + 5) == {"roulette": {A: 30}}
        assert get_balance(conn, GUILD, A) == 100


def test_boot_refund_leaves_settled_rounds_and_pools_alone(db):
    """Pools is a day-long communal market with pro-rata payouts — handing
    every bettor their stake back on a restart would be wrong, so it is
    deliberately outside PRIVATE_ROUND_TABLES."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        done = _open_round(conn, user_id=A)
        svc.place_roulette_bet(conn, done, A, "red", 0, 10, now=NOW + 1)
        svc.settle_roulette_round(conn, done, 3, now=NOW + 45)
        after_settle = get_balance(conn, GUILD, A)

        pool = svc.open_pools_round(
            conn, GUILD, CHAN, "2026-08-11", 100.0, NOW + 3600, now=NOW
        )
        assert pool is not None
        assert svc.refund_live_rounds(conn, now=NOW + 50) == {}
        assert get_balance(conn, GUILD, A) == after_settle
        still_open = svc.get_pools_round(conn, pool)
        assert still_open is not None and str(still_open["status"]) == "open"


def test_boot_refund_ignores_a_round_nobody_staked(db):
    with open_db(db) as conn:
        _open_round(conn, user_id=A)
        assert svc.refund_live_rounds(conn, now=NOW + 5) == {}


# ── derby races (docs/plans/casino-derby.md) ───────────────────────────


def _open_race(conn, channel=CHAN, now=NOW, user_id=0):
    round_id = svc.open_race_round(
        conn, GUILD, channel, 60, user_id=user_id, now=now
    )
    assert round_id is not None
    return round_id


def test_race_bets_debit_and_close_with_the_window(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_race(conn)
        assert svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 1) is None
        assert get_balance(conn, GUILD, A) == 90
        err = svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 61)
        assert err == "Betting on that race has closed."
        with pytest.raises(ValueError):
            svc.place_race_bet(conn, round_id, A, 99, 10, now=NOW + 2)


def test_race_bet_refused_when_table_closed(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"derby_enabled": False})
        _fund(conn, A, 100)
        round_id = _open_race(conn)
        err = svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 1)
        assert err == "That table is closed right now."
        assert get_balance(conn, GUILD, A) == 100


def test_settle_race_pays_winners_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_race(conn)
        svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 1)  # hare 2.5×
        svc.place_race_bet(conn, round_id, A, 5, 10, now=NOW + 2)  # snail 12×
        svc.place_race_bet(conn, round_id, B, 1, 20, now=NOW + 3)  # hedgehog

        bets = svc.settle_race_round(conn, round_id, 0, now=NOW + 60)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [25, 0, 0]
        assert get_balance(conn, GUILD, A) == 100 - 20 + 25
        assert get_balance(conn, GUILD, B) == 80
        # losing stakes recorded in the stats books
        stats = svc.member_casino_stats(conn, GUILD, B)
        assert stats is not None and int(stats["plays"]) == 1
        # replay pays nothing again
        assert svc.settle_race_round(conn, round_id, 0, now=NOW + 61) is None
        assert get_balance(conn, GUILD, A) == 105
        # a settled race takes no more bets
        err = svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 2)
        assert err == "Betting on that race has closed."


def test_void_race_refunds_totals_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_race(conn)
        svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 1)
        svc.place_race_bet(conn, round_id, A, 5, 15, now=NOW + 2)
        assert svc.void_race_round(conn, round_id, now=NOW + 5) == {A: 25}
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)
        assert svc.void_race_round(conn, round_id, now=NOW + 6) == {}


def test_boot_sweep_lists_open_races(db):
    with open_db(db) as conn:
        r1 = _open_race(conn, user_id=A)
        r2 = _open_race(conn, user_id=B)
        svc.settle_race_round(conn, r2, 0, now=NOW + 60)
        assert [int(r["id"]) for r in svc.open_race_rounds(conn)] == [r1]


def test_stale_precheck_cannot_strand_a_race_stake(db, monkeypatch):
    """The roulette buzzer-beater race, on the derby: the settler claimed
    the race between our pre-check and our debit — the in-transaction
    claim must refuse the bet before any money moves."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_race(conn)
        assert svc.settle_race_round(conn, round_id, 0, now=NOW + 60) is not None
        stale = {
            "id": round_id, "status": "open",
            "closes_at": NOW + 60, "guild_id": GUILD,
        }
        monkeypatch.setattr(svc, "get_race_round", lambda *_: stale)
        err = svc.place_race_bet(conn, round_id, A, 0, 10, now=NOW + 2)
        assert err == "Betting on that race has closed."
        assert get_balance(conn, GUILD, A) == 100  # nothing debited
        monkeypatch.undo()
        assert all(int(b["user_id"]) != A for b in svc.race_bets(conn, round_id))


def test_losing_race_stakes_feed_the_jackpot(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        round_id = _open_race(conn)
        svc.place_race_bet(conn, round_id, A, 5, 40, now=NOW + 1)
        assert svc.settle_race_round(conn, round_id, 0, now=NOW + 60) is not None
        # seed (100) + 25% of the lost 40-coin stake
        assert svc.get_jackpot(conn, GUILD) == 110


# ── baccarat coups (casino-classics Stage 1a) ──────────────────────────

# Deterministic coups — settle takes the dealt hands, so no RNG to pin.
_P_WIN = (["A♠", "8♦"], ["K♠", "Q♦"])          # player 9 beats banker 0
_TIE = (["4♠", "3♦"], ["2♠", "5♦"])            # 7 all — the long shot lands
_DRAGON7 = (["2♠", "3♦"], ["A♠", "2♦", "4♣"])  # banker 3-card 7 beats 5


def _open_coup(conn, channel=CHAN, now=NOW, user_id=0):
    round_id = svc.open_baccarat_round(
        conn, GUILD, channel, 45, user_id=user_id, now=now
    )
    assert round_id is not None
    return round_id


def test_baccarat_bets_debit_and_close_with_the_window(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        assert (
            svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 1)
            is None
        )
        assert get_balance(conn, GUILD, A) == 90
        err = svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 46)
        assert err == "Betting on that hand has closed."
        with pytest.raises(ValueError):
            svc.place_baccarat_bet(conn, round_id, A, "dragon", 10, now=NOW + 2)


def test_baccarat_bet_refused_when_table_closed(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"baccarat_enabled": False})
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        err = svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 1)
        assert err == "That table is closed right now."
        assert get_balance(conn, GUILD, A) == 100


def test_settle_coup_pays_winners_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 1)
        svc.place_baccarat_bet(conn, round_id, A, "tie", 10, now=NOW + 2)
        svc.place_baccarat_bet(conn, round_id, B, "banker", 20, now=NOW + 3)

        bets = svc.settle_baccarat_round(conn, round_id, *_P_WIN, now=NOW + 45)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [20, 0, 0]
        assert get_balance(conn, GUILD, A) == 100 - 20 + 20
        assert get_balance(conn, GUILD, B) == 80
        # losing stakes recorded in the stats books
        stats = svc.member_casino_stats(conn, GUILD, B)
        assert stats is not None and int(stats["plays"]) == 1
        # the dealt coup persists as JSON for recaps
        rnd = svc.get_baccarat_round(conn, round_id)
        assert rnd is not None
        assert json.loads(str(rnd["result"])) == {
            "player": _P_WIN[0], "banker": _P_WIN[1],
        }
        # replay pays nothing again
        assert svc.settle_baccarat_round(conn, round_id, *_P_WIN, now=NOW + 46) is None
        assert get_balance(conn, GUILD, A) == 100
        # a settled coup takes no more bets
        err = svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 2)
        assert err == "Betting on that hand has closed."


def test_settle_coup_tie_pays_9x_and_pushes_the_sides(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "tie", 10, now=NOW + 1)
        svc.place_baccarat_bet(conn, round_id, B, "player", 20, now=NOW + 2)
        bets = svc.settle_baccarat_round(conn, round_id, *_TIE, now=NOW + 45)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [90, 20]
        assert get_balance(conn, GUILD, A) == 100 - 10 + 90
        assert get_balance(conn, GUILD, B) == 100  # pushed


def test_settle_coup_dragon7_pushes_banker_bets(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "banker", 30, now=NOW + 1)
        bets = svc.settle_baccarat_round(conn, round_id, *_DRAGON7, now=NOW + 45)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [30]  # barred to a push
        assert get_balance(conn, GUILD, A) == 100


def test_void_coup_refunds_totals_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 1)
        svc.place_baccarat_bet(conn, round_id, A, "tie", 15, now=NOW + 2)
        assert svc.void_baccarat_round(conn, round_id, now=NOW + 5) == {A: 25}
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)
        assert svc.void_baccarat_round(conn, round_id, now=NOW + 6) == {}


def test_boot_sweep_lists_open_coups(db):
    with open_db(db) as conn:
        r1 = _open_coup(conn, user_id=A)
        r2 = _open_coup(conn, user_id=B)
        svc.settle_baccarat_round(conn, r2, *_P_WIN, now=NOW + 45)
        assert [int(r["id"]) for r in svc.open_baccarat_rounds(conn)] == [r1]


def test_stale_precheck_cannot_strand_a_baccarat_stake(db, monkeypatch):
    """The roulette buzzer-beater race, on baccarat: the settler claimed
    the coup between our pre-check and our debit — the in-transaction
    claim must refuse the bet before any money moves."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        assert (
            svc.settle_baccarat_round(conn, round_id, *_P_WIN, now=NOW + 45)
            is not None
        )
        stale = {
            "id": round_id, "status": "open",
            "closes_at": NOW + 45, "guild_id": GUILD,
        }
        monkeypatch.setattr(svc, "get_baccarat_round", lambda *_: stale)
        err = svc.place_baccarat_bet(conn, round_id, A, "player", 10, now=NOW + 2)
        assert err == "Betting on that hand has closed."
        assert get_balance(conn, GUILD, A) == 100  # nothing debited
        monkeypatch.undo()
        assert all(int(b["user_id"]) != A for b in svc.baccarat_bets(conn, round_id))


def test_losing_baccarat_stakes_feed_the_jackpot(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "tie", 40, now=NOW + 1)
        assert (
            svc.settle_baccarat_round(conn, round_id, *_P_WIN, now=NOW + 45)
            is not None
        )
        # seed (100) + 25% of the lost 40-coin stake
        assert svc.get_jackpot(conn, GUILD) == 110


def test_member_leave_refunds_live_baccarat_stakes(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_coup(conn)
        svc.place_baccarat_bet(conn, round_id, A, "banker", 25, now=NOW + 1)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 2)
        assert out.get("baccarat") == 25
        assert get_balance(conn, GUILD, A) == 100
        assert svc.baccarat_bets(conn, round_id) == []


# ── dice rolls (casino-classics Stage 1b) ──────────────────────────────


def _open_roll(conn, channel=CHAN, now=NOW, user_id=0):
    round_id = svc.open_dice_round(
        conn, GUILD, channel, 45, user_id=user_id, now=now
    )
    assert round_id is not None
    return round_id


def test_dice_bets_debit_and_close_with_the_window(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        assert (
            svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 1)
            is None
        )
        assert get_balance(conn, GUILD, A) == 90
        err = svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 46)
        assert err == "Betting on that roll has closed."
        with pytest.raises(ValueError):
            svc.place_dice_bet(conn, round_id, A, "triple", 10, now=NOW + 2)


def test_dice_bet_refused_when_table_closed(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"dice_enabled": False})
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        err = svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 1)
        assert err == "That table is closed right now."
        assert get_balance(conn, GUILD, A) == 100


def test_settle_roll_pays_winners_exactly_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_roll(conn)
        svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 1)
        svc.place_dice_bet(conn, round_id, A, "odd", 10, now=NOW + 2)
        svc.place_dice_bet(conn, round_id, B, "small", 20, now=NOW + 3)

        bets = svc.settle_dice_round(conn, round_id, (6, 5, 4), now=NOW + 45)
        assert bets is not None  # 15: big and odd win, small loses
        assert [int(b["payout"]) for b in bets] == [20, 20, 0]
        assert get_balance(conn, GUILD, A) == 100 - 20 + 40
        assert get_balance(conn, GUILD, B) == 80
        # the roll persists as JSON for recaps
        rnd = svc.get_dice_round(conn, round_id)
        assert rnd is not None and json.loads(str(rnd["result"])) == [6, 5, 4]
        # replay pays nothing again
        assert svc.settle_dice_round(conn, round_id, (6, 5, 4), now=NOW + 46) is None
        assert get_balance(conn, GUILD, A) == 120
        # a settled roll takes no more bets
        err = svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 2)
        assert err == "Betting on that roll has closed."


def test_settle_roll_triple_sweeps_every_bet(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 1)   # 12 is big…
        svc.place_dice_bet(conn, round_id, A, "even", 10, now=NOW + 2)  # …and even
        bets = svc.settle_dice_round(conn, round_id, (4, 4, 4), now=NOW + 45)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [0, 0]  # triple beats both
        assert get_balance(conn, GUILD, A) == 80


def test_void_roll_refunds_totals_once(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 1)
        svc.place_dice_bet(conn, round_id, A, "odd", 15, now=NOW + 2)
        assert svc.void_dice_round(conn, round_id, now=NOW + 5) == {A: 25}
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)
        assert svc.void_dice_round(conn, round_id, now=NOW + 6) == {}


def test_boot_sweep_lists_open_rolls(db):
    with open_db(db) as conn:
        r1 = _open_roll(conn, user_id=A)
        r2 = _open_roll(conn, user_id=B)
        svc.settle_dice_round(conn, r2, (1, 2, 3), now=NOW + 45)
        assert [int(r["id"]) for r in svc.open_dice_rounds(conn)] == [r1]


def test_stale_precheck_cannot_strand_a_dice_stake(db, monkeypatch):
    """The roulette buzzer-beater race, on dice: the settler claimed the
    roll between our pre-check and our debit — the in-transaction claim
    must refuse the bet before any money moves."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        assert (
            svc.settle_dice_round(conn, round_id, (1, 2, 3), now=NOW + 45)
            is not None
        )
        stale = {
            "id": round_id, "status": "open",
            "closes_at": NOW + 45, "guild_id": GUILD,
        }
        monkeypatch.setattr(svc, "get_dice_round", lambda *_: stale)
        err = svc.place_dice_bet(conn, round_id, A, "big", 10, now=NOW + 2)
        assert err == "Betting on that roll has closed."
        assert get_balance(conn, GUILD, A) == 100  # nothing debited
        monkeypatch.undo()
        assert all(int(b["user_id"]) != A for b in svc.dice_bets(conn, round_id))


def test_losing_dice_stakes_feed_the_jackpot(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        svc.place_dice_bet(conn, round_id, A, "big", 40, now=NOW + 1)
        assert (
            svc.settle_dice_round(conn, round_id, (1, 2, 3), now=NOW + 45)
            is not None
        )
        # seed (100) + 25% of the lost 40-coin stake
        assert svc.get_jackpot(conn, GUILD) == 110


def test_member_leave_refunds_live_dice_stakes(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_roll(conn)
        svc.place_dice_bet(conn, round_id, A, "even", 25, now=NOW + 1)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 2)
        assert out.get("dice") == 25
        assert get_balance(conn, GUILD, A) == 100
        assert svc.dice_bets(conn, round_id) == []


# ── keno draws (casino-classics Stage 1d) ──────────────────────────────


def _open_draw(conn, channel=CHAN, now=NOW, user_id=0):
    round_id = svc.open_keno_round(
        conn, GUILD, channel, 45, user_id=user_id, now=now
    )
    assert round_id is not None
    return round_id


def _fixed_picks(monkeypatch, picks):
    """Pin the quick-pick (random.sample) to a fixed ticket."""
    monkeypatch.setattr(logic.random, "sample", lambda pop, k: list(picks)[:k])


def test_keno_tickets_quick_pick_debit_and_close(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [4, 12, 33, 41, 56, 78])
        picks = svc.place_keno_ticket(conn, round_id, A, 6, 10, now=NOW + 1)
        assert picks == [4, 12, 33, 41, 56, 78]  # returned for the confirmation
        assert get_balance(conn, GUILD, A) == 90
        err = svc.place_keno_ticket(conn, round_id, A, 6, 10, now=NOW + 46)
        assert err == "Tickets for that draw have closed."
        with pytest.raises(ValueError):
            svc.place_keno_ticket(conn, round_id, A, 5, 10, now=NOW + 2)


def test_keno_ticket_refused_when_table_closed(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"keno_enabled": False})
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        err = svc.place_keno_ticket(conn, round_id, A, 6, 10, now=NOW + 1)
        assert err == "That table is closed right now."
        assert get_balance(conn, GUILD, A) == 100


def test_settle_draw_pays_by_catches_exactly_once(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [1, 2, 3, 4])       # will catch 4/4 → 60×
        svc.place_keno_ticket(conn, round_id, A, 4, 10, now=NOW + 1)
        _fixed_picks(monkeypatch, [61, 62, 63, 64])   # 0 catches
        svc.place_keno_ticket(conn, round_id, B, 4, 20, now=NOW + 2)

        drawn = list(range(1, 21))
        bets = svc.settle_keno_round(conn, round_id, drawn, now=NOW + 45)
        assert bets is not None
        assert [int(b["payout"]) for b in bets] == [600, 0]
        assert get_balance(conn, GUILD, A) == 100 - 10 + 600
        assert get_balance(conn, GUILD, B) == 80
        # the draw persists as JSON for recaps
        rnd = svc.get_keno_round(conn, round_id)
        assert rnd is not None and json.loads(str(rnd["result"])) == drawn
        # replay pays nothing again
        assert svc.settle_keno_round(conn, round_id, drawn, now=NOW + 46) is None
        assert get_balance(conn, GUILD, A) == 690
        # a settled draw takes no more tickets
        err = svc.place_keno_ticket(conn, round_id, A, 4, 10, now=NOW + 2)
        assert err == "Tickets for that draw have closed."


def test_void_draw_refunds_totals_once(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [1, 2, 3, 4])
        svc.place_keno_ticket(conn, round_id, A, 4, 10, now=NOW + 1)
        svc.place_keno_ticket(conn, round_id, A, 4, 15, now=NOW + 2)
        assert svc.void_keno_round(conn, round_id, now=NOW + 5) == {A: 25}
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 25)
        assert svc.void_keno_round(conn, round_id, now=NOW + 6) == {}


def test_boot_sweep_lists_open_draws(db):
    with open_db(db) as conn:
        r1 = _open_draw(conn, user_id=A)
        r2 = _open_draw(conn, user_id=B)
        svc.settle_keno_round(conn, r2, list(range(1, 21)), now=NOW + 45)
        assert [int(r["id"]) for r in svc.open_keno_rounds(conn)] == [r1]


def test_stale_precheck_cannot_strand_a_keno_stake(db, monkeypatch):
    """The roulette buzzer-beater race, on keno: the settler claimed the
    draw between our pre-check and our debit — the in-transaction claim
    must refuse the ticket before any money moves."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        assert (
            svc.settle_keno_round(conn, round_id, list(range(1, 21)), now=NOW + 45)
            is not None
        )
        stale = {
            "id": round_id, "status": "open",
            "closes_at": NOW + 45, "guild_id": GUILD,
        }
        monkeypatch.setattr(svc, "get_keno_round", lambda *_: stale)
        err = svc.place_keno_ticket(conn, round_id, A, 4, 10, now=NOW + 2)
        assert err == "Tickets for that draw have closed."
        assert get_balance(conn, GUILD, A) == 100  # nothing debited
        monkeypatch.undo()
        assert all(int(b["user_id"]) != A for b in svc.keno_bets(conn, round_id))


def test_losing_keno_tickets_feed_the_jackpot(db, monkeypatch):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [61, 62, 63, 64])
        svc.place_keno_ticket(conn, round_id, A, 4, 40, now=NOW + 1)
        assert (
            svc.settle_keno_round(conn, round_id, list(range(1, 21)), now=NOW + 45)
            is not None
        )
        # seed (100) + 25% of the lost 40-coin ticket
        assert svc.get_jackpot(conn, GUILD) == 110


def test_member_leave_refunds_live_keno_tickets(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [1, 2, 3, 4])
        svc.place_keno_ticket(conn, round_id, A, 4, 25, now=NOW + 1)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 2)
        assert out.get("keno") == 25
        assert get_balance(conn, GUILD, A) == 100
        assert svc.keno_bets(conn, round_id) == []


class _RacingConn:
    """Wraps a connection; fires ``trigger`` once, just before the first
    DELETE against ``table`` — a cross-connection interleave (a settle
    landing between the leaver sweep's read and its delete) reproduced
    deterministically on one connection."""

    def __init__(self, conn, table, trigger):
        self._conn = conn
        self._table = table
        self._trigger = trigger
        self._fired = False

    def _maybe_fire(self, sql):
        if (
            not self._fired
            and sql.lstrip().upper().startswith("DELETE")
            and self._table in sql
        ):
            self._fired = True
            self._trigger(self._conn)

    def execute(self, sql, *args):
        self._maybe_fire(sql)
        return self._conn.execute(sql, *args)

    def executemany(self, sql, *args):
        self._maybe_fire(sql)
        return self._conn.executemany(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_leaver_sweep_cannot_refund_a_bet_the_settle_already_paid(db, monkeypatch):
    """The buzzer-beater race on the leaver sweep: a settle claiming the
    round between the sweep's read and its delete must win — a bet pays
    OR refunds, never both. The sweep's DELETE carries the status='open'
    claim, so a just-settled round leaves it nothing to remove."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        round_id = _open_draw(conn)
        _fixed_picks(monkeypatch, [1, 2, 3, 4])
        svc.place_keno_ticket(conn, round_id, A, 4, 10, now=NOW + 1)  # 4/4 → 60×

        def _settle(raw):
            assert (
                svc.settle_keno_round(raw, round_id, list(range(1, 21)), now=NOW + 2)
                is not None
            )

        racing = _RacingConn(conn, "casino_keno_bets", _settle)
        out = svc.refund_member_live_stakes(racing, GUILD, A, now=NOW + 3)
        assert out == {}  # the settle won the race — nothing left to refund
        assert get_balance(conn, GUILD, A) == 100 - 10 + 600  # paid exactly once
        assert _kinds(conn, A)[-1] == ("casino_payout", 600)  # no trailing refund


# ── casino war (casino-classics Stage 1c) ──────────────────────────────


def _war_shoe(monkeypatch, ranks):
    """Feed draw_war_cards an exact rank sequence (suits pinned to ♠)."""
    queue = list(ranks)
    monkeypatch.setattr(
        logic.random,
        "choice",
        lambda seq: queue.pop(0) if seq is logic._RANKS else "♠",
    )


def test_play_war_high_card_settles_instantly(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["K", "5"])
        step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
        assert step.err is None and step.outcome == "win"
        assert (step.player, step.dealer) == ("K♠", "5♠")
        assert step.payout == 20 and step.streak == 1
        assert get_balance(conn, GUILD, A) == 110
        assert svc.live_war_hand(conn, GUILD, A) is None  # no row for a clean win
        # instant games land on the floor ticker
        assert any(
            str(r["game"]) == "war" for r in svc.recent_ticker(conn, GUILD)
        )


def test_play_war_loss_feeds_the_jackpot(db, monkeypatch):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["2", "A"])
        step = svc.play_war(conn, GUILD, CHAN, A, 40, now=NOW)
        assert step.outcome == "lose" and step.payout == 0
        assert step.pot_after == 110  # seed 100 + 25% of 40
        assert get_balance(conn, GUILD, A) == 60


def test_play_war_tie_opens_a_live_decision(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
        assert step.err is None and step.outcome is None
        assert step.hand_id > 0
        assert get_balance(conn, GUILD, A) == 90  # staked, not settled
        # one live decision per member
        again = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW + 1)
        assert again.err == (
            "You already have a war decision pending — finish it first."
        )


def test_war_raise_win_and_second_tie_pay_3x_original(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        for war_ranks, expect_payout in ((["9", "5"], 30), (["6", "6"], 30)):
            _war_shoe(monkeypatch, ["7", "7"])
            step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
            before = get_balance(conn, GUILD, A)
            _war_shoe(monkeypatch, war_ranks)
            done = svc.resolve_war_action(
                conn, GUILD, step.hand_id, A, "war", now=NOW + 1
            )
            assert done.err is None and done.outcome == "war_win"
            assert done.stake == 20 and done.payout == expect_payout
            assert get_balance(conn, GUILD, A) == before - 10 + expect_payout
            # replayed action reports finished, pays nothing again
            replay = svc.resolve_war_action(
                conn, GUILD, step.hand_id, A, "war", now=NOW + 2
            )
            assert replay.err == "That hand is already finished."


def test_war_raise_loss_feeds_the_doubled_stake(db, monkeypatch):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 20, now=NOW)
        _war_shoe(monkeypatch, ["2", "K"])
        done = svc.resolve_war_action(
            conn, GUILD, step.hand_id, A, "war", now=NOW + 1
        )
        assert done.outcome == "war_lose" and done.payout == 0
        assert done.stake == 40
        assert get_balance(conn, GUILD, A) == 60
        assert svc.get_jackpot(conn, GUILD) == 110  # seed + 25% of all 40


def test_war_retreat_returns_half_floored(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 11, now=NOW)
        done = svc.resolve_war_action(
            conn, GUILD, step.hand_id, A, "retreat", now=NOW + 1
        )
        assert done.outcome == "retreat" and done.payout == 5
        assert get_balance(conn, GUILD, A) == 100 - 11 + 5
        stats = svc.member_casino_stats(conn, GUILD, A)
        assert stats is not None and int(stats["streak"]) == -1  # a loss


def test_war_decision_owner_and_action_guards(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
        poked = svc.resolve_war_action(
            conn, GUILD, step.hand_id, B, "war", now=NOW + 1
        )
        assert poked.err == "That's not your battle — play your own!"
        with pytest.raises(ValueError):
            svc.resolve_war_action(
                conn, GUILD, step.hand_id, A, "flee", now=NOW + 2
            )


def test_war_raise_needs_funds_and_leaves_the_hand_live(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 10)  # exactly the opening stake
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
        broke = svc.resolve_war_action(
            conn, GUILD, step.hand_id, A, "war", now=NOW + 1
        )
        assert broke.err is not None and "You need" in broke.err
        assert svc.live_war_hand(conn, GUILD, A) is not None  # still decidable
        done = svc.resolve_war_action(
            conn, GUILD, step.hand_id, A, "retreat", now=NOW + 2
        )
        assert done.outcome == "retreat" and done.payout == 5


def test_idle_war_hand_defaults_to_war_with_retreat_fallback(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 10, now=NOW)
        assert [int(r["id"]) for r in svc.idle_live_war_hands(conn, NOW + 1)] == [
            step.hand_id
        ]
        _war_shoe(monkeypatch, ["A", "2"])
        done = svc.resolve_idle_war_hand(conn, step.hand_id, now=NOW + 200)
        assert done is not None and done.outcome == "war_win"
        assert svc.resolve_idle_war_hand(conn, step.hand_id, now=NOW + 201) is None

        # broke member: the sweep retreats instead of erroring out
        _fund(conn, B, 10)
        _war_shoe(monkeypatch, ["4", "4"])
        tie = svc.play_war(conn, GUILD, CHAN, B, 10, now=NOW)
        idle = svc.resolve_idle_war_hand(conn, tie.hand_id, now=NOW + 200)
        assert idle is not None and idle.outcome == "retreat"
        assert get_balance(conn, GUILD, B) == 5


def test_boot_sweep_refunds_live_war_decisions(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        step = svc.play_war(conn, GUILD, CHAN, A, 30, now=NOW)
        swept = svc.refund_live_war_hands(conn, now=NOW + 1)
        assert [int(r["id"]) for r in swept] == [step.hand_id]
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 30)
        # a refund is not a play and never feeds the pot
        assert svc.member_casino_stats(conn, GUILD, A) is None
        assert svc.refund_live_war_hands(conn, now=NOW + 2) == []


def test_member_leave_refunds_live_war_decision(db, monkeypatch):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        _war_shoe(monkeypatch, ["7", "7"])
        svc.play_war(conn, GUILD, CHAN, A, 25, now=NOW)
        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 1)
        assert out.get("war") == 25
        assert get_balance(conn, GUILD, A) == 100


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
        race_id = _open_race(conn)
        svc.place_race_bet(conn, race_id, A, 0, 12, now=NOW + 3)
        svc.place_race_bet(conn, race_id, B, 1, 8, now=NOW + 4)

        out = svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 4)
        assert out == {"blackjack": 20, "roulette": 25, "derby": 12}
        assert get_balance(conn, GUILD, A) == 200  # made whole
        assert svc.live_blackjack_hand(conn, GUILD, A) is None
        # A's bets are gone so the spin can't pay a ghost; B's bets survive
        remaining = svc.roulette_bets(conn, round_id)
        assert [int(b["user_id"]) for b in remaining] == [B]
        assert [int(b["user_id"]) for b in svc.race_bets(conn, race_id)] == [B]
        bets = svc.settle_roulette_round(conn, round_id, 2, now=NOW + 45)  # 2 = black
        assert bets is not None and [int(b["payout"]) for b in bets] == [40]
        # a second leave call finds nothing live
        assert svc.refund_member_live_stakes(conn, GUILD, A, now=NOW + 5) == {}


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
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 1_000)
        # a lost slots spin feeds 25% of the stake
        r = svc.settle_slots(conn, GUILD, A, 40, ("🌻", "🍀", "🐝"), now=NOW)
        assert r.payout == 0
        assert svc.get_jackpot(conn, GUILD) == svc.DEFAULT_CASINO_SETTINGS.jackpot_seed + 10
        # a winning spin feeds nothing
        pot = svc.get_jackpot(conn, GUILD)
        r = svc.settle_slots(conn, GUILD, A, 40, ("🌻", "🌻", "🍀"), now=NOW)
        # a pair on 40 pays 40 * 29 // 20 = 58
        assert r.payout == 58 and svc.get_jackpot(conn, GUILD) == pot
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
        svc.feed_jackpot(conn, GUILD, 100, now=NOW)  # pot = seed 100 + the cut
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
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        svc.feed_jackpot(conn, GUILD, 1_000, now=NOW)  # 100 seed + 250
        assert svc.claim_jackpot(conn, GUILD, A, now=NOW) == 350
        assert svc.claim_jackpot(conn, GUILD, B, now=NOW) == 100  # just the reseed


def test_blackjack_and_roulette_losses_feed_the_pot(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
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


def test_daily_standings_names_biggest_winner_and_loser(db):
    with open_db(db) as conn:
        svc.record_play(conn, GUILD, A, "coinflip", 10, 19, now=NOW)  # net +9
        svc.record_play(conn, GUILD, B, "slots", 20, 0, now=NOW)      # net −20
        earner, loser = svc.daily_standings(conn, GUILD, now=NOW)
        assert earner == svc.DailyStanding(A, 9)
        assert loser == svc.DailyStanding(B, -20)


def test_daily_standings_accumulate_a_members_plays_across_the_day(db):
    with open_db(db) as conn:
        svc.record_play(conn, GUILD, A, "slots", 10, 0, now=NOW)       # −10
        svc.record_play(conn, GUILD, A, "coinflip", 10, 30, now=NOW)   # +20 ⇒ A net +10
        svc.record_play(conn, GUILD, B, "slots", 50, 0, now=NOW)       # −50
        earner, loser = svc.daily_standings(conn, GUILD, now=NOW)
        assert earner == svc.DailyStanding(A, 10)
        assert loser == svc.DailyStanding(B, -50)


def test_daily_standings_no_loser_when_everyone_is_up(db):
    with open_db(db) as conn:
        svc.record_play(conn, GUILD, A, "coinflip", 10, 19, now=NOW)   # +9
        svc.record_play(conn, GUILD, B, "roulette", 10, 30, now=NOW)   # +20
        earner, loser = svc.daily_standings(conn, GUILD, now=NOW)
        assert earner == svc.DailyStanding(B, 20)
        assert loser is None


def test_daily_standings_no_earner_when_everyone_is_down(db):
    with open_db(db) as conn:
        svc.record_play(conn, GUILD, A, "slots", 10, 0, now=NOW)       # −10
        svc.record_play(conn, GUILD, B, "slots", 20, 0, now=NOW)       # −20
        earner, loser = svc.daily_standings(conn, GUILD, now=NOW)
        assert earner is None
        assert loser == svc.DailyStanding(B, -20)


def test_daily_standings_ignore_break_even_players(db):
    with open_db(db) as conn:
        svc.record_play(conn, GUILD, A, "blackjack", 10, 10, now=NOW)  # push, net 0
        assert svc.daily_standings(conn, GUILD, now=NOW) == (None, None)


def test_daily_standings_reset_at_the_day_boundary(db):
    with open_db(db) as conn:
        # Yesterday's blowout must not colour today's board.
        svc.record_play(conn, GUILD, B, "slots", 10, 200, now=NOW - 86_400)
        svc.record_play(conn, GUILD, A, "coinflip", 10, 19, now=NOW)
        earner, loser = svc.daily_standings(conn, GUILD, now=NOW)
        assert earner == svc.DailyStanding(A, 9)
        assert loser is None
        # A day nobody has played yet shows an empty board.
        assert svc.daily_standings(conn, GUILD, now=NOW + 86_400) == (None, None)


def test_daily_standings_bucket_by_guild_local_day_not_utc(db):
    # A −10h guild: a win at 05:00 UTC belongs to the *previous* local day
    # (19:00), so a same-UTC-day read at 20:00 UTC (10:00 local) must not
    # see it — the same guild-local boundary the wager cap uses.
    with open_db(db) as conn:
        set_config_value(conn, "tz_offset_hours", "-10", GUILD)
        early = 1_799_989_200.0  # 05:00 UTC Jan 15 → 19:00 Jan 14 local
        late = 1_800_043_200.0   # 20:00 UTC Jan 15 → 10:00 Jan 15 local
        svc.record_play(conn, GUILD, A, "slots", 10, 100, now=early)   # +90, prev local day
        svc.record_play(conn, GUILD, B, "coinflip", 10, 19, now=late)  # +9, this local day
        earner, loser = svc.daily_standings(conn, GUILD, now=late)
        assert earner == svc.DailyStanding(B, 9)  # A's cross-midnight win is excluded
        assert loser is None


def test_daily_standings_ignore_refunded_bets(db):
    # record_play is the only writer; refunds/voids never reach it, so a
    # handed-back stake leaves the board untouched.
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _deal(conn, A, stake=80)  # stake debited, hand still live
        assert svc.daily_standings(conn, GUILD, now=NOW) == (None, None)
        assert svc.refund_live_blackjack_hands(conn, now=NOW)  # boot sweep
        assert svc.daily_standings(conn, GUILD, now=NOW) == (None, None)


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


# ── UX round: cap visibility + honeypot lines ──────────────────────────


def test_cap_error_names_the_reset_time(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 50})
        _fund(conn, A, 1_000)
        assert svc.take_stake(conn, GUILD, A, 50, "slots", now=NOW) is None
        err = svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW)
        assert err is not None and "resets <t:" in err


def test_daily_cap_status_reports_used_cap_and_reset(db):
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        used, cap, reset_ts = svc.daily_cap_status(conn, GUILD, A, now=NOW)
        assert (used, cap) == (0, svc.DEFAULT_CASINO_SETTINGS.daily_wager_cap)
        assert reset_ts > NOW
        assert svc.take_stake(conn, GUILD, A, 30, "slots", now=NOW) is None
        used, _, _ = svc.daily_cap_status(conn, GUILD, A, now=NOW)
        assert used == 30


def test_instant_results_carry_the_pot_on_losses_only(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 1_000)
        lost = svc.settle_slots(conn, GUILD, A, 40, ("🌻", "🍀", "🐝"), now=NOW)
        assert lost.fed == 10 and lost.pot_after == 110  # seed 100 + 10
        won = svc.settle_coinflip(conn, GUILD, A, 10, "heads", "heads", now=NOW)
        assert (won.fed, won.pot_after) == (0, 0)
        svc.save_casino_settings(conn, GUILD, {"jackpot_enabled": False})
        off = svc.settle_coinflip(conn, GUILD, A, 10, "heads", "tails", now=NOW)
        assert (off.fed, off.pot_after) == (0, 0)


def test_blackjack_step_carries_pot_on_a_loss(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 25})
        _fund(conn, A, 1_000)
        hand_id = _deal_state(conn, A, 40, [], ["10♠", "7♦"], ["10♥", "8♥"])
        step = svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "stand", now=NOW)
        assert (step.outcome, step.payout) == ("lose", 0)
        assert step.pot_after == 110  # seed 100 + 25% of 40
        hand2 = _deal_state(conn, A, 40, [], ["10♠", "9♦"], ["10♥", "8♥"])
        step = svc.resolve_blackjack_action(conn, GUILD, hand2, A, "stand", now=NOW)
        assert (step.outcome, step.payout) == ("win", 80)
        assert step.pot_after == 0
def test_concurrent_bets_cannot_overshoot_the_daily_cap(db, monkeypatch):
    """Simultaneous bets must not jointly pass the day's wager cap.

    wagered_today is read in autocommit, so without the cap enforced inside
    the casino_daily upsert both bets clear the check and the only spend
    limit the casino has is bypassable by betting from two places at once.
    """
    real_debit = svc.apply_debit
    fired: list[bool] = []

    def racing_debit(conn, *args, **kwargs):
        if not fired:
            fired.append(True)
            svc.take_stake(conn, GUILD, A, 30, "slots", now=NOW)
        return real_debit(conn, *args, **kwargs)

    monkeypatch.setattr(svc, "apply_debit", racing_debit)

    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 50})
        _fund(conn, A, 1_000)

        svc.take_stake(conn, GUILD, A, 30, "slots", now=NOW)

        assert fired, "the racing bet never fired — test is not exercising the race"
        booked = conn.execute(
            "SELECT wagered FROM casino_daily WHERE guild_id = ? AND user_id = ?",
            (GUILD, A),
        ).fetchone()["wagered"]
        assert booked <= 50, f"daily cap overshot: {booked} booked against a cap of 50"


def test_unaffordable_bet_does_not_eat_the_daily_cap(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"daily_wager_cap": 100})
        _fund(conn, A, 10)
        assert svc.take_stake(conn, GUILD, A, 40, "slots", now=NOW) is not None
        # Refused for funds — the day's allowance must be untouched.
        assert svc.wagered_today(
            conn, GUILD, A, svc.local_day_for(NOW, 0)
        ) == 0


def test_stranger_pressing_hit_cannot_reset_the_idle_clock(db):
    """The hand's buttons sit on a public message anyone can press.

    Bumping last_action_at before checking ownership let a stranger hold the
    auto-stand off forever, stranding the owner's stake in a hand they had
    walked away from — and locking them out of dealing another.
    """
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn)
        before = conn.execute(
            "SELECT last_action_at FROM casino_blackjack_hands WHERE id = ?", (hand_id,)
        ).fetchone()["last_action_at"]

        step = svc.resolve_blackjack_action(
            conn, GUILD, hand_id, B, "hit", now=NOW + 120
        )
        assert step.err is not None and "not your hand" in step.err

        after = conn.execute(
            "SELECT last_action_at FROM casino_blackjack_hands WHERE id = ?", (hand_id,)
        ).fetchone()["last_action_at"]
        assert after == before, "a stranger's click reset the owner's idle clock"


def test_owner_pressing_hit_still_resets_the_idle_clock(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        hand_id = _deal(conn)
        svc.resolve_blackjack_action(conn, GUILD, hand_id, A, "hit", now=NOW + 120)
        after = conn.execute(
            "SELECT last_action_at FROM casino_blackjack_hands WHERE id = ?", (hand_id,)
        ).fetchone()["last_action_at"]
        assert after == NOW + 120


# ── the floor ticker (hub panel "On the Floor" section) ────────────────


def test_ticker_rows_land_via_instant_settle_paths(db):
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        svc.settle_coinflip(conn, GUILD, A, 10, "heads", "heads", now=NOW)
        svc.settle_slots(conn, GUILD, A, 20, ("🌻", "🍀", "🐝"), now=NOW + 1)
        hand_id = _deal(conn, stake=30)
        svc.settle_blackjack_hand(conn, hand_id, 60, "win", now=NOW + 2)
        rows = svc.recent_ticker(conn, GUILD)
    # newest first, one row per resolved play
    assert [
        (int(r["user_id"]), str(r["game"]), int(r["stake"]), int(r["payout"]))
        for r in rows
    ] == [
        (A, "blackjack", 30, 60),
        (A, "slots", 20, 0),
        (A, "coinflip", 10, 18),  # 10 * 37 // 20
    ]


def test_ticker_now_carries_the_private_round_games(db):
    """The five windowed games used to stay off the ticker because their
    public recap was their visibility. Private rounds have no recap, so
    the ticker is the only place the channel sees them at all — leaving
    them off would make them invisible rather than merely quiet."""
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        round_id = _open_round(conn, user_id=A)
        svc.place_roulette_bet(conn, round_id, A, "red", 0, 10, now=NOW + 1)
        svc.settle_roulette_round(conn, round_id, 3, now=NOW + 45)  # 3 = red
        assert [
            (str(r["game"]), int(r["stake"]), int(r["payout"]))
            for r in svc.recent_ticker(conn, GUILD)
        ] == [("roulette", 10, 20)]


def test_ticker_skips_refunds(db):
    """A bet the house handed back is not a play, so a boot-sweep refund
    can never be floor news."""
    with open_db(db) as conn:
        _fund(conn, A, 1_000)
        hand_id = _deal(conn, stake=20)
        assert hand_id and svc.refund_live_blackjack_hands(conn, now=NOW)
        assert svc.recent_ticker(conn, GUILD) == []


def test_ticker_still_skips_pools(db):
    """Pools keeps its own daily-market panel and settles there, so it
    stays off the hub ticker even though the five games joined."""
    assert "pools" not in svc.TICKER_GAMES


def test_ticker_trims_to_keep_and_respects_limit(db):
    with open_db(db) as conn:
        for i in range(svc.TICKER_KEEP + 7):
            svc.record_ticker(
                conn, GUILD, A, "slots", 5, i, now=NOW + i
            )
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM casino_ticker WHERE guild_id = ?",
            (GUILD,),
        ).fetchone()["n"]
        assert int(total) == svc.TICKER_KEEP
        rows = svc.recent_ticker(conn, GUILD, limit=3)
    assert [int(r["payout"]) for r in rows] == [
        svc.TICKER_KEEP + 6, svc.TICKER_KEEP + 5, svc.TICKER_KEEP + 4,
    ]


def test_ticker_is_per_guild(db):
    other = GUILD + 1
    with open_db(db) as conn:
        svc.record_ticker(conn, GUILD, A, "slots", 5, 0, now=NOW)
        svc.record_ticker(conn, other, B, "coinflip", 7, 13, now=NOW)
        assert [int(r["user_id"]) for r in svc.recent_ticker(conn, GUILD)] == [A]
        assert [int(r["user_id"]) for r in svc.recent_ticker(conn, other)] == [B]


def test_broadcast_min_payout_defaults_off_and_roundtrips(db):
    with open_db(db) as conn:
        assert svc.load_casino_settings(conn, GUILD).broadcast_min_payout == 0
        svc.save_casino_settings(conn, GUILD, {"broadcast_min_payout": 500})
        assert svc.load_casino_settings(conn, GUILD).broadcast_min_payout == 500


def test_broadcast_ping_defaults_on_and_roundtrips(db):
    """Default on: the @here is the behaviour every guild already had, so a
    guild that never touches the dial keeps it."""
    with open_db(db) as conn:
        assert svc.load_casino_settings(conn, GUILD).broadcast_ping_enabled is True
        svc.save_casino_settings(conn, GUILD, {"broadcast_ping_enabled": False})
        assert svc.load_casino_settings(conn, GUILD).broadcast_ping_enabled is False
        svc.save_casino_settings(conn, GUILD, {"broadcast_ping_enabled": True})
        assert svc.load_casino_settings(conn, GUILD).broadcast_ping_enabled is True


# ── win history (the broadcast's top-3% percentile, migration 162) ─────


def _bank_wins(conn, payouts, guild=GUILD):
    for payout in payouts:
        svc.record_win(conn, guild, payout, now=NOW)


def test_win_percentile_refuses_a_sample_under_the_floor(db):
    """The guard that stops a fresh guild @here-ing its very first win: with
    a thin window the answer is a refusal, never a number the caller could
    read as "everything qualifies"."""
    with open_db(db) as conn:
        _bank_wins(conn, [10_000] * (svc.PING_MIN_SAMPLE - 1))
        assert svc.win_percentile(conn, GUILD) is None
        svc.record_win(conn, GUILD, 10_000, now=NOW)
        assert svc.win_percentile(conn, GUILD) == 10_000


def test_win_percentile_marks_the_top_three_percent(db):
    """1..200 banked: the top 3% is the largest six, so the mark lands where
    exactly those clear it and the 194th does not."""
    with open_db(db) as conn:
        _bank_wins(conn, range(1, 201))
        mark = svc.win_percentile(conn, GUILD)
        assert mark == 195
        over = [p for p in range(1, 201) if p >= mark]
        assert len(over) == 6 == round(200 * 0.03)


def test_win_percentile_is_scoped_per_guild(db):
    """Two live guilds run economies ~8× apart (memory: guild "nut"), so a
    shared bar would ping one constantly and never the other."""
    other = GUILD + 1
    with open_db(db) as conn:
        _bank_wins(conn, range(1, 201))
        _bank_wins(conn, range(1000, 1200), guild=other)
        assert svc.win_percentile(conn, GUILD) == 195
        assert svc.win_percentile(conn, other) == 1194


def test_win_history_trims_to_the_window_and_keeps_the_newest(db):
    """A rolling window, not an all-time archive — an old economy's payouts
    must age out or the percentile calcifies around them."""
    with open_db(db) as conn:
        _bank_wins(conn, [1] * svc.WIN_HISTORY_KEEP)
        _bank_wins(conn, [9_000] * 10)
        rows = conn.execute(
            "SELECT payout FROM casino_win_history WHERE guild_id = ?",
            (GUILD,),
        ).fetchall()
        assert len(rows) == svc.WIN_HISTORY_KEEP
        assert sum(1 for r in rows if int(r["payout"]) == 9_000) == 10


def test_win_history_stores_no_user_id(db):
    """Migration 162's whole privacy claim, pinned: this table is outside
    personal data, so it carries no column naming a member. A future column
    added here needs a data_register.md row and a purge decision."""
    with open_db(db) as conn:
        svc.record_win(conn, GUILD, 1000, now=NOW)
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(casino_win_history)")
        }
    assert cols == {"id", "guild_id", "payout", "ts"}


def test_record_play_no_longer_banks_win_history(db):
    """Banking moved to the cog's broadcast seam. Inside the settle
    transaction it counted a five-bet roulette round five times for one card,
    banked jackpot spins whose big-win card is suppressed, and committed the
    current win into the population it was about to be ranked against."""
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"broadcast_min_payout": 500})
        svc.record_play(conn, GUILD, A, "slots", 100, 900, now=NOW)
        assert conn.execute(
            "SELECT COUNT(*) c FROM casino_win_history"
        ).fetchone()["c"] == 0


@pytest.mark.parametrize(
    ("total", "expected_over"),
    [
        pytest.param(200, 6, id="200-rows-top-3pct-is-6"),
        pytest.param(100, 3, id="100-rows-top-3pct-is-3"),
        pytest.param(40, 1, id="at-the-sample-floor-round-down-not-up"),
        pytest.param(50, 1, id="50-rows-stays-conservative"),
    ],
)
def test_win_percentile_band_never_rounds_loose(db, total, expected_over):
    """The mark must not let MORE than 3% through. ``total * 97 // 100``
    rounded the wrong way below multiples of 100 — at the 40-row floor it left
    two rows above the mark, the top 5%, so the smallest guilds got the
    loosest ping bar."""
    with open_db(db) as conn:
        _bank_wins(conn, range(1, total + 1))
        mark = svc.win_percentile(conn, GUILD)
        assert mark is not None
        over = [p for p in range(1, total + 1) if p >= mark]
        assert len(over) == expected_over
        assert len(over) / total <= 0.03 or len(over) == 1


# ── mines grids ────────────────────────────────────────────────────────


def _mines_deal(conn, user_id=A, stake=20, bombs=3, bomb_tiles=(0, 1, 2)):
    """Deal a grid with the bombs pinned, so a test can press a known tile."""
    with_patched = list(bomb_tiles)
    orig = logic.mines_place_bombs
    logic.mines_place_bombs = lambda b, **kw: with_patched  # type: ignore[assignment]
    try:
        return svc.deal_mines_hand(conn, GUILD, CHAN, user_id, stake, bombs, now=NOW)
    finally:
        logic.mines_place_bombs = orig  # type: ignore[assignment]


def test_mines_deal_debits_and_opens_one_grid(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        assert step.err is None and step.hand_id
        assert get_balance(conn, GUILD, A) == 80
        assert _kinds(conn, A)[-1] == ("casino_stake", -20)
        row = svc.live_mines_hand(conn, GUILD, A)
        assert row is not None and int(row["bombs"]) == 3
        bombs, revealed = svc.deserialize_mines(str(row["state_json"]))
        assert (bombs, revealed) == ([0, 1, 2], [])


def test_mines_deal_never_leaks_the_board_while_it_is_live(db):
    """A live step carries no bomb positions — nothing downstream can leak
    a board the player is still betting into."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn)
        assert step.bomb_tiles is None
        live = svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 5, now=NOW)
        assert live.outcome is None and live.bomb_tiles is None
        # ...and it IS carried once the round is over.
        done = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert done.bomb_tiles == (0, 1, 2)


def test_mines_one_live_grid_per_member(db):
    with open_db(db) as conn:
        _fund(conn, A, 200)
        assert _mines_deal(conn).err is None
        assert "already have a grid" in (_mines_deal(conn).err or "")


def test_mines_one_live_grid_index_backstops_the_precheck(db):
    """The partial unique index is the real guard, not the pre-check."""
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _mines_deal(conn)
        assert svc.take_stake(conn, GUILD, A, 20, "mines", now=NOW) is None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO casino_mines_hands (guild_id, channel_id, user_id, "
                "stake, bombs, state_json, created_at, last_action_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (GUILD, CHAN, A, 20, 3, svc.serialize_mines([4], []), NOW, NOW),
            )


def test_mines_safe_reveal_steps_the_ladder_without_paying(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        after = svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        assert after.err is None and after.outcome is None
        assert after.revealed == (9,)
        assert after.mult == logic.mines_multiplier(3, 1)
        assert after.next_mult == logic.mines_multiplier(3, 2)
        assert get_balance(conn, GUILD, A) == 80  # nothing moved


def test_mines_bomb_settles_at_zero_and_feeds_the_pot(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        boom = svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 0, now=NOW)
        assert boom.outcome == "bombed" and boom.payout == 0
        assert boom.bomb_tiles == (0, 1, 2)
        assert get_balance(conn, GUILD, A) == 80
        assert svc.live_mines_hand(conn, GUILD, A) is None
        assert boom.pot_after > 0


def test_mines_cash_out_pays_the_rung_and_closes_the_grid(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=100, bombs=10, bomb_tiles=list(range(10)))
        for tile in (10, 11):
            svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, tile, now=NOW)
        cashed = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert cashed.outcome == "cashed"
        assert cashed.payout == logic.mines_payout(10, 2, 100) == 401
        assert get_balance(conn, GUILD, A) == 401
        assert _kinds(conn, A)[-1] == ("casino_payout", 401)
        assert svc.live_mines_hand(conn, GUILD, A) is None


def test_mines_cash_out_refused_on_an_untouched_grid(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        nope = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert "Open a tile first" in (nope.err or "")
        assert svc.live_mines_hand(conn, GUILD, A) is not None
        assert get_balance(conn, GUILD, A) == 80


def test_mines_break_even_rung_is_a_push_not_a_win(db):
    """The 1.00× first rung of the one-bomb ladder hands the stake back;
    calling that a win is losses-disguised-as-wins at its mildest."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=100, bombs=1, bomb_tiles=[19])
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 0, now=NOW)
        pushed = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert pushed.outcome == "pushed"
        assert pushed.payout == 100 == pushed.stake
        assert get_balance(conn, GUILD, A) == 100


def test_mines_clearing_the_ladder_auto_cashes(db):
    """The ceiling ends the round — there is no infinite climb to press into."""
    with open_db(db) as conn:
        _fund(conn, A, 1000)
        step = _mines_deal(conn, stake=100, bombs=10, bomb_tiles=list(range(10)))
        last = None
        for tile in (10, 11, 12, 13):
            last = svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, tile, now=NOW)
        assert last is not None
        assert last.topped is True and last.outcome == "cashed"
        assert last.payout == logic.mines_payout(10, 4, 100) == 2192
        assert svc.live_mines_hand(conn, GUILD, A) is None
        assert last.next_mult == 0


def test_mines_reveal_guards(db):
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _fund(conn, B, 200)
        step = _mines_deal(conn, stake=20)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        again = svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        assert "already opened that tile" in (again.err or "")
        theirs = svc.reveal_mines_tile(conn, GUILD, step.hand_id, B, 8, now=NOW)
        assert "not your grid" in (theirs.err or "")
        with pytest.raises(ValueError, match="tile out of range"):
            svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 20, now=NOW)


def test_mines_a_stranger_cannot_reset_someone_elses_idle_clock(db):
    """Ownership rides in the claim itself: a rejected press must not bump
    last_action_at, or a stranger could block the auto-cash forever and
    strand the owner's stake."""
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _fund(conn, B, 200)
        step = _mines_deal(conn, stake=20)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, B, 9, now=NOW + 500)
        row = svc.get_mines_hand(conn, step.hand_id)
        assert row is not None and float(row["last_action_at"]) == NOW


def test_mines_settle_is_exactly_once_under_a_race(db):
    """The player's Cash Out, the idle auto-cash and the boot sweep can all
    reach one grid; the BALANCE proves only one of them paid."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        first = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert first.err is None
        balance = get_balance(conn, GUILD, A)
        assert svc.resolve_idle_mines_hand(conn, step.hand_id, now=NOW) is None
        assert svc.refund_live_mines_hands(conn, now=NOW) == []
        second = svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert "already finished" in (second.err or "")
        assert get_balance(conn, GUILD, A) == balance


def test_mines_idle_auto_cash_pays_what_a_manual_press_would(db):
    """Walking away costs nothing that staying would not have."""
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _fund(conn, B, 200)
        a_step = _mines_deal(conn, A, stake=100, bombs=5, bomb_tiles=[0, 1, 2, 3, 4])
        b_step = _mines_deal(conn, B, stake=100, bombs=5, bomb_tiles=[0, 1, 2, 3, 4])
        for tile in (10, 11):
            svc.reveal_mines_tile(conn, GUILD, a_step.hand_id, A, tile, now=NOW)
            svc.reveal_mines_tile(conn, GUILD, b_step.hand_id, B, tile, now=NOW)
        manual = svc.cash_out_mines_hand(conn, GUILD, a_step.hand_id, A, now=NOW)
        idle = svc.resolve_idle_mines_hand(conn, b_step.hand_id, now=NOW)
        assert idle is not None
        assert idle.payout == manual.payout == 172
        assert get_balance(conn, GUILD, A) == get_balance(conn, GUILD, B)


def test_mines_idle_on_an_untouched_grid_refunds_rather_than_taking_the_edge(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=40)
        idle = svc.resolve_idle_mines_hand(conn, step.hand_id, now=NOW)
        assert idle is not None and idle.outcome == "refunded"
        assert idle.payout == 40
        assert get_balance(conn, GUILD, A) == 100
        assert _kinds(conn, A)[-1] == ("casino_refund", 40)
        # A refund is not a play: no jackpot skim, nothing in the stats.
        assert svc.member_casino_stats(conn, GUILD, A) is None


def test_mines_idle_sweep_finds_only_stale_grids(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        assert svc.idle_live_mines_hands(conn, NOW - 1) == []
        assert [int(r["id"]) for r in svc.idle_live_mines_hands(conn, NOW + 1)] == [
            step.hand_id
        ]
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW + 500)
        assert svc.idle_live_mines_hands(conn, NOW + 1) == []


def test_mines_boot_sweep_refunds_a_live_grid_in_full_and_replays_free(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        swept = svc.refund_live_mines_hands(conn, now=NOW)
        assert [int(r["user_id"]) for r in swept] == [A]
        assert get_balance(conn, GUILD, A) == 100  # made whole, not cashed
        assert svc.refund_live_mines_hands(conn, now=NOW) == []


def test_boot_sweep_covers_every_live_hand_game(db):
    """The sweep must not be the place a new table is forgotten — this is
    the test that fails if a fourth live-hand game is added without
    joining ALL_HAND_TABLES."""
    assert {t.game for t in svc.ALL_HAND_TABLES} == {"blackjack", "war", "mines"}
    with open_db(db) as conn:
        _fund(conn, A, 200)
        _fund(conn, B, 200)
        _deal(conn, A, stake=20)                      # blackjack
        _mines_deal(conn, B, stake=30)                # mines
        swept = svc.refund_all_live_hands(conn, now=NOW)
        assert len(swept) == 2
        assert get_balance(conn, GUILD, A) == 200
        assert get_balance(conn, GUILD, B) == 200
        assert svc.refund_all_live_hands(conn, now=NOW) == []


def test_leaver_refund_returns_a_live_mines_stake(db):
    """Same rule, the other seam: ALL_HAND_TABLES, not a third copy-paste."""
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=25)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 9, now=NOW)
        assert svc.refund_member_live_stakes(conn, GUILD, A, now=NOW) == {"mines": 25}
        assert get_balance(conn, GUILD, A) == 100
        assert svc.live_mines_hand(conn, GUILD, A) is None


def test_mines_respects_the_table_toggle_and_the_casino_channel(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        svc.save_casino_settings(conn, GUILD, {"mines_enabled": False})
        assert "table is closed" in (_mines_deal(conn).err or "")
        svc.save_casino_settings(conn, GUILD, {"mines_enabled": True})
        moved = svc.deal_mines_hand(conn, GUILD, CHAN + 1, A, 20, 3, now=NOW)
        assert "casino has moved" in (moved.err or "")
        assert get_balance(conn, GUILD, A) == 100


def test_mines_rejects_a_bomb_count_with_no_ladder(db):
    with open_db(db) as conn:
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="mines bomb count"):
            svc.deal_mines_hand(conn, GUILD, CHAN, A, 20, 7, now=NOW)
        assert get_balance(conn, GUILD, A) == 100  # nothing debited


def test_mines_jackpot_feeds_only_the_lost_slice(db):
    with open_db(db) as conn:
        svc.save_casino_settings(conn, GUILD, {"jackpot_cut_pct": 50, "jackpot_seed": 0})
        _fund(conn, A, 1000)
        # A winning cash-out feeds nothing.
        step = _mines_deal(conn, A, stake=100, bombs=10, bomb_tiles=list(range(10)))
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 10, now=NOW)
        svc.cash_out_mines_hand(conn, GUILD, step.hand_id, A, now=NOW)
        assert svc.get_jackpot(conn, GUILD) == 0
        # A bomb feeds half of the whole stake at a 50% cut.
        step = _mines_deal(conn, A, stake=100, bombs=3, bomb_tiles=[0, 1, 2])
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 0, now=NOW)
        assert svc.get_jackpot(conn, GUILD) == 50


def test_mines_plays_land_on_the_floor_ticker(db):
    """A private game with no public recap is invisible without this."""
    assert "mines" in svc.TICKER_GAMES
    with open_db(db) as conn:
        _fund(conn, A, 100)
        step = _mines_deal(conn, stake=20)
        svc.reveal_mines_tile(conn, GUILD, step.hand_id, A, 0, now=NOW)
        rows = conn.execute(
            "SELECT game, stake, payout FROM casino_ticker WHERE guild_id = ?",
            (GUILD,),
        ).fetchall()
        assert [(r["game"], r["stake"], r["payout"]) for r in rows] == [("mines", 20, 0)]


# ── casino_play quest trigger ──────────────────────────────────────────
#
# Quest 89 "Take a Seat" shipped in prod against a `casino_play` trigger kind
# that never existed in the vocabulary, so it could never be cleared while
# sitting in the daily pool consuming board draws (2026-08-28 economy review
# §9). These pin the kind's contract: it fires on money actually at risk, and
# only then.


def _casino_quest(conn, *, target_count=1, reward=10):
    from bot_modules.services.economy_quests_service import (
        create_quest,
        set_quest_active,
    )

    qid = create_quest(
        conn, GUILD,
        title="Take a Seat", description="", qtype="daily", reward=reward,
        signoff=0, criteria="", starts_at=None, ends_at=None, rotate_tag="",
        community_target=None, created_by=None, trigger_kind="casino_play",
        target_count=target_count,
    )
    set_quest_active(conn, GUILD, qid, True)
    return qid


def _marks(conn, quest_id, user_id):
    return [
        r["occurrence"]
        for r in conn.execute(
            "SELECT occurrence FROM econ_quest_progress_marks "
            "WHERE quest_id = ? AND user_id = ?",
            (quest_id, user_id),
        )
    ]


def test_casino_play_fires_once_per_charged_stake(db):
    with open_db(db) as conn:
        qid = _casino_quest(conn, target_count=3)
        _fund(conn, A, 500)
        for _ in range(3):
            assert svc.take_stake(conn, GUILD, A, 10, "slots", now=NOW) is None
        marks = _marks(conn, qid, A)
        # Three distinct bets -> three countable occurrences, even though they
        # share a game, an amount and a timestamp: the key is the ledger row.
        assert len(marks) == 3, marks
        assert len(set(marks)) == 3, marks


@pytest.mark.parametrize(
    ("setup", "amount", "game"),
    [
        pytest.param(lambda c: None, 10_000, "slots", id="insufficient-funds"),
        pytest.param(
            lambda c: svc.save_casino_settings(c, GUILD, {"slots_enabled": False}),
            10, "slots", id="table-closed",
        ),
        pytest.param(
            lambda c: svc.save_casino_settings(c, GUILD, {"min_bet": 50}),
            10, "slots", id="under-min-bet",
        ),
    ],
)
def test_casino_play_does_not_fire_when_the_stake_is_refused(db, setup, amount, game):
    with open_db(db) as conn:
        qid = _casino_quest(conn)
        _fund(conn, A, 100)
        setup(conn)
        assert svc.take_stake(conn, GUILD, A, amount, game, now=NOW) is not None
        # A bet that was never charged is not money at risk, so it earns nothing.
        assert _marks(conn, qid, A) == []


def test_casino_play_is_a_known_trigger_kind():
    from bot_modules.economy.quests import TRIGGER_KIND_INFO, TRIGGER_KINDS

    # The defect this fixes: the quest row existed, the kind did not.
    assert "casino_play" in TRIGGER_KINDS
    assert "casino_play" in TRIGGER_KIND_INFO
