"""MusicalChairsGame dataclass, factory, and pure helpers (no Discord)."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from bot_modules.games.utils.game_store import row_value


@dataclass
class MusicalChairsGame:
    id: int
    guild_id: int
    channel_id: int
    host_id: int
    state: str
    phase: str | None = None
    round: int = 0
    chairs: int | None = None
    roster: list[int] = field(default_factory=list)
    alive: list[int] = field(default_factory=list)
    elimination_order: list[int] = field(default_factory=list)
    seated: list[int] = field(default_factory=list)
    winner_id: int | None = None
    loser_id: int | None = None
    stakes_text: str | None = None
    #: Loser gets renamed. Independent of stakes_text/wager since
    #: migration 177 — see duels.filters.resolve_nick_stake.
    nick_stake: bool = False
    message_id: int | None = None
    result_message_id: int | None = None
    phase_started_at: float | None = None
    phase_duration: float | None = None
    #: Consecutive rounds nobody sat in (reset by any round with a sitter).
    reruns: int = 0
    last_action_at: float | None = None
    resolved_at: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def challenger_id(self) -> int:
        return self.host_id


def game_from_row(row) -> MusicalChairsGame:
    return MusicalChairsGame(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        host_id=row["host_id"],
        state=row["state"],
        phase=row["phase"],
        round=row["round"] or 0,
        chairs=row["chairs"],
        roster=json.loads(row["roster"] or "[]"),
        alive=json.loads(row["alive"] or "[]"),
        elimination_order=json.loads(row["elimination_order"] or "[]"),
        seated=json.loads(row["seated"] or "[]"),
        winner_id=row["winner_id"],
        loser_id=row["loser_id"],
        stakes_text=row["stakes_text"],
        nick_stake=bool(row_value(row, "nick_stake", 0)),
        message_id=row["message_id"],
        result_message_id=row["result_message_id"],
        phase_started_at=row["phase_started_at"],
        phase_duration=row["phase_duration"],
        reruns=int(row_value(row, "reruns", 0) or 0),
        last_action_at=row["last_action_at"],
        resolved_at=row["resolved_at"],
        created_at=row["created_at"] or time.time(),
    )


# ── Pure helpers ───────────────────────────────────────────────────────────────

#: A round nobody sat in re-runs this many times before the game is voided.
MAX_NO_SITTER_RERUNS = 1


def no_sitter_verdict(alive: list[int], survivors: list[int], reruns: int) -> str | None:
    """What to do with a round that seated nobody.

    ``None`` when someone sat (resolve as normal, or when there was nobody
    to seat). Otherwise ``"rerun"`` — same players, fresh music — until the
    round has already re-run ``MAX_NO_SITTER_RERUNS`` times in a row, when
    it is ``"void"``: called off, every stake refunded. Resolving instead
    used to make the later-listed player both winner and runner-up and pay
    them the pot for a chair they never sat in (duels-party-114).
    """
    if not alive or survivors:
        return None
    return "void" if reruns >= MAX_NO_SITTER_RERUNS else "rerun"


def chairs_for(n: int) -> int:
    """Number of chairs for n players: one fewer than the field (never negative)."""
    return max(0, n - 1)


def is_false_start(phase: str | None) -> bool:
    """Sitting during MUSIC is jumping the gun."""
    return phase == "MUSIC"


def resolve_round(
    alive: list[int], seated: list[int], chairs: int
) -> tuple[list[int], list[int]]:
    """Given the round's press order, return (survivors, eliminated).

    The first `chairs` players who pressed (and are still alive) keep a seat; everyone
    else alive is eliminated. Survivor order follows the alive order for stability.
    """
    valid_seated = [u for u in seated if u in alive]
    survivors_set = set(valid_seated[:chairs])
    survivors = [u for u in alive if u in survivors_set]
    eliminated = [u for u in alive if u not in survivors_set]
    return survivors, eliminated
