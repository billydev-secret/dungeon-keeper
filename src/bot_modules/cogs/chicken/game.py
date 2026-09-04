"""ChickenGame dataclass, factory, and pure helpers (no Discord)."""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from bot_modules.games.utils.game_store import row_value


@dataclass
class ChickenGame:
    id: int
    guild_id: int
    channel_id: int
    host_id: int
    state: str
    phase: str | None = None
    roster: list[int] = field(default_factory=list)
    alive: list[int] = field(default_factory=list)  # still holding
    elimination_order: list[int] = field(default_factory=list)
    bail_log: list[dict] = field(default_factory=list)
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    #: Loser gets renamed. Independent of stakes_text/wager since
    #: migration 177 — see duels.filters.resolve_nick_stake.
    nick_stake: bool = False
    message_id: int | None = None
    result_message_id: int | None = None
    climb_started_at: float | None = None
    #: Seconds the meter is drawn over (the guild's ``max_climb`` at start).
    climb_duration: float | None = None
    #: Hidden: seconds after ``climb_started_at`` at which the meter blows.
    #: Rolled in ``[min_climb, max_climb]`` per game — never shown, so the
    #: bar can crash at 60% and nobody can count it out. ``None`` on rows
    #: from before migration 208, where the crash was at ``climb_duration``.
    crash_at: float | None = None
    last_action_at: float | None = None
    resolved_at: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def challenger_id(self) -> int:
        return self.host_id


def game_from_row(row) -> ChickenGame:
    return ChickenGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        host_id=row["host_id"],
        state=row["state"],
        phase=row["phase"],
        roster=json.loads(row["roster"] or "[]"),
        alive=json.loads(row["alive"] or "[]"),
        elimination_order=json.loads(row["elimination_order"] or "[]"),
        bail_log=json.loads(row["bail_log"] or "[]"),
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        nick_stake=bool(row_value(row, "nick_stake", 0)),
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        climb_started_at=row["climb_started_at"],
        climb_duration=row["climb_duration"],
        crash_at=row_value(row, "crash_at", None),
        last_action_at=row["last_action_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"] or time.time(),
    )


# ── Pure helpers ───────────────────────────────────────────────────────────────

def meter_pct(now: float, start: float | None, duration: float | None) -> float:
    """Current meter percentage [0, 100]."""
    if start is None or duration is None or duration <= 0:
        return 0.0
    frac = (now - start) / duration
    return max(0.0, min(100.0, frac * 100.0))


def roll_crash_at(
    min_climb: float, max_climb: float, rng: random.Random | None = None
) -> float:
    """Where this game's meter blows, in seconds after the climb starts.

    Uniform in ``[min_climb, max_climb]``; a ceiling below the floor is
    lifted to it rather than inverting the range. ``rng`` is injectable so
    the roll is reproducible from a logged seed.
    """
    lo = max(0.0, float(min_climb))
    hi = max(lo, float(max_climb))
    return (rng or random).uniform(lo, hi)


def crash_pct(crash_at: float | None, duration: float | None) -> float:
    """The meter reading at the moment it blew (``crash_at`` over the drawn
    span), for the result card. 100 when the crash point is unknown."""
    if crash_at is None or duration is None or duration <= 0:
        return 100.0
    return max(0.0, min(100.0, crash_at / duration * 100.0))


def bravest_bailer(bail_log: list[dict]) -> dict | None:
    """The bail entry with the highest meter % (cut it closest)."""
    if not bail_log:
        return None
    return max(bail_log, key=lambda b: b["meter_pct"])


def resolve_crash(
    crashers: list[int], bail_log: list[dict], rng: random.Random | None = None
) -> tuple[int | None, int | None]:
    """Resolve a crash (the meter blew with players still holding).

    Returns (winner_id, loser_id):
      * crashers + bailers → winner = bravest bailer, loser = ONE crasher drawn
        at random who eats the nick. It used to be the lowest user id, which
        made the oldest account at the table the permanent scapegoat
        (duels-party-123); the cog seeds ``rng`` and logs the seed so a
        disputed draw can be replayed.
      * crashers only (nobody bailed) → total wipeout: (None, None), cosmetic.
    """
    best = bravest_bailer(bail_log)
    winner = best["player_id"] if best else None
    if crashers and bail_log:
        return winner, (rng or random).choice(crashers)
    return winner, None
