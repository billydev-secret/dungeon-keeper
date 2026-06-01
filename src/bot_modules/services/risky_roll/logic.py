"""Pure decision logic for the Risky Rolls cog.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog and views call these to
decide question-prompt shape, sanitize user-supplied auto-close values,
and collect the per-channel state IDs that the reset command clears.

Serialization helpers (:func:`serialize_user_ids`, :func:`deserialize_user_ids`)
are the storage round-trip used by ``store.py`` for the comma-joined
``TEXT`` columns. :func:`run_tie_rolloff` is the random-driven loop that
resolves ties for highest/lowest — kept here so callers can patch the
sequence deterministically in tests.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PendingQuestionState, PostedQuestionState, RiskyRollState


def serialize_user_ids(user_ids: set[int]) -> str | None:
    """Comma-join sorted user IDs for sqlite TEXT storage.

    Returns ``None`` for empty sets so the column reads as NULL — the
    deserializer treats ``None`` and the empty string as "no users".
    """
    if not user_ids:
        return None
    return ",".join(str(uid) for uid in sorted(user_ids))


def deserialize_user_ids(raw: str | None) -> set[int]:
    """Parse the comma-joined user-ID string back into a set.

    ``None`` or empty input returns an empty set, matching what
    :func:`serialize_user_ids` writes for empty inputs.
    """
    if not raw:
        return set()
    return {int(part) for part in raw.split(",") if part}


def run_tie_rolloff(
    tied_user_ids: list[int], pick_lowest: bool = False
) -> tuple[int, list[dict[int, int]]]:
    """Roll 1-100 for each contender until one wins (or loses, if pick_lowest).

    Returns ``(winner_id, rounds)`` where ``rounds`` is the list of
    ``{user_id: roll}`` dicts produced in order — the formatters use
    this to render a per-round rolloff embed.
    """
    contenders = sorted(set(tied_user_ids))
    rounds: list[dict[int, int]] = []

    while True:
        round_rolls = {uid: random.randint(1, 100) for uid in contenders}
        rounds.append(round_rolls)
        target = min(round_rolls.values()) if pick_lowest else max(round_rolls.values())
        winners = sorted(uid for uid, roll in round_rolls.items() if roll == target)
        if len(winners) == 1:
            return winners[0], rounds
        contenders = winners


def normalize_auto_close_options(
    auto_close_players: int | None,
    auto_close_minutes: int | None,
) -> tuple[int | None, int | None]:
    """Sanitize raw ``/risky start`` option values.

    The slash command accepts arbitrary ints; we coerce out-of-range
    values to ``None`` so the rest of the pipeline can use simple
    truthiness checks. Players must be ≥2 (a single-player auto-close
    is meaningless) and minutes must be positive.
    """
    players = (
        auto_close_players
        if auto_close_players is not None and auto_close_players >= 2
        else None
    )
    minutes = (
        auto_close_minutes
        if auto_close_minutes is not None and auto_close_minutes > 0
        else None
    )
    return players, minutes


def collect_channel_state_ids(
    active_games: dict[str, RiskyRollState],
    pending_questions: dict[str, PendingQuestionState],
    posted_questions: dict[int, PostedQuestionState],
    channel_id: int,
) -> tuple[list[str], list[str], list[int]]:
    """Filter the in-memory state stores down to a single channel.

    Returns ``(active_game_ids, pending_question_game_ids, posted_message_ids)``
    so ``/risky reset_state`` can iterate cleanly. Returns three empty
    lists when the channel has nothing pending — the caller uses that
    to short-circuit with an ephemeral "nothing to reset" reply.
    """
    game_ids = [gid for gid, s in active_games.items() if s.channel_id == channel_id]
    question_ids = [
        gid for gid, s in pending_questions.items() if s.channel_id == channel_id
    ]
    posted_message_ids = [
        mid for mid, s in posted_questions.items() if s.channel_id == channel_id
    ]
    return game_ids, question_ids, posted_message_ids


def build_main_prompt_state(
    game_id: str,
    state: RiskyRollState,
    result_type,
):
    """Build the post-resolution prompt PendingQuestionState (room or direct).

    Returns ``None`` when the round didn't produce a winner (resolution
    bailed out before assigning ``highest_user``). On a 69/SIXTYNINE_TIE
    result the prompt targets the whole room; otherwise it targets the
    lowest player (plus the second-lowest when the 100 rule fires).
    """
    # Local imports avoid a circular dependency: models imports logic.
    from .models import PendingQuestionState, PromptKind, RoundResult

    if state.highest_user is None:
        return None
    if result_type in (RoundResult.SIXTYNINE, RoundResult.SIXTYNINE_TIE):
        return PendingQuestionState(
            channel_id=state.channel_id,
            guild_id=state.guild_id,
            winner_id=state.highest_user,
            participant_user_ids=set(state.rolls),
            game_id=game_id,
            prompt_kind=PromptKind.ROOM,
        )
    if state.lowest_user is None:
        return None
    targets = {state.lowest_user}
    if state.second_lowest_user is not None:
        targets.add(state.second_lowest_user)
    return PendingQuestionState(
        channel_id=state.channel_id,
        guild_id=state.guild_id,
        winner_id=state.highest_user,
        participant_user_ids=targets,
        game_id=game_id,
        lowest_tie_user_ids=set(state.lowest_tie_user_ids),
        prompt_kind=PromptKind.DIRECT,
    )


def build_one_rule_prompt_state(game_id: str, state: RiskyRollState):
    """Build the secondary "two questioners" prompt when the 1 rule fires.

    The 1 rule lets the second-highest player also ask the loser. This
    returns ``None`` unless the lowest player rolled exactly 1 and a
    winner exists; the caller skips the second prompt when ``None``.

    The returned game_id is suffixed with ``":1"`` so the secondary
    prompt is keyed independently of the main one in the pending
    questions store.
    """
    from .models import PendingQuestionState, PromptKind

    if (
        state.lowest_user is None
        or state.rolls.get(state.lowest_user) != 1
        or state.highest_user is None
    ):
        return None
    return PendingQuestionState(
        channel_id=state.channel_id,
        guild_id=state.guild_id,
        winner_id=state.highest_user,
        participant_user_ids={state.lowest_user},
        game_id=f"{game_id}:1",
        extra_questioner_id=state.second_highest_user,
        prompt_kind=PromptKind.TWO_QUESTIONERS,
    )
