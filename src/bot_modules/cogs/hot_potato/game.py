"""HotPotatoGame dataclass, factory, and style-points helper."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field


@dataclass
class HotPotatoGame:
    id: int
    guild_id: int
    channel_id: int
    challenger_id: int
    target_id: int
    state: str
    holder_id: int | None = None
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    message_id: int | None = None
    result_message_id: int | None = None
    timer_seconds: float | None = None
    started_at: float | None = None
    pass_log: list[dict] = field(default_factory=list)
    last_action_at: float | None = None
    resolved_at: float | None = None
    created_at: float = field(default_factory=time.time)


def game_from_row(row) -> HotPotatoGame:
    return HotPotatoGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        challenger_id=row["challenger_id"],
        target_id=row["target_id"],
        state=row["state"],
        holder_id=row["holder_id"],
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        timer_seconds=row["timer_seconds"],
        started_at=row["started_at"],
        pass_log=json.loads(row["pass_log"] or "[]"),
        last_action_at=row["last_action_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"] or time.time(),
    )


def compute_style_points(
    pass_log: list[dict],
    started_at: float,
    timer_seconds: float,
    loser_id: int,
    winner_id: int,
) -> dict[int, int]:
    """10 style points per second spent holding in the danger zone (last 30% of timer)."""
    explosion_at = started_at + timer_seconds
    danger_start = started_at + timer_seconds * 0.7
    pts: dict[int, int] = {}
    for entry in pass_log:
        holder = entry["holder_id"]
        received = entry["received_at"]
        passed = entry.get("passed_at") or explosion_at
        overlap = max(0.0, min(passed, explosion_at) - max(received, danger_start))
        if overlap > 0:
            pts[holder] = pts.get(holder, 0) + int(overlap * 10)
    return pts
