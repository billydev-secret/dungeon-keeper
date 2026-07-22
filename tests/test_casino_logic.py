"""Tests for services/casino_logic.py — the fixed casino paytables.

The RTP tests are the design contract: exact expected-value enumeration
(not sampling) pinning each game inside its intended house-edge band, so a
paytable edit that turns a game into a coin printer fails here before it
ships.
"""

from __future__ import annotations

import itertools

import pytest

from bot_modules.services import casino_logic as logic

# ── coinflip ───────────────────────────────────────────────────────────


def test_coinflip_pays_1_9x_floored():
    assert logic.coinflip_payout(100) == 190
    assert logic.coinflip_payout(5) == 9  # floor(9.5)


def test_coinflip_rtp_is_95_percent():
    # 50% win chance × 1.9 total return.
    stake = 100
    assert 0.5 * logic.coinflip_payout(stake) / stake == pytest.approx(0.95)


def test_flip_coin_uses_module_random(monkeypatch):
    monkeypatch.setattr(logic.random, "choice", lambda seq: seq[0])
    assert logic.flip_coin() == "heads"


# ── slots ──────────────────────────────────────────────────────────────


def test_slots_triple_sevens_is_the_jackpot():
    payout, label = logic.slots_payout((logic.SEVEN,) * 3, 10)
    assert payout == 1200
    assert label is not None and "JACKPOT" in label


def test_slots_two_sevens_pay_5x_even_with_a_pair_present():
    payout, _ = logic.slots_payout((logic.SEVEN, logic.SEVEN, "🌻"), 10)
    assert payout == 50


def test_slots_pair_pays_1_5x_floored():
    payout, label = logic.slots_payout(("🌻", "🌻", "🍀"), 5)
    assert payout == 7  # floor(7.5)
    assert label == "A matching pair"


def test_slots_lone_seven_with_pair_counts_as_pair():
    payout, _ = logic.slots_payout((logic.SEVEN, "🍀", "🍀"), 10)
    assert payout == 15


def test_slots_no_match_pays_nothing():
    payout, label = logic.slots_payout(("🌻", "🍀", "🐝"), 10)
    assert payout == 0
    assert label is None


def test_slots_exact_rtp_in_design_band():
    """Enumerate all 26³ reel outcomes; RTP must sit in [0.90, 0.96]."""
    stake = 100  # multiple of 2 — the pair payout floors nothing
    total = 0
    combos = 0
    for reels in itertools.product(logic.SLOT_REEL, repeat=3):
        payout, _ = logic.slots_payout(reels, stake)
        total += payout
        combos += 1
    rtp = total / (combos * stake)
    assert 0.90 <= rtp <= 0.96, f"slots RTP drifted to {rtp:.4f}"


def test_spin_slots_uses_module_random(monkeypatch):
    monkeypatch.setattr(logic.random, "choice", lambda seq: seq[-1])
    assert logic.spin_slots() == (logic.SEVEN,) * 3


# ── blackjack ──────────────────────────────────────────────────────────


def test_hand_value_flexes_aces():
    assert logic.hand_value(["A♠", "K♦"]) == 21
    assert logic.hand_value(["A♠", "A♦"]) == 12
    assert logic.hand_value(["A♠", "9♦", "A♣"]) == 21
    assert logic.hand_value(["10♠", "9♦", "5♣"]) == 24


def test_natural_is_two_card_21_only():
    assert logic.is_natural(["A♠", "Q♦"])
    assert not logic.is_natural(["7♠", "7♦", "7♣"])


def test_new_deck_is_52_unique_cards():
    deck = logic.new_deck()
    assert len(deck) == 52
    assert len(set(deck)) == 52


def test_dealer_draws_to_17_and_stands():
    deck = ["2♣", "9♣"]  # pops from the end
    dealer = ["10♠", "6♦"]
    logic.dealer_play(deck, dealer)
    assert logic.hand_value(dealer) == 25  # drew the 9, busted, stopped
    assert deck == ["2♣"]

    stands = ["10♠", "7♦"]
    logic.dealer_play(deck, stands)
    assert stands == ["10♠", "7♦"]  # stands on all 17


def test_settle_matrix():
    settle = logic.blackjack_settle
    assert settle(["10♠", "9♦", "5♣"], ["10♥", "7♥"], 10) == (0, "bust")
    assert settle(["A♠", "K♦"], ["10♥", "7♥"], 10) == (25, "blackjack")
    assert settle(["A♠", "K♦"], ["A♥", "Q♥"], 10) == (10, "push")
    assert settle(["10♠", "9♦"], ["A♥", "Q♥"], 10) == (0, "lose")
    assert settle(["10♠", "9♦"], ["10♥", "8♥"], 10) == (20, "win")
    assert settle(["10♠", "8♦"], ["10♥", "6♥", "K♣"], 10) == (20, "win")  # dealer bust
    assert settle(["10♠", "8♦"], ["10♥", "8♥"], 10) == (10, "push")
    assert settle(["10♠", "7♦"], ["10♥", "8♥"], 10) == (0, "lose")


def test_settle_blackjack_pays_3_to_2_floored():
    payout, outcome = logic.blackjack_settle(["A♠", "K♦"], ["10♥", "7♥"], 5)
    assert (payout, outcome) == (12, "blackjack")  # floor(12.5)


# ── roulette ───────────────────────────────────────────────────────────


def test_wheel_colors():
    assert logic.wheel_color(0) == "green"
    assert logic.wheel_color(1) == "red"
    assert logic.wheel_color(2) == "black"
    assert logic.wheel_color(19) == "red"
    assert logic.wheel_color(10) == "black"
    reds = sum(1 for n in range(37) if logic.wheel_color(n) == "red")
    blacks = sum(1 for n in range(37) if logic.wheel_color(n) == "black")
    assert (reds, blacks) == (18, 18)


def test_roulette_color_bets_pay_double():
    assert logic.roulette_payout("red", 0, 3, 10) == 20
    assert logic.roulette_payout("black", 0, 3, 10) == 0
    assert logic.roulette_payout("red", 0, 0, 10) == 0  # zero beats colors


def test_roulette_dozen_bets():
    assert logic.roulette_payout("dozen", 1, 12, 10) == 30
    assert logic.roulette_payout("dozen", 2, 13, 10) == 30
    assert logic.roulette_payout("dozen", 2, 12, 10) == 0
    assert logic.roulette_payout("dozen", 3, 0, 10) == 0  # zero beats dozens


def test_roulette_straight_number_pays_36x():
    assert logic.roulette_payout("number", 17, 17, 10) == 360
    assert logic.roulette_payout("number", 0, 0, 10) == 360  # zero is bettable
    assert logic.roulette_payout("number", 17, 16, 10) == 0


def test_roulette_unknown_bet_type_raises():
    with pytest.raises(ValueError):
        logic.roulette_payout("split", 1, 1, 10)


@pytest.mark.parametrize(
    ("bet_type", "selection", "expected_rtp"),
    [("red", 0, 18 * 2 / 37), ("dozen", 2, 12 * 3 / 37), ("number", 7, 36 / 37)],
)
def test_roulette_rtp_is_single_zero(bet_type, selection, expected_rtp):
    stake = 10
    total = sum(
        logic.roulette_payout(bet_type, selection, result, stake)
        for result in range(37)
    )
    assert total / (37 * stake) == pytest.approx(expected_rtp)


def test_spin_roulette_uses_module_random(monkeypatch):
    monkeypatch.setattr(logic.random, "randint", lambda a, b: 36)
    assert logic.spin_roulette() == 36


def test_describe_bet_labels():
    assert logic.describe_bet("red", 0) == "🔴 Red"
    assert logic.describe_bet("dozen", 2) == "Dozen 13–24"
    assert logic.describe_bet("number", 17) == "Straight 17"


# ── fancy round: streaks & thresholds ──────────────────────────────────


def test_next_streak_runs_and_resets():
    ns = logic.next_streak
    assert ns(0, 10, 19) == 1        # win starts a run
    assert ns(3, 10, 19) == 4        # win extends
    assert ns(-2, 10, 19) == 1       # win flips a cold run
    assert ns(0, 10, 0) == -1        # loss starts a cold run
    assert ns(-2, 10, 0) == -3       # loss extends
    assert ns(4, 10, 0) == -1        # loss flips a hot run
    assert ns(5, 10, 10) == 0        # push resets either way
    assert ns(-5, 10, 10) == 0


def test_is_big_win_is_10x():
    assert logic.is_big_win(10, 100)
    assert not logic.is_big_win(10, 99)


def test_is_big_bet_tiers():
    assert logic.is_big_bet(70, 100)       # ≥70% of the table max
    assert not logic.is_big_bet(69, 100)
    assert logic.is_big_bet(100, 0)        # uncapped: flat 100 floor
    assert not logic.is_big_bet(99, 0)
