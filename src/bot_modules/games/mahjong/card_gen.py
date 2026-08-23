"""Candidate hand generation and card selection — stage 3 of
docs/plans/mahjong-card-generator.md.

Three steps, deliberately separate so each can be inspected on its own:

1. :func:`candidates` enumerates hands from a **motif grammar** — the
   familiar American section vocabulary (a year, all-evens, all-odds, 369,
   consecutive runs, like numbers, winds and dragons, quints, singles and
   pairs). Motifs are mechanics, which nobody owns; what a published card
   owns is its *selection and arrangement*, which is precisely the part
   steps 2 and 3 compute for us and step 4 (a person) signs off on.
2. Every candidate goes through the real linter before it may enter the
   pool, so the generator is structurally incapable of emitting a hand the
   engine cannot play, and duplicates are collapsed by the same
   suit-relabeling-invariant signature the linter warns on.
3. :func:`select` picks a card out of the pool against the properties a
   card has to have: section quotas, demand spread, and — the one that
   matters most — **pivot paths**, so a blocked player is looking at a
   detour rather than a dead hand.

What this module deliberately does *not* do is decide values. A hand's
price should come from its measured completion rate (`sim_logic`), not from
a guess made at generation time; :func:`provisional_value` exists only so
the pool is playable enough to *be* measured, and stage 4 overwrites it.

Everything here is pure and seeded: same inputs, same card.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from itertools import combinations

from bot_modules.games.mahjong.card_logic import (
    HAND_TILES,
    VALUE_MAX,
    VALUE_MIN,
    Card,
    Hand,
    canonical_shape,
    lint_card_data,
    load_card,
)

#: Card suit letters, in the order the canonicaliser prefers them.
LETTERS = ("a", "b", "c")

EVENS = (2, 4, 6, 8)
ODDS = (1, 3, 5, 7, 9)
THREES = (3, 6, 9)

#: Section names. Generic category vocabulary — the words players already
#: use — not anyone's selection of hands.
S_YEAR = "Year"
S_EVENS = "Evens"
S_ODDS = "Odds"
S_369 = "369"
S_RUNS = "Consecutive Run"
S_LIKE = "Like Numbers"
S_HONORS = "Winds & Dragons"
S_QUINTS = "Quints"
S_PAIRS = "Singles & Pairs"

SECTIONS = (
    S_YEAR, S_EVENS, S_ODDS, S_369, S_RUNS,
    S_LIKE, S_HONORS, S_QUINTS, S_PAIRS,
)

#: Tail groups used to pad a motif core out to fourteen tiles, by size. Kept
#: small and honour-flavoured: the core carries a hand's identity, and the
#: filler should not quietly turn every line into a numbers hand.
_FILLERS: dict[int, tuple[tuple[dict, ...], ...]] = {
    2: (
        ({"count": 2, "rank": "F"},),
        ({"count": 2, "rank": "D", "suit": "a"},),
        ({"count": 2, "rank": "N"},),
    ),
    3: (
        ({"count": 3, "rank": "F"},),
        ({"count": 3, "rank": "D", "suit": "a"},),
        ({"count": 3, "rank": "R"},),
    ),
    4: (
        ({"count": 4, "rank": "F"},),
        ({"count": 4, "rank": "D", "suit": "a"},),
        ({"count": 4, "rank": "D"},),
        ({"count": 4, "rank": "N"},),
    ),
    5: (
        ({"count": 2, "rank": "F"}, {"count": 3, "rank": "D", "suit": "a"}),
        ({"count": 5, "rank": "F"},),
    ),
    6: (
        ({"count": 6, "rank": "F"},),
        ({"count": 3, "rank": "R"}, {"count": 3, "rank": "G"}),
        ({"count": 2, "rank": "F"}, {"count": 4, "rank": "D", "suit": "a"}),
    ),
    7: (
        ({"count": 3, "rank": "F"}, {"count": 4, "rank": "D", "suit": "a"}),
    ),
    8: (
        ({"count": 4, "rank": "N"}, {"count": 4, "rank": "S"}),
        ({"count": 4, "rank": "F"}, {"count": 4, "rank": "D", "suit": "a"}),
    ),
}

#: Fillers for a hand that must stay all-pairs-and-singles. A group of 3+
#: in that section would be a lie twice over: the name says pairs, and the
#: linter would then rightly let the line be exposed.
_PAIR_FILLERS: dict[int, tuple[tuple[dict, ...], ...]] = {
    2: (
        ({"count": 2, "rank": "F"},),
        ({"count": 2, "rank": "N"},),
        ({"count": 2, "rank": "D", "suit": "a"},),
    ),
    4: (
        ({"count": 2, "rank": "F"}, {"count": 2, "rank": "N"}),
        ({"count": 2, "rank": "R"}, {"count": 2, "rank": "G"}),
        ({"count": 2, "rank": "F"}, {"count": 2, "rank": "D", "suit": "a"}),
    ),
    6: (
        ({"count": 2, "rank": "F"}, {"count": 2, "rank": "R"},
         {"count": 2, "rank": "G"}),
        ({"count": 2, "rank": "N"}, {"count": 2, "rank": "E"},
         {"count": 2, "rank": "W"}),
    ),
}

#: Count patterns a run of k number-groups may take, each summing to ≤ 14.
_COUNT_PATTERNS: dict[int, tuple[tuple[int, ...], ...]] = {
    2: ((4, 4), (5, 5), (4, 6), (3, 3)),
    3: ((4, 4, 4), (3, 3, 3), (4, 4, 2), (5, 4, 5), (3, 4, 3), (2, 4, 4)),
    4: ((4, 4, 4, 2), (3, 3, 4, 4), (2, 4, 4, 4), (3, 3, 3, 3), (2, 2, 4, 4)),
    5: ((2, 3, 4, 3, 2), (2, 2, 2, 4, 4), (3, 3, 2, 3, 3), (2, 2, 2, 2, 2)),
}


def _suit_patterns(k: int) -> list[tuple[str, ...]]:
    """Every assignment of k groups to suit letters, canonicalised so the
    first group is always ``a`` and a new letter is only ever the next
    unused one. Without that, ``abc`` and ``bca`` would both be generated
    and then collapsed later — cheaper to never make them."""
    out: list[tuple[str, ...]] = []

    def walk(prefix: tuple[str, ...], used: int) -> None:
        if len(prefix) == k:
            out.append(prefix)
            return
        for i in range(min(used + 1, len(LETTERS))):
            walk((*prefix, LETTERS[i]), max(used, i + 1))

    walk((), 0)
    return out


def _pad(core: list[dict], *, pairs_only: bool = False) -> list[list[dict]]:
    """Complete a core to fourteen tiles with each filler that fits."""
    short = HAND_TILES - sum(g["count"] for g in core)
    if short == 0:
        return [core]
    if short < 0:
        return []
    table = _PAIR_FILLERS if pairs_only else _FILLERS
    return [core + list(filler) for filler in table.get(short, ())]


def _numbers_cores(numbers: tuple[int, ...], sizes: tuple[int, ...]):
    """Pungs/kongs of a fixed number set — the 2468 / 13579 / 369 shape."""
    for size in sizes:
        if size > len(numbers):
            continue
        for chosen in combinations(numbers, size):
            for counts in _COUNT_PATTERNS.get(size, ()):
                for suits in _suit_patterns(size):
                    yield [
                        {"count": c, "rank": str(n), "suit": s}
                        for c, n, s in zip(counts, chosen, suits)
                    ]


def _run_cores():
    """Consecutive runs: x, x+1, … as a rank variable, so the line plays at
    any starting number the player's tiles allow."""
    for size in (3, 4, 5):
        ranks = ["x"] + [f"x+{i}" for i in range(1, size)]
        for counts in _COUNT_PATTERNS.get(size, ()):
            for suits in _suit_patterns(size):
                yield [
                    {"count": c, "rank": r, "suit": s}
                    for c, r, s in zip(counts, ranks, suits)
                ]


def _like_cores():
    """One number, repeated across suits — 'like numbers'."""
    for counts in ((4, 4, 4), (5, 5, 4), (3, 3, 3), (4, 4, 2), (5, 4, 3)):
        yield [
            {"count": c, "rank": "x", "suit": s}
            for c, s in zip(counts, LETTERS)
        ]


def _year_cores(year: str):
    """The year, as four singles in a row and as a group per digit. ``0``
    is soap and suitless (§2.1's soap-doubles-as-zero)."""
    def digit(count: int, d: str, suit: str) -> dict:
        return ({"count": count, "rank": "0"} if d == "0"
                else {"count": count, "rank": d, "suit": suit})

    digits = list(year)
    # the year written out, once or twice, as singles
    for repeats in (1, 2):
        for suits in _suit_patterns(1):
            yield [digit(1, d, suits[0]) for d in digits] * repeats
    # a group per digit
    for counts in ((3, 3, 4, 4), (4, 4, 3, 3), (2, 4, 4, 4), (3, 3, 3, 3)):
        for suits in _suit_patterns(len(digits)):
            yield [
                digit(c, d, s) for c, d, s in zip(counts, digits, suits)
            ]


def _honor_cores():
    """Winds and dragons — the one family that needs no suit at all."""
    winds = ("N", "E", "W", "S")
    for counts in ((4, 3, 3, 4), (3, 4, 4, 3), (2, 2, 2, 2), (4, 4, 3, 3)):
        yield [{"count": c, "rank": w} for c, w in zip(counts, winds)]
    for counts in ((4, 4, 4), (5, 5, 4), (3, 3, 3), (4, 4, 2)):
        yield [
            {"count": c, "rank": d}
            for c, d in zip(counts, ("R", "G", "soap"))
        ]
    # winds plus a matched dragon riding one suit
    for wind_counts in ((4, 4), (3, 3)):
        for numbers in (2, 5, 8):
            yield [
                {"count": wind_counts[0], "rank": "N"},
                {"count": wind_counts[1], "rank": "S"},
                {"count": 3, "rank": str(numbers), "suit": "a"},
                {"count": 14 - sum(wind_counts) - 3, "rank": "D", "suit": "a"},
            ]


def _quint_cores():
    """Groups of five — only possible with jokers, which is the point."""
    for second in (4, 5):
        for suits in _suit_patterns(3):
            yield [
                {"count": 5, "rank": "x", "suit": suits[0]},
                {"count": second, "rank": "x+1", "suit": suits[1]},
                {"count": 14 - 5 - second, "rank": "x+2", "suit": suits[2]},
            ]
    for numbers in (EVENS, ODDS):
        for chosen in combinations(numbers, 3):
            yield [
                {"count": 5, "rank": str(chosen[0]), "suit": "a"},
                {"count": 5, "rank": str(chosen[1]), "suit": "a"},
                {"count": 4, "rank": str(chosen[2]), "suit": "b"},
            ]


def _pairs_cores():
    """All pairs and singles: concealed by construction, since a group of
    two can never be called (§2.5) — the linter enforces exactly that."""
    for numbers in (EVENS, ODDS, THREES):
        for size in (5, 6, 7):
            if size > len(numbers) + 2:
                continue
            chosen = numbers[:min(size, len(numbers))]
            core = [
                {"count": 2, "rank": str(n), "suit": "a"} for n in chosen
            ]
            yield core
            yield [
                {"count": 2, "rank": str(n), "suit": s}
                for n, s in zip(chosen, ("a", "a", "b", "b", "c", "c", "a"))
            ]
    # a consecutive run of pairs
    for size in (5, 6):
        yield [
            {"count": 2, "rank": "x" if i == 0 else f"x+{i}", "suit": "a"}
            for i in range(min(size, 5))
        ]


def _section_cores(year: str):
    yield from ((S_YEAR, c) for c in _year_cores(year))
    yield from ((S_EVENS, c) for c in _numbers_cores(EVENS, (3, 4)))
    yield from ((S_ODDS, c) for c in _numbers_cores(ODDS, (3, 4, 5)))
    yield from ((S_369, c) for c in _numbers_cores(THREES, (2, 3)))
    yield from ((S_RUNS, c) for c in _run_cores())
    yield from ((S_LIKE, c) for c in _like_cores())
    yield from ((S_HONORS, c) for c in _honor_cores())
    yield from ((S_QUINTS, c) for c in _quint_cores())
    yield from ((S_PAIRS, c) for c in _pairs_cores())


# ── Scoring a candidate ──────────────────────────────────────────────────────


def provisional_value(hand: Hand) -> int:
    """A placeholder price so the pool can be *played*; stage 4 replaces it
    with one derived from the measured completion rate. Structural only:
    concealed lines, uncallable small groups, extra suits and quints all
    make a line harder, so all four cost more."""
    value = VALUE_MIN
    if hand.concealed:
        value += 10
    value += 5 * sum(1 for g in hand.groups if not g.takes_jokers)
    value += 5 * (len({g.suit for g in hand.groups if g.suit} or {None}) - 1)
    value += 5 * sum(1 for g in hand.groups if g.count >= 5)
    return max(VALUE_MIN, min(VALUE_MAX, value - value % 5))


def _tokens(hand: Hand) -> Counter[str]:
    """A hand's appetite as ``rank@suit`` tokens with their counts — the
    binding-free stand-in for "which tiles does this line want".

    Binding-free is the honest limit: the physical tiles depend on the suit
    map a player picks, so no card-time reading can be exact. It is good
    enough for spread and overlap, and `sim_logic` measures the real thing.
    """
    out: Counter[str] = Counter()
    for g in hand.groups:
        out[f"{g.rank}@{g.suit or ''}"] += g.count
    return out


def _token_overlap(a: Counter[str], b: Counter[str]) -> int:
    return sum((a & b).values())


def overlap(a: Hand, b: Hand) -> int:
    """Tiles two lines want in common — the pivot-path metric. High overlap
    means a player part-way to ``a`` is also part-way to ``b``."""
    return _token_overlap(_tokens(a), _tokens(b))


@dataclass(frozen=True)
class Candidate:
    """One generated hand that survived the linter.

    ``tokens`` and ``key`` are cached rather than derived on demand: the
    selector compares every remaining candidate against every chosen hand
    at every pick, so recomputing them turned selection into the slowest
    thing in the module by an order of magnitude.
    """

    hand: Hand
    shape: tuple
    tokens: Counter[str]
    key: tuple

    @property
    def section(self) -> str:
        return self.hand.section


def candidates(*, year: str = "2026") -> list[Candidate]:
    """Every lint-clean hand the motif grammar can make, deduped by shape.

    Deterministic: the enumeration order is the section order, and the first
    spelling of a shape wins.
    """
    seen: set[tuple] = set()
    pool: list[Candidate] = []
    counter = 0
    for section, core in _section_cores(year):
        for groups in _pad(list(core), pairs_only=section == S_PAIRS):
            counter += 1
            hand_id = f"g{counter:04d}"
            concealed = all(g["count"] <= 2 for g in groups)
            data = {
                "card_id": "candidates", "display_name": "Candidates",
                "season": year,
                "hands": [{
                    "id": hand_id, "section": section, "name": hand_id,
                    "concealed": concealed, "value": VALUE_MIN,
                    "groups": groups,
                }],
            }
            if not lint_card_data(data).ok:
                continue
            hand = load_card(data).hands[0]
            shape = canonical_shape(hand)
            if shape in seen:
                continue
            seen.add(shape)
            pool.append(Candidate(
                hand=hand, shape=shape,
                tokens=_tokens(hand), key=stutter_key(hand),
            ))
    return pool


# ── Selecting a card out of the pool ─────────────────────────────────────────


#: A line with fewer neighbours than this is a trap: reach for it, get
#: blocked, and there is nowhere to go. Two is the floor the plan sets.
MIN_NEIGHBOURS = 2

#: Tiles two lines must share before they count as neighbours.
NEIGHBOUR_OVERLAP = 8

#: …and the point past which they are not neighbours but the same hand
#: printed twice. The shape signature cannot catch these: swap one line's
#: two-flower tail for a two-wind tail and the shapes genuinely differ,
#: while the card reads as a stutter. Found in the first generated card,
#: which opened with six Year lines differing only in their filler.
MAX_OVERLAP = 11

#: Ranks that carry a line's identity. A hand is "the same hand again" when
#: these match, whatever tail it wears.
_IDENTITY_RANKS = frozenset("0123456789") | {"x"}


def select(
    pool: list[Candidate],
    *,
    per_section: int = 7,
    seed: int = 0,
) -> list[Hand]:
    """Choose a card: up to ``per_section`` lines from each section, taking
    the one that most improves the card each time.

    Greedy rather than exhaustive — the pool runs to thousands of hands and
    the objective (spread + pivots) has no closed form. Each step picks the
    candidate whose marginal score is best: it should want tokens the card
    is not already saturated with, and it should sit close enough to lines
    already chosen that a blocked player can pivot onto it.
    """
    rng = random.Random(seed)
    by_section: dict[str, list[Candidate]] = {s: [] for s in SECTIONS}
    for c in pool:
        by_section.setdefault(c.section, []).append(c)

    chosen: list[Candidate] = []
    demand: Counter[str] = Counter()
    for section in SECTIONS:
        available = list(by_section.get(section, ()))
        rng.shuffle(available)  # break ties without a positional bias
        for _ in range(min(per_section, len(available))):
            fresh = [c for c in available if not _is_reprint(c, chosen)]
            if not fresh:
                break
            best = max(fresh, key=lambda c: _marginal_score(c, chosen, demand))
            available.remove(best)
            chosen.append(best)
            demand.update(best.tokens)
    _repair_stranded(chosen, by_section, rng)
    return [c.hand for c in chosen]


def stutter_key(hand: Hand) -> tuple:
    """What makes two lines in one section read as the same line twice.

    The first generated card opened with six Year hands identical but for
    their two-tile tail, which the shape signature cannot catch — swap a
    flower pair for a wind pair and the shapes genuinely differ. So identity
    is the *numeric* part of a hand: its number and rank-variable groups,
    with counts and suit letters. Two same-section lines sharing that are
    one line with two tails.

    A hand with no numeric groups at all (a pure winds-and-dragons line) has
    nothing else to be identified by, so there its whole group list is the
    key — otherwise every honours hand would collapse into one.
    """
    numeric = tuple(sorted(
        (g.count, g.rank, g.suit or "")
        for g in hand.groups
        if g.rank.rstrip("+0123456789") in _IDENTITY_RANKS
        or g.rank in _IDENTITY_RANKS
    ))
    if numeric:
        return numeric
    return tuple(sorted((g.count, g.rank, g.suit or "") for g in hand.groups))


def _is_reprint(candidate: Candidate, chosen: list[Candidate]) -> bool:
    """Too close to something already on the card: a clone anywhere, or the
    same line wearing a different tail under the same heading."""
    for other in chosen:
        if other.section == candidate.section and other.key == candidate.key:
            return True
        if _token_overlap(candidate.tokens, other.tokens) >= MAX_OVERLAP:
            return True
    return False


def _neighbours(candidate: Candidate, others: list[Candidate]) -> int:
    return sum(
        1 for o in others
        if o is not candidate
        and _token_overlap(candidate.tokens, o.tokens) >= NEIGHBOUR_OVERLAP
    )


def _repair_stranded(
    chosen: list[Candidate],
    by_section: dict[str, list[Candidate]],
    rng: random.Random,
) -> None:
    """Swap out lines left with too few pivot neighbours.

    The greedy cannot avoid stranding on its own: sections are filled in
    order, so the first section chooses against an empty card and can only
    be judged once the rest exists. This pass runs afterwards, when every
    line's real neighbourhood is finally visible, and replaces a stranded
    line with the best-connected non-reprint from its own section. A line
    with no better alternative is left in place — `pivot_report` will say
    so rather than the card silently pretending otherwise.
    """
    for index, current in enumerate(list(chosen)):
        if _neighbours(current, chosen) >= MIN_NEIGHBOURS:
            continue
        rest = [c for i, c in enumerate(chosen) if i != index]
        taken = {c.hand.id for c in chosen}
        options = [
            c for c in by_section.get(current.section, ())
            if c.hand.id not in taken and not _is_reprint(c, rest)
        ]
        if not options:
            continue
        rng.shuffle(options)
        best = max(options, key=lambda c: _neighbours(c, rest))
        if _neighbours(best, rest) > _neighbours(current, rest):
            chosen[index] = best


def _marginal_score(
    candidate: Candidate, chosen: list[Candidate], demand: Counter[str]
) -> float:
    """How much this line would improve the card as it stands.

    Two pulls in tension, which is the whole balance: novelty (wanting
    tiles the card does not already fight over) against connection (sitting
    within pivot distance of lines already on the card). A card of pure
    novelty has no pivots; a card of pure connection is one hand printed
    nine ways.
    """
    novelty = sum(
        count / (1 + demand.get(token, 0))
        for token, count in candidate.tokens.items()
    )
    if not chosen:
        return novelty
    neighbours = _neighbours(candidate, chosen)
    # Connection is worth a lot up to the floor and little past it — the
    # goal is "no line is stranded", not "every line looks like its
    # neighbours".
    connection = min(neighbours, MIN_NEIGHBOURS) * 4.0 + neighbours * 0.25
    return novelty + connection


def build_card(
    hands: list[Hand],
    *,
    card_id: str,
    display_name: str,
    season: str,
) -> dict:
    """Assemble chosen hands into card JSON, ids renumbered per section and
    values set provisionally (stage 4 re-prices from measurement)."""
    per_section: Counter[str] = Counter()
    out_hands = []
    for hand in hands:
        per_section[hand.section] += 1
        slug = "".join(w[0] for w in hand.section.split()).lower()
        out_hands.append({
            "id": f"{slug}-{per_section[hand.section]}",
            "section": hand.section,
            "name": hand.name,
            "concealed": hand.concealed,
            "value": provisional_value(hand),
            "groups": [
                {"count": g.count, "rank": g.rank}
                | ({"suit": g.suit} if g.suit else {})
                for g in hand.groups
            ],
        })
    return {
        "card_id": card_id, "display_name": display_name,
        "season": season, "hands": out_hands,
    }


def pivot_report(card: Card) -> dict[str, int]:
    """Neighbour count per line — the check that no line is stranded."""
    return {
        h.id: sum(
            1 for other in card.hands
            if other.id != h.id and overlap(h, other) >= NEIGHBOUR_OVERLAP
        )
        for h in card.hands
    }
