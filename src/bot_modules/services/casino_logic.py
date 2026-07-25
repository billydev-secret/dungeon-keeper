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

from typing import NamedTuple

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


# ── Meadow Derby (fixed-odds critter race) ─────────────────────────────

class DerbyRunner(NamedTuple):
    emoji: str
    name: str
    weight: int  # win probability out of DERBY_TOTAL_WEIGHT
    mult_num: int  # total-return multiplier as a ratio (integer math)
    mult_den: int


# Favorite pays least, the snail is the moonshot. Weights sum to exactly
# 100 and every runner's RTP sits in the slots band (0.90–0.97) — pinned
# by the exact-EV test, so no runner is a strictly better pick.
DERBY_FIELD: tuple[DerbyRunner, ...] = (
    DerbyRunner("🐇", "Hazel the Hare", 38, 5, 2),
    DerbyRunner("🦔", "Bramble the Hedgehog", 19, 5, 1),
    DerbyRunner("🐝", "Buzz the Bee", 13, 7, 1),
    DerbyRunner("🦋", "Flutter the Butterfly", 12, 8, 1),
    DerbyRunner("🐢", "Sheldon the Tortoise", 10, 19, 2),
    DerbyRunner("🐌", "Turbo the Snail", 8, 12, 1),
)
DERBY_TOTAL_WEIGHT = 100

DERBY_TRACK_LEN = 12
# Two intermediate frames before the finish: with the result edit that is
# three edits per race, staying clear of the shared casino channel's
# ~5-edits/5s bucket even when another game's reveal overlaps.
DERBY_FRAMES = 2


def run_derby() -> int:
    """Draw the winning runner's index, weighted by the field."""
    return random.choices(
        range(len(DERBY_FIELD)), weights=[r.weight for r in DERBY_FIELD]
    )[0]


def derby_payout(runner: int, winner: int, amount: int) -> int:
    """Total return for one bet on ``runner`` (0 = lost)."""
    if runner != winner:
        return 0
    r = DERBY_FIELD[winner]
    return amount * r.mult_num // r.mult_den


def derby_frames(winner: int) -> list[list[int]]:
    """Per-frame track positions for the race show, winner decided first.

    DERBY_FRAMES mid-race frames then the finish: positions only ever
    advance, nobody reaches the line early, and the final frame has the
    winner at DERBY_TRACK_LEN alone in front. Pure cosmetics over an
    already-settled draw — the "money before the first frame" rule.
    """
    n = len(DERBY_FIELD)
    pos = [0] * n
    frames: list[list[int]] = []
    for _ in range(DERBY_FRAMES):
        for i in range(n):
            pos[i] = min(pos[i] + random.randint(1, 3), DERBY_TRACK_LEN - 2)
        frames.append(list(pos))
    final = [min(p + random.randint(1, 3), DERBY_TRACK_LEN - 1) for p in pos]
    final[winner] = DERBY_TRACK_LEN
    frames.append(final)
    return frames


def describe_runner(runner: int) -> str:
    r = DERBY_FIELD[runner]
    return f"{r.emoji} {r.name}"


def derby_odds_label(runner: int) -> str:
    """"2.5×" / "5×" — the odds board's payout column."""
    r = DERBY_FIELD[runner]
    if r.mult_num % r.mult_den == 0:
        return f"{r.mult_num // r.mult_den}×"
    return f"{r.mult_num / r.mult_den:g}×"


def cap_lines(lines: list[str], *, limit: int, more_label: str = "more") -> list[str]:
    """Keep leading lines whose newline-join stays under ``limit``.

    Any dropped tail is summarized with an "…and N more" marker (mirrors the
    bets-open embed). Guards Discord's 1024-char field limit: a round with
    dozens of winners would otherwise 400 the result edit and freeze the
    panel on the fabricated near-miss numbers. The marker reserve uses the
    full ``len(lines)`` digit count, so the actual (smaller) marker always
    fits and the joined result stays within ``limit``.
    """
    total = len(lines)
    kept: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        sep = 1 if kept else 0
        reserve = 0 if i == total - 1 else len(f"\n*…and {total} {more_label}*")
        if used + sep + len(line) + reserve > limit:
            break
        kept.append(line)
        used += sep + len(line)
    if len(kept) < total:
        kept.append(f"*…and {total - len(kept)} {more_label}*")
    return kept


_DOZEN_LABELS = {1: "1–12", 2: "13–24", 3: "25–36"}


def describe_bet(bet_type: str, selection: int) -> str:
    if bet_type == "red":
        return "🔴 Red"
    if bet_type == "black":
        return "⚫ Black"
    if bet_type == "dozen":
        return f"Dozen {_DOZEN_LABELS[selection]}"
    return f"Straight {selection}"


# ── Baccarat (Punto Banco, EZ-Baccarat commission-free) ────────────────
#
# A no-decision windowed game: members back Player / Banker / Tie, both
# hands are dealt by the fixed punto-banco drawing rules, nearest to 9 wins.
# Cards are drawn from an *infinite shoe* — each card independent, uniform
# over the 13 ranks — which is the standard no-removal baccarat model and,
# critically, makes the RTP an EXACT enumeration (values 1–9 land 1/13 each,
# the four 0-valued tens/faces 4/13), pinned by the test rather than sampled.
#
# Paytable (EZ-Baccarat, avoids the fractional 5% Banker commission): Player
# 1:1, Banker 1:1 EXCEPT a Banker win on a three-card total of 7 pushes (the
# "Dragon 7" bar that stands in for the commission), Tie 8:1. Player/Banker
# sit ABOVE the house band (~98.8–99.0% RTP) — baccarat is a low-edge classic
# — with Tie the clearly-labeled high-edge long shot (~85.6%).

BACCARAT_SIDES = ("player", "banker", "tie")
# Tie pays 8:1 → 9× the stake back in total-return terms.
BACCARAT_TIE_MULT = 9


def baccarat_card_value(card: str) -> int:
    """Baccarat pip value: A=1, 2–9 face, 10/J/Q/K=0 (rank+suit string)."""
    rank = card[:-1]
    if rank == "A":
        return 1
    if rank in ("10", "J", "Q", "K"):
        return 0
    return int(rank)


def baccarat_total(cards: list[str]) -> int:
    """A baccarat hand's total — pip sum modulo 10 (0–9)."""
    return sum(baccarat_card_value(c) for c in cards) % 10


def _draw_shoe_card() -> str:
    """One card from the infinite shoe (rank uniform over 13, suit cosmetic).

    Shared by baccarat and war — both use the no-removal shoe model so
    their RTP tests are exact enumerations over independent draws."""
    return random.choice(_RANKS) + random.choice(_SUITS)


def _banker_draws(banker_total: int, player_third: int) -> bool:
    """The punto-banco Banker third-card rule, given the Player's third-card
    pip value. Only consulted when the Player drew a third card and neither
    hand was a natural; a Banker total of 7 always stands here."""
    if banker_total <= 2:
        return True
    if banker_total == 3:
        return player_third != 8
    if banker_total == 4:
        return 2 <= player_third <= 7
    if banker_total == 5:
        return 4 <= player_third <= 7
    if banker_total == 6:
        return 6 <= player_third <= 7
    return False  # 7 stands (8/9 are naturals, handled before this)


def deal_baccarat() -> tuple[list[str], list[str]]:
    """Deal one coup from the infinite shoe; returns (player, banker) cards
    after the fixed draws. No decisions — the drawing tree is deterministic
    given the cards. Draw order: p1 p2 b1 b2 [p3] [b3]."""
    player: list[str] = []
    banker: list[str] = []

    def draw(hand: list[str]) -> int:
        card = _draw_shoe_card()
        hand.append(card)
        return baccarat_card_value(card)

    # Two to each side, then the fixed third-card rules.
    pt = (draw(player) + draw(player)) % 10
    bt = (draw(banker) + draw(banker)) % 10
    if pt < 8 and bt < 8:  # neither a natural → drawing rules apply
        if pt <= 5:
            if _banker_draws(bt, draw(player)):
                draw(banker)
        elif bt <= 5:  # Player stood → Banker draws on 0–5
            draw(banker)
    return player, banker


def baccarat_winner(player: list[str], banker: list[str]) -> str:
    """Which side won the coup — ``"player"`` / ``"banker"`` / ``"tie"``."""
    pt, bt = baccarat_total(player), baccarat_total(banker)
    if pt > bt:
        return "player"
    if bt > pt:
        return "banker"
    return "tie"


def baccarat_payout(
    side: str, player: list[str], banker: list[str], amount: int
) -> int:
    """Total return for one bet on ``side`` against the dealt coup (0 = lost).

    Player/Banker bets push (stake back) on a tie. A Banker win on a
    three-card total of 7 pushes Banker bets (the EZ-Baccarat Dragon-7 bar
    that replaces the 5% commission). Tie pays 8:1.
    """
    if side not in BACCARAT_SIDES:
        raise ValueError(f"unknown baccarat side: {side}")
    winner = baccarat_winner(player, banker)
    if side == "tie":
        return amount * BACCARAT_TIE_MULT if winner == "tie" else 0
    if winner == "tie":
        return amount  # Player/Banker bets push on a tie
    if side != winner:
        return 0
    if side == "banker" and len(banker) == 3 and baccarat_total(banker) == 7:
        return amount  # Dragon-7: a three-card-7 Banker win is barred to a push
    return amount * 2


_BACCARAT_LABELS = {
    "player": "🔵 Player",
    "banker": "🔴 Banker",
    "tie": "🟡 Tie",
}


def describe_baccarat_side(side: str) -> str:
    return _BACCARAT_LABELS[side]


# ── Dice (Sic Bo, Big/Small/Odd/Even) ──────────────────────────────────
#
# Three dice, one roll, everyone settles. v1 keeps the classic even-money
# quartet — Big (11–17), Small (4–10), Odd, Even, each paying 2× total
# return and ALL losing to any triple (the house's tax on the wheel) —
# which lands every bet at exactly 105/216 → 97.22% RTP, already in the
# design band with no bespoke tuning. Exact-total and triple bets are a
# deliberate later iteration (their casino pays are sucker-bet territory
# and need custom in-band math).

SICBO_BET_TYPES = ("big", "small", "odd", "even")
DICE_FACES = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}


def roll_sicbo() -> tuple[int, int, int]:
    return (
        random.randint(1, 6), random.randint(1, 6), random.randint(1, 6)
    )


def sicbo_payout(bet_type: str, dice: tuple[int, int, int], amount: int) -> int:
    """Total return for one bet against the rolled ``dice`` (0 = lost).

    Every v1 bet pays even money and loses to any triple — that exclusion
    IS the house edge (2.78%).
    """
    if bet_type not in SICBO_BET_TYPES:
        raise ValueError(f"unknown dice bet type: {bet_type}")
    a, b, c = dice
    if a == b == c:
        return 0
    total = a + b + c
    won = (
        total >= 11 if bet_type == "big"
        else total <= 10 if bet_type == "small"
        else total % 2 == 1 if bet_type == "odd"
        else total % 2 == 0
    )
    return amount * 2 if won else 0


_SICBO_LABELS = {
    "big": "⬆️ Big (11–17)",
    "small": "⬇️ Small (4–10)",
    "odd": "1️⃣ Odd",
    "even": "2️⃣ Even",
}


def describe_sicbo_bet(bet_type: str) -> str:
    return _SICBO_LABELS[bet_type]


def dice_faces(dice: tuple[int, int, int]) -> str:
    """"⚃ ⚅ ⚀" — the roll as die faces."""
    return " ".join(DICE_FACES[d] for d in dice)


# ── Casino War ─────────────────────────────────────────────────────────
#
# One card each, high card wins even money — the fastest game in the
# canon. On the ~1/13 tie the member chooses: **go to war** (stake a
# matching raise; win OR tie the war card and the raise pays even while
# the original pushes — 3× total return on the doubled stake) or
# **retreat** (surrender half). Cards come from the infinite shoe (rank
# uniform /13, the baccarat model), so the RTP is exact: always-war
# 177/182 ≈ 97.25%, always-retreat 25/26 ≈ 96.15% — both in band, war
# strictly better, no Tie side bet (its ~19% edge has no place here).

WAR_ACTIONS = ("war", "retreat")


def war_rank(card: str) -> int:
    """Aces high, suits meaningless: 2 → 2 … K → 13, A → 14."""
    rank = card[:-1]
    if rank == "A":
        return 14
    if rank == "J":
        return 11
    if rank == "Q":
        return 12
    if rank == "K":
        return 13
    return int(rank)


def draw_war_cards() -> tuple[str, str]:
    """(member's card, dealer's card) from the infinite shoe."""
    return _draw_shoe_card(), _draw_shoe_card()


def war_payout(player: str, dealer: str, stake: int) -> int | None:
    """Total return for the opening cards — None means a tie (the member
    chooses war or retreat; nothing settles yet)."""
    if war_rank(player) > war_rank(dealer):
        return stake * 2
    if war_rank(player) < war_rank(dealer):
        return 0
    return None


def war_raise_payout(player: str, dealer: str, doubled_stake: int) -> int:
    """Total return once the war cards land, on the doubled stake.

    A win **or second tie** takes it: the raise pays even money and the
    original pushes — 3× the original bet, which is exactly 3/2 of the
    doubled stake."""
    if war_rank(player) >= war_rank(dealer):
        return doubled_stake * 3 // 2
    return 0


def war_retreat_payout(stake: int) -> int:
    """Retreat surrenders half (floored — the house keeps the odd coin)."""
    return stake // 2


# ── Keno (bespoke paytable — NOT casino keno) ──────────────────────────
#
# 20 of 80 numbers drawn once per communal round; a ticket is a quick-
# picked set of 4/6/8/10 spots paying by catch count. Real casino keno
# returns a punishing 65–75% — these paytables are built from scratch on
# the exact hypergeometric P(catch k) = C(t,k)·C(80−t,20−k)/C(80,20) to
# land every tier at ~94–96%, pinned by the EV test. The pays are total
# return (×stake); the low tiers give frequent money-back moments, the
# top catches stay splashy (a Pick-8 solid ticket pays 5000×).

KENO_POOL = 80
KENO_DRAWN = 20
KENO_TIERS = (4, 6, 8, 10)

KENO_PAYTABLE: dict[int, dict[int, int]] = {
    4: {2: 2, 3: 8, 4: 60},                                    # RTP 95.51%
    6: {2: 1, 3: 2, 4: 8, 5: 30, 6: 500},                      # RTP 95.36%
    8: {3: 1, 4: 3, 5: 12, 6: 70, 7: 500, 8: 5000},            # RTP 94.66%
    10: {4: 1, 5: 4, 6: 20, 7: 130, 8: 1000, 9: 4000, 10: 5000},  # RTP 95.25%
}


def keno_quick_pick(spots: int) -> list[int]:
    """A sorted quick-pick ticket of ``spots`` numbers from 1–80."""
    return sorted(random.sample(range(1, KENO_POOL + 1), spots))


def draw_keno() -> list[int]:
    """The round's 20 drawn numbers, sorted."""
    return sorted(random.sample(range(1, KENO_POOL + 1), KENO_DRAWN))


def keno_catches(picks: list[int], drawn: list[int]) -> int:
    return len(set(picks) & set(drawn))


def keno_payout(picks: list[int], drawn: list[int], amount: int) -> int:
    """Total return for one ticket against the draw (0 = lost)."""
    tier = KENO_PAYTABLE.get(len(picks))
    if tier is None:
        raise ValueError(f"unknown keno tier: {len(picks)} spots")
    return amount * tier.get(keno_catches(picks, drawn), 0)


def describe_keno_ticket(picks: list[int]) -> str:
    """"Pick-6 · 4 12 33 41 56 78" — the bets board / recap line."""
    return f"Pick-{len(picks)} · {' '.join(str(n) for n in picks)}"
