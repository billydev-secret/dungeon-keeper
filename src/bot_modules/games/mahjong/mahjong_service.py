"""Persistence, escrow, timers, table lifecycle — stage 4 of docs/plans/meadow-mahjong.md.

The service owns everything the pure engine refuses to know: SQLite rows,
coins, clocks, and randomness. The split is strict — ``game_logic`` computes
*point* deltas and never sleeps; this module multiplies by the stake, moves
the coins through ``economy_wager_service`` (plan D4), persists the
serialized state after **every** transition, and re-arms ``asyncio`` timers
that feed ``timeout`` actions back in (§5). On restart, live tables reload
and re-arm with their *remaining* time (§6.2's "timeouts survive restart").

Escrow model: one hold per seat per hand, ``game_type='mahjong'``,
``game_id = table_id·100000 + hand_no`` (unique per hand, so a rematch can
re-escrow while hand 1's settled rows stand). Escrow = card max × payout cap
(6 Duel / 4-seat 4, §2.9) × stake. Debits at seating; a hand settles through
``settle_split`` (zero-sum deltas over returned escrow); wall games,
cancels, dissolves and purges refund. A table whose state can't load at
boot refunds and closes — that is mahjong's orphaned-escrow safety net.

Discord glue registers a listener; the service calls it after every
transition (player-driven or timer-driven) with the new state and the
engine's events. Nothing here imports discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from bot_modules.core.db_utils import get_config_value, open_db
from bot_modules.games.mahjong import game_logic as engine
from bot_modules.games.mahjong.game_logic import ASSIST_MODES
from bot_modules.games.mahjong.card_logic import Card, CardError, load_card
from bot_modules.games.mahjong.game_logic import (
    ActionRejected,
    GameState,
    Phase,
    TableConfig,
)
from bot_modules.games.mahjong.tiles import shuffled_wall
from bot_modules.services import economy_wager_service as wager_svc
from bot_modules.services.no_contact_service import is_no_contact_conn

log = logging.getLogger(__name__)

GAME_TYPE = "mahjong"
#: Escrow multiplier cap per mode (§2.9): the worst a seat can lose.
PAYOUT_CAP = {2: 6, 4: 4}
#: Seconds from table-full to the deal (§6.3's countdown footer).
DEAL_COUNTDOWN = 10.0
#: Lobby that never fills / settle screen nobody rematches: dissolve (§6.2).
LOBBY_LIFETIME = 600.0

_HAND_GID_BASE = 100_000


@dataclass(frozen=True)
class MahjongSettings:
    """Per-guild house rules (§8), stored as ``config`` KV ``mahjong_*`` keys.

    Every key here has a dashboard writer (stage 7) — and only keys here may
    be written, so a staged dial can never outlive its reader.
    """

    enabled: bool = False
    claim_window_4: float = 8.0
    claim_window_2: float = 6.0
    turn_timer: float = 45.0
    phase_timer: float = 60.0
    duel_wall_trim: int = 0
    second_charleston: bool = True
    stakes_allowed: tuple[int, ...] = (1, 2, 5)
    assist_default: str = "gap"  # house default for members with no pick (A8)

    def claim_window(self, seat_count: int) -> float:
        return self.claim_window_2 if seat_count == 2 else self.claim_window_4


def load_settings(conn, guild_id: int) -> MahjongSettings:
    def _f(key: str, default: float) -> float:
        try:
            return float(get_config_value(conn, f"mahjong_{key}", str(default), guild_id))
        except ValueError:
            return default

    def _i(key: str, default: int) -> int:
        try:
            return int(get_config_value(conn, f"mahjong_{key}", str(default), guild_id))
        except ValueError:
            return default

    raw_stakes = get_config_value(conn, "mahjong_stakes_allowed", "1,2,5", guild_id)
    try:
        stakes = tuple(sorted({int(s) for s in raw_stakes.split(",") if int(s) > 0}))
    except ValueError:
        stakes = (1, 2, 5)
    return MahjongSettings(
        enabled=get_config_value(conn, "mahjong_enabled", "0", guild_id) == "1",
        claim_window_4=_f("claim_window_4", 8.0),
        claim_window_2=_f("claim_window_2", 6.0),
        turn_timer=_f("turn_timer", 45.0),
        phase_timer=_f("phase_timer", 60.0),
        duel_wall_trim=_i("duel_wall_trim", 0),
        second_charleston=get_config_value(
            conn, "mahjong_second_charleston", "1", guild_id) == "1",
        stakes_allowed=stakes or (1, 2, 5),
        assist_default=_assist(
            get_config_value(conn, "mahjong_assist_default", "gap", guild_id)
        ),
    )


def _assist(raw: str, fallback: str = "gap") -> str:
    """THE clamp to a known mode — a corrupt store degrades, never raises.
    One clamp, two fallbacks: the dial degrades to the shipped default, a
    member row to the (already-clamped) guild default (F11)."""
    return raw if raw in ASSIST_MODES else fallback


def get_assist_mode(
    conn, guild_id: int, user_id: int, settings: MahjongSettings | None = None
) -> str:
    """The member's chosen level, or the guild default when they never chose
    (or the stored value is no longer a mode we know). Pass ``settings`` when
    you already hold them; without them only the one dial key is read — the
    rack-render path pays for a single config value, not all nine (F5)."""
    row = conn.execute(
        "SELECT mode FROM mahjong_prefs WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    ).fetchone()
    fallback = (
        settings.assist_default
        if settings is not None
        else _assist(get_config_value(conn, "mahjong_assist_default", "gap", guild_id))
    )
    return _assist(str(row[0]), fallback) if row is not None else fallback


def set_assist_mode(conn, guild_id: int, user_id: int, mode: str) -> None:
    if mode not in ASSIST_MODES:
        raise ValueError(f"unknown assist mode: {mode!r}")
    conn.execute(
        "INSERT INTO mahjong_prefs (guild_id, user_id, mode, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (guild_id, user_id) DO UPDATE SET mode = excluded.mode, "
        "updated_at = excluded.updated_at",
        (guild_id, user_id, mode, time.time()),
    )  # open_db commits on scope exit — no explicit commit (F12)


#: The surface's ordinary failure — used for genuine stale-table races AND
#: for the no-contact refusal on join, so the two are indistinguishable by
#: construction (docs/no_contact_spec.md: a believable race that explains
#: the absence). Never used for anything a member could falsify.
STALE_TABLE = "That didn't go through — the table moved on before your tap landed."


class TableError(Exception):
    """Member-presentable refusal from the service layer (house ❌ copy)."""


# ── Cards ────────────────────────────────────────────────────────────────────


def save_card(conn, guild_id: int, data: object, uploaded_by: int | None) -> int:
    """Validate + upsert a card (archived). Raises CardError with every
    problem — the same gate as scripts/validate_card.py (§3.4)."""
    from bot_modules.games.mahjong.card_logic import lint_card_data

    report = lint_card_data(data)
    if not report.ok:
        raise CardError(report.errors)
    card = load_card(data)
    now = time.time()
    row = conn.execute(
        "SELECT id FROM mahjong_cards WHERE guild_id = ? AND card_id = ?",
        (guild_id, card.card_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE mahjong_cards SET display_name = ?, season = ?, card_json = ?, "
            "uploaded_by = ? WHERE id = ?",
            (card.display_name, card.season, json.dumps(data), uploaded_by, row["id"]),
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO mahjong_cards (guild_id, card_id, display_name, season, "
        "card_json, status, uploaded_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'archived', ?, ?)",
        (guild_id, card.card_id, card.display_name, card.season,
         json.dumps(data), uploaded_by, now),
    )
    return int(cur.lastrowid or 0)


def set_card_status(
    conn, guild_id: int, row_id: int, status: str, activate_at: float | None = None
) -> None:
    """active | scheduled | archived. Activation demotes the current active
    card in the same transaction (the schema forbids two actives)."""
    if status not in ("active", "scheduled", "archived"):
        raise TableError("Unknown card status.")
    if status == "active":
        conn.execute(
            "UPDATE mahjong_cards SET status = 'archived' "
            "WHERE guild_id = ? AND status = 'active' AND id != ?",
            (guild_id, row_id),
        )
    changed = conn.execute(
        "UPDATE mahjong_cards SET status = ?, activate_at = ? "
        "WHERE guild_id = ? AND id = ?",
        (status, activate_at if status == "scheduled" else None, guild_id, row_id),
    ).rowcount
    if changed != 1:
        raise TableError("That card doesn't exist here.")


def activate_due_cards(conn, guild_id: int, now: float | None = None) -> bool:
    """Promote a scheduled card whose time has come (earliest wins)."""
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT id FROM mahjong_cards WHERE guild_id = ? AND status = 'scheduled' "
        "AND activate_at IS NOT NULL AND activate_at <= ? "
        "ORDER BY activate_at LIMIT 1",
        (guild_id, now),
    ).fetchone()
    if not row:
        return False
    set_card_status(conn, guild_id, int(row["id"]), "active")
    return True


def get_active_card(conn, guild_id: int) -> tuple[int, Card] | None:
    row = conn.execute(
        "SELECT id, card_json FROM mahjong_cards "
        "WHERE guild_id = ? AND status = 'active'",
        (guild_id,),
    ).fetchone()
    if not row:
        return None
    return int(row["id"]), load_card(json.loads(row["card_json"]))


def seed_first_light(conn, guild_id: int) -> int:
    """Install the shipped starter card (idempotent); returns its row id."""
    from bot_modules.games.mahjong.card_logic import FIRST_LIGHT_PATH

    data = json.loads(FIRST_LIGHT_PATH.read_text(encoding="utf-8"))
    return save_card(conn, guild_id, data, uploaded_by=None)


# ── Escrow helpers ───────────────────────────────────────────────────────────


def escrow_amount(card: Card, seat_count: int, stake: int) -> int:
    """Card max × payout cap × stake (§2.9) — the worst a seat can lose."""
    return card.max_value * PAYOUT_CAP[seat_count] * stake


def _hand_gid(table_id: int, hand_no: int) -> int:
    return table_id * _HAND_GID_BASE + hand_no


# ── The service ──────────────────────────────────────────────────────────────

Listener = Callable[[int, GameState, list[engine.Event]], Awaitable[None]]


class MahjongService:
    """Table lifecycle over the pure engine: per-table locks, one DB
    transaction per transition, timers that survive restart."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._locks: dict[int, asyncio.Lock] = {}
        self._timers: dict[int, asyncio.Task] = {}
        self._listener: Listener | None = None
        self._rng = secrets.SystemRandom()
        self._pending_events: dict[int, tuple[GameState, list[engine.Event]]] = {}
        # parsed-Card cache keyed by row id; the hash guards against the
        # upsert path rewriting card_json under a stable row id (F5)
        self._card_cache: dict[int, tuple[int, Card]] = {}

    def set_listener(self, listener: Listener) -> None:
        self._listener = listener

    def _lock(self, table_id: int) -> asyncio.Lock:
        return self._locks.setdefault(table_id, asyncio.Lock())

    # ── Table creation & seating ─────────────────────────────────────────

    async def create_table(
        self, guild_id: int, channel_id: int, host_id: int,
        seat_count: int, stake: int,
    ) -> int:
        """Open a lobby: validate the stake, hold the host's escrow, insert
        the table + seat rows. Raises TableError with the house ❌ copy."""

        def _tx() -> int:
            with open_db(self.db_path) as conn:
                settings = load_settings(conn, guild_id)
                if not settings.enabled:
                    raise TableError("Meadow Mahjong isn't open on this server yet.")
                if stake not in settings.stakes_allowed:
                    raise TableError(
                        "Pick a stake from "
                        + ", ".join(map(str, settings.stakes_allowed)) + "."
                    )
                active = get_active_card(conn, guild_id)
                if active is None:
                    raise TableError("No Meadow Card is active — an admin sets one on the dashboard.")
                card_row_id, card = active
                now = time.time()
                config = TableConfig(
                    seat_count=seat_count,
                    wall_trim=settings.duel_wall_trim if seat_count == 2 else 0,
                    second_charleston=settings.second_charleston,
                )
                state = engine.create_table(config, host_id)
                try:
                    cur = conn.execute(
                        "INSERT INTO mahjong_tables (guild_id, channel_id, mode, "
                        "stake, card_row_id, host_id, status, state, deadline_at, "
                        "created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?)",
                        (guild_id, channel_id, seat_count, stake, card_row_id,
                         host_id, json.dumps(engine.state_to_dict(state)),
                         now + LOBBY_LIFETIME, now, now),
                    )
                except Exception as e:  # the one-live-table-per-channel index
                    if "mahjong_tables.guild_id, mahjong_tables.channel_id" in str(e):
                        raise TableError(
                            "This channel already has a live table — join it, "
                            "or play in another channel."
                        ) from e
                    raise
                table_id = int(cur.lastrowid or 0)
                self._seat_member(conn, table_id, guild_id, host_id, 0)
                try:
                    wager_svc.hold_stake(
                        conn, guild_id, GAME_TYPE, _hand_gid(table_id, 1),
                        host_id, escrow_amount(card, seat_count, stake),
                    )
                except ValueError as e:
                    raise TableError(str(e)) from e
                return table_id

        table_id = await asyncio.to_thread(_tx)
        await self._arm_timer(table_id, time.time() + LOBBY_LIFETIME)
        return table_id

    def _seat_member(
        self, conn, table_id: int, guild_id: int, member_id: int, seat_index: int
    ) -> None:
        try:
            conn.execute(
                "INSERT INTO mahjong_seats (table_id, guild_id, user_id, "
                "seat_index, live) VALUES (?, ?, ?, ?, 1)",
                (table_id, guild_id, member_id, seat_index),
            )
        except Exception as e:  # the one-seat-per-member index
            if "mahjong_seats.guild_id, mahjong_seats.user_id" in str(e):
                raise TableError(
                    "You're already seated at a table on this server — "
                    "finish that hand first."
                ) from e
            raise

    async def join_table(self, table_id: int, member_id: int) -> list[engine.Event]:
        async with self._lock(table_id):
            deadline = await asyncio.to_thread(self._tx_join, table_id, member_id)
            events = self._last_events(table_id)
            await self._notify_and_arm(table_id, deadline)
            return events

    def _tx_join(self, table_id: int, member_id: int) -> float | None:
        with open_db(self.db_path) as conn:
            row = self._table_row(conn, table_id)
            state = engine.state_from_dict(json.loads(row["state"]))
            card = self._card_for(conn, row)
            for seat_state in state.seats:
                # any surface seating two members consults the no-contact
                # list (CLAUDE.md); the refusal is the surface's ordinary
                # stale-race failure, per docs/no_contact_spec.md
                if is_no_contact_conn(
                    conn, int(row["guild_id"]), member_id, seat_state.member_id
                ):
                    raise TableError(STALE_TABLE)
            try:
                state, events = engine.join_table(state, member_id)
            except ActionRejected as e:
                raise TableError(str(e)) from e
            seat_index = len(state.seats) - 1
            self._seat_member(
                conn, table_id, int(row["guild_id"]), member_id, seat_index
            )
            try:
                wager_svc.hold_stake(
                    conn, int(row["guild_id"]), GAME_TYPE,
                    _hand_gid(table_id, state.hand_no + 1), member_id,
                    escrow_amount(card, int(row["mode"]), int(row["stake"])),
                )
            except ValueError as e:
                raise TableError(str(e)) from e
            full = any(k == "table_full" for k, _ in events)
            deadline = (
                time.time() + DEAL_COUNTDOWN if full
                else float(row["deadline_at"] or 0) or None
            )
            self._persist(conn, table_id, state, deadline)
            self._stash_events(table_id, state, events)
            return deadline

    # ── Actions from the cog ─────────────────────────────────────────────

    async def act(self, table_id: int, action: str, /, **kwargs) -> list[engine.Event]:
        """Apply one member action by name; returns the transition's events so
        the tap's handler can deliver any private ones ephemerally.
        ActionRejected/TableError raise through to the cog (ephemeral ❌);
        anything else is a bug."""
        async with self._lock(table_id):
            deadline = await asyncio.to_thread(self._tx_act, table_id, action, kwargs)
            events = self._last_events(table_id)
            await self._notify_and_arm(table_id, deadline)
            return events

    def _tx_act(self, table_id: int, action: str, kwargs: dict) -> float | None:
        with open_db(self.db_path) as conn:
            row = self._table_row(conn, table_id)
            guild_id = int(row["guild_id"])
            state = engine.state_from_dict(json.loads(row["state"]))
            card = self._card_for(conn, row)
            settings = load_settings(conn, guild_id)
            if action == "cancel":
                state, events = engine.cancel_table(state, kwargs.get("member_id"))
                deadline = self._after_transition(
                    conn, row, state, events, card, settings)
                self._stash_events(table_id, state, events)
                return deadline

            seat = state.seat_of(kwargs["member_id"]) if "member_id" in kwargs else None
            if seat is None:
                raise TableError("You're not at this table.")

            if action == "charleston_pick":
                state, events = engine.charleston_pick(
                    state, seat, kwargs["tiles"], kwargs["blind_n"], self._rng)
            elif action == "vote":
                state, events = engine.vote_second_charleston(state, seat, kwargs["yes"])
            elif action == "courtesy_propose":
                state, events = engine.courtesy_propose(state, seat, kwargs["n"])
            elif action == "courtesy_pick":
                state, events = engine.courtesy_pick(state, seat, kwargs["tiles"])
            elif action == "discard":
                state, events = engine.discard(state, seat, kwargs["tile"])
            elif action == "claim":
                state, events = engine.claim(
                    state, seat, kwargs["kind"], kwargs.get("tiles", []),
                    card, self._rng)
            elif action == "redeem_joker":
                state, events = engine.redeem_joker(
                    state, seat, kwargs["exposure_id"], kwargs["tile"])
            elif action == "mahjong":
                state, events = engine.declare_mahjong_own_turn(state, seat, card)
            elif action == "rematch":
                state, events = engine.rematch_vote(state, seat)
                if engine.rematch_ready(state):
                    state, more = self._deal_rematch(conn, row, state, card)
                    events += more
            else:
                raise TableError("Unknown action.")

            deadline = self._after_transition(
                conn, row, state, events, card, settings)
            self._stash_events(table_id, state, events)
            return deadline

    # ── Timers ───────────────────────────────────────────────────────────

    async def timeout(self, table_id: int) -> None:
        """The armed timer fired. LOBBY dissolves or deals; SETTLE closes;
        every play phase feeds the engine a ``timeout`` action."""
        async with self._lock(table_id):
            deadline = await asyncio.to_thread(self._tx_timeout, table_id)
            await self._notify_and_arm(table_id, deadline)

    def _tx_timeout(self, table_id: int) -> float | None:
        with open_db(self.db_path) as conn:
            row = self._table_row_opt(conn, table_id)
            if row is None or row["status"] != "live":
                return None
            state = engine.state_from_dict(json.loads(row["state"]))
            card = self._card_for(conn, row)
            settings = load_settings(conn, int(row["guild_id"]))

            if state.phase is Phase.LOBBY:
                if len(state.seats) == state.seat_count:
                    state, events = engine.deal(state, shuffled_wall(self._rng))
                else:  # never filled — dissolve + refund (§6.2)
                    state, events = engine.close_table(state, "dissolved")
            elif state.phase is Phase.SETTLE:
                state, events = engine.close_table(state, "expired")
            elif state.phase is Phase.CLOSED:
                return None
            else:
                state, events = engine.timeout(state, card, self._rng)

            deadline = self._after_transition(
                conn, row, state, events, card, settings)
            self._stash_events(table_id, state, events)
            return deadline

    async def _arm_timer(self, table_id: int, deadline: float | None) -> None:
        old = self._timers.pop(table_id, None)
        if old is not None:
            old.cancel()
        if deadline is None:
            return

        async def fire() -> None:
            try:
                await asyncio.sleep(max(0.0, deadline - time.time()))
                await self.timeout(table_id)
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("mahjong timer for table %d failed", table_id)

        self._timers[table_id] = asyncio.create_task(fire())

    # ── Transition bookkeeping ───────────────────────────────────────────

    def _after_transition(
        self, conn, row, state: GameState, events: list[engine.Event],
        card: Card, settings: MahjongSettings,
    ) -> float | None:
        """Everything a finished transition owes: settle money if the hand
        ended, close out rows if the table closed, persist, pick the next
        deadline. Runs inside the caller's transaction."""
        table_id = int(row["id"])

        if any(k == "hand_settled" for k, _ in events):
            self._settle_money(conn, row, state)

        deadline: float | None
        now = time.time()
        if state.phase is Phase.CLOSED:
            reason = next(
                (d.get("reason") for k, d in events if k == "table_closed"),
                "closed",
            )
            # Live escrow can sit at the current hand's gid (mid-hand
            # dissolve; lobby escrow rides gid 1) or the next (a rematch that
            # half-escrowed). refund_game no-ops on settled/absent rows.
            for gid in (
                _hand_gid(table_id, max(state.hand_no, 1)),
                _hand_gid(table_id, state.hand_no + 1),
            ):
                wager_svc.refund_game(conn, GAME_TYPE, gid)
            conn.execute(
                "UPDATE mahjong_seats SET live = 0 WHERE table_id = ?", (table_id,)
            )
            conn.execute(
                "UPDATE mahjong_tables SET status = 'closed', closed_reason = ?, "
                "state = ?, deadline_at = NULL, updated_at = ? WHERE id = ?",
                (str(reason), json.dumps(engine.state_to_dict(state)), now, table_id),
            )
            return None

        if state.phase is Phase.LOBBY:
            deadline = float(row["deadline_at"] or (now + LOBBY_LIFETIME))
        elif state.phase is Phase.CLAIM_WINDOW:
            deadline = now + settings.claim_window(state.seat_count)
        elif state.phase is Phase.AWAIT_DISCARD:
            deadline = now + settings.turn_timer
        elif state.phase is Phase.SETTLE:
            deadline = now + settings.phase_timer
        else:  # charleston / vote / courtesy
            deadline = now + settings.phase_timer
        self._persist(conn, table_id, state, deadline)
        return deadline

    def _settle_money(self, conn, row, state: GameState) -> None:
        """Outcome → coins: settle_split for a scored hand, refunds for a
        wall game; results, per-seat rows, and stats in the same commit."""
        out = state.outcome
        assert out is not None
        table_id = int(row["id"])
        guild_id = int(row["guild_id"])
        stake = int(row["stake"])
        gid = _hand_gid(table_id, state.hand_no)
        member_of = {i: s.member_id for i, s in enumerate(state.seats)}

        if out.kind in ("wall_game", "all_fallow"):
            wager_svc.refund_game(conn, GAME_TYPE, gid)
            coin_deltas = {seat: 0 for seat in member_of}
        else:
            coin_deltas = {s: d * stake for s, d in out.point_deltas.items()}
            wager_svc.settle_split(
                conn, GAME_TYPE, gid,
                {member_of[s]: d for s, d in coin_deltas.items()},
            )

        now = time.time()
        cur = conn.execute(
            "INSERT INTO mahjong_results (guild_id, table_id, hand_no, mode, "
            "stake, card_id, kind, winner_id, line_id, line_name, base_value, "
            "won_by, jokerless, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (guild_id, table_id, state.hand_no, state.seat_count, stake,
             str(row["card_row_id"]), out.kind,
             member_of.get(out.winner) if out.winner is not None else None,
             out.line_id, out.line_name, out.value, out.won_by,
             1 if out.jokerless_double else 0, now),
        )
        result_id = int(cur.lastrowid or 0)
        for seat, member_id in member_of.items():
            delta = coin_deltas.get(seat, 0)
            conn.execute(
                "INSERT INTO mahjong_result_seats (result_id, guild_id, user_id, "
                "seat_index, points_delta, coins_delta, fallow) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, guild_id, member_id, seat,
                 out.point_deltas.get(seat, 0), delta,
                 1 if state.seats[seat].fallow else 0),
            )
            won = out.winner == seat and out.kind not in ("wall_game", "all_fallow")
            conn.execute(
                "INSERT INTO mahjong_stats (guild_id, user_id, mode, hands_played, "
                "wins, jokerless_wins, coins_won, coins_lost, biggest_win) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, mode) DO UPDATE SET "
                "hands_played = hands_played + 1, "
                "wins = wins + excluded.wins, "
                "jokerless_wins = jokerless_wins + excluded.jokerless_wins, "
                "coins_won = coins_won + excluded.coins_won, "
                "coins_lost = coins_lost + excluded.coins_lost, "
                "biggest_win = MAX(biggest_win, excluded.biggest_win)",
                (guild_id, member_id, state.seat_count,
                 1 if won else 0,
                 1 if won and out.jokerless_double else 0,
                 delta if delta > 0 else 0,
                 -delta if delta < 0 else 0,
                 delta if delta > 0 else 0),
            )

    def _deal_rematch(self, conn, row, state: GameState, card: Card):
        """Unanimous rematch: re-escrow every seat for the next hand, then
        deal. Any seat that can't cover closes the table instead — the fresh
        holds refund inside this same transaction."""
        table_id = int(row["id"])
        guild_id = int(row["guild_id"])
        next_gid = _hand_gid(table_id, state.hand_no + 1)
        amount = escrow_amount(card, int(row["mode"]), int(row["stake"]))
        for seat_state in state.seats:
            try:
                wager_svc.hold_stake(
                    conn, guild_id, GAME_TYPE, next_gid,
                    seat_state.member_id, amount,
                )
            except ValueError:
                wager_svc.refund_game(conn, GAME_TYPE, next_gid)
                closed, ev = engine.close_table(state, "rematch_unfunded")
                return closed, ev
        return engine.deal(state, shuffled_wall(self._rng))

    # ── Persistence plumbing ─────────────────────────────────────────────

    def _table_row_opt(self, conn, table_id: int):
        return conn.execute(
            "SELECT * FROM mahjong_tables WHERE id = ?", (table_id,)
        ).fetchone()

    def _table_row(self, conn, table_id: int):
        """The row for a MEMBER action — closed tables answer with the
        stale copy, so a lingering card (purge-closed, dissolved) can never
        seat anyone or debit into dead escrow (stage-6 review)."""
        row = self._table_row_opt(conn, table_id)
        if row is None or row["status"] != "live":
            raise TableError(STALE_TABLE)
        return row

    def _card_for(self, conn, row) -> Card:
        row_id = int(row["card_row_id"])
        card_row = conn.execute(
            "SELECT card_json FROM mahjong_cards WHERE id = ?",
            (row_id,),
        ).fetchone()
        if card_row is None:
            raise TableError("This table's card has been deleted.")
        raw = card_row["card_json"]
        cached = self._card_cache.get(row_id)
        if cached is not None and cached[0] == hash(raw):
            return cached[1]
        card = load_card(json.loads(raw))
        self._card_cache[row_id] = (hash(raw), card)
        return card

    def _persist(
        self, conn, table_id: int, state: GameState, deadline: float | None
    ) -> None:
        conn.execute(
            "UPDATE mahjong_tables SET state = ?, deadline_at = ?, updated_at = ? "
            "WHERE id = ?",
            (json.dumps(engine.state_to_dict(state)), deadline, time.time(), table_id),
        )

    # events collected inside the transaction, delivered after commit
    def _last_events(self, table_id: int) -> list[engine.Event]:
        stash = self._pending_events.get(table_id)
        return list(stash[1]) if stash else []

    def _stash_events(
        self, table_id: int, state: GameState, events: list[engine.Event]
    ) -> None:
        self._pending_events[table_id] = (state, events)

    async def _notify_and_arm(self, table_id: int, deadline: float | None) -> None:
        await self._arm_timer(table_id, deadline)
        stash = self._pending_events.pop(table_id, None)
        if stash and self._listener is not None:
            state, events = stash
            try:
                await self._listener(table_id, state, events)
            except Exception:
                log.exception("mahjong listener failed for table %d", table_id)

    # ── Boot / shutdown ──────────────────────────────────────────────────

    async def load_state(self, table_id: int) -> GameState | None:
        """Current state for a render; None when the table is gone."""
        def _q():
            with open_db(self.db_path) as conn:
                row = self._table_row_opt(conn, table_id)
                if row is None:
                    return None
                return engine.state_from_dict(json.loads(row["state"]))
        return await asyncio.to_thread(_q)

    async def table_meta(self, table_id: int) -> dict | None:
        """The row's render-relevant columns (stake, mode, sticky id, deadline)."""
        def _q():
            with open_db(self.db_path) as conn:
                row = self._table_row_opt(conn, table_id)
                if row is None:
                    return None
                return {
                    "guild_id": int(row["guild_id"]),
                    "channel_id": int(row["channel_id"]),
                    "mode": int(row["mode"]),
                    "stake": int(row["stake"]),
                    "status": str(row["status"]),
                    "sticky_message_id": row["sticky_message_id"],
                    "deadline_at": row["deadline_at"],
                    "card_row_id": int(row["card_row_id"]),
                }
        return await asyncio.to_thread(_q)

    async def assist_context(
        self, table_id: int, member_id: int
    ) -> tuple[Card, str] | None:
        """The table's pinned card and this member's assistance level, in one
        round-trip — what a rack render needs to build its readout. None when
        the table is gone (the render simply carries no readout)."""
        def _q():
            with open_db(self.db_path) as conn:
                row = self._table_row_opt(conn, table_id)
                if row is None:
                    return None
                guild_id = int(row["guild_id"])
                mode = get_assist_mode(conn, guild_id, member_id)
                if mode == "off":
                    return None  # don't parse a card nobody will read
                return self._card_for(conn, row), mode
        return await asyncio.to_thread(_q)

    async def member_assist_mode(self, guild_id: int, member_id: int) -> str:
        def _q():
            with open_db(self.db_path) as conn:
                return get_assist_mode(conn, guild_id, member_id)
        return await asyncio.to_thread(_q)

    async def choose_assist_mode(
        self, guild_id: int, member_id: int, mode: str
    ) -> None:
        def _q():
            with open_db(self.db_path) as conn:
                set_assist_mode(conn, guild_id, member_id, mode)
        return await asyncio.to_thread(_q)

    async def set_sticky_message(self, table_id: int, message_id: int | None) -> None:
        def _q():
            with open_db(self.db_path) as conn:
                conn.execute(
                    "UPDATE mahjong_tables SET sticky_message_id = ? WHERE id = ?",
                    (message_id, table_id),
                )
        await asyncio.to_thread(_q)

    async def card_for_table(self, table_id: int) -> Card | None:
        def _q():
            with open_db(self.db_path) as conn:
                row = self._table_row_opt(conn, table_id)
                if row is None:
                    return None
                return self._card_for(conn, row)
        return await asyncio.to_thread(_q)

    async def member_left(self, guild_id: int, member_id: int) -> None:
        """A seated member left the guild: fold them fallow mid-hand (their
        escrow stays held and pays per §1); a lobby seat just refunds and
        dissolves the lobby (it can no longer fill fairly)."""
        def _find() -> int | None:
            with open_db(self.db_path) as conn:
                row = conn.execute(
                    "SELECT t.id FROM mahjong_tables t "
                    "JOIN mahjong_seats s ON s.table_id = t.id "
                    "WHERE t.status = 'live' AND s.guild_id = ? "
                    "AND s.user_id = ? AND s.live = 1",
                    (guild_id, member_id),
                ).fetchone()
                return int(row["id"]) if row else None

        table_id = await asyncio.to_thread(_find)
        if table_id is None:
            return
        async with self._lock(table_id):
            def _tx() -> float | None:
                with open_db(self.db_path) as conn:
                    row = self._table_row_opt(conn, table_id)
                    if row is None or row["status"] != "live":
                        return None
                    state = engine.state_from_dict(json.loads(row["state"]))
                    card = self._card_for(conn, row)
                    settings = load_settings(conn, int(row["guild_id"]))
                    seat = state.seat_of(member_id)
                    if seat is None:
                        return None
                    if state.phase is engine.Phase.LOBBY:
                        state, events = engine.close_table(state, "dissolved")
                    elif state.phase in (engine.Phase.SETTLE,):
                        # settled: they just can't rematch — close it out
                        state, events = engine.close_table(state, "closed")
                    else:
                        state, events = engine.force_fallow(
                            state, seat, card, self._rng)
                    deadline = self._after_transition(
                        conn, row, state, events, card, settings)
                    self._stash_events(table_id, state, events)
                    return deadline
            deadline = await asyncio.to_thread(_tx)
            await self._notify_and_arm(table_id, deadline)

    async def dissolve_table(self, table_id: int, reason: str = "dissolved") -> None:
        """Force-close from outside the engine's own flow (mod tool, purge
        aftermath): refunds live escrow, frees the seats."""
        async with self._lock(table_id):
            def _tx() -> None:
                with open_db(self.db_path) as conn:
                    row = self._table_row_opt(conn, table_id)
                    if row is None or row["status"] != "live":
                        return
                    state = engine.state_from_dict(json.loads(row["state"]))
                    state, events = engine.close_table(state, reason)
                    card = self._card_for(conn, row)
                    settings = load_settings(conn, int(row["guild_id"]))
                    self._after_transition(conn, row, state, events, card, settings)
                    self._stash_events(table_id, state, events)
            await asyncio.to_thread(_tx)
            await self._notify_and_arm(table_id, None)

    async def resume_tables(self) -> list[int]:
        """Reload every live table after a restart, re-arming each timer
        with its remaining time; a table whose state can't load refunds and
        closes (the orphaned-escrow net)."""

        def _load() -> list[tuple[int, float | None]]:
            out: list[tuple[int, float | None]] = []
            with open_db(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT * FROM mahjong_tables WHERE status = 'live'"
                ).fetchall()
                for row in rows:
                    table_id = int(row["id"])
                    try:
                        engine.state_from_dict(json.loads(row["state"]))
                    except Exception:
                        log.exception(
                            "mahjong table %d state unloadable — refunding", table_id)
                        held = conn.execute(
                            "SELECT DISTINCT game_id FROM econ_game_wagers "
                            "WHERE game_type = ? AND state = 'held' "
                            "AND game_id >= ? AND game_id < ?",
                            (GAME_TYPE, _hand_gid(table_id, 0),
                             _hand_gid(table_id + 1, 0)),
                        ).fetchall()
                        for r in held:
                            wager_svc.refund_game(conn, GAME_TYPE, int(r["game_id"]))
                        conn.execute(
                            "UPDATE mahjong_tables SET status = 'closed', "
                            "closed_reason = 'unloadable', deadline_at = NULL "
                            "WHERE id = ?", (table_id,),
                        )
                        conn.execute(
                            "UPDATE mahjong_seats SET live = 0 WHERE table_id = ?",
                            (table_id,),
                        )
                        continue
                    deadline = row["deadline_at"]
                    out.append((table_id, float(deadline) if deadline else None))
            return out

        resumed = await asyncio.to_thread(_load)
        for table_id, deadline in resumed:
            # a deadline already past fires immediately (still via the loop)
            await self._arm_timer(table_id, deadline or time.time())
        return [t for t, _ in resumed]

    async def shutdown(self) -> None:
        for task in self._timers.values():
            task.cancel()
        self._timers.clear()
