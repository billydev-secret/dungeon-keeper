"""Tests for services/casino_logic.py — the fixed casino paytables.

The RTP tests are the design contract: exact expected-value enumeration
(not sampling) pinning each game inside its intended house-edge band, so a
paytable edit that turns a game into a coin printer fails here before it
ships.
"""

from __future__ import annotations

import itertools
from fractions import Fraction

import pytest

from bot_modules.services import casino_logic as logic

# ── coinflip ───────────────────────────────────────────────────────────


def test_coinflip_pays_1_85x_floored():
    assert logic.coinflip_payout(100) == 185
    assert logic.coinflip_payout(5) == 9  # floor(9.25)


def test_coinflip_rtp_is_92_5_percent():
    # 50% win chance × 1.85 total return.
    stake = 100
    assert 0.5 * logic.coinflip_payout(stake) / stake == pytest.approx(0.925)


def test_coinflip_advertised_return_matches_the_paytable():
    """The How It Works embed quotes COINFLIP_RTP_PCT; it must be true.

    An advertised edge that isn't the real one is the gambling equivalent
    of shipping a preference nobody enforces.
    """
    stake = 100
    actual = 0.5 * logic.coinflip_payout(stake) / stake
    assert logic.COINFLIP_RTP_PCT == pytest.approx(actual * 100, abs=0.05)


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


def test_slots_pair_pays_1_45x_floored():
    payout, label = logic.slots_payout(("🌻", "🌻", "🍀"), 5)
    assert payout == 7  # floor(7.25)
    assert label == "A matching pair"


def test_slots_lone_seven_with_pair_counts_as_pair():
    payout, _ = logic.slots_payout((logic.SEVEN, "🍀", "🍀"), 10)
    assert payout == 14  # floor(14.5)


def test_slots_no_match_pays_nothing():
    payout, label = logic.slots_payout(("🌻", "🍀", "🐝"), 10)
    assert payout == 0
    assert label is None


def _slots_exact_rtp() -> float:
    stake = 2000  # a multiple of SLOT_PAIR_DEN — the pair payout floors nothing
    total = 0
    combos = 0
    for reels in itertools.product(logic.SLOT_REEL, repeat=3):
        payout, _ = logic.slots_payout(reels, stake)
        total += payout
        combos += 1
    return total / (combos * stake)


def test_slots_exact_rtp_in_design_band():
    """Enumerate all 26³ reel outcomes; RTP must sit in [0.89, 0.94].

    Band lowered from [0.90, 0.96] on 2026-07-26 with the pair trim (1.5x
    -> 1.45x); the exact value is 4935/5408 = 0.91254.
    """
    rtp = _slots_exact_rtp()
    assert 0.89 <= rtp <= 0.94, f"slots RTP drifted to {rtp:.4f}"


def test_slots_advertised_return_matches_the_paytable():
    """SLOTS_RTP_PCT is quoted in the How It Works embed; keep it honest."""
    assert logic.SLOTS_RTP_PCT == pytest.approx(_slots_exact_rtp() * 100, abs=0.05)


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


# ── derby ──────────────────────────────────────────────────────────────


def test_derby_weights_sum_to_total():
    assert sum(r.weight for r in logic.DERBY_FIELD) == logic.DERBY_TOTAL_WEIGHT


def test_derby_every_runner_rtp_in_design_band():
    """Exact EV per runner: weight/100 × multiplier must sit in the slots
    band, so no runner is a strictly better (or house-robbing) pick."""
    stake = 100  # multiple of every mult_den — floors nothing
    for i, r in enumerate(logic.DERBY_FIELD):
        rtp = (r.weight / logic.DERBY_TOTAL_WEIGHT) * (
            logic.derby_payout(i, i, stake) / stake
        )
        assert 0.90 <= rtp <= 0.97, f"runner {r.name} RTP drifted to {rtp:.4f}"


def test_derby_payout_wins_and_losses():
    assert logic.derby_payout(0, 0, 10) == 25   # the hare's 2.5×
    assert logic.derby_payout(4, 4, 5) == 47    # 9.5× floored
    assert logic.derby_payout(0, 1, 10) == 0    # wrong runner


def test_run_derby_uses_module_random(monkeypatch):
    monkeypatch.setattr(
        logic.random, "choices", lambda seq, weights: [list(seq)[2]]
    )
    assert logic.run_derby() == 2


def test_derby_frames_invariants():
    """For every possible winner: positions only advance, nobody finishes
    early, and the final frame has the winner at the line alone."""
    for winner in range(len(logic.DERBY_FIELD)):
        for _ in range(20):
            frames = logic.derby_frames(winner)
            assert len(frames) == logic.DERBY_FRAMES + 1
            prev = [0] * len(logic.DERBY_FIELD)
            for frame in frames[:-1]:
                assert all(p >= q for p, q in zip(frame, prev))
                assert all(p < logic.DERBY_TRACK_LEN - 1 for p in frame)
                prev = frame
            final = frames[-1]
            assert all(p >= q for p, q in zip(final, prev))
            assert final[winner] == logic.DERBY_TRACK_LEN
            assert all(
                p < logic.DERBY_TRACK_LEN
                for i, p in enumerate(final)
                if i != winner
            )


def test_derby_labels():
    assert logic.describe_runner(0) == "🐇 Hazel the Hare"
    assert logic.derby_odds_label(0) == "2.5×"
    assert logic.derby_odds_label(1) == "5×"
    assert logic.derby_odds_label(4) == "9.5×"


# ── baccarat ───────────────────────────────────────────────────────────


def test_baccarat_card_and_hand_values():
    assert logic.baccarat_card_value("A♠") == 1
    assert logic.baccarat_card_value("9♦") == 9
    for rank in ("10", "J", "Q", "K"):
        assert logic.baccarat_card_value(rank + "♥") == 0
    assert logic.baccarat_total(["7♠", "8♦"]) == 5  # 15 → 5
    assert logic.baccarat_total(["K♠", "Q♦"]) == 0


@pytest.mark.parametrize(
    ("banker_total", "draws_on", "stands_on"),
    [
        (0, range(10), ()),
        (1, range(10), ()),
        (2, range(10), ()),
        (3, [v for v in range(10) if v != 8], [8]),
        (4, range(2, 8), [0, 1, 8, 9]),
        (5, range(4, 8), [0, 1, 2, 3, 8, 9]),
        (6, [6, 7], [0, 1, 2, 3, 4, 5, 8, 9]),
        (7, (), range(10)),
    ],
)
def test_banker_third_card_rule_matrix(banker_total, draws_on, stands_on):
    """The full punto-banco tableau — the drawing rules ARE the paytable."""
    for p3 in draws_on:
        assert logic._banker_draws(banker_total, p3), (banker_total, p3)
    for p3 in stands_on:
        assert not logic._banker_draws(banker_total, p3), (banker_total, p3)


def _scripted_shoe(monkeypatch, ranks):
    """Feed deal_baccarat an exact rank sequence (suits pinned to ♠)."""
    queue = list(ranks)
    monkeypatch.setattr(
        logic.random,
        "choice",
        lambda seq: queue.pop(0) if seq is logic._RANKS else "♠",
    )


def test_deal_baccarat_natural_stands_both(monkeypatch):
    _scripted_shoe(monkeypatch, ["A", "8", "K", "K"])
    player, banker = logic.deal_baccarat()
    assert player == ["A♠", "8♠"] and banker == ["K♠", "K♠"]
    assert logic.baccarat_winner(player, banker) == "player"


def test_deal_baccarat_player_and_banker_third_cards_in_deal_order(monkeypatch):
    # Player 2+3=5 draws; third card 5 → banker on 4 draws against 5.
    _scripted_shoe(monkeypatch, ["2", "3", "4", "K", "5", "6"])
    player, banker = logic.deal_baccarat()
    assert player == ["2♠", "3♠", "5♠"]
    assert banker == ["4♠", "K♠", "6♠"]


def test_deal_baccarat_player_stands_banker_draws_to_five(monkeypatch):
    # Player 3+4=7 stands; banker 2+3=5 draws when the player stood.
    _scripted_shoe(monkeypatch, ["3", "4", "2", "3", "9"])
    player, banker = logic.deal_baccarat()
    assert player == ["3♠", "4♠"]
    assert banker == ["2♠", "3♠", "9♠"]
    assert logic.baccarat_winner(player, banker) == "player"  # 7 beats 4


def test_baccarat_payout_matrix():
    pay = logic.baccarat_payout
    p9, b0 = ["A♠", "8♦"], ["K♠", "Q♦"]
    assert pay("player", p9, b0, 10) == 20
    assert pay("banker", p9, b0, 10) == 0
    assert pay("tie", p9, b0, 10) == 0
    # Two-card banker 7 win pays even — no Dragon-7 bar on two cards.
    p5, b7 = ["2♠", "3♦"], ["4♠", "3♦"]
    assert pay("banker", p5, b7, 10) == 20
    # Three-card banker 7 win is barred to a push (EZ Dragon-7).
    b7_3 = ["A♠", "2♦", "4♣"]
    assert pay("banker", p5, b7_3, 10) == 10
    assert pay("player", p5, b7_3, 10) == 0  # player bet still just loses
    # Ties: tie bet pays 8:1, side bets push.
    p_t, b_t = ["4♠", "3♦"], ["2♠", "5♦"]
    assert pay("tie", p_t, b_t, 10) == 90
    assert pay("player", p_t, b_t, 10) == 10
    assert pay("banker", p_t, b_t, 10) == 10


def test_baccarat_unknown_side_raises():
    with pytest.raises(ValueError):
        logic.baccarat_payout("dragon", ["A♠", "8♦"], ["K♠", "Q♦"], 10)


def test_baccarat_exact_rtp_pinned():
    """Exact EV over the infinite-shoe punto-banco tree (values 1–9 weigh
    1/13 each, the 0-valued tens/faces 4/13). Pins all three bets: Player
    98.77%, Banker 98.98% (EZ Dragon-7 replaces the 5% commission), Tie
    85.88% — the labeled long shot, priced like the house intends."""
    from fractions import Fraction

    w = {v: Fraction(4 if v == 0 else 1, 13) for v in range(10)}
    # Cards realizing a value: 0 via a face card so 3-card hands read right.
    card = {v: ("K♠" if v == 0 else ("A♠" if v == 1 else f"{v}♠")) for v in range(10)}
    dist2: dict[int, Fraction] = {t: Fraction(0) for t in range(10)}
    for v1 in range(10):
        for v2 in range(10):
            dist2[(v1 + v2) % 10] += w[v1] * w[v2]

    ev = {side: Fraction(0) for side in logic.BACCARAT_SIDES}
    total_prob = Fraction(0)

    def settle(prob, player, banker):
        nonlocal total_prob
        total_prob += prob
        for side in logic.BACCARAT_SIDES:
            ev[side] += prob * logic.baccarat_payout(side, player, banker, 1)

    for pt in range(10):
        for bt in range(10):
            prob0 = dist2[pt] * dist2[bt]
            p2, b2 = [card[pt], card[0]], [card[bt], card[0]]
            if pt >= 8 or bt >= 8:  # natural — both stand
                settle(prob0, p2, b2)
            elif pt <= 5:  # player draws; banker consults the tableau
                for p3 in range(10):
                    p_hand = p2 + [card[p3]]
                    if logic._banker_draws(bt, p3):
                        for b3 in range(10):
                            settle(prob0 * w[p3] * w[b3], p_hand, b2 + [card[b3]])
                    else:
                        settle(prob0 * w[p3], p_hand, b2)
            elif bt <= 5:  # player stands on 6/7; banker draws on 0–5
                for b3 in range(10):
                    settle(prob0 * w[b3], p2, b2 + [card[b3]])
            else:
                settle(prob0, p2, b2)

    assert total_prob == 1
    assert float(ev["player"]) == pytest.approx(0.987719, abs=1e-6)
    assert float(ev["banker"]) == pytest.approx(0.989752, abs=1e-6)
    assert float(ev["tie"]) == pytest.approx(0.858830, abs=1e-6)


def test_deal_baccarat_uses_module_random(monkeypatch):
    _scripted_shoe(monkeypatch, ["9", "K", "K", "K"])  # 9 vs 0, both natural-side
    player, banker = logic.deal_baccarat()
    assert logic.baccarat_winner(player, banker) == "player"


def test_baccarat_labels():
    assert logic.describe_baccarat_side("player") == "🔵 Player"
    assert logic.describe_baccarat_side("banker") == "🔴 Banker"
    assert logic.describe_baccarat_side("tie") == "🟡 Tie"


# ── dice (sic bo) ──────────────────────────────────────────────────────


def test_sicbo_payout_matrix():
    pay = logic.sicbo_payout
    assert pay("big", (6, 5, 4), 10) == 20     # 15 is big
    assert pay("big", (1, 2, 3), 10) == 0      # 6 is small
    assert pay("small", (1, 2, 3), 10) == 20
    assert pay("odd", (1, 2, 4), 10) == 20     # 7
    assert pay("even", (1, 2, 3), 10) == 20    # 6
    assert pay("odd", (1, 2, 3), 10) == 0
    # every bet loses to any triple — that exclusion is the house edge
    for bet in logic.SICBO_BET_TYPES:
        assert pay(bet, (4, 4, 4), 10) == 0, bet


def test_sicbo_unknown_bet_type_raises():
    with pytest.raises(ValueError):
        logic.sicbo_payout("triple", (1, 2, 3), 10)


@pytest.mark.parametrize("bet_type", logic.SICBO_BET_TYPES)
def test_sicbo_exact_rtp_is_105_216(bet_type):
    """Enumerate all 216 rolls: each even-money bet wins exactly 105 of
    them (triples excluded), pinning RTP at 210/216 ≈ 97.22%."""
    stake = 10
    total = sum(
        logic.sicbo_payout(bet_type, (a, b, c), stake)
        for a, b, c in itertools.product(range(1, 7), repeat=3)
    )
    assert total == 105 * stake * 2
    assert total / (216 * stake) == pytest.approx(105 * 2 / 216)


def test_roll_sicbo_uses_module_random(monkeypatch):
    monkeypatch.setattr(logic.random, "randint", lambda a, b: 6)
    assert logic.roll_sicbo() == (6, 6, 6)


def test_sicbo_labels_and_faces():
    assert logic.describe_sicbo_bet("big") == "⬆️ Big (11–17)"
    assert logic.describe_sicbo_bet("even") == "2️⃣ Even"
    assert logic.dice_faces((1, 3, 6)) == "⚀ ⚂ ⚅"


# ── war ────────────────────────────────────────────────────────────────


def test_war_ranks_aces_high():
    assert logic.war_rank("A♠") == 14
    assert logic.war_rank("K♦") == 13
    assert logic.war_rank("Q♥") == 12
    assert logic.war_rank("J♣") == 11
    assert logic.war_rank("10♠") == 10
    assert logic.war_rank("2♠") == 2


def test_war_payout_matrix():
    assert logic.war_payout("A♠", "K♦", 10) == 20   # high card wins even
    assert logic.war_payout("2♠", "3♦", 10) == 0
    assert logic.war_payout("7♠", "7♦", 10) is None  # tie → member decides
    # war round: win or SECOND tie takes 3× the original (on the doubled stake)
    assert logic.war_raise_payout("9♠", "5♦", 20) == 30
    assert logic.war_raise_payout("5♠", "5♦", 20) == 30
    assert logic.war_raise_payout("4♠", "9♦", 20) == 0
    assert logic.war_retreat_payout(10) == 5
    assert logic.war_retreat_payout(11) == 5  # floored — the house keeps the odd coin


def test_war_exact_rtp_pinned_for_both_strategies():
    """Exact EV over the infinite shoe (13×13 first cards; 13×13 war cards
    on a tie). Always-war returns 177/182 ≈ 97.25%; always-retreat 25/26 ≈
    96.15% — both in band, and war strictly better, so the idle default
    (war when affordable) never plays against the member."""
    from fractions import Fraction

    s = 26  # divisible by 2 — retreat floors nothing
    n = Fraction(1, 13)
    ranks = [logic.war_rank(r + "♠") for r in logic._RANKS]
    ev_war = Fraction(0)
    ev_retreat = Fraction(0)
    wagered_war = Fraction(0)
    for p in ranks:
        for d in ranks:
            prob = n * n
            if p != d:
                ret = 2 * s if p > d else 0
                ev_war += prob * ret
                ev_retreat += prob * ret
                wagered_war += prob * s
                continue
            # tie: retreat takes half; war doubles and draws again
            ev_retreat += prob * (s // 2)
            wagered_war += prob * 2 * s
            for wp in ranks:
                for wd in ranks:
                    if wp >= wd:
                        ev_war += prob * n * n * 3 * s
    assert ev_war / wagered_war == Fraction(177, 182)
    assert ev_retreat / (s * 1) == Fraction(25, 26)
    assert 0.93 <= 177 / 182 <= 0.975 and 0.93 <= 25 / 26 <= 0.975


def test_draw_war_cards_uses_module_random(monkeypatch):
    monkeypatch.setattr(logic.random, "choice", lambda seq: seq[0])
    assert logic.draw_war_cards() == ("A♠", "A♠")


# ── keno ───────────────────────────────────────────────────────────────


def test_keno_payout_matrix():
    drawn = list(range(1, 21))  # 1–20 drawn
    assert logic.keno_payout([1, 2, 30, 40], drawn, 10) == 20      # 2/4 → 2×
    assert logic.keno_payout([1, 2, 3, 4], drawn, 10) == 600       # 4/4 → 60×
    assert logic.keno_payout([21, 22, 23, 24], drawn, 10) == 0     # 0/4
    assert logic.keno_payout([1, 2, 30, 40, 50, 60], drawn, 10) == 10  # 2/6 money back
    assert logic.keno_payout(list(range(1, 11)), drawn, 10) == 50_000  # 10/10 → 5000×
    with pytest.raises(ValueError):
        logic.keno_payout([1, 2, 3], drawn, 10)  # 3 spots is not a tier


def test_keno_catches_counts_the_overlap():
    assert logic.keno_catches([1, 2, 3, 4], [3, 4, 5, 6]) == 2


@pytest.mark.parametrize(
    ("tier", "pinned"),
    [(4, 0.955058), (6, 0.953625), (8, 0.946554), (10, 0.952529)],
)
def test_keno_exact_rtp_pinned_and_in_band(tier, pinned):
    """Exact hypergeometric EV per tier — the bespoke paytables sit in
    ~94–96% (nothing like real casino keno's 65–75%), pinned exactly."""
    from fractions import Fraction
    from math import comb

    total = comb(80, 20)
    rtp = sum(
        Fraction(comb(tier, k) * comb(80 - tier, 20 - k), total) * mult
        for k, mult in logic.KENO_PAYTABLE[tier].items()
    )
    assert float(rtp) == pytest.approx(pinned, abs=1e-6)
    assert 0.94 <= float(rtp) <= 0.96, f"Pick-{tier} RTP drifted to {float(rtp):.4f}"


def test_keno_quick_pick_and_draw_shapes(monkeypatch):
    monkeypatch.setattr(
        logic.random, "sample", lambda pop, k: list(pop)[:k][::-1]
    )
    assert logic.keno_quick_pick(6) == [1, 2, 3, 4, 5, 6]  # sorted
    assert logic.draw_keno() == list(range(1, 21))


def test_keno_ticket_label():
    assert logic.describe_keno_ticket([4, 12, 33]) == "Pick-3 · 4 12 33"


# The round-7 complaint: three losing tickets, no way to see the near miss.
_ROUND_7 = [2, 4, 5, 10, 14, 18, 20, 27, 31, 33,
            35, 36, 38, 47, 50, 57, 63, 68, 70, 71]


def test_keno_pay_threshold_reads_the_paytable():
    assert logic.keno_pay_threshold(4) == 2
    assert logic.keno_pay_threshold(6) == 2
    assert logic.keno_pay_threshold(8) == 3
    assert logic.keno_pay_threshold(10) == 4
    with pytest.raises(ValueError):
        logic.keno_pay_threshold(3)


@pytest.mark.parametrize(
    ("picks", "expected"),
    [
        # 3 of 10 — the near miss that read as a missing payout.
        pytest.param(
            [3, 4, 7, 18, 30, 40, 52, 62, 68, 72],
            "Pick-10 · 3 **4** 7 **18** 30 40 52 62 **68** 72 · caught 3 "
            "· 4 returns your stake",
            id="near-miss-bolds-its-hits",
        ),
        # Nothing caught: still itemised, still says what it needed.
        pytest.param(
            [8, 16, 24, 77],
            "Pick-4 · 8 16 24 77 · caught 0 · 2 pays",
            id="no-catch-tier-with-a-real-multiplier-says-pays",
        ),
        # Pick-8's floor is 3 catches at 1× — stake back, not a win.
        pytest.param(
            [11, 13, 15, 22, 39, 58, 60, 68],
            "Pick-8 · 11 13 15 22 39 58 60 **68** · caught 1 "
            "· 3 returns your stake",
            id="break-even-floor-never-promises-a-win",
        ),
    ],
)
def test_describe_keno_result_annotates_a_losing_ticket(picks, expected):
    assert logic.describe_keno_result(picks, _ROUND_7, 0) == expected


def test_describe_keno_result_drops_the_threshold_for_winners():
    """A paid ticket shows stake → payout already; the line it cleared
    would only be noise."""
    line = logic.describe_keno_result([2, 4, 5, 99], _ROUND_7, 240)
    assert line == "Pick-4 · **2** **4** **5** 99 · caught 3"
    assert "pays" not in line and "stake" not in line


@pytest.mark.parametrize("spots", sorted(logic.KENO_PAYTABLE))
def test_keno_threshold_matches_what_keno_payout_actually_pays(spots):
    """The label is derived, never hardcoded: one catch below the stated
    threshold must pay nothing, and the threshold itself must pay."""
    need = logic.keno_pay_threshold(spots)
    drawn = list(range(1, 21))
    at = [*range(1, need + 1), *range(41, 41 + spots - need)]
    below = [*range(1, need), *range(41, 41 + spots - need + 1)]
    assert logic.keno_payout(at, drawn, 10) > 0
    assert logic.keno_payout(below, drawn, 10) == 0
    # ...and "returns your stake" appears exactly when the floor is 1×.
    line = logic.describe_keno_result(below, drawn, 0)
    breaks_even = logic.keno_payout(at, drawn, 10) == 10
    assert ("returns your stake" in line) is breaks_even


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


# ── big-win broadcast tiers ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payout", "threshold", "expected"),
    [
        pytest.param(499, 500, None, id="under-the-bar-stays-private"),
        pytest.param(5000, 0, None, id="bar-of-zero-is-the-off-switch"),
        pytest.param(1200, 0, None, id="off-switch-beats-any-payout"),
        pytest.param(5000, -1, None, id="negative-bar-is-also-off"),
        pytest.param(500, 500, "💰 Big Win", id="exactly-the-bar-broadcasts"),
        pytest.param(1200, 500, "💰 Big Win", id="1x-tier"),
        pytest.param(1499, 500, "💰 Big Win", id="just-under-3x"),
        pytest.param(1500, 500, "🔥 Huge Win", id="3x-tier"),
        pytest.param(50_000, 500, "🔥 Huge Win", id="far-over-3x-without-history"),
    ],
)
def test_big_win_tier_ladder_without_history(payout, threshold, expected):
    """The ladder with no percentile to rank against — every guild's first
    weeks, and any guild whose announced-win history is under the sample
    floor. Two rungs, and the top of the ladder is as loud as it gets."""
    tier = logic.big_win_tier(payout, threshold, stake=1)
    assert (None if tier is None else tier.header) == expected
    # No history can ever produce the @here: an unknown percentile withholds
    # the ping rather than passing it.
    assert tier is None or not tier.ping


def test_every_ladder_rung_is_reachable():
    """Guards the defect this ladder shipped with: a 🌟 Monster Win rung at
    10× the bar that no payout could reach, because Legendary's floor was the
    same 10× and always won. Every rung must have a payout that renders it."""
    rendered = {
        logic.big_win_tier(payout, 500, stake=1, top_pct_payout=2500).header
        for payout in range(500, 6001, 1)
    }
    assert rendered == {h for _, h in logic.BIG_WIN_TIERS} | {
        logic.LEGENDARY_HEADER
    }


@pytest.mark.parametrize(
    ("payout", "top_pct", "pings"),
    [
        pytest.param(3000, 2500, True, id="over-a-percentile-above-the-floor"),
        pytest.param(2500, 2500, True, id="exactly-the-percentile-qualifies"),
        pytest.param(2499, 2500, False, id="just-under-the-percentile"),
        pytest.param(2000, 1000, True, id="cheap-percentile-still-pings-over-the-floor"),
        pytest.param(1500, 1000, True, id="the-3x-floor-is-the-ping-minimum"),
        pytest.param(1499, 1000, False, id="cheap-percentile-cannot-ping-under-the-floor"),
        pytest.param(1499, 100, False, id="a-cheap-percentile-cannot-ping-a-big-win"),
        pytest.param(50_000, None, False, id="no-history-never-pings"),
    ],
)
def test_big_win_tier_ping_takes_the_higher_of_percentile_and_floor(
    payout, top_pct, pings
):
    """The @here fires on the top 3% of recent ANNOUNCED wins, floored at the
    ladder's top rung.

    The floor is the guard that matters: a guild whose announced wins all
    cluster near its bar would have a percentile barely over that bar, and
    without the floor every routine broadcast would ping the whole channel.
    """
    tier = logic.big_win_tier(payout, 500, stake=1, top_pct_payout=top_pct)
    assert tier is not None
    assert tier.ping is pings
    # The loudest tier is the only one that renames itself and adds a lead
    # line; the quieter rungs must never carry either.
    assert (tier.header == logic.LEGENDARY_HEADER) is pings
    assert (tier.lead is not None) is pings


def test_legendary_supersedes_the_rung_it_lands_on():
    """Documented, accepted behaviour rather than an accident: when a guild's
    percentile sits at or under the ladder's top rung, Legendary and Huge Win
    coincide and Huge Win is subsumed. Reserving a sliver of range for it
    would buy a rung nobody would ever see fire."""
    flat = logic.big_win_tier(1500, 500, stake=1, top_pct_payout=900)
    assert flat is not None and flat.header == logic.LEGENDARY_HEADER
    # With a percentile above the floor, Huge Win gets its own band back.
    spread = logic.big_win_tier(1500, 500, stake=1, top_pct_payout=2500)
    assert spread is not None and spread.header == "🔥 Huge Win"


@pytest.mark.parametrize(
    ("payout", "stake", "announced"),
    [
        pytest.param(2000, 2000, False, id="blackjack-push-returns-the-stake"),
        pytest.param(2000, 4000, False, id="war-retreat-hands-back-half"),
        pytest.param(2000, 1999, True, id="one-coin-up-is-a-win"),
        pytest.param(2000, 100, True, id="an-ordinary-win"),
    ],
)
def test_a_payout_that_is_not_a_win_never_broadcasts(payout, stake, announced):
    """A push returns the stake and a retreat hands back half — both clear a
    500 bar comfortably on payout alone. Gating without the stake announced a
    2,000-coin blackjack push as "🔥 Huge Win": a headline asserting a win
    that did not happen. Same rule ``record_play`` uses to count a win.
    """
    tier = logic.big_win_tier(payout, 500, stake=stake, top_pct_payout=1000)
    assert (tier is not None) is announced


def test_big_win_tier_ping_still_obeys_the_broadcast_bar():
    """A percentile can only ever escalate a broadcast, never create one. A
    guild with the feature switched off stays silent however rare the win."""
    assert logic.big_win_tier(999_999, 0, stake=1, top_pct_payout=1) is None
    assert logic.big_win_tier(499, 500, stake=1, top_pct_payout=1) is None


# ── cap_lines (Discord field-limit guard) ──────────────────────────────


def test_cap_lines_passthrough_when_all_fit():
    lines = ["a", "b", "c"]
    assert logic.cap_lines(lines, limit=1022) == lines


def test_cap_lines_empty():
    assert logic.cap_lines([], limit=1022) == []


def test_cap_lines_caps_and_appends_marker():
    # 200 winner-ish lines of ~50 chars each blow past 1024.
    lines = [f"<@{i:018d}> — Straight {i % 37} · 10,000 → 360,000" for i in range(200)]
    capped = logic.cap_lines(lines, limit=1022)
    body = "\n".join(capped)
    assert len(body) <= 1022, len(body)
    # Real result embed appends "\n​" (2 chars) — stays under 1024.
    assert len(body + "\n​") <= 1024
    assert capped[-1].startswith("*…and ")
    assert capped[-1].endswith(" more*")
    # marker count == number actually dropped
    dropped = 200 - (len(capped) - 1)
    assert f"…and {dropped} more*" in capped[-1]


def test_cap_lines_marker_only_when_overflow():
    lines = ["x" * 500, "y" * 500]
    # Both fit under 1022 (500 + 1 + 500 = 1001) — no marker.
    capped = logic.cap_lines(lines, limit=1022)
    assert capped == lines
    assert not any("…and" in line for line in capped)


# ── mines ──────────────────────────────────────────────────────────────


def _hypergeom_reach(tiles: int, bombs: int, k: int) -> Fraction:
    """P(the first k presses are all safe), written out independently of
    the ladder generator so the EV test is not checking itself."""
    p = Fraction(1)
    for i in range(k):
        p *= Fraction(tiles - bombs - i, tiles - i)
    return p


@pytest.mark.parametrize("bombs", logic.MINES_BOMB_CHOICES)
def test_mines_every_rung_of_every_ladder_is_in_the_design_band(bombs):
    """THE design contract. The player picks the bomb count and picks when
    to stop, so the band has to hold at every cash-out point of every
    configuration — 43 of them — not at one headline number.
    """
    ladder = logic.mines_ladder(bombs)
    for k, rung in enumerate(ladder, start=1):
        reach = _hypergeom_reach(logic.MINES_TILES, bombs, k)
        rtp = reach * Fraction(rung, 100)
        assert Fraction(93, 100) <= rtp <= Fraction(97, 100), (
            f"{bombs} bombs, {k} tiles: {rung / 100}× returns {float(rtp):.4f}"
        )


def test_mines_rungs_are_the_fair_curve_times_the_house_edge():
    """Pins the generator against the hypergeometric written the other way
    round: pay(k) = round(0.95 / P(reach k)), half up."""
    for bombs in logic.MINES_BOMB_CHOICES:
        for k, rung in enumerate(logic.mines_ladder(bombs), start=1):
            fair = 1 / _hypergeom_reach(logic.MINES_TILES, bombs, k)
            expected = int(Fraction(95) * fair + Fraction(1, 2))
            assert rung == expected, f"{bombs} bombs, {k} tiles"


def test_mines_advertised_return_sits_inside_what_the_rungs_actually_pay():
    """MINES_RTP_PCT is quoted at the player; keep it true. Rounding to two
    decimals is the only thing that moves a rung off 95%."""
    rtps = [
        float(_hypergeom_reach(logic.MINES_TILES, bombs, k) * Fraction(rung, 100))
        for bombs in logic.MINES_BOMB_CHOICES
        for k, rung in enumerate(logic.mines_ladder(bombs), start=1)
    ]
    assert min(rtps) * 100 <= logic.MINES_RTP_PCT <= max(rtps) * 100
    assert min(rtps) == pytest.approx(0.9480, abs=0.0005)
    assert max(rtps) == pytest.approx(0.9540, abs=0.0005)


def test_mines_ladders_are_capped_and_shaped_as_designed():
    """All four ceilings land within 19–22× — the property that makes the
    risk options comparable rather than a difficulty ramp."""
    shapes = {
        bombs: (len(logic.mines_ladder(bombs)), logic.mines_ladder(bombs)[-1])
        for bombs in logic.MINES_BOMB_CHOICES
    }
    assert shapes == {1: (19, 1900), 3: (12, 1934), 5: (8, 1860), 10: (4, 2192)}
    for bombs in logic.MINES_BOMB_CHOICES:
        assert all(
            rung <= logic.MINES_MAX_MULT_HUNDREDTHS
            for rung in logic.mines_ladder(bombs)
        )
    # One bomb tops out by clearing the field; nothing runs past the grid.
    assert len(logic.mines_ladder(1)) == logic.MINES_TILES - 1


def test_mines_top_rung_is_reachable_about_one_time_in_twenty():
    """P(top) = 0.95 / pay(top) falls out of the flat edge — so every bomb
    count is roughly a 1-in-20 clear, not a difficulty ladder."""
    for bombs in logic.MINES_BOMB_CHOICES:
        top = logic.mines_top_rung(bombs)
        reach = float(_hypergeom_reach(logic.MINES_TILES, bombs, top))
        assert 0.04 <= reach <= 0.06, f"{bombs} bombs: P(top) = {reach:.4f}"


@pytest.mark.parametrize(
    ("bombs", "revealed", "stake", "expected"),
    [
        (1, 1, 100, 100),    # the 1.00× rung is a push, not a win
        (1, 2, 100, 106),
        (3, 1, 100, 112),
        (10, 4, 1000, 21920),  # the biggest payout the table can hand over
        (1, 4, 5, 6),        # rounds UP at min bet where floor would pay 5
        (5, 1, 36, 46),      # prod's average stake
    ],
)
def test_mines_payout_rounds_half_up(bombs, revealed, stake, expected):
    assert logic.mines_payout(bombs, revealed, stake) == expected


def test_mines_payout_at_min_bet_never_becomes_a_sucker_rung():
    """The test that fails if anyone 'fixes' the rounding back to floor.

    Flooring drops the worst rung to 0.80 at a 5-coin stake; rounding holds
    it at 0.90. Integer payouts can't be exact at tiny stakes, but they must
    not turn a 95% paytable into a 80% one.
    """
    for stake, floor_at in ((5, 0.895), (10, 0.93), (25, 0.94)):
        worst = min(
            float(_hypergeom_reach(logic.MINES_TILES, bombs, k))
            * logic.mines_payout(bombs, k, stake)
            / stake
            for bombs in logic.MINES_BOMB_CHOICES
            for k in range(1, logic.mines_top_rung(bombs) + 1)
        )
        assert worst >= floor_at, f"stake {stake}: worst rung {worst:.4f}"


def test_mines_multiplier_is_dead_on_an_untouched_grid():
    """Cash Out has no rung to pay at zero reveals — a button paying 0.95×
    for doing nothing is a trap, so it pays nothing and the UI disables it."""
    assert logic.mines_multiplier(1, 0) == 0
    assert logic.mines_payout(1, 0, 500) == 0


def test_mines_multiplier_clamps_at_the_top_rung():
    """Defence in depth: the top rung auto-cashes, so a reveal past it
    should be unreachable — if it ever happens it must not index off the
    end of the ladder."""
    for bombs in logic.MINES_BOMB_CHOICES:
        top = logic.mines_top_rung(bombs)
        assert logic.mines_multiplier(bombs, top + 5) == logic.mines_multiplier(
            bombs, top
        )


def test_mines_place_bombs_draws_once_from_the_whole_grid():
    for bombs in logic.MINES_BOMB_CHOICES:
        tiles = logic.mines_place_bombs(bombs)
        assert len(tiles) == bombs
        assert len(set(tiles)) == bombs
        assert tiles == sorted(tiles)
        assert all(0 <= t < logic.MINES_TILES for t in tiles)


def test_mines_place_bombs_uses_module_random(monkeypatch):
    monkeypatch.setattr(
        logic.random, "sample", lambda pop, k: list(range(k))
    )
    assert logic.mines_place_bombs(3) == [0, 1, 2]


def test_mines_rejects_bomb_counts_that_have_no_ladder():
    for bad in (0, 2, 4, 11, 20):
        with pytest.raises(ValueError, match="mines bomb count"):
            logic.mines_ladder(bad)
        with pytest.raises(ValueError, match="mines bomb count"):
            logic.mines_place_bombs(bad)


def test_mines_labels_read_the_way_players_see_them():
    assert logic.mines_mult_label(100) == "1.00×"
    assert logic.mines_mult_label(1934) == "19.34×"
    assert logic.mines_risk_label(1) == "1 bomb · 19 tiles to 19.00×"
    assert logic.mines_risk_label(10) == "10 bombs · 4 tiles to 21.92×"


def test_mines_grid_leaves_room_for_the_cash_out_button():
    """The constraint that chose the grid: Discord allows 25 components per
    message (5 rows × 5), so 25 tiles would leave nowhere to put the stop
    button — and the voluntary stop is the whole game."""
    rows = logic.MINES_TILES / logic.MINES_GRID_WIDTH
    assert rows == int(rows)
    assert int(rows) + 1 <= 5
    assert logic.MINES_TILES + logic.MINES_GRID_WIDTH <= 25
