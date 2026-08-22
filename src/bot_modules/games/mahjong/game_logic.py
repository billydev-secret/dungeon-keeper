"""The Meadow Mahjong state machine — pure ``(state, action) -> state``.

Stage 3 of docs/plans/meadow-mahjong.md. One engine for both table sizes:
``seat_count`` (2 | 4) is a constructor parameter, and pass routing, claim
arbitration, and the payout tables derive from it (spec §5). The engine
never shuffles, never sleeps, and never touches money: ``deal``/``rematch``
take an injected pre-shuffled wall, randomness-needing transitions take an
injected ``rng`` (D14), timers live in the service layer and arrive as
``timeout`` actions, and settlement emits *point* deltas the service
multiplies by the stake.

Phases::

    LOBBY → DEAL → CHARLESTON(1)[r,a,l] → CHARLESTON_VOTE → CHARLESTON(2)[l,a,r]
          → COURTESY_PROPOSE → COURTESY_PICK
          → PLAY{AWAIT_DISCARD | CLAIM_WINDOW} → SETTLE → (rematch → DEAL | CLOSED)

Every transition validates seat, phase, and legality; an illegal action
raises :class:`ActionRejected` — typed, member-presentable, never a crash
and never a public message. That is what makes an invalid Mahjong fail
privately as a Pass (§1) and what keeps a stray button tap from leaking
hand information (§2.5).

Transitions return ``(new_state, events)``; the input state is never
mutated. Events are ``(kind, data)`` tuples for the announcement-worthy
facts only — the cog re-renders the table from state, so events carry what
a render can't know it needs to say (a reveal, a strike, a private
rejection-as-pass). An event whose data carries ``"private": True`` must
reach only ``data["seat"]`` (ephemerally) — rendering it anywhere public
breaks §2.5's "a tap never leaks" promise.
"""

from __future__ import annotations

import copy
import random
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from bot_modules.games.mahjong.card_logic import Card, Hand
from bot_modules.games.mahjong.match_logic import (
    Exposure,
    Match,
    best_match,
    fallow_base_value,
    match_hand,
)
from bot_modules.games.mahjong.tiles import Tile

DEALER_TILES = 14
SEAT_TILES = 13
CHARLESTON_TILES = 3
MAX_COURTESY = 3
STRIKES_TO_FALLOW = 3

#: Pass directions per Charleston, as seat offsets (turn order runs to the
#: right, so "right" = the next seat). Duel overrides every pass to the
#: opponent — which +1/+3 mod 2 already are, and "across" is mapped below.
FIRST_CHARLESTON = (1, 2, 3)   # right, across, left
SECOND_CHARLESTON = (3, 2, 1)  # left, across, right


class Phase(str, Enum):
    LOBBY = "lobby"
    CHARLESTON = "charleston"
    CHARLESTON_VOTE = "charleston_vote"
    COURTESY_PROPOSE = "courtesy_propose"
    COURTESY_PICK = "courtesy_pick"
    AWAIT_DISCARD = "await_discard"
    CLAIM_WINDOW = "claim_window"
    SETTLE = "settle"
    CLOSED = "closed"


class RejectCode(str, Enum):
    """Why an action was refused. The message on the exception is
    member-presentable; the code is for the cog/tests to branch on."""

    NOT_SEATED = "not_seated"
    WRONG_PHASE = "wrong_phase"
    NOT_YOUR_TURN = "not_your_turn"
    TABLE_FULL = "table_full"
    ALREADY_SEATED = "already_seated"
    NOT_HOST = "not_host"
    NOT_ENOUGH_SEATS = "not_enough_seats"
    TILE_NOT_IN_RACK = "tile_not_in_rack"
    JOKERS_NEVER_PASS = "jokers_never_pass"
    BAD_COUNT = "bad_count"
    ALREADY_SUBMITTED = "already_submitted"
    BLIND_NOT_NOW = "blind_not_now"
    INVALID_MAHJONG = "invalid_mahjong"
    CALL_NO_MATCH = "call_no_match"
    JOKER_DISCARD_CLAIM = "joker_discard_claim"
    NO_SUCH_EXPOSURE = "no_such_exposure"
    NOT_A_JOKER_SLOT = "not_a_joker_slot"
    SEAT_FALLOW = "seat_fallow"
    NOT_SETTLED = "not_settled"


class ActionRejected(Exception):
    """Typed refusal — caught by the service/cog, shown ephemerally."""

    def __init__(self, code: RejectCode, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class TableConfig:
    """Per-table knobs fixed at creation (dashboard-sourced)."""

    seat_count: int            # 2 (Duel) or 4
    wall_trim: int = 0         # Duel pacing lever: dead tiles removed at deal
    second_charleston: bool = True

    def __post_init__(self) -> None:
        if self.seat_count not in (2, 4):
            raise ValueError("seat_count must be 2 or 4")
        if self.wall_trim < 0:
            raise ValueError("wall_trim must be >= 0")


@dataclass
class ExposureState:
    """A face-up called/exposed group on a seat's row."""

    exposure_id: int
    natural: Tile
    count: int
    jokers: int

    def as_match(self) -> Exposure:
        return Exposure(natural=self.natural, count=self.count, jokers=self.jokers)


@dataclass
class SeatState:
    member_id: int
    rack: list[Tile] = field(default_factory=list)
    exposures: list[ExposureState] = field(default_factory=list)
    strikes: int = 0
    fallow: bool = False


@dataclass
class Outcome:
    """What the hand ended as; the service turns point deltas into coins."""

    kind: str                       # "mahjong" | "wall_game" | "fallow_end"
    winner: int | None              # seat index
    line_id: str | None
    line_name: str | None
    value: int
    won_by: str | None              # "discard" | "self_pick"
    jokerless_double: bool
    discarder: int | None           # seat that fed the winning discard
    point_deltas: dict[int, int]    # seat -> signed points (sums to zero)


@dataclass
class GameState:
    config: TableConfig
    host: int                       # member id of the table creator
    seats: list[SeatState]
    phase: Phase = Phase.LOBBY
    dealer: int = 0
    turn: int = 0
    hand_no: int = 0
    wall: list[Tile] = field(default_factory=list)
    discards: list[tuple[int, Tile]] = field(default_factory=list)
    live_discard: Tile | None = None
    live_discarder: int | None = None
    drawn: Tile | None = None       # current player's just-drawn tile
    turn_source: str = "deal"       # deal | draw | call — how this turn began
    claimed_discarder: int | None = None  # whose discard fed a call this turn
    charleston_round: int = 1
    pass_index: int = 0
    pending_picks: dict[int, tuple[list[Tile], int]] = field(default_factory=dict)
    votes: dict[int, bool] = field(default_factory=dict)
    proposals: dict[int, int] = field(default_factory=dict)
    courtesy_owed: dict[int, int] = field(default_factory=dict)
    courtesy_gives: dict[int, list[Tile]] = field(default_factory=dict)
    claims: dict[int, tuple[str, list[Tile]]] = field(default_factory=dict)
    rematch_votes: set[int] = field(default_factory=set)
    next_exposure_id: int = 1
    outcome: Outcome | None = None

    # ── conveniences (read-only; no rules logic) ─────────────────────────────

    @property
    def seat_count(self) -> int:
        return self.config.seat_count

    def seat_of(self, member_id: int) -> int | None:
        for i, s in enumerate(self.seats):
            if s.member_id == member_id:
                return i
        return None

    def live_seats(self) -> list[int]:
        return [i for i, s in enumerate(self.seats) if not s.fallow]

    def next_live_seat(self, seat: int) -> int:
        n = len(self.seats)
        for step in range(1, n + 1):
            cand = (seat + step) % n
            if not self.seats[cand].fallow:
                return cand
        return seat  # unreachable while any seat is live


Event = tuple[str, dict]


# ── Serialization (codes only; the service stores this JSON) ─────────────────


def state_to_dict(state: GameState) -> dict:
    return {
        "config": {
            "seat_count": state.config.seat_count,
            "wall_trim": state.config.wall_trim,
            "second_charleston": state.config.second_charleston,
        },
        "host": state.host,
        "seats": [
            {
                "member_id": s.member_id,
                "rack": [t.code for t in s.rack],
                "exposures": [
                    {"exposure_id": e.exposure_id, "natural": e.natural.code,
                     "count": e.count, "jokers": e.jokers}
                    for e in s.exposures
                ],
                "strikes": s.strikes,
                "fallow": s.fallow,
            }
            for s in state.seats
        ],
        "phase": state.phase.value,
        "dealer": state.dealer,
        "turn": state.turn,
        "hand_no": state.hand_no,
        "wall": [t.code for t in state.wall],
        "discards": [[seat, t.code] for seat, t in state.discards],
        "live_discard": state.live_discard.code if state.live_discard else None,
        "live_discarder": state.live_discarder,
        "drawn": state.drawn.code if state.drawn else None,
        "turn_source": state.turn_source,
        "claimed_discarder": state.claimed_discarder,
        "charleston_round": state.charleston_round,
        "pass_index": state.pass_index,
        "pending_picks": {
            str(seat): [[t.code for t in tiles], blind]
            for seat, (tiles, blind) in state.pending_picks.items()
        },
        "votes": {str(k): v for k, v in state.votes.items()},
        "proposals": {str(k): v for k, v in state.proposals.items()},
        "courtesy_owed": {str(k): v for k, v in state.courtesy_owed.items()},
        "courtesy_gives": {
            str(k): [t.code for t in v] for k, v in state.courtesy_gives.items()
        },
        "claims": {
            str(seat): [kind, [t.code for t in tiles]]
            for seat, (kind, tiles) in state.claims.items()
        },
        "rematch_votes": sorted(state.rematch_votes),
        "next_exposure_id": state.next_exposure_id,
        "outcome": None if state.outcome is None else {
            "kind": state.outcome.kind,
            "winner": state.outcome.winner,
            "line_id": state.outcome.line_id,
            "line_name": state.outcome.line_name,
            "value": state.outcome.value,
            "won_by": state.outcome.won_by,
            "jokerless_double": state.outcome.jokerless_double,
            "discarder": state.outcome.discarder,
            "point_deltas": {str(k): v for k, v in state.outcome.point_deltas.items()},
        },
    }


def state_from_dict(data: dict) -> GameState:
    cfg = data["config"]
    outcome = data.get("outcome")
    return GameState(
        config=TableConfig(
            seat_count=cfg["seat_count"],
            wall_trim=cfg["wall_trim"],
            second_charleston=cfg["second_charleston"],
        ),
        host=data["host"],
        seats=[
            SeatState(
                member_id=s["member_id"],
                rack=[Tile(c) for c in s["rack"]],
                exposures=[
                    ExposureState(
                        exposure_id=e["exposure_id"], natural=Tile(e["natural"]),
                        count=e["count"], jokers=e["jokers"],
                    )
                    for e in s["exposures"]
                ],
                strikes=s["strikes"],
                fallow=s["fallow"],
            )
            for s in data["seats"]
        ],
        phase=Phase(data["phase"]),
        dealer=data["dealer"],
        turn=data["turn"],
        hand_no=data["hand_no"],
        wall=[Tile(c) for c in data["wall"]],
        discards=[(seat, Tile(c)) for seat, c in data["discards"]],
        live_discard=Tile(data["live_discard"]) if data["live_discard"] else None,
        live_discarder=data["live_discarder"],
        drawn=Tile(data["drawn"]) if data["drawn"] else None,
        turn_source=data["turn_source"],
        claimed_discarder=data["claimed_discarder"],
        charleston_round=data["charleston_round"],
        pass_index=data["pass_index"],
        pending_picks={
            int(seat): ([Tile(c) for c in tiles], blind)
            for seat, (tiles, blind) in data["pending_picks"].items()
        },
        votes={int(k): v for k, v in data["votes"].items()},
        proposals={int(k): v for k, v in data["proposals"].items()},
        courtesy_owed={int(k): v for k, v in data["courtesy_owed"].items()},
        courtesy_gives={
            int(k): [Tile(c) for c in v] for k, v in data["courtesy_gives"].items()
        },
        claims={
            int(seat): (kind, [Tile(c) for c in tiles])
            for seat, (kind, tiles) in data["claims"].items()
        },
        rematch_votes=set(data["rematch_votes"]),
        next_exposure_id=data["next_exposure_id"],
        outcome=None if outcome is None else Outcome(
            kind=outcome["kind"],
            winner=outcome["winner"],
            line_id=outcome["line_id"],
            line_name=outcome["line_name"],
            value=outcome["value"],
            won_by=outcome["won_by"],
            jokerless_double=outcome["jokerless_double"],
            discarder=outcome["discarder"],
            point_deltas={int(k): v for k, v in outcome["point_deltas"].items()},
        ),
    )


# ── Guards ───────────────────────────────────────────────────────────────────


def _require_phase(state: GameState, *phases: Phase) -> None:
    if state.phase not in phases:
        raise ActionRejected(
            RejectCode.WRONG_PHASE, "That isn't something the table can do right now."
        )


def _require_seat(state: GameState, seat: int) -> SeatState:
    if not 0 <= seat < len(state.seats):
        raise ActionRejected(RejectCode.NOT_SEATED, "You're not at this table.")
    s = state.seats[seat]
    if s.fallow:
        raise ActionRejected(
            RejectCode.SEAT_FALLOW, "Your seat has folded out of this hand."
        )
    return s


def _take_from_rack(seat: SeatState, tiles: list[Tile]) -> None:
    """Remove ``tiles`` from the rack or reject; order-independent."""
    rack = Counter(seat.rack)
    need = Counter(tiles)
    for tile, n in need.items():
        if rack[tile] < n:
            raise ActionRejected(
                RejectCode.TILE_NOT_IN_RACK,
                f"You don't have {n}× {tile.label} to give.",
            )
    for tile in tiles:
        seat.rack.remove(tile)


def _no_jokers(tiles: list[Tile]) -> None:
    if any(t is Tile.JOKER for t in tiles):
        raise ActionRejected(
            RejectCode.JOKERS_NEVER_PASS, "Jokers can never be passed."
        )


def _random_pass(rng: random.Random, seat: SeatState, n: int = CHARLESTON_TILES) -> list[Tile]:
    """The auto-resolve/fallback pick: n random non-jokers from the rack.
    A 13-tile rack always holds >= 5 non-jokers (only 8 jokers exist)."""
    pool = [t for t in seat.rack if t is not Tile.JOKER]
    return rng.sample(pool, n)


# ── Lobby ────────────────────────────────────────────────────────────────────


def create_table(config: TableConfig, host_member_id: int) -> GameState:
    return GameState(
        config=config,
        host=host_member_id,
        seats=[SeatState(member_id=host_member_id)],
    )


def join_table(state: GameState, member_id: int) -> tuple[GameState, list[Event]]:
    _require_phase(state, Phase.LOBBY)
    state = copy.deepcopy(state)
    if state.seat_of(member_id) is not None:
        raise ActionRejected(RejectCode.ALREADY_SEATED, "You're already at this table.")
    if len(state.seats) >= state.seat_count:
        raise ActionRejected(RejectCode.TABLE_FULL, "Every seat is taken.")
    state.seats.append(SeatState(member_id=member_id))
    events: list[Event] = [("seat_taken", {"member_id": member_id})]
    if len(state.seats) == state.seat_count:
        events.append(("table_full", {}))
    return state, events


def cancel_table(state: GameState, member_id: int | None) -> tuple[GameState, list[Event]]:
    """Host cancel (member_id) or mod/service cancel (None). Only in LOBBY —
    a dealt hand ends through settle or dissolve, never cancel."""
    _require_phase(state, Phase.LOBBY)
    state = copy.deepcopy(state)
    if member_id is not None and member_id != state.host:
        raise ActionRejected(RejectCode.NOT_HOST, "Only the host can cancel this table.")
    state.phase = Phase.CLOSED
    return state, [("table_closed", {"reason": "cancelled"})]


def close_table(state: GameState, reason: str) -> tuple[GameState, list[Event]]:
    """Service-driven close (dissolve on inactivity, settle screen's Close)."""
    state = copy.deepcopy(state)
    state.phase = Phase.CLOSED
    return state, [("table_closed", {"reason": reason})]


# ── Deal ─────────────────────────────────────────────────────────────────────


def deal(state: GameState, wall: list[Tile]) -> tuple[GameState, list[Event]]:
    """Start a hand from a full lobby (or a unanimous rematch). ``wall`` is a
    pre-shuffled 152 (D14); the Duel trim comes off before dealing."""
    _require_phase(state, Phase.LOBBY, Phase.SETTLE)
    if len(state.seats) != state.seat_count:
        raise ActionRejected(RejectCode.NOT_ENOUGH_SEATS, "The table isn't full yet.")
    if state.phase is Phase.SETTLE and not rematch_ready(state):
        raise ActionRejected(
            RejectCode.NOT_SETTLED, "A rematch needs every seat to press Rematch."
        )
    state = copy.deepcopy(state)
    wall = list(wall)
    if state.config.wall_trim:
        needed = DEALER_TILES + SEAT_TILES * (state.seat_count - 1)
        trim = min(state.config.wall_trim, max(0, len(wall) - needed))
        del wall[:trim]

    if state.phase is Phase.SETTLE:  # rematch: rotate/alternate the dealer
        state.dealer = (state.dealer + 1) % state.seat_count
    state.hand_no += 1
    state.turn = state.dealer
    state.outcome = None
    state.discards = []
    state.live_discard = None
    state.live_discarder = None
    state.drawn = None
    state.claims = {}
    state.votes = {}
    state.proposals = {}
    state.courtesy_owed = {}
    state.courtesy_gives = {}
    state.pending_picks = {}
    state.rematch_votes = set()
    state.charleston_round = 1
    state.pass_index = 0
    for i, seat in enumerate(state.seats):
        n = DEALER_TILES if i == state.dealer else SEAT_TILES
        seat.rack = [wall.pop() for _ in range(n)]
        seat.exposures = []
        seat.strikes = 0
        seat.fallow = False
    state.wall = wall
    state.phase = Phase.CHARLESTON
    return state, [("hand_dealt", {"hand_no": state.hand_no, "dealer": state.dealer})]


# ── Charleston (§2.3) ────────────────────────────────────────────────────────


def _pass_offset(state: GameState) -> int:
    directions = FIRST_CHARLESTON if state.charleston_round == 1 else SECOND_CHARLESTON
    offset = directions[state.pass_index]
    if state.seat_count == 2:
        return 1  # every Duel pass goes to the opponent
    return offset


def _is_final_pass(state: GameState) -> bool:
    return state.pass_index == 2


def charleston_pick(
    state: GameState, seat: int, tiles: list[Tile], blind_n: int, rng: random.Random
) -> tuple[GameState, list[Event]]:
    """Submit this pass's outgoing pick: 3 - blind_n tiles, jokers excluded.
    Blind slots exist only on the final pass of each Charleston (§2.3.4)."""
    _require_phase(state, Phase.CHARLESTON)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat in state.pending_picks:
        raise ActionRejected(RejectCode.ALREADY_SUBMITTED, "Your pass is already in.")
    if not 0 <= blind_n <= CHARLESTON_TILES:
        raise ActionRejected(RejectCode.BAD_COUNT, "Blind 0–3 tiles.")
    if blind_n and not _is_final_pass(state):
        raise ActionRejected(
            RejectCode.BLIND_NOT_NOW, "Blind passing opens on the final pass."
        )
    if len(tiles) != CHARLESTON_TILES - blind_n:
        raise ActionRejected(
            RejectCode.BAD_COUNT,
            f"Pick exactly {CHARLESTON_TILES - blind_n} tiles for this pass.",
        )
    _no_jokers(tiles)
    rack = Counter(seat_state.rack)
    for tile, n in Counter(tiles).items():
        if rack[tile] < n:
            raise ActionRejected(
                RejectCode.TILE_NOT_IN_RACK, f"You don't hold {n}× {tile.label}."
            )
    state.pending_picks[seat] = (list(tiles), blind_n)
    seat_state.strikes = 0  # a timely act breaks the consecutive-timeout run
    events: list[Event] = [("pass_submitted", {"seat": seat})]
    events += _resolve_charleston_if_ready(state, rng)
    return state, events


def _resolve_charleston_if_ready(state: GameState, rng: random.Random) -> list[Event]:
    if set(state.pending_picks) != set(range(len(state.seats))):
        return []
    events = _resolve_charleston_pass(state, rng)
    if state.phase is not Phase.CHARLESTON:
        return events  # a Duel fallow settle ended the hand mid-phase
    # advance: next pass, or on to the vote / courtesy
    if not _is_final_pass(state):
        state.pass_index += 1
        return events
    if state.charleston_round == 1:
        if state.config.second_charleston:
            state.phase = Phase.CHARLESTON_VOTE
            state.votes = {}
        else:
            _enter_courtesy(state)
        return events
    _enter_courtesy(state)
    return events


def _resolve_charleston_pass(state: GameState, rng: random.Random) -> list[Event]:
    """Move every lane at once (D12): a lane is its sender's picked tiles plus
    blind pass-through from the sender's own incoming lane, resolved
    iteratively; a pure-blind cycle falls back to random rack tiles."""
    n = len(state.seats)
    offset = _pass_offset(state)
    picked: dict[int, list[Tile]] = {}
    blind: dict[int, int] = {}
    for i in range(n):
        tiles, k = state.pending_picks[i]
        picked[i], blind[i] = list(tiles), k

    lanes: dict[int, list[Tile]] = {}  # sender -> the 3 tiles they send
    progress = True
    while progress:
        progress = False
        for i in range(n):
            if i in lanes:
                continue
            if blind[i] == 0:
                lanes[i] = list(picked[i])
                progress = True
                continue
            feeder = (i - offset) % n  # who passes TO seat i
            if feeder in lanes:
                lanes[i] = list(picked[i]) + lanes[feeder][: blind[i]]
                progress = True
    for i in range(n):
        if i not in lanes:  # blind cycle (or fed by one): random rack tiles
            pool = [t for t in state.seats[i].rack if t is not Tile.JOKER]
            for t in picked[i]:
                pool.remove(t)  # picked tiles are validated non-jokers
            # a 13-tile rack holds >= 5 non-jokers (8 jokers exist), so after
            # removing the 3-k picked there are always >= k left to sample
            extra = rng.sample(pool, blind[i])
            lanes[i] = list(picked[i]) + extra
            picked[i] = list(picked[i]) + extra  # they leave this rack too

    # apply: each seat loses its originated tiles and blind-through tiles it
    # forwarded, gains its incoming lane minus what it forwarded onward.
    for i in range(n):
        seat = state.seats[i]
        feeder = (i - offset) % n
        incoming = lanes[feeder]
        forwarded = len(lanes[i]) - len(picked[i])  # blind-through count
        _take_from_rack(seat, picked[i])
        seat.rack.extend(incoming[forwarded:] if forwarded else incoming)
        # forwarded tiles pass straight through — they were never rack tiles
    state.pending_picks = {}
    return [(
        "pass_resolved",
        {"round": state.charleston_round, "pass_index": state.pass_index},
    )]


# ── Second-Charleston vote & courtesy (§2.3.2–§2.3.3) ────────────────────────


def vote_second_charleston(
    state: GameState, seat: int, yes: bool
) -> tuple[GameState, list[Event]]:
    """Unanimous yes starts the optional Charleston; any no resolves early."""
    _require_phase(state, Phase.CHARLESTON_VOTE)
    state = copy.deepcopy(state)
    _require_seat(state, seat)
    if seat in state.votes:
        raise ActionRejected(RejectCode.ALREADY_SUBMITTED, "You've already voted.")
    state.votes[seat] = yes
    state.seats[seat].strikes = 0  # timely act — consecutive run broken
    events: list[Event] = [("vote_cast", {"seat": seat})]
    if not yes:
        _enter_courtesy(state)
        events.append(("vote_resolved", {"passed": False}))
    elif set(state.votes) == set(range(len(state.seats))):
        state.phase = Phase.CHARLESTON
        state.charleston_round = 2
        state.pass_index = 0
        state.pending_picks = {}
        events.append(("vote_resolved", {"passed": True}))
    return state, events


def _enter_courtesy(state: GameState) -> None:
    state.phase = Phase.COURTESY_PROPOSE
    state.proposals = {}
    state.courtesy_owed = {}
    state.courtesy_gives = {}


def _courtesy_partner(state: GameState, seat: int) -> int:
    """Across-pairs: (0,2) and (1,3); in Duel, the two players (§2.3.3)."""
    return (seat + state.seat_count // 2) % state.seat_count


def courtesy_propose(
    state: GameState, seat: int, n: int
) -> tuple[GameState, list[Event]]:
    _require_phase(state, Phase.COURTESY_PROPOSE)
    state = copy.deepcopy(state)
    _require_seat(state, seat)
    if seat in state.proposals:
        raise ActionRejected(RejectCode.ALREADY_SUBMITTED, "Your proposal is in.")
    if not 0 <= n <= MAX_COURTESY:
        raise ActionRejected(RejectCode.BAD_COUNT, "Propose 0–3 tiles.")
    state.proposals[seat] = n
    state.seats[seat].strikes = 0  # timely act — consecutive run broken
    events: list[Event] = [("courtesy_proposed", {"seat": seat})]
    if set(state.proposals) == set(range(len(state.seats))):
        events += _resolve_courtesy_proposals(state)
    return state, events


def _resolve_courtesy_proposals(state: GameState) -> list[Event]:
    """Each pair exchanges the minimum of its two proposals."""
    owed: dict[int, int] = {}
    for seat in range(len(state.seats)):
        partner = _courtesy_partner(state, seat)
        owed[seat] = min(state.proposals[seat], state.proposals[partner])
    state.courtesy_owed = {s: n for s, n in owed.items() if n > 0}
    events: list[Event] = [("courtesy_resolved", {"owed": dict(state.courtesy_owed)})]
    if state.courtesy_owed:
        state.phase = Phase.COURTESY_PICK
        state.courtesy_gives = {}
    else:
        events += _begin_play(state)
    return events


def courtesy_pick(
    state: GameState, seat: int, tiles: list[Tile]
) -> tuple[GameState, list[Event]]:
    """Pick which tiles to give (the pair's agreed minimum, jokers excluded)."""
    _require_phase(state, Phase.COURTESY_PICK)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat not in state.courtesy_owed:
        raise ActionRejected(RejectCode.BAD_COUNT, "You have no courtesy tiles to give.")
    if seat in state.courtesy_gives:
        raise ActionRejected(RejectCode.ALREADY_SUBMITTED, "Your tiles are already in.")
    if len(tiles) != state.courtesy_owed[seat]:
        raise ActionRejected(
            RejectCode.BAD_COUNT,
            f"Give exactly {state.courtesy_owed[seat]} tile(s).",
        )
    _no_jokers(tiles)
    rack = Counter(seat_state.rack)
    for tile, n in Counter(tiles).items():
        if rack[tile] < n:
            raise ActionRejected(
                RejectCode.TILE_NOT_IN_RACK, f"You don't hold {n}× {tile.label}."
            )
    state.courtesy_gives[seat] = list(tiles)
    seat_state.strikes = 0  # timely act — consecutive run broken
    events: list[Event] = [("courtesy_given", {"seat": seat})]
    if set(state.courtesy_gives) == set(state.courtesy_owed):
        events += _apply_courtesy(state)
    return state, events


def _apply_courtesy(state: GameState) -> list[Event]:
    for seat, tiles in state.courtesy_gives.items():
        _take_from_rack(state.seats[seat], tiles)
    for seat, tiles in state.courtesy_gives.items():
        partner = _courtesy_partner(state, seat)
        state.seats[partner].rack.extend(tiles)
    return _begin_play(state)


def _begin_play(state: GameState) -> list[Event]:
    """Charleston done: the dealer (already holding 14) discards first."""
    state.phase = Phase.AWAIT_DISCARD
    state.turn = state.dealer
    state.drawn = None
    state.turn_source = "deal"
    state.claimed_discarder = None
    return [("play_begins", {"turn": state.turn})]


# ── The turn loop (§2.4–§2.6) ────────────────────────────────────────────────


def discard(state: GameState, seat: int, tile: Tile) -> tuple[GameState, list[Event]]:
    _require_phase(state, Phase.AWAIT_DISCARD)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat != state.turn:
        raise ActionRejected(RejectCode.NOT_YOUR_TURN, "It isn't your turn.")
    if tile not in seat_state.rack:
        raise ActionRejected(RejectCode.TILE_NOT_IN_RACK, "That tile isn't in your rack.")
    seat_state.strikes = 0  # a timely discard breaks the consecutive-timeout run
    return _place_discard(state, seat, tile)


def _place_discard(state: GameState, seat: int, tile: Tile) -> tuple[GameState, list[Event]]:
    state.seats[seat].rack.remove(tile)
    state.discards.append((seat, tile))
    state.live_discard = tile
    state.live_discarder = seat
    state.drawn = None
    state.claims = {}
    state.phase = Phase.CLAIM_WINDOW
    return state, [("discard_placed", {"seat": seat, "tile": tile.code})]


def redeem_joker(
    state: GameState, seat: int, exposure_id: int, tile: Tile
) -> tuple[GameState, list[Event]]:
    """Swap a held natural for the joker impersonating it, in any exposure —
    own turn, before the discard, any number of times (§2.6)."""
    _require_phase(state, Phase.AWAIT_DISCARD)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat != state.turn:
        raise ActionRejected(RejectCode.NOT_YOUR_TURN, "Redeem on your own turn.")
    if tile not in seat_state.rack:
        raise ActionRejected(RejectCode.TILE_NOT_IN_RACK, "That tile isn't in your rack.")
    target: ExposureState | None = None
    owner: int | None = None
    for i, s in enumerate(state.seats):
        for e in s.exposures:
            if e.exposure_id == exposure_id:
                target, owner = e, i
    if target is None or owner is None:
        raise ActionRejected(RejectCode.NO_SUCH_EXPOSURE, "That exposure doesn't exist.")
    if target.jokers < 1 or target.natural is not tile:
        raise ActionRejected(
            RejectCode.NOT_A_JOKER_SLOT,
            f"No joker in that exposure stands in for {tile.label}.",
        )
    seat_state.rack.remove(tile)
    seat_state.rack.append(Tile.JOKER)
    seat_state.strikes = 0  # timely act — consecutive run broken
    target.jokers -= 1
    return state, [(
        "joker_redeemed",
        {"seat": seat, "owner": owner, "tile": tile.code, "exposure_id": exposure_id},
    )]


def declare_mahjong_own_turn(
    state: GameState, seat: int, card: Card
) -> tuple[GameState, list[Event]]:
    """Self-pick declaration (wall draw or post-redemption). Silent until
    valid: failure is a private ❌ and nothing else changes (§2.7, §1)."""
    _require_phase(state, Phase.AWAIT_DISCARD)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat != state.turn:
        raise ActionRejected(RejectCode.NOT_YOUR_TURN, "It isn't your turn.")
    matches = match_hand(
        list(seat_state.rack), [e.as_match() for e in seat_state.exposures], card
    )
    match = best_match(matches)
    if match is None:
        raise ActionRejected(
            RejectCode.INVALID_MAHJONG,
            "That 14 doesn't match a line on the card — play on. "
            "(Nobody else saw this.)",
        )
    if state.turn_source == "call":
        # The 14th tile arrived by discard: this is a discard win however it
        # is declared, and the discarder pays their 2x (D15 — the alternative
        # lets call-then-declare inflate a discard win into self-pick).
        return _settle_mahjong(
            state, seat, match, won_by="discard",
            discarder=state.claimed_discarder,
        )
    return _settle_mahjong(state, seat, match, won_by="self_pick", discarder=None)


# ── The claim window (§2.5) ──────────────────────────────────────────────────


def claim(
    state: GameState,
    seat: int,
    kind: str,
    tiles: list[Tile],
    card: Card,
    rng: random.Random,
) -> tuple[GameState, list[Event]]:
    """One seat's response to the live discard: "pass", "call" (with the 2+
    rack tiles completing the exposure), or "mahjong". Illegal calls and
    invalid Mahjongs convert privately to Pass — the tap never leaks (§2.5).
    The window resolves as soon as every eligible seat has responded."""
    _require_phase(state, Phase.CLAIM_WINDOW)
    state = copy.deepcopy(state)
    seat_state = _require_seat(state, seat)
    if seat == state.live_discarder:
        raise ActionRejected(RejectCode.NOT_YOUR_TURN, "It's your own discard.")
    if seat in state.claims:
        raise ActionRejected(RejectCode.ALREADY_SUBMITTED, "You've already responded.")
    if kind not in ("pass", "call", "mahjong"):
        raise ActionRejected(RejectCode.BAD_COUNT, "Pass, Call, or Mahjong.")

    events: list[Event] = []
    discard_tile = state.live_discard
    assert discard_tile is not None
    if kind == "call":
        reason = _call_rejection(state, seat_state, discard_tile, tiles)
        if reason is not None:
            events.append((
                "claim_downgraded",
                {"seat": seat, "why": reason, "private": True},
            ))
            kind, tiles = "pass", []
    elif kind == "mahjong":
        rack_with_claim = list(seat_state.rack) + [discard_tile]
        exposures = [e.as_match() for e in seat_state.exposures]
        if discard_tile is Tile.JOKER or not match_hand(rack_with_claim, exposures, card):
            # §2.7: fails privately; the seat's response becomes Pass
            events.append((
                "claim_downgraded",
                {"seat": seat, "why": "invalid_mahjong", "private": True},
            ))
            kind, tiles = "pass", []
    state.claims[seat] = (kind, list(tiles))
    # Deliberately NOT a strike reset: a free Pass tap would let a turn-AFK
    # player dodge folding forever (D17). Own-turn actions and simultaneous-
    # phase submissions are what prove presence.
    events.append(("claim_recorded", {"seat": seat}))

    responders = [
        s for s in state.live_seats() if s != state.live_discarder
    ]
    if set(state.claims) >= set(responders):
        events += _resolve_claim_window(state, card, rng)
    return state, events


def _call_rejection(
    state: GameState, seat_state: SeatState, discard_tile: Tile, tiles: list[Tile]
) -> str | None:
    """Why this call is illegal, or None if it stands. Pair-calls arrive here
    as a too-short tile list; concealed-hand calls are indistinguishable in
    v1 (no line intent is declared — `strict_exposures` ships later)."""
    if discard_tile is Tile.JOKER:
        return "joker_discard"  # discarded jokers can never be claimed
    if len(tiles) < 2:
        return "pair_call"  # an exposure is 3+: the claim plus 2+ from the rack
    if len(tiles) > 5:
        return "too_big"  # groups cap at 6 tiles: discard + 5
    if len(seat_state.rack) - len(tiles) < 1:
        # exposing your whole rack leaves nothing to discard — that position
        # is either a Mahjong (claim it as one) or a wedge; refuse privately
        return "no_discard_left"
    rack = Counter(seat_state.rack)
    for tile, n in Counter(tiles).items():
        if rack[tile] < n:
            return "not_in_rack"
    if any(t is not Tile.JOKER and t is not discard_tile for t in tiles):
        return "no_matching_group"  # naturals must match the discard exactly
    return None


def _claim_priority(state: GameState, seats: list[int]) -> int:
    """Nearest in turn order from the discarder (§2.5)."""
    assert state.live_discarder is not None
    discarder = state.live_discarder  # bound for the closure's narrowing
    n = len(state.seats)
    return min(seats, key=lambda s: (s - discarder) % n)


def _resolve_claim_window(
    state: GameState, card: Card, rng: random.Random
) -> list[Event]:
    """Collect-then-arbitrate: Mahjong > exposure > nearest in turn order."""
    discard_tile = state.live_discard
    discarder = state.live_discarder
    assert discard_tile is not None and discarder is not None
    events: list[Event] = []

    mahjong_seats = [s for s, (k, _) in state.claims.items() if k == "mahjong"]
    if mahjong_seats:
        winner = _claim_priority(state, mahjong_seats)
        seat_state = state.seats[winner]
        seat_state.rack.append(discard_tile)
        state.live_discard = None
        state.discards.pop()  # the claimed tile leaves the pit
        match = best_match(match_hand(
            list(seat_state.rack),
            [e.as_match() for e in seat_state.exposures],
            card,
        ))
        assert match is not None  # validated when the claim was recorded
        _, settle_events = _settle_mahjong(
            state, winner, match, won_by="discard", discarder=discarder,
            in_place=True,
        )
        return events + settle_events

    call_seats = [s for s, (k, _) in state.claims.items() if k == "call"]
    if call_seats:
        winner = _claim_priority(state, call_seats)
        _, tiles = state.claims[winner]
        seat_state = state.seats[winner]
        _take_from_rack(seat_state, tiles)
        jokers = sum(1 for t in tiles if t is Tile.JOKER)
        seat_state.exposures.append(ExposureState(
            exposure_id=state.next_exposure_id,
            natural=discard_tile,
            count=len(tiles) + 1,
            jokers=jokers,
        ))
        state.next_exposure_id += 1
        state.live_discard = None
        state.discards.pop()  # claimed off the pit
        state.claims = {}
        # winner reveals, then must discard; play continues to their right
        state.phase = Phase.AWAIT_DISCARD
        state.turn = winner
        state.drawn = None
        state.turn_source = "call"
        state.claimed_discarder = discarder
        events.append((
            "exposure_made",
            {"seat": winner, "tile": discard_tile.code, "count": len(tiles) + 1,
             "jokers": jokers},
        ))
        return events

    # everyone passed: the discard dies when the next one lands (§2.5) —
    # it stays in the pit; the next live seat draws.
    state.claims = {}
    return events + _draw_for_next(state, state.next_live_seat(discarder), rng)


def _draw_for_next(state: GameState, seat: int, rng: random.Random) -> list[Event]:
    if not state.wall:
        return _settle_wall_game(state)
    tile = state.wall.pop()
    state.seats[seat].rack.append(tile)
    state.drawn = tile
    state.turn = seat
    state.phase = Phase.AWAIT_DISCARD
    state.turn_source = "draw"
    state.claimed_discarder = None
    return [("tile_drawn", {"seat": seat, "wall_left": len(state.wall)})]


# ── Settlement (§2.8–§2.9) ───────────────────────────────────────────────────


def _jokerless_applies(match_hand_line: Hand, jokers_used: int) -> bool:
    """No jokers in the winning 14 — but a line with no 3+ group is jokerless
    by definition and gets no extra (§2.9's Quiet Pairs rule)."""
    return jokers_used == 0 and any(g.takes_jokers for g in match_hand_line.groups)


def _settle_mahjong(
    state: GameState,
    winner: int,
    match: Match,
    *,
    won_by: str,
    discarder: int | None,
    in_place: bool = False,
) -> tuple[GameState, list[Event]]:
    """Compute point deltas (§2.9). The service multiplies by the stake.

    4-seat: discard win → discarder pays 2×value, others 1×; self-pick →
    everyone pays 2×. Duel: discard 2×, self-pick 3×. Jokerless doubles
    either. Fallow seats pay like any other loser (§1).
    """
    if not in_place:
        state = copy.deepcopy(state)
    line = match.hand
    value = line.value
    double = 2 if _jokerless_applies(line, match.jokers_used) else 1

    deltas: dict[int, int] = {}
    if state.seat_count == 2:
        per_loser = value * (2 if won_by == "discard" else 3) * double
        loser = 1 - winner
        deltas = {winner: per_loser, loser: -per_loser}
    else:
        total = 0
        for seat in range(state.seat_count):
            if seat == winner:
                continue
            mult = 2 if (won_by == "self_pick" or seat == discarder) else 1
            deltas[seat] = -value * mult * double
            total += value * mult * double
        deltas[winner] = total

    state.outcome = Outcome(
        kind="mahjong",
        winner=winner,
        line_id=line.id,
        line_name=line.name,
        value=value,
        won_by=won_by,
        jokerless_double=double == 2,
        discarder=discarder,
        point_deltas=deltas,
    )
    state.phase = Phase.SETTLE
    state.rematch_votes = set()
    return state, [(
        "mahjong",
        {"seat": winner, "line_id": line.id, "line_name": line.name,
         "won_by": won_by, "jokerless": double == 2},
    ), ("hand_settled", {"kind": "mahjong"})]


def _settle_wall_game(state: GameState) -> list[Event]:
    """Wall out, no winner: nobody pays, escrow returns, Rematch offered."""
    state.outcome = Outcome(
        kind="wall_game", winner=None, line_id=None, line_name=None,
        value=0, won_by=None, jokerless_double=False, discarder=None,
        point_deltas={s: 0 for s in range(state.seat_count)},
    )
    state.phase = Phase.SETTLE
    state.rematch_votes = set()
    return [("wall_game", {}), ("hand_settled", {"kind": "wall_game"})]


def _settle_fallow_end(state: GameState, card: Card) -> list[Event]:
    """One live seat remains (always in Duel; D10 generalizes to 4-seat):
    the survivor collects their lowest-value live line's base payout from
    each fallow escrow — no multipliers (§1, amendment 2)."""
    live = state.live_seats()
    assert len(live) == 1
    survivor = live[0]
    seat_state = state.seats[survivor]

    seen: Counter[Tile] = Counter()
    for _, tile in state.discards:
        seen[tile] += 1
    for i, s in enumerate(state.seats):
        if i == survivor:
            continue
        for e in s.exposures:
            seen[e.natural] += e.count - e.jokers
            seen[Tile.JOKER] += e.jokers

    base = fallow_base_value(
        list(seat_state.rack),
        [e.as_match() for e in seat_state.exposures],
        card,
        seen,
    )
    deltas = {s: (-base) for s in range(state.seat_count) if s != survivor}
    deltas[survivor] = base * (state.seat_count - 1)
    state.outcome = Outcome(
        kind="fallow_end", winner=survivor, line_id=None, line_name=None,
        value=base, won_by=None, jokerless_double=False, discarder=None,
        point_deltas=deltas,
    )
    state.phase = Phase.SETTLE
    state.rematch_votes = set()
    return [
        ("fallow_end", {"survivor": survivor, "base": base}),
        ("hand_settled", {"kind": "fallow_end"}),
    ]


# ── Strikes, fallow, timeouts (§2.4, §6.2) ───────────────────────────────────


def _strike(
    state: GameState, seat: int, card: Card, *, settle: bool = True
) -> list[Event]:
    """One AFK strike; the third folds the seat fallow. In Duel — and in
    4-seat once only one live seat remains (D10) — that ends the hand.
    ``settle=False`` defers that judgement: simultaneous phases strike every
    absent seat first and judge once, so an equally-AFK seat can't collect a
    fallow payout just for sitting at a higher index (stage-3 review)."""
    s = state.seats[seat]
    s.strikes += 1
    events: list[Event] = [("strike", {"seat": seat, "strikes": s.strikes})]
    if s.strikes >= STRIKES_TO_FALLOW and not s.fallow:
        s.fallow = True
        events.append(("seat_fallow", {"seat": seat}))
        if settle:
            events += _judge_survivors(state, card)
    return events


def _judge_survivors(state: GameState, card: Card) -> list[Event]:
    """After folding: one live seat collects the fallow end; zero live seats
    (everyone timed out together) settles as ``all_fallow`` — every escrow
    returns, nobody profits from a table that died of unanimous absence."""
    live = state.live_seats()
    if len(live) == 1:
        return _settle_fallow_end(state, card)
    if len(live) == 0:
        state.outcome = Outcome(
            kind="all_fallow", winner=None, line_id=None, line_name=None,
            value=0, won_by=None, jokerless_double=False, discarder=None,
            point_deltas={s: 0 for s in range(state.seat_count)},
        )
        state.phase = Phase.SETTLE
        state.rematch_votes = set()
        return [("all_fallow", {}), ("hand_settled", {"kind": "all_fallow"})]
    return []


def force_fallow(
    state: GameState, seat: int, card: Card, rng: random.Random
) -> tuple[GameState, list[Event]]:
    """Fold a seat out immediately — a member who left the guild mid-hand.
    Their escrow stays held and pays per the fallow rules (§1); in Duel, or
    once only one live seat remains (D10), the hand settles. If it was their
    turn the hand moves straight on; a half-collected claim window resolves
    once they stop being counted; the simultaneous phases self-heal on
    their own timers."""
    state = copy.deepcopy(state)
    if not 0 <= seat < len(state.seats) or state.seats[seat].fallow:
        return state, []
    if state.phase in (Phase.LOBBY, Phase.SETTLE, Phase.CLOSED):
        raise ActionRejected(
            RejectCode.WRONG_PHASE, "No live hand to fold out of."
        )
    s = state.seats[seat]
    s.strikes = STRIKES_TO_FALLOW
    s.fallow = True
    events: list[Event] = [("seat_fallow", {"seat": seat, "left_guild": True})]
    events += _judge_survivors(state, card)
    if state.phase is Phase.AWAIT_DISCARD and state.turn == seat:
        events += _draw_for_next(state, state.next_live_seat(seat), rng)
    elif state.phase is Phase.CLAIM_WINDOW and seat != state.live_discarder:
        responders = [
            r for r in state.live_seats() if r != state.live_discarder
        ]
        if set(state.claims) >= set(responders):
            events += _resolve_claim_window(state, card, rng)
    return state, events


def timeout(
    state: GameState, card: Card, rng: random.Random
) -> tuple[GameState, list[Event]]:
    """The service's timer expired for the current phase. Auto-resolve per
    §5: missing Charleston picks pass 3 random non-jokers, missing votes are
    no, missing proposals are 0, missing claims are Pass; a turn timeout
    auto-discards the drawn tile. Every auto-resolved seat takes a strike."""
    state = copy.deepcopy(state)
    events: list[Event] = []

    if state.phase is Phase.CHARLESTON:
        for seat in range(len(state.seats)):
            if seat not in state.pending_picks:
                state.pending_picks[seat] = (
                    _random_pass(rng, state.seats[seat]), 0
                )
                events += _strike(state, seat, card, settle=False)
        events += _judge_survivors(state, card)
        if state.phase is not Phase.CHARLESTON:
            return state, events  # the folds settled the hand
        events += _resolve_charleston_if_ready(state, rng)
        return state, events

    if state.phase is Phase.CHARLESTON_VOTE:
        # any missing vote is a no — resolve early without one
        for seat in range(len(state.seats)):
            if seat not in state.votes:
                events += _strike(state, seat, card, settle=False)
        events += _judge_survivors(state, card)
        if state.phase is not Phase.CHARLESTON_VOTE:
            return state, events
        _enter_courtesy(state)
        events.append(("vote_resolved", {"passed": False}))
        return state, events

    if state.phase is Phase.COURTESY_PROPOSE:
        for seat in range(len(state.seats)):
            if seat not in state.proposals:
                state.proposals[seat] = 0
                events += _strike(state, seat, card, settle=False)
        events += _judge_survivors(state, card)
        if state.phase is not Phase.COURTESY_PROPOSE:
            return state, events
        events += _resolve_courtesy_proposals(state)
        return state, events

    if state.phase is Phase.COURTESY_PICK:
        for seat in list(state.courtesy_owed):
            if seat not in state.courtesy_gives:
                state.courtesy_gives[seat] = _random_pass(
                    rng, state.seats[seat], state.courtesy_owed[seat]
                )
                events += _strike(state, seat, card, settle=False)
        events += _judge_survivors(state, card)
        if state.phase is not Phase.COURTESY_PICK:
            return state, events
        events += _apply_courtesy(state)
        return state, events

    if state.phase is Phase.AWAIT_DISCARD:
        seat = state.turn
        seat_state = state.seats[seat]
        tile = state.drawn
        if not seat_state.rack:
            # unreachable while calls must leave a discard behind, but an
            # empty rack must strike-and-move-on, never crash the timer
            events += _strike(state, seat, card)
            if state.phase is Phase.AWAIT_DISCARD:
                events += _draw_for_next(state, state.next_live_seat(seat), rng)
            return state, events
        if tile is None or tile not in seat_state.rack:
            # dealer's first turn / post-claim: no drawn tile — a random
            # non-joker goes (jokers are too precious to time away)
            pool = [t for t in seat_state.rack if t is not Tile.JOKER]
            tile = rng.choice(pool) if pool else seat_state.rack[0]
        events += _strike(state, seat, card)
        if state.phase is not Phase.AWAIT_DISCARD:
            return state, events  # the strike settled the hand
        if state.seats[seat].fallow:
            # seat just folded (4-seat, 2+ live): no discard — next draws
            events += _draw_for_next(state, state.next_live_seat(seat), rng)
            return state, events
        _, place_events = _place_discard(state, seat, tile)
        return state, events + place_events

    if state.phase is Phase.CLAIM_WINDOW:
        responders = [
            s for s in state.live_seats() if s != state.live_discarder
        ]
        for seat in responders:
            if seat not in state.claims:
                state.claims[seat] = ("pass", [])
                # a claim-window lapse is not an AFK strike: staying silent
                # IS the normal way to pass (§2.5's window just closes)
        events += _resolve_claim_window(state, card, rng)
        return state, events

    raise ActionRejected(RejectCode.WRONG_PHASE, "Nothing here can time out.")


# ── Settle screen (§6.13) ────────────────────────────────────────────────────


def rematch_vote(state: GameState, seat: int) -> tuple[GameState, list[Event]]:
    """Unanimous re-up (D11): every seat presses Rematch; the service deals
    the next hand (with fresh escrow) once the last vote lands."""
    _require_phase(state, Phase.SETTLE)
    state = copy.deepcopy(state)
    if not 0 <= seat < len(state.seats):
        raise ActionRejected(RejectCode.NOT_SEATED, "You're not at this table.")
    state.rematch_votes.add(seat)
    ready = state.rematch_votes >= set(range(len(state.seats)))
    return state, [("rematch_vote", {"seat": seat, "ready": ready})]


def rematch_ready(state: GameState) -> bool:
    return (
        state.phase is Phase.SETTLE
        and state.rematch_votes >= set(range(len(state.seats)))
    )
