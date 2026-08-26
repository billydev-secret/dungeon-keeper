"""Meadow Card model: load, lint, display strings (spec §3.1–§3.4).

Stage 1 of docs/plans/meadow-mahjong.md. A card is versioned *data* — an
original seasonal hand set, never NMJL content (the mechanics are
uncopyrightable; the League's card compilation is not; do not copy it).

Two layers, one entry point:

* :func:`load_card` enforces the pattern grammar structurally and raises
  :class:`CardError` listing **every** problem, so a card author fixes a bad
  upload in one round trip.
* :func:`lint_card` runs the semantic checks of spec §3.4 on a structurally
  valid card.
* :func:`lint_card_data` folds both into one report — the shared gate for
  the CLI (``scripts/validate_card.py``) and the dashboard upload route, so a
  card that passes here is exactly a card the engine can play.

Rank grammar (§3.1, v1.1): ``"1"``–``"9"`` (suited; suit letter required),
``"0"`` (satisfied only by soap — §2.1's "soap doubles as zero"; suitless,
like the dragon it rides on), ``"x"``…``"x+5"`` (rank variable, one x
binding per hand; suit letter required; a hand may lock parity with
``"x_parity": "odd"/"even"``), ``"N"/"E"/"W"/"S"``, ``"R"/"G"/"soap"``,
``"D"`` **suitless** (any one dragon, one binding per hand), ``"D"``
**with a suit letter** (the dragon OF that suit — dots↔soap, bams↔green,
craks↔red — following the letter's binding), ``"D2"`` (a second any-dragon
that must bind a *different* dragon than ``D``), ``"F"`` (flower). Suit
letters ``"a"/"b"/"c"``: same letter = same physical suit, distinct letters
= pairwise distinct suits. Binding *enumeration* belongs to match_logic;
this module only guarantees the shapes are well-formed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from itertools import permutations
from pathlib import Path

#: The starter card that ships with v1 (spec §4). Lives inside the package —
#: not a top-level data/ dir — so the remote gate runner (which syncs only
#: src/, tests/, scripts/) can always see it.
FIRST_LIGHT_PATH = Path(__file__).parent / "cards" / "meadow_first_light.json"

VALUE_MIN, VALUE_MAX = 25, 75
GROUP_COUNT_MIN, GROUP_COUNT_MAX = 1, 6
HAND_TILES = 14
#: Naturals in existence: 4 of everything except flowers (8). Jokers never
#: stand in a group of count <= 2, so demand above these in small groups is
#: unwinnable by construction (§3.4).
NATURAL_COPIES = 4
FLOWER_COPIES = 8
JOKER_COPIES = 8

_WINDS = ("N", "E", "W", "S")
_DRAGONS = ("R", "G", "soap")
_SUIT_LETTERS = ("a", "b", "c")
_VAR_RE = re.compile(r"^x(?:\+([1-9]))?$")  # "x+0"/"x+01" are not grammar
MAX_VAR_OFFSET = 5  # grammar caps runs at x..x+5 (§3.1 v1.1; was 4 in v1)


class RankKind(Enum):
    CONCRETE = "concrete"      # 1–9, suited
    ZERO = "zero"              # "0" — satisfied only by soap; suitless
    VAR = "var"                # x+offset — one x binding per hand; suited
    WIND = "wind"              # N/E/W/S
    DRAGON = "dragon"          # a specific dragon: R, G, or soap
    ANY_DRAGON = "any_dragon"  # D — any single dragon, one binding per hand
    ANY_DRAGON2 = "any_dragon2"  # D2 — a second, DIFFERENT dragon binding
    SUIT_DRAGON = "suit_dragon"  # D with a suit letter — that suit's dragon
    FLOWER = "flower"          # F — suitless, all 8 interchangeable


class CardError(ValueError):
    """A card file is structurally invalid. ``problems`` lists every issue."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class Group:
    """One group of a hand: ``{count, rank, suit}`` per §3.1, pre-parsed.

    ``rank`` keeps the raw token (it *is* the display form); exactly one of
    the kind-specific fields is set for kinds that carry a payload.
    """

    count: int
    rank: str
    suit: str | None
    kind: RankKind
    concrete: int | None = None   # CONCRETE: rank 1–9
    offset: int | None = None     # VAR: 0–5 above x
    wind: str | None = None       # WIND: "N"/"E"/"W"/"S"
    dragon: str | None = None     # DRAGON: "R"/"G"/"soap"

    @property
    def takes_jokers(self) -> bool:
        """Jokers stand in only inside groups of 3+ (§2.6)."""
        return self.count >= 3

    @property
    def token(self) -> str:
        """§4's ``count(rank)suit`` notation — the generated display form."""
        return f"{self.count}({self.rank}){self.suit or ''}"


@dataclass(frozen=True)
class Hand:
    """One card line: ordered groups plus its identity and price."""

    id: str
    section: str
    name: str
    concealed: bool
    value: int
    groups: tuple[Group, ...]
    notes: str = ""
    x_parity: str | None = None  # "odd"/"even" locks the x binding's parity

    @property
    def tile_total(self) -> int:
        return sum(g.count for g in self.groups)

    @property
    def display(self) -> str:
        """Generated display string — matches the spec §4 Groups column."""
        return " ".join(g.token for g in self.groups)


@dataclass(frozen=True)
class Card:
    """A loaded, structurally valid Meadow Card."""

    card_id: str
    display_name: str
    season: str
    hands: tuple[Hand, ...]

    @property
    def max_value(self) -> int:
        """The card max — escrow is card max × payout cap × stake (§2.9)."""
        return max(h.value for h in self.hands)

    def sections(self) -> list[str]:
        """Section names in card order (first appearance)."""
        seen: dict[str, None] = {}
        for h in self.hands:
            seen.setdefault(h.section, None)
        return list(seen)

    def hand_by_id(self, hand_id: str) -> Hand | None:
        return next((h for h in self.hands if h.id == hand_id), None)


@dataclass
class LintReport:
    """§3.4's verdict: hard failures and advisories, member-presentable."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# ── Structural parse ─────────────────────────────────────────────────────────


def _parse_group(raw: object, where: str, problems: list[str]) -> Group | None:
    """Parse one group dict; on failure, record why and return None."""
    if not isinstance(raw, dict):
        problems.append(f"{where}: group must be an object, got {type(raw).__name__}")
        return None

    count = raw.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        problems.append(f"{where}: count must be an integer")
        return None
    if not GROUP_COUNT_MIN <= count <= GROUP_COUNT_MAX:
        problems.append(
            f"{where}: count {count} outside "
            f"{GROUP_COUNT_MIN}–{GROUP_COUNT_MAX}"
        )
        return None

    rank = raw.get("rank")
    if not isinstance(rank, str) or not rank:
        problems.append(f"{where}: rank must be a non-empty string")
        return None

    suit = raw.get("suit")
    if suit is not None and suit not in _SUIT_LETTERS:
        problems.append(f"{where}: suit must be one of a/b/c, got {suit!r}")
        return None

    kind: RankKind
    concrete = offset = None
    wind = dragon = None
    if rank in _WINDS:
        kind = RankKind.WIND
        wind = rank
    elif rank in _DRAGONS:
        kind = RankKind.DRAGON
        dragon = rank
    elif rank == "D":
        # with a suit letter it is that suit's own dragon; bare it is the
        # one-per-hand any-dragon binding (grammar v1.1)
        kind = RankKind.SUIT_DRAGON if suit is not None else RankKind.ANY_DRAGON
    elif rank == "D2":
        kind = RankKind.ANY_DRAGON2
    elif rank == "F":
        kind = RankKind.FLOWER
    elif rank == "0":
        kind = RankKind.ZERO
    elif len(rank) == 1 and rank in "123456789":  # ASCII only — "²".isdigit()
        kind = RankKind.CONCRETE                  # is True but int("²") raises
        concrete = int(rank)
    else:
        m = _VAR_RE.match(rank)
        if m:
            offset = int(m.group(1) or 0)
            if offset > MAX_VAR_OFFSET:
                # §3.4's offset rule, enforced at the grammar: runs are capped
                # at x..x+5 so every offset can land in 1–9 together.
                problems.append(
                    f"{where}: rank offset +{offset} beyond the "
                    f"x..x+{MAX_VAR_OFFSET} grammar"
                )
                return None
            kind = RankKind.VAR
        else:
            problems.append(f"{where}: unknown rank token {rank!r}")
            return None

    needs_suit = kind in (RankKind.CONCRETE, RankKind.VAR)
    takes_suit = needs_suit or kind is RankKind.SUIT_DRAGON
    if needs_suit and suit is None:
        problems.append(f"{where}: suited rank {rank!r} needs a suit letter")
        return None
    if not takes_suit and suit is not None:
        problems.append(f"{where}: rank {rank!r} is suitless — drop the suit")
        return None

    return Group(
        count=count, rank=rank, suit=suit, kind=kind,
        concrete=concrete, offset=offset, wind=wind, dragon=dragon,
    )


def _parse_hand(raw: object, index: int, problems: list[str]) -> Hand | None:
    where = f"hands[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: hand must be an object")
        return None

    hand_id = raw.get("id")
    if not isinstance(hand_id, str) or not hand_id:
        problems.append(f"{where}: id must be a non-empty string")
        return None
    where = f"hand {hand_id!r}"

    ok = True
    section = raw.get("section")
    if not isinstance(section, str) or not section:
        problems.append(f"{where}: section must be a non-empty string")
        ok = False
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}: name must be a non-empty string")
        ok = False
    concealed = raw.get("concealed")
    if not isinstance(concealed, bool):
        problems.append(f"{where}: concealed must be true or false")
        ok = False
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{where}: value must be an integer")
        ok = False
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        problems.append(f"{where}: notes must be a string")
        ok = False
    x_parity = raw.get("x_parity")
    if x_parity is not None and x_parity not in ("odd", "even"):
        problems.append(f"{where}: x_parity must be \"odd\" or \"even\"")
        ok = False

    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        problems.append(f"{where}: groups must be a non-empty list")
        return None
    groups: list[Group] = []
    for gi, g in enumerate(raw_groups):
        parsed = _parse_group(g, f"{where} groups[{gi}]", problems)
        if parsed is None:
            ok = False
        else:
            groups.append(parsed)

    if not ok:
        return None
    assert isinstance(section, str) and isinstance(name, str)
    assert isinstance(concealed, bool) and isinstance(value, int)
    assert isinstance(notes, str)
    return Hand(
        id=hand_id, section=section, name=name, concealed=concealed,
        value=value, groups=tuple(groups), notes=notes,
        x_parity=x_parity,
    )


def load_card(data: object) -> Card:
    """Build a :class:`Card` from decoded JSON; raise CardError with every
    structural problem found (never just the first)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        raise CardError(["card must be a JSON object"])

    card_id = data.get("card_id")
    if not isinstance(card_id, str) or not card_id:
        problems.append("card_id must be a non-empty string")
        card_id = ""
    display_name = data.get("display_name")
    if not isinstance(display_name, str) or not display_name:
        problems.append("display_name must be a non-empty string")
        display_name = ""
    season = data.get("season")
    if not isinstance(season, str) or not season:
        problems.append("season must be a non-empty string")
        season = ""

    raw_hands = data.get("hands")
    hands: list[Hand] = []
    if not isinstance(raw_hands, list) or not raw_hands:
        problems.append("hands must be a non-empty list")
    else:
        for i, h in enumerate(raw_hands):
            parsed = _parse_hand(h, i, problems)
            if parsed is not None:
                hands.append(parsed)

    if problems:
        raise CardError(problems)
    return Card(
        card_id=card_id, display_name=display_name, season=season,
        hands=tuple(hands),
    )


def load_card_file(path: Path | str) -> Card:
    """Load a card JSON file (utf-8 — the gate runner is Windows/cp1252)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise CardError([f"cannot read {path}: {e}"]) from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CardError([f"invalid JSON: {e}"]) from e
    return load_card(data)


def load_first_light() -> Card:
    """The starter card, Meadow Card — First Light (spec §4)."""
    return load_card_file(FIRST_LIGHT_PATH)


# ── Difficulty (stage 4 of plans/mahjong-card-generator.md) ──────────────────

#: Score at or above which a line is a **long shot**. Calibrated against
#: simulation, not chosen: across two cards and ~150 measured games, lines
#: scoring >= 12 took 295 opening picks between them and won **nothing**,
#: while every one of the 63 wins came from below it. The separation is
#: stable anywhere in [12, 14]; at 10 it starts leaking winners.
LONG_SHOT_SCORE = 12.0

#: Weights. Hand-set and individually explainable rather than fitted — a
#: member is told *why* a line is hard, and "no group here takes a joker" is
#: worth more to them than a star rating. Only the LONG_SHOT_SCORE boundary
#: is validated against measurement; treat the fine-grained ordering as
#: informed opinion until a larger run earns it.
_W_RIGID = 1.0        # per tile in a group no joker may fill (§2.6)
_W_DRAW_ONLY = 0.8    # per tile in a group that cannot be called either (§2.5)
_W_CONCEALED = 3.0    # flat: the line forfeits calling entirely
_W_SUIT = 1.5         # per distinct suit to chase
_W_FORCED_JOKER = 2.0 # per tile demanded past the natural supply
_W_VARIABLE = -2.0    # per rank variable: binds where your tiles already sit
_W_FLOWER = -0.5      # per flower: eight copies, the cheapest tiles there are

#: Score mapped onto the card's value range. 0 -> VALUE_MIN, 25 -> VALUE_MAX.
_VALUE_SPAN = 25.0


@dataclass(frozen=True)
class Difficulty:
    """How hard a line is to complete, from the tiles alone.

    Structural: no simulation, so it can be shown the instant a card is
    uploaded. The formula is what the simulator *calibrated*, not what it
    runs — the expensive measurement happened once, offline, and this is the
    cheap thing that carries its conclusion.

    ``reasons`` names the terms that actually moved this line, worst first,
    so a reader learns something about how to play it.
    """

    score: float
    reasons: tuple[str, ...]

    @property
    def long_shot(self) -> bool:
        return self.score >= LONG_SHOT_SCORE

    @property
    def band(self) -> str:
        """Two bands, deliberately. Only the long-shot boundary is validated;
        inventing a five-point scale would imply resolution we do not have."""
        return "long shot" if self.long_shot else "standard"


def difficulty(hand: Hand) -> Difficulty:
    """Score a line's difficulty, with the reasons that drove it."""
    rigid = sum(g.count for g in hand.groups if not g.takes_jokers)
    draw_only = sum(g.count for g in hand.groups if g.count <= 2)
    suits = len({g.suit for g in hand.groups if g.suit})
    forced = sum(
        max(0, g.count - (FLOWER_COPIES if g.kind is RankKind.FLOWER
                          else NATURAL_COPIES))
        for g in hand.groups
    )
    variables = sum(1 for g in hand.groups if g.kind is RankKind.VAR)
    flowers = sum(g.count for g in hand.groups if g.kind is RankKind.FLOWER)

    score = (
        _W_RIGID * rigid
        + _W_DRAW_ONLY * draw_only
        + (_W_CONCEALED if hand.concealed else 0.0)
        + _W_SUIT * suits
        + _W_FORCED_JOKER * forced
        + _W_VARIABLE * variables
        + _W_FLOWER * flowers
    )

    reasons: list[tuple[float, str]] = []
    if rigid == hand.tile_total:
        reasons.append((_W_RIGID * rigid,
                        "no group takes a joker — every tile must be a natural"))
    elif rigid:
        reasons.append((_W_RIGID * rigid,
                        f"{rigid} of {hand.tile_total} tiles cannot take a joker"))
    if draw_only:
        reasons.append((_W_DRAW_ONLY * draw_only,
                        f"{draw_only} tiles sit in groups too small to call"))
    if hand.concealed:
        reasons.append((_W_CONCEALED, "concealed — no exposures at all"))
    if suits > 1:
        reasons.append((_W_SUIT * suits, f"spans {suits} suits"))
    if forced:
        reasons.append((_W_FORCED_JOKER * forced,
                        f"needs {forced} joker(s) past the natural supply"))
    if variables:
        reasons.append((_W_VARIABLE * variables,
                        f"{variables} rank variable(s) — binds to the tiles you hold"))
    if flowers:
        reasons.append((_W_FLOWER * flowers, f"{flowers} flowers, of which 8 exist"))
    reasons.sort(key=lambda r: -r[0])
    return Difficulty(score=score, reasons=tuple(r[1] for r in reasons))


def value_for_difficulty(score: float) -> int:
    """Price a line from its difficulty, on the card's 25–75 scale.

    A card already encodes difficulty in its values — First Light's author
    and this score agree at r = 0.64 — but by hand and unevenly. Deriving it
    makes the relationship explicit and consistent across a whole card.
    """
    span = VALUE_MAX - VALUE_MIN
    raw = VALUE_MIN + span * max(0.0, min(1.0, score / _VALUE_SPAN))
    return int(round(raw / 5) * 5)


def required_rank(hand: Hand) -> int:
    """Lowest deck ceiling this line can be played on.

    A concrete rank needs a deck that reaches it; a run needs room for its
    whole span, so ``x..x+4`` needs 5 even though no single token says so.
    """
    needed = 1
    for g in hand.groups:
        if g.kind is RankKind.CONCRETE and g.concrete is not None:
            needed = max(needed, g.concrete)
        elif g.kind is RankKind.VAR and g.offset is not None:
            needed = max(needed, g.offset + 1)
    return needed


def lines_needing_more_rank(card: Card, max_rank: int) -> list[str]:
    """Ids of lines a table playing to ``max_rank`` could never complete.

    A short deck does not make a card invalid — it makes some of its lines
    dead ink, which is a thing to warn an admin about at activation rather
    than discover as an unwinnable hand.
    """
    return [h.id for h in card.hands if required_rank(h) > max_rank]


def difficulty_profile(card: Card) -> dict[str, Difficulty]:
    """Every line's difficulty, by hand id — the card viewer's data."""
    return {h.id: difficulty(h) for h in card.hands}


# ── Linter (§3.4) ────────────────────────────────────────────────────────────


def _demand_key(group: Group) -> tuple[object, ...] | None:
    """Identity of the *specific natural tile* a group demands, where that
    is binding-independent — the unit the small-group scarcity check counts.

    Same key ⇒ same physical tile under every binding (x and suit letters
    bind once per hand; soap-the-dragon and rank 0 are the same tile).
    Distinct keys can coincide under *some* binding, but the player picks
    bindings, so a coincidental collision never makes a line unwinnable —
    accumulating identical keys only is exactly the impossibility test.
    ANY_DRAGON is one binding per hand, so its demands pool together; that
    its pool might dodge into an unclaimed dragon is deliberately ignored —
    a D-pair next to pairs of all three named dragons is exotic enough to
    leave to the author.
    """
    if group.kind is RankKind.CONCRETE:
        return ("suited", group.concrete, group.suit)
    if group.kind is RankKind.VAR:
        return ("var", group.offset, group.suit)
    if group.kind is RankKind.WIND:
        return ("wind", group.wind)
    if group.kind is RankKind.ANY_DRAGON:
        return ("any_dragon",)
    if group.kind is RankKind.ANY_DRAGON2:
        return ("any_dragon2",)
    if group.kind is RankKind.SUIT_DRAGON:
        return ("suit_dragon", group.suit)
    if group.kind is RankKind.DRAGON and group.dragon != "soap":
        return ("dragon", group.dragon)
    if group.kind is RankKind.ZERO or group.kind is RankKind.DRAGON:
        return ("soap",)  # rank 0 and the soap dragon are one physical tile
    return None  # FLOWER — 8 copies, checked by the flower total instead


def _shape_payload(g: Group) -> tuple[str, str]:
    if g.kind is RankKind.CONCRETE:
        return ("concrete", str(g.concrete))
    if g.kind is RankKind.VAR:
        return ("var", f"+{g.offset}")
    if g.kind is RankKind.ZERO or (g.kind is RankKind.DRAGON and g.dragon == "soap"):
        return ("dragon", "soap")  # 4(0) and 4(soap) are the same physical group
    return (g.kind.value, g.wind or g.dragon or "")


def canonical_shape(hand: Hand) -> tuple[tuple[int, str, str, str], ...]:
    """Shape signature for the near-duplicate warning: groups sorted, suit
    letters canonicalized by taking the minimum over all 3! relabelings — so
    a line recolored *and* reordered still reads as the same shape. The
    parity lock is part of the identity: an odd/even twin pair is two
    genuinely disjoint lines, not a near-duplicate (grammar-verify G3).

    Public because the generator (`card_gen`) dedupes its candidate pool by
    exactly the identity the linter warns about — two spellings of one shape
    must not be able to enter the pool as separate hands.
    """
    signatures = []
    for perm in permutations(_SUIT_LETTERS):
        mapping: dict[str, str] = dict(zip(_SUIT_LETTERS, perm))
        signatures.append((hand.x_parity or "", tuple(sorted(
            (g.count, *_shape_payload(g), mapping[g.suit] if g.suit else "")
            for g in hand.groups
        ))))
    return min(signatures)


def lint_card(card: Card) -> LintReport:
    """The semantic checks of §3.4 on a structurally valid card."""
    report = LintReport()

    seen_ids: set[str] = set()
    for hand in card.hands:
        if hand.id in seen_ids:
            report.errors.append(f"duplicate hand id {hand.id!r}")
        seen_ids.add(hand.id)

    for hand in card.hands:
        where = f"hand {hand.id!r} ({hand.name})"

        if hand.tile_total != HAND_TILES:
            report.errors.append(
                f"{where}: groups sum to {hand.tile_total}, not {HAND_TILES}"
            )

        if not VALUE_MIN <= hand.value <= VALUE_MAX:
            report.errors.append(
                f"{where}: value {hand.value} outside {VALUE_MIN}–{VALUE_MAX}"
            )

        # Rank-variable landability: every offset must land in 1–9 for one
        # shared x. The x..x+4 grammar makes this unfailable today; the check
        # stays so a future grammar widening can't silently strand a line.
        offsets = [g.offset for g in hand.groups if g.offset is not None]
        if offsets and max(offsets) > 8:
            report.errors.append(
                f"{where}: no x in 1–9 can carry offset +{max(offsets)}"
            )
        if hand.x_parity is not None:
            if not offsets:
                report.errors.append(
                    f"{where}: x_parity set but no rank-variable group uses x"
                )
            else:
                start = 1 if hand.x_parity == "odd" else 2
                if not any(
                    x + max(offsets) <= 9 for x in range(start, 10, 2)
                ):
                    report.errors.append(
                        f"{where}: no {hand.x_parity} x can carry offset "
                        f"+{max(offsets)}"
                    )

        # Jokerless-impossible small groups: jokers never stand in a pair or
        # single (§2.6), so demand for one specific tile across count<=2
        # groups is capped by its natural copies.
        small_demand: dict[tuple[object, ...], int] = {}
        small_rank: dict[tuple[object, ...], str] = {}
        flower_total = 0
        for g in hand.groups:
            if g.kind is RankKind.FLOWER:
                flower_total += g.count
            if g.count > 2:
                continue
            key = _demand_key(g)
            if key is None:  # flowers: 8 copies, small groups can't exceed it
                continue
            small_demand[key] = small_demand.get(key, 0) + g.count
            small_rank.setdefault(key, f"{g.rank}{g.suit or ''}")
        for key, demand in small_demand.items():
            if demand > NATURAL_COPIES:
                report.errors.append(
                    f"{where}: pair/single groups demand {demand} naturals of "
                    f"{small_rank[key]} — only {NATURAL_COPIES} exist and "
                    f"jokers can't fill groups of 2 or fewer"
                )

        if flower_total > FLOWER_COPIES:
            report.errors.append(
                f"{where}: demands {flower_total} flowers — only "
                f"{FLOWER_COPIES} exist"
            )

        # Total-supply check: naturals cap at 4 per specific tile (8 flowers)
        # and every shortfall must be a joker — of which 8 exist. A hand whose
        # combined deficits exceed that is unwinnable outright.
        total_demand: dict[tuple[object, ...], int] = {}
        for g in hand.groups:
            key = _demand_key(g)
            if key is not None:
                total_demand[key] = total_demand.get(key, 0) + g.count
        joker_deficit = sum(
            max(0, demand - NATURAL_COPIES) for demand in total_demand.values()
        ) + max(0, flower_total - FLOWER_COPIES)
        if joker_deficit > JOKER_COPIES:
            report.errors.append(
                f"{where}: needs {joker_deficit} jokers to cover its "
                f"shortfalls — only {JOKER_COPIES} exist"
            )

        # Unreachable concealed/exposure combination: an exposures-allowed
        # line with no callable group (all pairs/singles — §2.5 makes those
        # uncallable) can never actually use an exposure; mark it C.
        if not hand.concealed and not any(g.takes_jokers for g in hand.groups):
            report.errors.append(
                f"{where}: exposures allowed but no group of 3+ exists to "
                f"call — mark it concealed"
            )

    # Exhaustive winnability (grammar-verify G2): the per-key checks above
    # assume distinct demand keys can always dodge each other, which v1.1
    # broke — suit-dragons on all three letters make one of them soap under
    # EVERY binding, and D+D2 beside named dragons can pin two of three.
    # Lint runs at upload time, so brute force over the bindings is cheap.
    from bot_modules.games.mahjong.match_logic import (
        _bindings, _group_natural,
    )
    from bot_modules.games.mahjong.tiles import Tile, copies

    for hand in card.hands:
        if any(e.startswith(f"hand {hand.id!r}") for e in report.errors):
            continue  # already condemned with a more specific message
        feasible = False
        for x, suit_map, dragons in _bindings(hand):
            small: dict[Tile, int] = {}
            total: dict[Tile, int] = {}
            for g in hand.groups:
                natural = _group_natural(g, x, suit_map, dragons)
                total[natural] = total.get(natural, 0) + g.count
                if g.count <= 2:
                    small[natural] = small.get(natural, 0) + g.count
            if any(n > copies(tile) for tile, n in small.items()):
                continue
            deficit = sum(
                max(0, n - copies(tile)) for tile, n in total.items()
            )
            if deficit <= JOKER_COPIES:
                feasible = True
                break
        if not feasible:
            report.errors.append(
                f"hand {hand.id!r} ({hand.name}): no binding leaves this "
                f"hand winnable — some tile is over-demanded under every "
                f"suit/dragon assignment"
            )

    # Warnings — advisories, not failures.
    by_section: dict[str, int] = {}
    for hand in card.hands:
        by_section[hand.section] = by_section.get(hand.section, 0) + 1
    for section, n in by_section.items():
        if n < 2:
            report.warnings.append(
                f"section {section!r} has only {n} hand{'s' if n != 1 else ''}"
            )
    for hand in card.hands:
        kinds = {g.kind for g in hand.groups}
        if RankKind.ANY_DRAGON2 in kinds and RankKind.ANY_DRAGON not in kinds:
            report.warnings.append(
                f"hand {hand.id!r}: D2 without D is just an any-dragon — "
                f"write it as D"
            )

    shapes: dict[tuple[tuple[int, str, str, str], ...], str] = {}
    for hand in card.hands:
        shape = canonical_shape(hand)
        if shape in shapes:
            report.warnings.append(
                f"hands {shapes[shape]!r} and {hand.id!r} are near-duplicates "
                f"(same groups up to suit relabeling)"
            )
        else:
            shapes[shape] = hand.id
    return report


def lint_card_data(data: object) -> LintReport:
    """Structural parse + lint in one report — the shared CLI/upload gate."""
    try:
        card = load_card(data)
    except CardError as e:
        return LintReport(errors=list(e.problems))
    return lint_card(card)
