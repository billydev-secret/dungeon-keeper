"""The AI seat's brain — stage 1 of docs/plans/mahjong-bots.md.

One pure decider per phase, dispatched through :func:`decide`, all built on
the assistance engine (plan B1): ``closest_lines`` picks the target lines,
``suggest_discard`` (with its danger rail) picks the throw, ``match_hand``
decides Mahjong, and calls are judged by *simulating* the exposure and
asking whether the best line gets strictly closer — no private re-derivation
of bindings. The service driver (stage 2) feeds a :class:`BotAction`'s
``action``/``kwargs`` straight into ``MahjongService.act``; nothing here
touches Discord, the db, or the clock (plan B2).

House style of play, per plan B10: pass the three least useful tiles in the
Charleston, never blind; vote yes to the second Charleston in practice (more
reps for the human) and no in fill games; propose courtesy 0; redeem a joker
only for a natural no live line consumes (a conservative rule — pairs and
singles are the one place a natural beats a joker, and the Prospect doesn't
say which slot wants it); on rematch, follow the humans, never lead.

A bot must always act where a human may hesitate: the discard fallback
ignores the danger rail rather than stall, and every decider returns a
legal action for its phase or ``None`` when this seat has nothing to do.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from bot_modules.games.mahjong.card_logic import Card
from bot_modules.games.mahjong.game_logic import (
    GameState,
    Phase,
    obtainable_seen,
)
from bot_modules.games.mahjong.match_logic import (
    Prospect,
    call_advice,
    closest_lines,
    dangerous_tiles,
    match_hand,
    suggest_discard,
)
from bot_modules.games.mahjong.tiles import TILE_ORDER, Tile

#: Bot seats carry synthetic negative member ids (plan B3).
def is_bot_id(member_id: int) -> bool:
    return member_id < 0


def bot_member_id(table_id: int, seat_index: int) -> int:
    """Per-table synthetic id — can never collide with a snowflake or with
    another live bot table (the one-live-seat unique index sees them all)."""
    return -(table_id * 10 + seat_index)


_BOT_NAMES = ("Fern", "Bramble", "Wisteria", "Clover")


def bot_name(member_id: int) -> str:
    """Deterministic flora display name for a bot seat (plan B11) — the 🌱
    prefix is what makes a bot unmistakable in every embed."""
    return f"🌱 {_BOT_NAMES[(-member_id) % 10 % len(_BOT_NAMES)]}"


@dataclass(frozen=True)
class BotAction:
    """One action for ``MahjongService.act`` — name plus payload, the
    driver adds ``member_id``."""

    action: str
    kwargs: dict = field(default_factory=dict)


# ── Tile valuation (shared by charleston / courtesy / discard fallback) ─────


def _prospects(state: GameState, seat: int, card: Card) -> list[Prospect]:
    seat_state = state.seats[seat]
    return closest_lines(
        list(seat_state.rack),
        [e.as_match() for e in seat_state.exposures],
        card,
        obtainable_seen(state, seat, card),
        limit=None,
    )


def _usefulness(tile: Tile, rack: list[Tile], prospects: list[Prospect]) -> int:
    """How many live lines consume at least one copy — suggest_discard's
    ranking, exposed for the pickers that must choose several tiles."""
    have = sum(1 for t in rack if t is tile)
    return sum(
        1
        for p in prospects
        if have - dict(p.dead_weight).get(tile, 0) > 0
    )


def worst_tiles(state: GameState, seat: int, card: Card, n: int) -> list[Tile]:
    """The ``n`` least useful non-jokers in hand — what a bot passes in the
    Charleston and gives in courtesy. Deterministic: usefulness ascending,
    then display order."""
    rack = list(state.seats[seat].rack)
    prospects = _prospects(state, seat, card)
    pool = [t for t in rack if t is not Tile.JOKER]
    pool.sort(key=lambda t: (_usefulness(t, rack, prospects), TILE_ORDER[t]))
    return pool[:n]


def _choose_discard(state: GameState, seat: int, card: Card) -> Tile:
    """suggest_discard when it speaks; otherwise the least useful non-joker;
    a joker only when the rack holds nothing else (heavy exposures can leave
    an all-joker rack, and a bot must always discard — review round P5).
    The fallbacks ignore the danger rail — a bot cannot stall (module doc)."""
    rack = list(state.seats[seat].rack)
    prospects = _prospects(state, seat, card)
    if prospects:
        danger = dangerous_tiles(
            card,
            [
                [e.as_match() for e in s.exposures]
                for i, s in enumerate(state.seats)
                if i != seat and not s.fallow
            ],
        )
        pick = suggest_discard(rack, prospects, danger)
        if pick is not None:
            return pick
    worst = worst_tiles(state, seat, card, 1)
    return worst[0] if worst else Tile.JOKER


# ── Claim judgement ──────────────────────────────────────────────────────────


def _call_tiles(state: GameState, seat: int, card: Card) -> list[Tile] | None:
    """Rack tiles to expose with the live discard, or None to not call —
    the shared judgement in :func:`match_logic.call_advice`, which the
    coach readout also renders, so bot and advice can never diverge."""
    tile = state.live_discard
    assert tile is not None
    seat_state = state.seats[seat]
    advice = call_advice(
        list(seat_state.rack),
        [e.as_match() for e in seat_state.exposures],
        card,
        obtainable_seen(state, seat, card),
        tile,
    )
    return list(advice.tiles) if advice is not None else None


# ── The per-phase deciders ───────────────────────────────────────────────────


def decide(
    state: GameState,
    seat: int,
    card: Card,
    rng: random.Random,
    *,
    practice: bool = True,
) -> BotAction | None:
    """This bot seat's next action, or None when it has nothing to do."""
    seat_state = state.seats[seat]
    if state.phase is Phase.SETTLE:
        # a fallow seat still votes rematch — the engine accepts it and the
        # next deal resets fallow, so blocking here hung the table until
        # the settle window expired (review round P7)
        if seat not in state.rematch_votes:
            humans = [
                i for i, s in enumerate(state.seats)
                if not is_bot_id(s.member_id)
            ]
            if humans and all(h in state.rematch_votes for h in humans):
                return BotAction("rematch")  # follow the humans, never lead
        return None
    if seat_state.fallow:
        return None

    if state.phase is Phase.CHARLESTON and seat not in state.pending_picks:
        return BotAction(
            "charleston_pick",
            {"tiles": worst_tiles(state, seat, card, 3), "blind_n": 0},
        )

    if state.phase is Phase.CHARLESTON_VOTE and seat not in state.votes:
        return BotAction("vote", {"yes": practice})

    if state.phase is Phase.COURTESY_PROPOSE and seat not in state.proposals:
        return BotAction("courtesy_propose", {"n": 0})

    if (
        state.phase is Phase.COURTESY_PICK
        and seat in state.courtesy_owed
        and seat not in state.courtesy_gives
    ):
        owed = state.courtesy_owed[seat]
        return BotAction(
            "courtesy_pick", {"tiles": worst_tiles(state, seat, card, owed)}
        )

    if state.phase is Phase.AWAIT_DISCARD and state.turn == seat:
        rack = list(seat_state.rack)
        exposures = [e.as_match() for e in seat_state.exposures]
        if match_hand(rack, exposures, card):
            return BotAction("mahjong")
        redemption = _spare_redemption(state, seat, card)
        if redemption is not None:
            exposure_id, natural = redemption
            return BotAction(
                "redeem_joker", {"exposure_id": exposure_id, "tile": natural}
            )
        return BotAction("discard", {"tile": _choose_discard(state, seat, card)})

    if (
        state.phase is Phase.CLAIM_WINDOW
        and seat != state.live_discarder
        and seat not in state.claims
        and state.live_discard is not None
    ):
        tile = state.live_discard
        rack = list(seat_state.rack)
        exposures = [e.as_match() for e in seat_state.exposures]
        if tile is not Tile.JOKER and match_hand(rack + [tile], exposures, card):
            return BotAction("claim", {"kind": "mahjong", "tiles": []})
        if tile is not Tile.JOKER:
            tiles = _call_tiles(state, seat, card)
            if tiles is not None:
                return BotAction("claim", {"kind": "call", "tiles": tiles})
        return BotAction("claim", {"kind": "pass", "tiles": []})

    return None


def _spare_redemption(
    state: GameState, seat: int, card: Card
) -> tuple[int, Tile] | None:
    """A redemption worth making: any exposure's joker whose natural we hold
    and no live line consumes — trading a spare natural for a wild is pure
    upside; anything the hand still wants stays put (module doc)."""
    rack = list(state.seats[seat].rack)
    held = set(rack)
    prospects = _prospects(state, seat, card)
    for s in state.seats:
        for e in s.exposures:
            if e.jokers > 0 and e.natural in held:
                if _usefulness(e.natural, rack, prospects) == 0:
                    return e.exposure_id, e.natural
    return None
