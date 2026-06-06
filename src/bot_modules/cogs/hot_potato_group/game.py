"""HotPotatoGroupGame dataclass, factory, and pure helpers (no Discord)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class HotPotatoGroupGame:
    id: int
    guild_id: int
    channel_id: int
    host_id: int
    state: str
    round: int = 0
    roster: list[int] = field(default_factory=list)
    alive: list[int] = field(default_factory=list)
    elimination_order: list[int] = field(default_factory=list)
    holder_id: int | None = None
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    message_id: int | None = None
    result_message_id: int | None = None
    fuse_seconds: float | None = None
    phase_started_at: float | None = None
    pass_log: list[dict] = field(default_factory=list)
    last_action_at: float | None = None
    resolved_at: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def challenger_id(self) -> int:
        # Alias so the inherited nickname-stake flow (which reads game.challenger_id)
        # works unchanged for group games.
        return self.host_id


def game_from_row(row) -> HotPotatoGroupGame:
    return HotPotatoGroupGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        host_id=row["host_id"],
        state=row["state"],
        round=row["round"] or 0,
        roster=json.loads(row["roster"] or "[]"),
        alive=json.loads(row["alive"] or "[]"),
        elimination_order=json.loads(row["elimination_order"] or "[]"),
        holder_id=row["holder_id"],
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        fuse_seconds=row["fuse_seconds"],
        phase_started_at=row["phase_started_at"],
        pass_log=json.loads(row["pass_log"] or "[]"),
        last_action_at=row["last_action_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"] or time.time(),
    )


# ── Pure helpers ───────────────────────────────────────────────────────────────

def next_holder_clockwise(alive: list[int], current: int) -> int:
    """Next alive player after `current` in roster order, wrapping around.

    `alive` may still include `current` (called before removal) — the result is the
    following entry, which is always a survivor when len(alive) > 2.
    """
    if not alive:
        raise ValueError("alive is empty")
    if current not in alive:
        return alive[0]
    i = alive.index(current)
    return alive[(i + 1) % len(alive)]


def cumulative_hold_times(pass_log: list[dict], end_ts: float) -> dict[int, float]:
    """Total seconds each player held the bomb across the whole game. Open entries
    (no passed_at) are counted up to `end_ts`."""
    out: dict[int, float] = {}
    for entry in pass_log:
        holder = entry["holder_id"]
        received = entry["received_at"]
        passed = entry.get("passed_at")
        end = passed if passed is not None else end_ts
        out[holder] = out.get(holder, 0.0) + max(0.0, end - received)
    return out


def bravest(hold_times: dict[int, float]) -> int | None:
    """Player id with the most cumulative hold time, or None if empty."""
    if not hold_times:
        return None
    return max(hold_times, key=lambda k: hold_times[k])


def shake_emoji(elapsed: float, fuse: float, threshold: float = 0.70) -> str:
    """Escalating bomb emoji as the current fuse burns down."""
    if fuse <= 0:
        return "🥔💣💥💥"
    frac = elapsed / fuse
    if frac >= 0.9:
        return "🥔💣💥💥"
    if frac >= threshold:
        return "🥔💣💥"
    return "🥔💣"
