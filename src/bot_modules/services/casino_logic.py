"""Pure game math for the casino (docs/plans/casino.md).

Every RNG call lives at module level (``random.<fn>``) so tests patch
``bot_modules.services.casino_logic.random`` — the Risky Rolls rule that
keeps the patch point stable.

Payouts are TOTAL RETURN on the stake (stake included), floored to whole
coins; 0 means the stake is simply gone. The paytables are fixed constants,
not settings: the house edge is design, enforced by the RTP tests, not a
knob an admin could turn into a coin printer. Coinflip returns 95%, slots
~93% (see the exact-EV test), roulette is single-zero (~97.3%); blackjack's
edge comes from the rules (dealer stands all 17, 3:2 naturals, double on
two cards, no split).
"""

from __future__ import annotations

import random

# ── Coinflip ───────────────────────────────────────────────────────────

COINFLIP_SIDES = ("heads", "tails")
# ×1.9 total return, expressed as a ratio so payouts stay integer math.
COINFLIP_MULT_NUM = 19
COINFLIP_MULT_DEN = 10


def flip_coin() -> str:
    return random.choice(COINFLIP_SIDES)


def coinflip_payout(stake: int) -> int:
    """Total return on a won flip (floor of 1.9× the stake)."""
    return stake * COINFLIP_MULT_NUM // COINFLIP_MULT_DEN


# ── Slots ──────────────────────────────────────────────────────────────

SEVEN = "7️⃣"
# One weighted reel; three independent pulls. Weights: common meadow
# symbols pay small, the honeypot and the seven are the rare top of the
# table. 26 symbols per reel.
SLOT_REEL: tuple[str, ...] = (
    ("🌻",) * 6 + ("🍀",) * 5 + ("🐝",) * 5 + ("🌾",) * 4
    + ("🦋",) * 3 + ("🍯",) * 2 + (SEVEN,) * 1
)

# Triple payouts (×stake, total return). Precedence: triple > two sevens >
# any non-seven pair; a lone seven pays nothing on its own.
SLOT_TRIPLE_PAYOUT: dict[str, int] = {
    "🌻": 6,
    "🍀": 8,
    "🐝": 9,
    "🌾": 12,
    "🦋": 18,
    "🍯": 40,
    SEVEN: 120,
}
SLOT_TWO_SEVENS_MULT = 5
# Pair pays 1.5×, as a ratio for integer math.
SLOT_PAIR_NUM = 3
SLOT_PAIR_DEN = 2

SLOT_TRIPLE_LABELS: dict[str, str] = {
    "🌻": "A row of sunflowers!",
    "🍀": "Triple clover!",
    "🐝": "The whole hive!",
    "🌾": "A golden harvest!",
    "🦋": "A kaleidoscope of butterflies!",
    "🍯": "THE HONEYPOT!",
    SEVEN: "LUCKY SEVENS — JACKPOT!",
}


def spin_slots() -> tuple[str, str, str]:
    return (
        random.choice(SLOT_REEL),
        random.choice(SLOT_REEL),
        random.choice(SLOT_REEL),
    )


def slots_payout(reels: tuple[str, str, str], stake: int) -> tuple[int, str | None]:
    """(total return, win label) for a spin — (0, None) on a loss."""
    a, b, c = reels
    if a == b == c:
        return stake * SLOT_TRIPLE_PAYOUT[a], SLOT_TRIPLE_LABELS[a]
    if reels.count(SEVEN) == 2:
        return stake * SLOT_TWO_SEVENS_MULT, "Two sevens!"
    for sym in set(reels):
        if sym != SEVEN and reels.count(sym) == 2:
            return stake * SLOT_PAIR_NUM // SLOT_PAIR_DEN, "A matching pair"
    return 0, None


# ── Blackjack ──────────────────────────────────────────────────────────

_RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
_SUITS = ("♠", "♥", "♦", "♣")


def new_deck() -> list[str]:
    """A shuffled single deck, cards as rank+suit strings ("A♠", "10♥")."""
    deck = [rank + suit for rank in _RANKS for suit in _SUITS]
    random.shuffle(deck)
    return deck


def card_value(card: str) -> int:
    rank = card[:-1]
    if rank == "A":
        return 11
    if rank in ("J", "Q", "K"):
        return 10
    return int(rank)


def hand_value(cards: list[str]) -> int:
    """Best blackjack value — aces flex from 11 to 1 while the hand busts."""
    total = sum(card_value(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def is_natural(cards: list[str]) -> bool:
    return len(cards) == 2 and hand_value(cards) == 21


def dealer_play(deck: list[str], dealer: list[str]) -> None:
    """Draw (mutating both lists) until the dealer stands — on all 17s."""
    while hand_value(dealer) < 17:
        dealer.append(deck.pop())


def blackjack_settle(
    player: list[str], dealer: list[str], stake: int
) -> tuple[int, str]:
    """(total return, outcome) once both hands are final.

    ``stake`` is the member's TOTAL stake (a double-down has already folded
    into it). Naturals resolve before a double is possible, so 3:2 only ever
    applies to the original stake. Outcomes: blackjack | win | push | lose |
    bust.
    """
    pv = hand_value(player)
    if pv > 21:
        return 0, "bust"
    if is_natural(player):
        if is_natural(dealer):
            return stake, "push"
        return stake * 5 // 2, "blackjack"
    if is_natural(dealer):
        return 0, "lose"
    dv = hand_value(dealer)
    if dv > 21 or pv > dv:
        return stake * 2, "win"
    if pv == dv:
        return stake, "push"
    return 0, "lose"


# ── streaks, big wins, big bets (the fancy layer's thresholds) ────────

BIG_WIN_MULT = 10  # payout ≥ 10× the stake escalates the celebration
STREAK_CALLOUT_AT = 3  # |streak| ≥ 3 gets the 🔥/🧊 line
# A "big bet" earns the animated reveal: ≥70% of the table max, or ≥100
# coins on an uncapped table. Constants, not knobs — pacing is design.
BIG_BET_NUM = 7
BIG_BET_DEN = 10
BIG_BET_UNCAPPED = 100


def next_streak(streak: int, stake: int, payout: int) -> int:
    """Signed run tracker: wins extend +n, losses extend −n, a push (payout
    exactly returns the stake) resets to 0."""
    if payout > stake:
        return streak + 1 if streak > 0 else 1
    if payout < stake:
        return streak - 1 if streak < 0 else -1
    return 0


def is_big_win(stake: int, payout: int) -> bool:
    return payout >= stake * BIG_WIN_MULT


def is_big_bet(stake: int, max_bet: int) -> bool:
    if max_bet > 0:
        return stake * BIG_BET_DEN >= max_bet * BIG_BET_NUM
    return stake >= BIG_BET_UNCAPPED


# ── Roulette (European single zero) ────────────────────────────────────

RED_NUMBERS = frozenset(
    {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
)

ROULETTE_BET_TYPES = ("red", "black", "dozen", "number")


def wheel_color(n: int) -> str:
    if n == 0:
        return "green"
    return "red" if n in RED_NUMBERS else "black"


def spin_roulette() -> int:
    return random.randint(0, 36)


def roulette_payout(bet_type: str, selection: int, result: int, amount: int) -> int:
    """Total return for one bet against the spun ``result`` (0 = lost).

    Colors pay 2×, dozens 3×, straight numbers 36×; the zero beats every
    color and dozen bet, as a single-zero wheel must.
    """
    if bet_type in ("red", "black"):
        return amount * 2 if wheel_color(result) == bet_type else 0
    if bet_type == "dozen":
        return amount * 3 if result and (result - 1) // 12 + 1 == selection else 0
    if bet_type == "number":
        return amount * 36 if result == selection else 0
    raise ValueError(f"unknown roulette bet type: {bet_type}")


_DOZEN_LABELS = {1: "1–12", 2: "13–24", 3: "25–36"}


def describe_bet(bet_type: str, selection: int) -> str:
    if bet_type == "red":
        return "🔴 Red"
    if bet_type == "black":
        return "⚫ Black"
    if bet_type == "dozen":
        return f"Dozen {_DOZEN_LABELS[selection]}"
    return f"Straight {selection}"
