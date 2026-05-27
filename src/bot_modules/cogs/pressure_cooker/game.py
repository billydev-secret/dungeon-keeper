from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Literal

GAUGE_CEILING = 100
ROLL_MIN = 1
ROLL_MAX = 15

GameState = Literal[
    "PENDING",
    "ACCEPTED",
    "ACTIVE",
    "RESOLVED",
    "NICKED",
    "EXPIRED",
    "DECLINED",
    "ABANDONED",
    "NO_NICK_SET",
    "EXPIRED_PENDING",
    "REVERTED_EARLY",
]

NON_TERMINAL_STATES = {"PENDING", "ACCEPTED", "ACTIVE", "RESOLVED"}


@dataclass
class PumpEntry:
    player_id: int
    roll: int
    gauge_before: int
    ts: float


@dataclass
class PressureGame:
    id: int
    guild_id: int
    channel_id: int
    challenger_id: int
    target_id: int
    state: GameState
    gauge: int = 0
    active_player: int | None = None
    pumps: list[PumpEntry] = field(default_factory=list)
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    message_id: int | None = None
    result_message_id: int | None = None
    stakes_honored: int | None = None
    created_at: float = field(default_factory=time.time)
    last_pump_at: float | None = None
    resolved_at: float | None = None


@dataclass
class PumpResult:
    roll: int
    gauge_before: int
    gauge_after: int
    busted: bool
    loser_id: int | None
    winner_id: int | None
    next_active_player: int | None


def roll_pump() -> int:
    return random.randint(ROLL_MIN, ROLL_MAX)


def apply_pump(
    game: PressureGame, player_id: int, roll: int | None = None
) -> PumpResult:
    """Apply one pump press. Mutates game in-place. Returns PumpResult.

    First pump can never lose: gauge starts at 0, max roll is 15, ceiling is 100.
    """
    if game.state != "ACTIVE":
        raise ValueError(f"Cannot pump: game state is {game.state!r}")
    if player_id != game.active_player:
        raise ValueError(
            f"Not {player_id!r}'s turn (active player: {game.active_player!r})"
        )

    actual_roll = roll if roll is not None else roll_pump()
    gauge_before = game.gauge
    gauge_after = gauge_before + actual_roll

    # Invariant: first pump cannot bust (gauge=0, max roll=ROLL_MAX=15 < GAUGE_CEILING=100)
    assert not (gauge_before == 0 and gauge_after >= GAUGE_CEILING), (
        "First-pump-cannot-lose invariant violated — "
        f"ROLL_MAX ({ROLL_MAX}) must be < GAUGE_CEILING ({GAUGE_CEILING})"
    )

    now = time.time()
    entry = PumpEntry(player_id=player_id, roll=actual_roll, gauge_before=gauge_before, ts=now)
    game.pumps.append(entry)
    game.gauge = gauge_after
    game.last_pump_at = now

    if gauge_after >= GAUGE_CEILING:
        game.state = "RESOLVED"
        game.loser_id = player_id
        game.winner_id = (
            game.target_id if player_id == game.challenger_id else game.challenger_id
        )
        game.resolved_at = now
        return PumpResult(
            roll=actual_roll,
            gauge_before=gauge_before,
            gauge_after=gauge_after,
            busted=True,
            loser_id=game.loser_id,
            winner_id=game.winner_id,
            next_active_player=None,
        )

    game.active_player = (
        game.target_id if player_id == game.challenger_id else game.challenger_id
    )
    return PumpResult(
        roll=actual_roll,
        gauge_before=gauge_before,
        gauge_after=gauge_after,
        busted=False,
        loser_id=None,
        winner_id=None,
        next_active_player=game.active_player,
    )


def pumps_to_json(pumps: list[PumpEntry]) -> str:
    return json.dumps(
        [
            {
                "player_id": p.player_id,
                "roll": p.roll,
                "gauge_before": p.gauge_before,
                "ts": p.ts,
            }
            for p in pumps
        ]
    )


def _parse_pumps(raw: str | None) -> list[PumpEntry]:
    if not raw:
        return []
    return [PumpEntry(**p) for p in json.loads(raw)]


def game_from_row(row) -> PressureGame:
    return PressureGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        challenger_id=row["challenger_id"],
        target_id=row["target_id"],
        state=row["state"],
        gauge=row["gauge"] or 0,
        active_player=row["active_player"],
        pumps=_parse_pumps(row["pumps_json"]),
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        stakes_honored=row["stakes_honored"],
        created_at=row["created_at"] or time.time(),
        last_pump_at=row["last_pump_at"],
        resolved_at=row["resolved_at"],
    )
