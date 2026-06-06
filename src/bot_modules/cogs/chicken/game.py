"""ChickenGame dataclass, factory, and pure helpers (no Discord)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


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
    message_id: int | None = None
    result_message_id: int | None = None
    climb_started_at: float | None = None
    climb_duration: float | None = None
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
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        climb_started_at=row["climb_started_at"],
        climb_duration=row["climb_duration"],
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


def bravest_bailer(bail_log: list[dict]) -> dict | None:
    """The bail entry with the highest meter % (cut it closest)."""
    if not bail_log:
        return None
    return max(bail_log, key=lambda b: b["meter_pct"])


def resolve_crash(
    crashers: list[int], bail_log: list[dict]
) -> tuple[int | None, int | None]:
    """Resolve a crash (meter hit 100 with players still holding).

    Returns (winner_id, loser_id):
      * crashers + bailers → winner = bravest bailer, loser = deterministic crasher
        (lowest id) who eats the nick.
      * crashers only (nobody bailed) → total wipeout: (None, None), cosmetic.
    """
    best = bravest_bailer(bail_log)
    winner = best["player_id"] if best else None
    if crashers and bail_log:
        return winner, min(crashers)
    return winner, None
