"""QuickdrawGame dataclass and factory."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class QuickdrawGame:
    id: int
    guild_id: int
    channel_id: int
    challenger_id: int
    target_id: int
    state: str
    qd_state: str = "WAITING"
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    message_id: int | None = None
    result_message_id: int | None = None
    draw_delay: float | None = None
    fired_at: float | None = None
    last_action_at: float | None = None
    resolved_at: float | None = None
    created_at: float = field(default_factory=time.time)


def game_from_row(row) -> QuickdrawGame:
    return QuickdrawGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        challenger_id=row["challenger_id"],
        target_id=row["target_id"],
        state=row["state"],
        qd_state=row["qd_state"],
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        draw_delay=row["draw_delay"],
        fired_at=row["fired_at"],
        last_action_at=row["last_action_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"] or time.time(),
    )
