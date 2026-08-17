"""Pure game math for the casino (docs/plans/casino.md).

Every RNG call lives at module level (``random.<fn>``) so tests patch
``bot_modules.services.casino_logic.random`` — the Risky Rolls rule that
keeps the patch point stable.

Payouts are TOTAL RETURN on the stake (stake included), floored to whole
coins; 0 means the stake is simply gone. **Mines is the one exception — it
rounds half up**, because it is the only table paying a ladder of very
small multipliers and flooring one of those at the minimum bet drops an
in-band rung to 80% RTP; see ``mines_payout``. The paytables are fixed constants,
not settings: the house edge is design, enforced by the RTP tests, not a
knob an admin could turn into a coin printer. Coinflip returns 92.5%, slots
~91.3% (see the exact-EV test), roulette is single-zero (~97.3%);
blackjack's edge comes from the rules (dealer stands all 17, 3:2 naturals,
double on two cards, no split).

Coinflip and slots were trimmed ~2 points on 2026-07-26 (from 95% / 93.3%)
to lean the two tables carrying ~70% of the handle. Both cuts were placed
where they do not show on a single play — see the constants. Blackjack was
deliberately left alone: the standard lever there is paying naturals 6:5
instead of 3:2, which is the one change players recognize on sight and
resent, and it buys under half a point of blended edge.
"""

from __future__ import annotations

import random

from fractions import Fraction
from typing import NamedTuple

# ── Coinflip ───────────────────────────────────────────────────────────

COINFLIP_SIDES = ("heads", "tails")
# ×1.85 total return, expressed as a ratio so payouts stay integer math.
# Was ×1.9 (95% RTP) until 2026-07-26; the extra half-point of edge is
# invisible on a single flip and still reads as "nearly double or nothing".
COINFLIP_MULT_NUM = 37
COINFLIP_MULT_DEN = 20
# Advertised return for the How It Works embed. Pinned against the exact
# enumeration by the RTP tests, so the copy can never drift from the
# paytable it describes — an advertised edge that isn't the real one is the
# gambling equivalent of a preference that isn't enforced.
COINFLIP_RTP_PCT = 92.5


def mult_text(num: int, den: int) -> str:
    """A payout ratio as compact player-facing text: 37/20 -> "1.85"."""
    return f"{num / den:g}"


def flip_coin() -> str:
    return random.choice(COINFLIP_SIDES)


def coinflip_payout(stake: int) -> int:
    """Total return on a won flip (floor of 1.85× the stake)."""
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
# Pair pays 1.45×, as a ratio for integer math. This one line carries most
# of the slots edge: a pair lands on 41.4% of spins, so the pair multiplier
# contributes ~0.60 of the ~0.91 RTP and every other symbol combined
# contributes the rest. Trimmed from 1.5× on 2026-07-26 — on a 100-coin bet
# that is 145 back instead of 150, imperceptible per spin, ~2 points of RTP
# in aggregate. Take slots edge here rather than off the triples: the
# triples are what players remember, and shaving a jackpot line is felt.
SLOT_PAIR_NUM = 29
SLOT_PAIR_DEN = 20
# Advertised return — pinned to the enumeration by the RTP test, as above.
SLOTS_RTP_PCT = 91.3

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


# The public broadcast's header escalates with how far the payout clears the
# guild's bar, so a rare haul doesn't read the same as a routine one. Steps
# are MULTIPLES of ``broadcast_min_payout``, not coin amounts: the dial is
# the only thing that knows what "big" is worth in a given economy (see
# memory: guild "nut" runs ~8× The Golden Meadow's denomination).
#
# Deliberately no "Jackpot" tier — the casino already has a real progressive
# jackpot with its own celebration embed, and reusing the word for a merely
# large win would make the genuine article stop landing.
#
# The ladder is TWO rungs, not three, and the multiples are small. A 10× rung
# shipped on 2026-08-15 and was measured against prod the same day: The Golden
# Meadow has paid 4,350 winning bets on an average stake of 36 coins, and its
# largest single win ever is 3,000 against a 500 bar — 6×. A 10× rung could
# never have rendered. Rungs are sized to what the economy actually pays, and
# the top of the ladder is the percentile, which resizes itself.
BIG_WIN_TIERS: tuple[tuple[int, str], ...] = (
    (3, "🔥 Huge Win"),
    (1, "💰 Big Win"),
)
# Above the ladder sits the one that pings the channel: a payout in the top 3%
# of the wins this guild has actually ANNOUNCED lately — casino_win_history
# banks only broadcast-clearing wins, so the percentile ranks the population a
# player would recognise as big. Ranking all wins instead put the mark below
# the broadcast bar (most wins are ~52-coin pair payouts), which left the floor
# deciding everything and the percentile contributing nothing.
LEGENDARY_HEADER = "💎 Legendary Win"
LEGENDARY_LEAD = "**One of the biggest payouts this casino has ever handed over.**"
# The ping can never fire below the ladder's top rung: a guild whose announced
# wins all cluster near its bar would otherwise have a percentile barely over
# that bar and would ping on every broadcast.
LEGENDARY_MIN_MULT = BIG_WIN_TIERS[0][0]


class BigWinTier(NamedTuple):
    header: str  # embed title prefix, before " — {game}"
    lead: str | None  # extra first line of the description, loudest tier only
    ping: bool  # whether the broadcast carries an @here


def big_win_tier(
    payout: int,
    threshold: int,
    *,
    stake: int,
    top_pct_payout: int | None = None,
) -> BigWinTier | None:
    """The broadcast tier for ``payout``, or None when it stays private.

    None means "don't broadcast" — a payout under the bar, a bar of 0 (the
    guild's off switch), or a payout that isn't a win at all. Callers treat
    None as the whole decision; there is no second check anywhere.

    ``stake`` is why this needs more than the payout. A **push returns the
    stake**: blackjack pushes, baccarat Player/Banker bets push on a tie, and
    a war retreat hands back half. Gating on ``payout >= threshold`` alone,
    a 2,000-coin blackjack push against a 500 bar cleared 3× and announced
    itself as "🔥 Huge Win" — a headline asserting a win that did not happen,
    for a hand that won nothing. Same rule the service already used to decide
    what counts as a win (``record_play``'s ``payout > stake``); the two must
    not disagree, or the broadcast advertises what the stats refuse to count.

    ``top_pct_payout`` is the guild's top-3% mark over its ANNOUNCED wins, or
    None when there isn't enough history to rank against. None can only ever
    *withhold* the ping: an unknown percentile is never treated as a passing
    one, and it can never create a broadcast the dial switched off.

    **Legendary supersedes the rung it lands on** rather than sitting above
    it as a fourth step, and that is a deliberate accepted cost. The ping is
    the larger of the percentile and ``LEGENDARY_MIN_MULT``× the bar, so when
    a guild's percentile sits at or under that floor the two conditions
    coincide and 🔥 Huge Win is subsumed — the ladder is then 💰 Big Win plus
    the ping. The alternative, a strict inequality that reserves a sliver of
    range for Huge Win, buys a rung nobody would ever see fire. Whichever is
    loudest and true wins; the floor is what stops "loudest and true" from
    being every broadcast.
    """
    if threshold <= 0 or payout < threshold or payout <= stake:
        return None
    if top_pct_payout is not None and payout >= max(
        top_pct_payout, threshold * LEGENDARY_MIN_MULT
    ):
        return BigWinTier(LEGENDARY_HEADER, LEGENDARY_LEAD, True)
    for mult, header in BIG_WIN_TIERS:
        if payout >= threshold * mult:
            return BigWinTier(header, None, False)
    # Unreachable: the 1× row always matches above the bar. Kept explicit so a
    # future edit to the ladder can't silently start returning None here.
    return BigWinTier(BIG_WIN_TIERS[-1][1], None, False)


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


def keno_pay_threshold(spots: int) -> int:
    """Fewest catches that return anything on a ``spots`` ticket."""
    tier = KENO_PAYTABLE.get(spots)
    if tier is None:
        raise ValueError(f"unknown keno tier: {spots} spots")
    return min(tier)


def describe_keno_result(picks: list[int], drawn: list[int], payout: int) -> str:
    """One settled ticket: the picks with hits **bolded**, the catch count,
    and — on a losing ticket only — the line it needed to reach.

    A losing ticket is the whole point of this: "caught 3" beside the board
    is what tells a member her Pick-10 fell one short, instead of leaving a
    near miss indistinguishable from an unpaid win. Winners already show
    stake → payout, so the threshold would only be noise there.

    The threshold is read off KENO_PAYTABLE, never hardcoded, so it cannot
    drift from what keno_payout actually pays. Tiers whose lowest paying
    catch returns 1× (Pick-6 at 2, Pick-8 at 3, Pick-10 at 4) say "returns
    your stake" rather than "pays" — promising a *win* at a break-even
    tier is the next complaint waiting to happen.
    """
    hits = set(picks) & set(drawn)
    board = " ".join(f"**{n}**" if n in hits else str(n) for n in picks)
    line = f"Pick-{len(picks)} · {board} · caught {len(hits)}"
    if payout > 0:
        return line
    need = keno_pay_threshold(len(picks))
    verb = (
        "returns your stake"
        if KENO_PAYTABLE[len(picks)][need] == 1
        else "pays"
    )
    return f"{line} · {need} {verb}"


# ── Mines ──────────────────────────────────────────────────────────────

# A 20-tile grid (5 wide × 4 tall) hiding a player-chosen number of bombs.
# Each safe reveal steps the multiplier up; cashing out banks it; a bomb
# takes the lot. 5×5 is not an option — Discord allows 25 components per
# message, so a 25-tile grid leaves nowhere to put the Cash Out button, and
# the voluntary stop is the entire game (docs/plans/casino-mines.md).

MINES_TILES = 20
MINES_GRID_WIDTH = 5
MINES_BOMB_CHOICES = (1, 3, 5, 10)

# One house edge applied to the whole surface: the paid multiplier is
# 0.95 × the fair one, so P(reach k) × pay(k) = 0.95 identically and EVERY
# cash-out point on EVERY ladder returns the same 95% before rounding.
# Rungs are stored in HUNDREDTHS so the money path stays integer-only —
# two decimals is also all a player-facing "1.06×" can carry.
MINES_RTP_PCT = 95.0
_MINES_RTP_NUM = 95

# The ladder stops at the last rung paying no more than this. Uncapped, a
# 10-bomb full clear pays 175,518× — not a paytable but an unbounded
# liability against a five-figure float. The cap bends no RTP (every rung
# it keeps is paid at its own in-band multiplier; the rung it drops is
# simply not offered) and lands all four bomb counts within 18.6–21.9×, each
# reachable ~5% of the time. The risk dial changes the road, not the
# destination: 19 nervous presses or 4 brutal ones.
MINES_MAX_MULT_HUNDREDTHS = 2500


def _mines_ladder(bombs: int, *, tiles: int = MINES_TILES) -> tuple[int, ...]:
    """Generate one bomb count's rungs, in hundredths.

    ``fair(k) = C(n,k)/C(n−m,k) = Π (n−i)/(n−m−i)`` for i in 0..k−1, paid
    at ``round(0.95 × fair × 100)`` half up, stopping at the last rung
    within the cap.

    Generated rather than hand-typed on purpose: 43 hand-typed numbers is
    43 chances to fat-finger the house edge, and the enumeration test would
    then be checking a typo against itself. The test writes the
    hypergeometric out independently and asserts against this.
    """
    ladder: list[int] = []
    fair = Fraction(1)
    for i in range(tiles - bombs):
        fair *= Fraction(tiles - i, tiles - bombs - i)
        rung = int(Fraction(_MINES_RTP_NUM) * fair + Fraction(1, 2))
        if rung > MINES_MAX_MULT_HUNDREDTHS:
            break
        ladder.append(rung)
    return tuple(ladder)


MINES_LADDERS: dict[int, tuple[int, ...]] = {
    bombs: _mines_ladder(bombs) for bombs in MINES_BOMB_CHOICES
}


def mines_ladder(bombs: int) -> tuple[int, ...]:
    ladder = MINES_LADDERS.get(bombs)
    if ladder is None:
        raise ValueError(f"unknown mines bomb count: {bombs}")
    return ladder


def mines_top_rung(bombs: int) -> int:
    """Safe tiles needed to top the ladder out — which auto-cashes."""
    return len(mines_ladder(bombs))


def mines_multiplier(bombs: int, revealed: int) -> int:
    """The rung standing after ``revealed`` safe tiles, in hundredths.

    0 = no rung yet, which is why Cash Out is dead on an untouched grid: a
    button paying 0.95× for doing nothing is a trap, not a choice.
    """
    ladder = mines_ladder(bombs)
    if revealed <= 0:
        return 0
    return ladder[min(revealed, len(ladder)) - 1]


def mines_payout(bombs: int, revealed: int, stake: int) -> int:
    """Total return for cashing out after ``revealed`` safe tiles.

    ROUNDS half up where every other table floors, and the deviation is
    arithmetic rather than taste. Mines is the only game paying a ladder of
    very small multipliers: flooring a 1.19× rung on a 5-coin stake pays 5
    against a 5.95 expectation, collapsing that cash-out point to 80% RTP
    for a paytable that is exactly 95% on paper. The band is a promise
    about what a player actually receives, so it outranks the convention.
    Costs the house a fraction of a coin per small win, and can round a
    small rung *up* — player-favourable, the correct direction to err.
    """
    mult = mines_multiplier(bombs, revealed)
    if mult <= 0:
        return 0
    return (stake * mult + 50) // 100


def mines_place_bombs(bombs: int, *, tiles: int = MINES_TILES) -> list[int]:
    """Bomb tile indices, drawn ONCE at deal and never re-drawn.

    The alternative — roll each tile as it is pressed — is equally natural
    to write and statistically identical, and would leave the house holding
    a lever it should not have. Pre-committing is the version that cannot
    quietly grow adaptive difficulty later.
    """
    if bombs not in MINES_BOMB_CHOICES:
        raise ValueError(f"unknown mines bomb count: {bombs}")
    return sorted(random.sample(range(tiles), bombs))


def mines_mult_label(mult: int) -> str:
    """106 → "1.06×" — the multiplier as players read it."""
    return f"{mult // 100}.{mult % 100:02d}×"


def mines_risk_label(bombs: int) -> str:
    """"5 bombs · 8 tiles to 18.60×" — the risk picker's button.

    Says what the choice costs and what it tops out at before any money
    moves, so the shape of the bet is visible at the point of choosing.
    """
    ladder = mines_ladder(bombs)
    tiles = "tile" if len(ladder) == 1 else "tiles"
    bomb_word = "bomb" if bombs == 1 else "bombs"
    return (
        f"{bombs} {bomb_word} · {len(ladder)} {tiles} "
        f"to {mines_mult_label(ladder[-1])}"
    )
