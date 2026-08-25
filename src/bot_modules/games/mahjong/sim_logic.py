"""Headless card simulation — stage 2 of docs/plans/mahjong-card-generator.md.

A card is the whole difficulty design of American mahjong: the tiles, the
wall and the turn order never change, so how a table plays is decided
entirely by which lines are printed and what they pay. A published card gets
that tuned by a year of living-room play. We substitute volume: seat four
:func:`bot_logic.decide` brains at a table, play the candidate card a few
thousand times, and read the answers off the results.

What comes back is per line — how often a seat *aimed* at it, how often it
actually completed, how long that took, how many jokers it ate — and per
card: wall-game rate, mean length, how much of the card ever wins at all.
That is enough to price each line by measured difficulty (stage 4) and to
throw away the ones nobody can finish.

Pure and reproducible (G4): the caller supplies the seed, the engine already
takes an injected wall and rng (parent plan D14), and nothing here touches
the database, Discord or the clock. Same seed and same card ⇒ byte-identical
report, which is what makes a published value calibration falsifiable.

Not a rules engine of its own — every transition goes through ``game_logic``
and every decision through ``bot_logic``, so the simulation can only ever be
as right as the game itself. That is the point: it measures *this* engine.
"""

from __future__ import annotations

import random
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from bot_modules.games.mahjong import game_logic as engine
from bot_modules.games.mahjong.bot_logic import BotAction, decide
from bot_modules.games.mahjong.card_logic import Card
from bot_modules.games.mahjong.game_logic import (
    ActionRejected,
    GameState,
    Phase,
    TableConfig,
    obtainable_seen,
)
from bot_modules.games.mahjong import match_logic
from bot_modules.games.mahjong.match_logic import closest_lines
from bot_modules.games.mahjong.tiles import shuffled_wall

#: A hand cannot legally run forever — 152 tiles minus the deal is ~90 draws,
#: and every step either acts or times out a phase. Well above any real game;
#: hitting it means a stuck table, which the report surfaces rather than hides.
MAX_STEPS_PER_GAME = 2000

#: Below this many holds, a completion rate is noise dressed as a number.
#: At 500 games / 4 seats a healthy line holds dozens; ten is the floor at
#: which one more win moves the figure by less than ten points.
MIN_HELD_FOR_COMPLETION = 10

#: Synthetic seat ids. Negative, like real bot seats (bot_logic.is_bot_id), so
#: anything that branches on "is this a bot" behaves as it would in practice.
def _seat_id(index: int) -> int:
    return -(index + 1)


@dataclass
class HandStat:
    """What the simulation learned about one card line."""

    hand_id: str
    section: str
    name: str
    value: int
    concealed: bool
    #: seat-hands that entered play with this as their closest line
    targeted: int = 0
    #: seat-hands still closest to this line when the hand ended, however
    #: they arrived at it
    held: int = 0
    #: of those, the ones that also *opened* on it — so `kept` <= `targeted`
    #: and `kept` <= `held`, and the two derived rates below are real
    #: fractions rather than the unbounded ratio an aggregate would give
    kept: int = 0
    wins: int = 0
    #: summed discards-on-the-table at each win, for the mean
    win_turns: int = 0
    jokerless_wins: int = 0

    @property
    def mean_turns_to_win(self) -> float | None:
        return self.win_turns / self.wins if self.wins else None

    @property
    def completion(self) -> float | None:
        """**The ease-of-play number.** Of the seats still playing toward
        this line when the hand ended, the share that finished it.

        ``held`` is the right denominator: a winner is by definition still
        on their line at the settle, and a seat that walled out chasing it
        is counted too. So this reads as "if I commit to this hand, how
        often does it pay" — which is the question a member is really
        asking, and the one `wins_per_target` cannot answer because seats
        pivot away before the end.

        None below :data:`MIN_HELD_FOR_COMPLETION`, rather than a
        meaningless fraction of three.
        """
        if self.held < MIN_HELD_FOR_COMPLETION:
            return None
        return self.wins / self.held

    @property
    def retention(self) -> float | None:
        """Of the seats that *opened* on this line, the share still on it at
        the end — the diagnostic that separates *hard* from *trap*.

        A hard line players stay with shows high retention and low
        completion. A trap shows the reverse: `qp-2` drew 670 openings and
        held on to a handful, because it looks nearest at the deal and then
        cannot be finished.

        Measured per seat, not as ``held / targeted``: seats pivot *onto* a
        line too, so the aggregate ratio runs past 100% and conflates a hand
        people keep with one they arrive at late.
        """
        if not self.targeted:
            return None
        return self.kept / self.targeted

    @property
    def arrived(self) -> int:
        """Seats that finished on this line without starting on it — a
        pivot destination, and the direct evidence pivot paths work."""
        return self.held - self.kept

    @property
    def pull(self) -> int:
        """Seats gained (or lost) between the deal and the settle.

        The generator's whole selection objective is pivot paths, and this
        is the only direct evidence of them: a line nobody opens toward but
        several finish on is a pivot destination. Positive is a magnet,
        negative a hand people abandon.
        """
        return self.held - self.targeted

    @property
    def jokerless_rate(self) -> float | None:
        """Share of this line's wins that used no joker at all — the card's
        joker economy, read where it actually bites."""
        return self.jokerless_wins / self.wins if self.wins else None

    @property
    def wins_per_target(self) -> float | None:
        """Wins divided by opening targets.

        **Not a probability, and it routinely exceeds 1.** ``targeted``
        counts only the line a seat was closest to as play opened, while a
        win can land on any line the seat pivoted onto later — which is the
        whole point of pivot paths. Read it as pull: above 1 means the line
        collects players mid-hand, near 0 means it attracts them at the deal
        and then strands them. Both extremes are worth looking at.
        """
        return self.wins / self.targeted if self.targeted else None


@dataclass
class SimReport:
    """Everything one :func:`simulate` run measured. Rates are properties so
    the raw counts stay addable across runs."""

    card_id: str
    games: int
    seat_count: int
    seed: int
    hands: dict[str, HandStat] = field(default_factory=dict)
    mahjongs: int = 0
    wall_games: int = 0
    fallow_ends: int = 0
    other_ends: int = 0
    total_turns: int = 0
    #: bot actions the engine refused — always a defect, never a card fact
    rejected_actions: int = 0
    #: games that hit MAX_STEPS_PER_GAME — likewise a defect signal
    stuck_games: int = 0

    @property
    def win_rate(self) -> float:
        return self.mahjongs / self.games if self.games else 0.0

    @property
    def wall_game_rate(self) -> float:
        return self.wall_games / self.games if self.games else 0.0

    @property
    def mean_turns(self) -> float:
        return self.total_turns / self.games if self.games else 0.0

    @property
    def dead_lines(self) -> list[HandStat]:
        """Lines nothing ever won on — dead ink, in card order."""
        return [h for h in self.hands.values() if h.wins == 0]

    @property
    def never_targeted(self) -> list[HandStat]:
        """Worse than dead: lines no seat ever even aimed at."""
        return [h for h in self.hands.values() if h.targeted == 0]

    def playable_lines(self, floor: float = 0.05) -> list[HandStat]:
        """Lines that pay often enough to be worth printing, once enough
        seats have held them to judge."""
        return [
            h for h in self.hands.values()
            if h.completion is not None and h.completion >= floor
        ]

    def unjudged_lines(self) -> list[HandStat]:
        """Lines too rarely held to rate — more games needed, not a verdict."""
        return [h for h in self.hands.values() if h.completion is None]

    @property
    def healthy(self) -> bool:
        """No stuck tables and no refused bot actions. A card can be *bad*
        and still healthy; unhealthy means the harness or engine misbehaved
        and the numbers should not be trusted."""
        return self.stuck_games == 0 and self.rejected_actions == 0


def _rng_for(seed: int, game_index: int) -> random.Random:
    """Each game gets its own stream, derived from (seed, index).

    Not one shared generator: per-game seeding is what makes a run's result
    independent of how many workers computed it, and lets a single suspect
    game be replayed on its own. 1_000_003 is prime, so ordinary seeds and
    indices cannot alias onto the same stream.
    """
    return random.Random(seed * 1_000_003 + game_index)


def _empty_report(
    card: Card, games: int, seat_count: int, seed: int
) -> SimReport:
    return SimReport(
        card_id=card.card_id, games=games, seat_count=seat_count, seed=seed,
        hands={
            h.id: HandStat(
                hand_id=h.id, section=h.section, name=h.name,
                value=h.value, concealed=h.concealed,
            )
            for h in card.hands
        },
    )


def merge_into(target: SimReport, other: SimReport) -> None:
    """Fold one report's counts into another. Every measured field is a
    plain count, which is exactly why shards can be merged at all.

    ``games`` accumulates like the rest, so two independently produced
    reports merge into one whose rates are right. Callers building an
    aggregate must therefore start it at zero games and let the merges add
    up — which is what :func:`simulate` does on its parallel path.
    """
    target.games += other.games
    target.mahjongs += other.mahjongs
    target.wall_games += other.wall_games
    target.fallow_ends += other.fallow_ends
    target.other_ends += other.other_ends
    target.total_turns += other.total_turns
    target.rejected_actions += other.rejected_actions
    target.stuck_games += other.stuck_games
    for hand_id, stat in other.hands.items():
        into = target.hands[hand_id]
        into.targeted += stat.targeted
        into.held += stat.held
        into.kept += stat.kept
        into.wins += stat.wins
        into.win_turns += stat.win_turns
        into.jokerless_wins += stat.jokerless_wins


def _run_shard(job: tuple[Card, TableConfig, int, int, int, bool]) -> SimReport:
    """One worker's slice: games [start, stop). Module-level and
    argument-closed so it pickles into a process pool."""
    card, config, seed, start, stop, rank_by_effort = job
    # Set inside the worker, where this process runs nothing else. The flag
    # has to reach the matcher somehow and it is read deep inside a call
    # chain the bot brain owns; threading a parameter through six signatures
    # for an experiment would be worse than one assignment made here.
    match_logic.RANK_BY_EFFORT = rank_by_effort
    report = _empty_report(card, stop - start, config.seat_count, seed)
    for i in range(start, stop):
        _play_one(card, config, _rng_for(seed, i), report)
    return report


def simulate(
    card: Card,
    *,
    games: int,
    seat_count: int = 4,
    seed: int = 0,
    wall_trim: int = 0,
    second_charleston: bool = True,
    workers: int = 1,
    rank_by_effort: bool = False,
) -> SimReport:
    """Play ``card`` ``games`` times with every seat botted, and measure it.

    ``rank_by_effort`` switches the assist engine's line ranking for the
    whole run (`match_logic.RANK_BY_EFFORT`) so the two can be A/B'd at
    identical seeds. It is off by default, matching production.

    Deterministic in (card, games, seat_count, seed, wall_trim,
    second_charleston, rank_by_effort) — and *not* in ``workers``: game *i* always runs on
    the stream derived from (seed, i), so 1 worker and 12 return identical
    reports. A real game costs seconds of bot thinking, so anything past a
    few hundred games wants ``workers`` above 1.
    """
    if games < 1:
        raise ValueError("games must be >= 1")
    if workers < 1:
        raise ValueError("workers must be >= 1")
    match_logic.RANK_BY_EFFORT = rank_by_effort
    config = TableConfig(
        seat_count=seat_count,
        wall_trim=wall_trim,
        second_charleston=second_charleston,
    )
    report = _empty_report(card, games, seat_count, seed)

    if workers == 1:
        for i in range(games):
            _play_one(card, config, _rng_for(seed, i), report)
        return report

    # Zero games: merge_into accumulates the shards' counts, and a
    # pre-sized total would then double.
    report.games = 0
    shards = min(workers, games)
    edges = [games * k // shards for k in range(shards + 1)]
    jobs = [
        (card, config, seed, edges[k], edges[k + 1], rank_by_effort)
        for k in range(shards)
        if edges[k] < edges[k + 1]
    ]
    with ProcessPoolExecutor(max_workers=shards) as pool:
        for sub in pool.map(_run_shard, jobs):
            merge_into(report, sub)
    return report


def _play_one(
    card: Card, config: TableConfig, rng: random.Random, report: SimReport
) -> None:
    state = engine.create_table(config, _seat_id(0))
    for i in range(1, config.seat_count):
        state, _ = engine.join_table(state, _seat_id(i))
    state, _ = engine.deal(state, shuffled_wall(rng))

    targets_taken = False
    opening: dict[int, str] = {}
    for _ in range(MAX_STEPS_PER_GAME):
        if state.phase in (Phase.SETTLE, Phase.CLOSED):
            break
        # The opening target is read once, at the first decision of play
        # proper — after the Charleston has finished moving tiles, before
        # any discard has changed the picture.
        if not targets_taken and state.phase is Phase.AWAIT_DISCARD:
            opening = _closest_lines_by_seat(state, card)
            targets_taken = True
        state = _step(state, card, rng, report)
    else:
        report.stuck_games += 1

    # The closing target, read through the same lens as the opening one.
    # Quints and other late-blooming families are invisible to `targeted` by
    # construction — nobody is closest to a line needing five of a tile at
    # the deal — so without this the report cannot see them at all.
    closing = _closest_lines_by_seat(state, card)
    for seat, hand_id in opening.items():
        report.hands[hand_id].targeted += 1
    for seat, hand_id in closing.items():
        report.hands[hand_id].held += 1
        if opening.get(seat) == hand_id:
            report.hands[hand_id].kept += 1
    report.total_turns += state.discard_count
    _record_outcome(state, report)


def _step(
    state: GameState, card: Card, rng: random.Random, report: SimReport
) -> GameState:
    """One action from the first seat that has one, or a phase timeout when
    no seat does (which is how the engine resolves a window nobody fills)."""
    for seat in range(state.seat_count):
        action = decide(state, seat, card, rng, practice=True)
        if action is None:
            continue
        try:
            state, _ = _apply(state, seat, action, card, rng)
        except ActionRejected:
            # The brain proposed something the engine refused. That is a bug
            # in one of them, never a property of the card — count it, and
            # let the timeout below keep the table moving so one bad seat
            # cannot take a whole generation run down with it.
            report.rejected_actions += 1
            break
        return state
    state, _ = engine.timeout(state, card, rng)
    return state


def _apply(
    state: GameState,
    seat: int,
    action: BotAction,
    card: Card,
    rng: random.Random,
) -> tuple[GameState, list]:
    """The service's action dispatch (`MahjongService._tx_act`) minus the
    database — same names, same engine calls, so the bots play the game the
    members play."""
    kw = action.kwargs
    match action.action:
        case "charleston_pick":
            return engine.charleston_pick(
                state, seat, kw["tiles"], kw["blind_n"], rng)
        case "vote":
            return engine.vote_second_charleston(state, seat, kw["yes"])
        case "courtesy_propose":
            return engine.courtesy_propose(state, seat, kw["n"])
        case "courtesy_pick":
            return engine.courtesy_pick(state, seat, kw["tiles"])
        case "discard":
            return engine.discard(state, seat, kw["tile"])
        case "claim":
            return engine.claim(
                state, seat, kw["kind"], kw.get("tiles", []), card, rng)
        case "redeem_joker":
            return engine.redeem_joker(
                state, seat, kw["exposure_id"], kw["tile"])
        case "mahjong":
            return engine.declare_mahjong_own_turn(state, seat, card)
    raise ValueError(f"sim cannot apply bot action {action.action!r}")


def _closest_lines_by_seat(state: GameState, card: Card) -> dict[int, str]:
    """Each seat's closest line right now, by seat.

    Read through the same lens the bot uses to choose, so a "target" means
    what the seat was actually playing toward. Called twice per game — as
    play opens and again at the end — and the two are compared per seat, so
    a line people keep can be told apart from one they arrive at late.
    """
    out: dict[int, str] = {}
    for seat in range(state.seat_count):
        seat_state = state.seats[seat]
        if seat_state.fallow:
            continue  # a fallow seat is not playing toward anything
        prospects = closest_lines(
            list(seat_state.rack),
            [e.as_match() for e in seat_state.exposures],
            card,
            obtainable_seen(state, seat, card),
            limit=1,
        )
        if prospects:
            out[seat] = prospects[0].hand.id
    return out


def _record_outcome(state: GameState, report: SimReport) -> None:
    outcome = state.outcome
    if outcome is None:
        report.other_ends += 1
        return
    if outcome.kind == "wall_game":
        report.wall_games += 1
        return
    if outcome.kind == "fallow_end":
        report.fallow_ends += 1
        return
    if outcome.kind != "mahjong" or outcome.line_id is None:
        report.other_ends += 1
        return

    report.mahjongs += 1
    stat = report.hands.get(outcome.line_id)
    if stat is None:  # a line not on the card we were handed: impossible
        report.other_ends += 1
        return
    stat.wins += 1
    stat.win_turns += state.discard_count
    if outcome.jokerless_double:
        stat.jokerless_wins += 1


def demand_spread(card: Card) -> Counter[str]:
    """How many lines want each *rank token*, as a cheap structural read on
    whether the card spreads its appetite. Token-level, not tile-level: a
    line's physical tiles depend on the binding a player chooses, and no
    card-time analysis can know that. Tile-level demand is what
    :func:`simulate` measures by actually playing.
    """
    spread: Counter[str] = Counter()
    for hand in card.hands:
        for token in {g.rank for g in hand.groups}:
            spread[token] += 1
    return spread


def format_report(report: SimReport, *, limit: int | None = None) -> str:
    """The CLI's table — one row per line, worst conversion first, so the
    lines a card should lose are the ones you read first."""
    # Worst first: never won, then rarely won, then by how many players the
    # line pulled in for that — so a line that attracts and strands (qp-2's
    # failure mode) sorts straight to the top where it belongs.
    rows = sorted(
        report.hands.values(), key=lambda h: (h.wins, -h.targeted, h.hand_id)
    )
    if limit is not None:
        rows = rows[:limit]

    out = [
        f"{report.card_id}: {report.games} games, {report.seat_count} seats, "
        f"seed {report.seed}",
        f"  mahjong {report.win_rate:.1%}   wall {report.wall_game_rate:.1%}   "
        f"fallow {report.fallow_ends}   mean turns {report.mean_turns:.1f}",
    ]
    if report.other_ends:
        # Games that ended as none of the above. Without this the run reads
        # as "0% everything" with no clue where the games went.
        out.append(
            f"  {report.other_ends} game(s) ended in no scored outcome"
        )
    if not report.healthy:
        out.append(
            f"  ⚠ UNHEALTHY: {report.stuck_games} stuck games, "
            f"{report.rejected_actions} refused bot actions — "
            f"the numbers below are not trustworthy"
        )
    out.append(
        f"  {'line':<16}{'value':>6}{'opened':>8}{'held':>6}{'keep':>7}"
        f"{'came':>6}{'wins':>6}{'done':>7}{'turns':>8}"
    )
    for h in rows:
        turns = "—" if h.mean_turns_to_win is None else f"{h.mean_turns_to_win:.0f}"
        keep = "—" if h.retention is None else f"{h.retention:.0%}"
        arrived = f"+{h.arrived}" if h.arrived > 0 else "—"
        done = "—" if h.completion is None else f"{h.completion:.0%}"
        out.append(
            f"  {h.hand_id:<16}{h.value:>6}{h.targeted:>8}{h.held:>6}"
            f"{keep:>7}{arrived:>6}{h.wins:>6}{done:>7}{turns:>8}"
        )
    judged = [h for h in report.hands.values() if h.completion is not None]
    if judged:
        playable = report.playable_lines()
        out.append(
            f"  {len(playable)}/{len(judged)} judged lines complete at least "
            f"5% of the time; {len(report.unjudged_lines())} too rarely held "
            f"to rate"
        )
    dead = report.dead_lines
    if dead:
        out.append(
            f"  {len(dead)} line(s) never won: "
            + ", ".join(h.hand_id for h in dead)
        )
    return "\n".join(out)
