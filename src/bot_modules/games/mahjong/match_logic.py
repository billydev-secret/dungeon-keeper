"""The §3.3 matcher and the reachability pass — the most-tested unit in the cog.

Stage 2 of docs/plans/meadow-mahjong.md. Two pure entry points share one
binding enumerator:

* :func:`match_hand` — does this exact 14 (concealed + exposures) match a card
  line? Returns every matching line; settlement takes the highest value and
  computes jokerless from the actual tiles.
* :func:`reachable_lines` — amendment 2's *live line* test for the Duel fallow
  payout: which lines could this seat still complete, given the tiles they
  hold, their locked exposures, and the copies not yet seen elsewhere?
* :func:`closest_lines` — the assistance readout (plans/mahjong-assist.md):
  how far each still-live line sits from the tiles actually held, with the
  gap and the dead weight, ranked. :func:`dangerous_tiles` and
  :func:`suggest_discard` are coach mode's safety rail on top of it.

A binding is (x, suit-letter map, dragon): one ``x`` per hand with every
offset landing in 1–9, suit letters mapping injectively onto physical suits
(≤ 3! = 6), one dragon for ``D``. Brute force is fine (spec §3.3): ≤ ~30
lines × ≤ 9 x-bindings × 6 suit maps × ≤ 6 dragon bindings (ordered
distinct pairs when a hand carries both D and D2).

In American mahjong every group is same-tile (a pung/kong/quint of one tile,
flowers interchangeable), so a group under a binding resolves to exactly one
natural tile — that is what makes the concealed fit a counting argument
instead of a search: pairs/singles must be covered by held naturals (jokers
never stand in a count ≤ 2 group, §2.6), any 3+ deficit is joker-covered,
and every held tile must be consumed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import permutations

from bot_modules.games.mahjong.card_logic import Card, Group, Hand, RankKind
from bot_modules.games.mahjong.tiles import (
    FULL_RANK,
    SUITS,
    STANDARD_JOKERS,
    TILE_ORDER,
    Tile,
    copies_in_play,
)

#: **Experiment flag, default off — production behaviour is unchanged.**
#: Simulation found that ranking lines by raw tile distance sends players at
#: pair-heavy hands that never finish: across two cards, lines whose tiles
#: mostly cannot take a joker took 295 opening picks for zero wins. The
#: cause is that `distance` treats every missing tile as equally costly,
#: when a tile missing from a kong can arrive three ways (draw, claim, or a
#: joker standing in) and one missing from a pair can only be drawn — pairs
#: are uncallable (§2.5) and jokers never fill a group of two (§2.6).
#:
#: With this on, ranking uses `Prospect.effort` instead. `distance` keeps its
#: plain meaning either way, because it is what a member is shown.
#: `sim_logic.simulate(rank_by_effort=True)` sets it inside its own worker
#: processes to A/B the two; nothing else writes it.
RANK_BY_EFFORT = False

#: How much dearer a draw-only tile is than one with three routes. Three
#: opponents discard for every draw you take, so claims are the common way a
#: tile arrives; 3.0 is that ratio, not a fitted constant.
DRAW_ONLY_EFFORT = 3.0

_WIND_TILES = {"N": Tile.NORTH, "E": Tile.EAST, "W": Tile.WEST, "S": Tile.SOUTH}
_DRAGON_TILES = {"R": Tile.RED, "G": Tile.GREEN, "soap": Tile.SOAP}
HAND_TILES = 14


@dataclass(frozen=True)
class Exposure:
    """A face-up called group: ``count`` tiles representing ``natural``, of
    which ``jokers`` are jokers standing in (§2.5/§2.6). Exposures are always
    3+ and never pairs, by the claim rules; the matcher still checks."""

    natural: Tile
    count: int
    jokers: int = 0

    def __post_init__(self) -> None:
        if self.natural is Tile.JOKER:
            raise ValueError("an exposure represents a natural, never a joker")
        if self.count < 3:
            raise ValueError("exposures are 3+ — pairs and singles can't be called")
        if not 0 <= self.jokers <= self.count:
            raise ValueError("joker count outside the exposure")

    @property
    def naturals(self) -> int:
        return self.count - self.jokers


@dataclass(frozen=True)
class Match:
    """One card line this 14 matches, with the binding that made it work."""

    hand: Hand
    x: int | None
    suit_map: tuple[tuple[str, str], ...]  # (letter, physical suit), sorted
    dragon: str | None                     # the D binding: "dr"/"dg"/"soap"
    jokers_used: int
    dragon2: str | None = None             # the D2 binding (grammar v1.1)

    @property
    def jokerless(self) -> bool:
        """No jokers among the winning 14. Whether that *pays* is scoring's
        call — a line with no 3+ group is jokerless by definition and gets
        no extra (§2.9)."""
        return self.jokers_used == 0


# ── Binding enumeration ──────────────────────────────────────────────────────


def _bindings(hand: Hand, max_rank: int = FULL_RANK):
    """Yield every (x, suit_map, dragons) binding the hand's shape allows.

    ``dragons`` is a pair: the D binding and the D2 binding (grammar v1.1 —
    D2 groups must bind a *different* dragon than D). A hand's ``x_parity``
    filters the x candidates; suit-matched dragons need no slot here, they
    follow the suit map.

    ``max_rank`` is the table's deck ceiling. A run must land entirely
    inside it, or the binding names tiles nobody can draw — and since a
    caller then counts *unseen copies* of those tiles to judge liveness, an
    unfiltered range would keep dead lines looking alive on a short deck.
    """
    offsets = [g.offset for g in hand.groups if g.offset is not None]
    if offsets:
        xs: list[int | None] = list(range(1, max_rank + 1 - max(offsets)))
        if hand.x_parity == "odd":
            xs = [x for x in xs if x is not None and x % 2 == 1]
        elif hand.x_parity == "even":
            xs = [x for x in xs if x is not None and x % 2 == 0]
    else:
        xs = [None]


    letters = sorted({g.suit for g in hand.groups if g.suit is not None})
    if letters:
        suit_maps = [dict(zip(letters, perm)) for perm in permutations(SUITS, len(letters))]
    else:
        suit_maps = [{}]

    has_d = any(g.kind is RankKind.ANY_DRAGON for g in hand.groups)
    has_d2 = any(g.kind is RankKind.ANY_DRAGON2 for g in hand.groups)
    codes = ("dr", "dg", "soap")
    if has_d and has_d2:
        dragon_pairs: list[tuple[str | None, str | None]] = [
            (d1, d2) for d1 in codes for d2 in codes if d1 != d2
        ]
    elif has_d:
        dragon_pairs = [(d, None) for d in codes]
    elif has_d2:
        dragon_pairs = [(None, d) for d in codes]
    else:
        dragon_pairs = [(None, None)]

    for x in xs:
        for suit_map in suit_maps:
            for dragons in dragon_pairs:
                yield x, suit_map, dragons


#: American convention: each suit owns a dragon — dots↔soap, bams↔green,
#: craks↔red. A suit-matched D follows its letter through the suit map.
_SUIT_DRAGON_TILES = {"d": Tile.SOAP, "b": Tile.GREEN, "c": Tile.RED}


def _group_natural(
    g: Group, x: int | None, suit_map: Mapping[str, str],
    dragons: tuple[str | None, str | None],
) -> Tile:
    """The one natural tile this group demands under the binding."""
    if g.kind is RankKind.CONCRETE:
        assert g.concrete is not None and g.suit is not None
        return Tile(f"{g.concrete}{suit_map[g.suit]}")
    if g.kind is RankKind.VAR:
        assert x is not None and g.offset is not None and g.suit is not None
        return Tile(f"{x + g.offset}{suit_map[g.suit]}")
    if g.kind is RankKind.ZERO:
        return Tile.SOAP  # soap doubles as zero (§2.1)
    if g.kind is RankKind.WIND:
        assert g.wind is not None
        return _WIND_TILES[g.wind]
    if g.kind is RankKind.DRAGON:
        assert g.dragon is not None
        return _DRAGON_TILES[g.dragon]
    if g.kind is RankKind.SUIT_DRAGON:
        assert g.suit is not None
        return _SUIT_DRAGON_TILES[suit_map[g.suit]]
    if g.kind is RankKind.ANY_DRAGON:
        assert dragons[0] is not None
        return Tile(dragons[0])
    if g.kind is RankKind.ANY_DRAGON2:
        assert dragons[1] is not None
        return Tile(dragons[1])
    return Tile.FLOWER


# ── Exposure → group assignment ──────────────────────────────────────────────


def _exposure_assignments(
    exposures: list[Exposure], resolved: list[tuple[Group, Tile]]
):
    """Yield every injective exposures→groups assignment (as a frozenset of
    group indices used). Tiny search: exposures ≤ 4, groups ≤ ~10."""
    if not exposures:
        yield frozenset()
        return
    first, rest = exposures[0], exposures[1:]
    for i, (group, natural) in enumerate(resolved):
        if group.count != first.count or natural != first.natural:
            continue
        if first.jokers and not group.takes_jokers:
            continue  # jokers never sit in a count <= 2 group (§2.6)
        for used in _exposure_assignments(rest, resolved):
            if i not in used:
                yield used | {i}


def _demand_of(remaining: list[tuple[Group, Tile]]) -> tuple[Counter, Counter]:
    """Aggregate remaining groups per *tile*: total demand, and the pair/
    single portion jokers can never fill (§2.6). By tile, not by group —
    two groups of one line can resolve to the same natural."""
    demand: Counter[Tile] = Counter()
    small: Counter[Tile] = Counter()
    for group, natural in remaining:
        demand[natural] += group.count
        if not group.takes_jokers:
            small[natural] += group.count
    return demand, small


# ── The exact fit (§3.3) ─────────────────────────────────────────────────────


def _concealed_fit(
    concealed_counts: Counter[Tile],
    jokers_held: int,
    remaining: list[tuple[Group, Tile]],
) -> int | None:
    """Can the concealed tiles exactly fill these groups? Returns jokers used,
    or None. Counting argument (see module docstring): pairs/singles need
    held naturals; 3+ deficits take jokers; everything must be consumed."""
    demand: Counter[Tile] = Counter()
    small: Counter[Tile] = Counter()
    for group, natural in remaining:
        demand[natural] += group.count
        if not group.takes_jokers:
            small[natural] += group.count

    for tile, n in concealed_counts.items():
        if n > demand.get(tile, 0):
            return None  # leftover tile the line has no slot for
    for tile, n in small.items():
        if concealed_counts.get(tile, 0) < n:
            return None  # a pair/single slot would need a joker
    jokers_needed = sum(
        n - concealed_counts.get(tile, 0) for tile, n in demand.items()
    )
    if jokers_needed != jokers_held:
        return None  # too few jokers to fill, or spare jokers with no slot
    return jokers_held


def match_hand(
    concealed: list[Tile], exposures: list[Exposure], card: Card
) -> list[Match]:
    """Every card line this exact 14 matches (§3.3). Pure."""
    if len(concealed) + sum(e.count for e in exposures) != HAND_TILES:
        return []
    counts = Counter(concealed)
    jokers_held = counts.pop(Tile.JOKER, 0)
    exposure_jokers = sum(e.jokers for e in exposures)

    matches: list[Match] = []
    for hand in card.hands:
        if hand.concealed and exposures:
            continue  # a concealed line rejects any exposure
        match = _match_line(hand, counts, jokers_held, exposures, exposure_jokers)
        if match is not None:
            matches.append(match)
    return matches


def _match_line(
    hand: Hand,
    concealed_counts: Counter[Tile],
    jokers_held: int,
    exposures: list[Exposure],
    exposure_jokers: int,
) -> Match | None:
    for x, suit_map, dragons in _bindings(hand):
        resolved = [(g, _group_natural(g, x, suit_map, dragons)) for g in hand.groups]
        for used in _exposure_assignments(exposures, resolved):
            remaining = [rg for i, rg in enumerate(resolved) if i not in used]
            fit = _concealed_fit(concealed_counts, jokers_held, remaining)
            if fit is not None:
                return Match(
                    hand=hand,
                    x=x,
                    suit_map=tuple(sorted(suit_map.items())),
                    dragon=dragons[0],
                    dragon2=dragons[1],
                    jokers_used=fit + exposure_jokers,
                )
    return None


def resolved_groups(match: Match) -> list[tuple[Tile, int]]:
    """(natural, count) per group of the matched line, under its binding —
    the reveal embed's §6.12 "groups" rendering. Joker placement within a
    group isn't part of the match, so groups render as their naturals."""
    suit_map = dict(match.suit_map)
    return [
        (_group_natural(g, match.x, suit_map, (match.dragon, match.dragon2)),
         g.count)
        for g in match.hand.groups
    ]


def best_match(matches: list[Match]) -> Match | None:
    """Settlement's pick: the highest-value line (§3.3)."""
    return max(matches, key=lambda m: m.hand.value, default=None)


# ── Reachability (amendment 2 — the Duel fallow payout) ──────────────────────


def reachable_lines(
    concealed: list[Tile],
    exposures: list[Exposure],
    card: Card,
    seen_elsewhere: Counter[Tile],
    max_rank: int = FULL_RANK,
    *,
    joker_copies: int = STANDARD_JOKERS,
) -> list[Hand]:
    """Lines this seat could still complete, in card order.

    ``seen_elsewhere`` counts every tile visible *outside* this seat's own
    hand: the discard pile plus other seats' exposures (a joker in an
    exposure counts as a joker there, not as its impersonated natural).
    A line is live when its groups can absorb the locked exposures and, for
    every remaining group, held naturals + unseen copies + joker cover meet
    the count — with pairs/singles still needing naturals. Extra held tiles
    don't kill a line (they can be discarded on later turns).
    """
    counts = Counter(concealed)
    jokers_held = counts.pop(Tile.JOKER, 0)

    own: Counter[Tile] = Counter(concealed)
    for e in exposures:
        own[e.natural] += e.naturals
        own[Tile.JOKER] += e.jokers

    def unseen(tile: Tile) -> int:
        return max(
            0,
            copies_in_play(tile, max_rank, jokers=joker_copies)
            - seen_elsewhere.get(tile, 0)
            - own.get(tile, 0),
        )

    live: list[Hand] = []
    for hand in card.hands:
        if hand.concealed and exposures:
            continue
        if _line_reachable(hand, counts, jokers_held, exposures, unseen, max_rank):
            live.append(hand)
    return live


def _line_reachable(
    hand, concealed_counts, jokers_held, exposures, unseen, max_rank=FULL_RANK
) -> bool:
    for x, suit_map, dragons in _bindings(hand, max_rank):
        resolved = [(g, _group_natural(g, x, suit_map, dragons)) for g in hand.groups]
        for used in _exposure_assignments(exposures, resolved):
            remaining = [rg for i, rg in enumerate(resolved) if i not in used]
            demand, small = _demand_of(remaining)

            obtainable = {
                tile: concealed_counts.get(tile, 0) + unseen(tile) for tile in demand
            }
            if any(obtainable[tile] < n for tile, n in small.items()):
                continue  # a pair/single can no longer find its naturals
            joker_deficit = sum(
                max(0, n - obtainable[tile]) for tile, n in demand.items()
            )
            if joker_deficit <= jokers_held + unseen(Tile.JOKER):
                return True
    return False


def fallow_base_value(
    concealed: list[Tile],
    exposures: list[Exposure],
    card: Card,
    seen_elsewhere: Counter[Tile],
    max_rank: int = FULL_RANK,
    *,
    joker_copies: int = STANDARD_JOKERS,
) -> int:
    """The Duel fallow payout's base: the survivor's lowest-value live line,
    or the card minimum when nothing is live (amendment 2)."""
    live = reachable_lines(
        concealed, exposures, card, seen_elsewhere, max_rank,
        joker_copies=joker_copies)
    if live:
        return min(h.value for h in live)
    return min(h.value for h in card.hands)


# ── Assistance (plans/mahjong-assist.md A2–A6) ───────────────────────────────

@dataclass(frozen=True)
class Prospect:
    """One still-live card line, measured against the tiles actually held.

    ``distance`` is how many more tiles the seat must acquire (draw, claim,
    or redeem) to complete the line — held jokers already discounted against
    the 3+ groups they may stand in. ``needed`` lists exactly those missing
    tiles, summing to ``distance``; where a held joker covers part of a 3+
    gap it is discounted from the *scarcest* tile first (fewest unseen
    copies), since a wild tile is best spent where drawing is hardest.
    ``dead_weight`` is what the line cannot consume — never jokers (A5): a
    joker is always redeemable or tradeable, so it is never dead.
    """

    hand: Hand
    distance: int
    needed: tuple[tuple[Tile, int], ...]
    dead_weight: tuple[tuple[Tile, int], ...]
    #: ``distance`` reweighted by how a tile can actually arrive: one point
    #: per tile that a draw, a claim or a joker could supply, and
    #: ``DRAW_ONLY_EFFORT`` per tile that only your own draw can. Always
    #: computed; only used for ranking when ``RANK_BY_EFFORT`` is on.
    effort: float = 0.0


def closest_lines(
    concealed: list[Tile],
    exposures: list[Exposure],
    card: Card,
    seen_elsewhere: Counter[Tile],
    limit: int | None = 3,
    max_rank: int = FULL_RANK,
    *,
    joker_copies: int = STANDARD_JOKERS,
) -> list[Prospect]:
    """Every still-live line, closest first (A3/A4). Pure.

    Liveness is judged per binding exactly as :func:`reachable_lines` does,
    and a line's distance is minimised over its *live* bindings only — a
    dead binding may sit nearer, but pointing at tiles that can no longer
    be drawn would be a lie. Ties: distance, then value descending, then
    card order (distances cluster hard, so ties are the common case).
    """
    counts = Counter(concealed)
    jokers_held = counts.pop(Tile.JOKER, 0)

    own: Counter[Tile] = Counter(concealed)
    for e in exposures:
        own[e.natural] += e.naturals
        own[Tile.JOKER] += e.jokers

    def unseen(tile: Tile) -> int:
        return max(
            0,
            copies_in_play(tile, max_rank, jokers=joker_copies)
            - seen_elsewhere.get(tile, 0)
            - own.get(tile, 0),
        )

    exposure_total = sum(e.count for e in exposures)

    prospects: list[tuple[float, int, int, Prospect]] = []
    for index, hand in enumerate(card.hands):
        if hand.concealed and exposures:
            continue
        best = _line_prospect(
            hand, counts, jokers_held, exposures, exposure_total, unseen, max_rank
        )
        if best is not None:
            key = best.effort if RANK_BY_EFFORT else float(best.distance)
            prospects.append((key, -hand.value, index, best))
    prospects.sort(key=lambda p: p[:3])
    ranked = [p[3] for p in prospects]
    return ranked if limit is None else ranked[:limit]


def _line_prospect(
    hand: Hand,
    concealed_counts: Counter[Tile],
    jokers_held: int,
    exposures: list[Exposure],
    exposure_total: int,
    unseen,
    max_rank: int = FULL_RANK,
) -> Prospect | None:
    """Minimum distance over the line's live bindings, or None if none is
    live. First-found wins a distance tie, so the report is deterministic."""
    best: Prospect | None = None
    for x, suit_map, dragons in _bindings(hand, max_rank):
        resolved = [(g, _group_natural(g, x, suit_map, dragons)) for g in hand.groups]
        for used in _exposure_assignments(exposures, resolved):
            remaining = [rg for i, rg in enumerate(resolved) if i not in used]
            demand, small = _demand_of(remaining)

            # Liveness first (the reachable_lines test, verbatim): a binding
            # whose tiles are extinct must not set the distance.
            obtainable = {
                tile: concealed_counts.get(tile, 0) + unseen(tile) for tile in demand
            }
            if any(obtainable[tile] < n for tile, n in small.items()):
                continue
            joker_deficit = sum(
                max(0, n - obtainable[tile]) for tile, n in demand.items()
            )
            if joker_deficit > jokers_held + unseen(Tile.JOKER):
                continue

            # Distance: held naturals serve pairs/singles first — that frees
            # the joker-eligible remainder for held jokers, and no other
            # allocation matches more held tiles.
            needed: Counter[Tile] = Counter()
            large_deficit: Counter[Tile] = Counter()
            matched = exposure_total
            for tile, n in demand.items():
                have = concealed_counts.get(tile, 0)
                to_small = min(have, small.get(tile, 0))
                to_large = min(have - to_small, n - small.get(tile, 0))
                matched += to_small + to_large
                if (short_small := small.get(tile, 0) - to_small) > 0:
                    needed[tile] += short_small
                if (short_large := (n - small.get(tile, 0)) - to_large) > 0:
                    large_deficit[tile] += short_large
            small_short = sum(needed.values())
            jokers_used = min(jokers_held, sum(large_deficit.values()))
            matched += jokers_used

            # Discount held jokers from the scarcest 3+ gaps (docstring).
            for tile in sorted(
                large_deficit, key=lambda t: (unseen(t), TILE_ORDER[t])
            ):
                if jokers_used == 0:
                    break
                spent = min(jokers_used, large_deficit[tile])
                large_deficit[tile] -= spent
                jokers_used -= spent
            needed.update(+large_deficit)  # unary + drops zeroed gaps

            distance = HAND_TILES - matched
            # Draw-only tiles counted at their real cost; everything still
            # missing from a 3+ group can also be claimed or jokered.
            effort = (
                small_short * DRAW_ONLY_EFFORT
                + (distance - small_short) * 1.0
            )
            if best is not None:
                rank = effort if RANK_BY_EFFORT else float(distance)
                incumbent = best.effort if RANK_BY_EFFORT else float(best.distance)
                if rank >= incumbent:
                    continue
            dead = {
                tile: have - min(have, demand.get(tile, 0))
                for tile, have in concealed_counts.items()
                if have > demand.get(tile, 0)
            }
            best = Prospect(
                hand=hand,
                distance=distance,
                effort=effort,
                needed=tuple(
                    sorted(needed.items(), key=lambda p: TILE_ORDER[p[0]])
                ),
                dead_weight=tuple(
                    sorted(dead.items(), key=lambda p: TILE_ORDER[p[0]])
                ),
            )
            if distance == 0:
                return best
    return best


@dataclass(frozen=True)
class CallAdvice:
    """What calling the live discard would buy: which rack tiles go
    face-up with it, the group it forms, the line it advances, and the
    distance before/after. Only ever built when calling strictly helps —
    an exposure that doesn't advance the best line locks the hand (and
    kills every concealed line) for nothing."""

    tiles: tuple[Tile, ...]
    count: int
    hand: Hand
    distance: int
    distance_before: int


def call_advice(
    concealed: list[Tile],
    exposures: list[Exposure],
    card: Card,
    seen: Counter[Tile],
    tile: Tile,
    max_rank: int = FULL_RANK,
    *,
    joker_copies: int = STANDARD_JOKERS,
) -> CallAdvice | None:
    """Should this seat call ``tile``? Simulates every legal exposure size
    and returns the best one that strictly shortens the closest line, or
    None. Pure — shared by the bot brain and the coach readout so the
    advice a member gets is exactly the judgement a bot makes.

    Jokers can never be claimed (§2.5), and a pair can't be called: the
    smallest callable group is three, which needs two rack tiles.
    """
    if tile is Tile.JOKER:
        return None
    before = closest_lines(
        concealed, exposures, card, seen, limit=1, max_rank=max_rank,
        joker_copies=joker_copies)
    if not before:
        return None
    naturals = [t for t in concealed if t is tile]
    jokers = [t for t in concealed if t is Tile.JOKER]
    best: CallAdvice | None = None
    for count in (3, 4, 5):
        needed = count - 1
        if len(naturals) + len(jokers) < needed:
            continue
        given = naturals[:needed] + jokers[: max(0, needed - len(naturals))]
        remaining = list(concealed)
        for t in given:
            remaining.remove(t)
        after = closest_lines(
            remaining,
            exposures + [Exposure(
                natural=tile, count=count,
                jokers=sum(1 for t in given if t is Tile.JOKER),
            )],
            card, seen, limit=1, max_rank=max_rank,
            joker_copies=joker_copies,
        )
        if not after or after[0].distance >= before[0].distance:
            continue
        if best is None or after[0].distance < best.distance:
            best = CallAdvice(
                tiles=tuple(given), count=count, hand=after[0].hand,
                distance=after[0].distance,
                distance_before=before[0].distance,
            )
    return best


def dangerous_tiles(
    card: Card, opponent_exposures: list[list[Exposure]]
) -> frozenset[Tile]:
    """Tiles a visible opponent could plausibly want — coach's rail (A6).

    A seat's exposures lock groups of whatever line they are chasing, so the
    lines compatible with those exposures are public knowledge. The union of
    naturals those lines still demand is the danger set; a seat with no
    exposures reveals nothing and constrains nothing. Overcautious by
    design — their concealed tiles might rule a line out, but we can't see
    that, and a rail errs toward silence.
    """
    exposed = [e for e in opponent_exposures if e]
    if not exposed:
        return frozenset()
    danger: set[Tile] = set()
    # One enumeration of hands × bindings; only the exposure assignment
    # depends on the opponent (F9 — the per-opponent rescan tripled the
    # cost at a 4-seat table for identical resolved lists).
    for hand in card.hands:
        if hand.concealed:
            continue  # an exposed seat can't be on a concealed line
        for x, suit_map, dragons in _bindings(hand):
            resolved = [
                (g, _group_natural(g, x, suit_map, dragons)) for g in hand.groups
            ]
            for exposures in exposed:
                for used in _exposure_assignments(exposures, resolved):
                    danger.update(
                        natural
                        for i, (_, natural) in enumerate(resolved)
                        if i not in used
                    )
    return frozenset(danger)


def suggest_discard(
    concealed: list[Tile],
    prospects: list[Prospect],
    dangerous: frozenset[Tile] = frozenset(),
    *,
    shown: int = 3,
) -> Tile | None:
    """Coach's pick: a tile in the dead-weight *intersection* of the hands
    actually shown (the same set the embed prints), useful to the fewest
    live lines — never a joker (A5), never a tile in ``dangerous`` (A6).
    None when there is no safe suggestion: silence beats advice that hands
    another seat the pot.

    The intersection is what makes the advice self-consistent (review
    round 2, F2): excess beyond a hand's demand implies zero deficit, so a
    tile dead for every shown hand provably appears in none of their need
    lists — drawing candidates from the closest hand alone suggested tiles
    a shown sister hand was still waiting on (~16% of coach racks)."""
    if not prospects:
        return None
    have = Counter(t for t in concealed if t is not Tile.JOKER)
    display = prospects[:shown]
    common = dict(display[0].dead_weight)
    for p in display[1:]:
        dw = dict(p.dead_weight)
        common = {t: min(n, dw[t]) for t, n in common.items() if t in dw}
    candidates = [tile for tile in common if tile not in dangerous]
    if not candidates:
        return None

    def usefulness(tile: Tile) -> int:
        return sum(
            1
            for p in prospects
            if have[tile] - dict(p.dead_weight).get(tile, 0) > 0
        )

    return min(candidates, key=lambda t: (usefulness(t), TILE_ORDER[t]))
