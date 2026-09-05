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
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import PendingQuestionState, PostedQuestionState, RiskyRollState


# ── Chasing the payoff ───────────────────────────────────────────────
#
# The round's payoff is the winner's question, and most rounds never got
# one: the winner walked off, nothing re-pinged anyone, and the 7-day sweep
# deleted the prompt in silence. Two per-guild dials in the shared config
# table chase it. Both ship at 0 (off) — a dial an admin has not touched
# changes nothing about a live round.

#: Config key: hours after which the winner is re-pinged once to ask, and
#: (once a question exists) the answerer is re-pinged once to reply.
PAYOFF_CHASE_HOURS_KEY = "risky_chase_hours"
#: Config key: hours after which a winner who still has not asked has a
#: question drawn from the bank and posted for them.
PAYOFF_FALLBACK_HOURS_KEY = "risky_fallback_hours"
#: The dashboard's upper bound on either dial — a week, the same clock the
#: sweep runs on; a window longer than that would never fire.
PAYOFF_HOURS_MAX = 168
#: How often the chaser looks at the pending and posted questions.
PAYOFF_TICK_SECONDS = 300


@dataclass(frozen=True)
class PayoffDials:
    """A guild's two payoff dials, in hours. 0 means that dial is off."""

    chase_hours: int = 0
    fallback_hours: int = 0

    @property
    def enabled(self) -> bool:
        return self.chase_hours > 0 or self.fallback_hours > 0


class PayoffAction(str, Enum):
    CHASE = "chase"
    FALLBACK = "fallback"


def normalize_payoff_hours(raw) -> int:
    """A stored dial value as an int, clamped to ``0..PAYOFF_HOURS_MAX``.

    The config table stores text; anything unparseable or negative reads as
    0 (off) rather than as a window that fires at once.
    """
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(PAYOFF_HOURS_MAX, hours))


def unasked_questioners(pending: PendingQuestionState) -> list[int]:
    """Who still owes this prompt a question, winner first.

    The winner is listed first so the fallback speaks for them by default;
    the second questioner (the 1 rule) only gets it when the winner has
    already asked and it is their turn that stalled.
    """
    return [
        uid
        for uid in (pending.winner_id, pending.extra_questioner_id)
        if uid is not None and uid not in pending.questioners_asked
    ]


def pending_payoff_action(
    pending: PendingQuestionState, dials: PayoffDials, now: float
) -> PayoffAction | None:
    """What the chaser should do about a prompt nobody has asked on yet.

    The fallback wins when both are due — after a restart, or with a chase
    window no shorter than the fallback window — so a stalled prompt gets
    the question, not a nag *and* the question. The chase fires once
    (``chased_at`` is set when it goes), and only while at least one
    questioner still owes a question. A prompt with no usable age (the
    ``created_at`` column arrived in migration 173; the sweep clears those
    rows at startup) is left alone rather than treated as infinitely old.
    """
    if pending.created_at <= 0 or not unasked_questioners(pending):
        return None
    age = now - pending.created_at
    if dials.fallback_hours > 0 and age >= dials.fallback_hours * 3600:
        return PayoffAction.FALLBACK
    if (
        dials.chase_hours > 0
        and pending.chased_at is None
        and age >= dials.chase_hours * 3600
    ):
        return PayoffAction.CHASE
    return None


def posted_chase_due(
    posted: PostedQuestionState, dials: PayoffDials, now: float
) -> bool:
    """Whether the answerer of a posted question gets their one re-ping now.

    Only the chase dial applies here: there is nobody to answer *for*. The
    posted-question row has always carried ``created_at``, so no age guard.
    """
    return (
        dials.chase_hours > 0
        and posted.chased_at is None
        and now - posted.created_at >= dials.chase_hours * 3600
    )


def fallback_blocked(
    asker_id: int,
    target_ids: set[int],
    blocked_pairs: set[tuple[int, int]],
) -> bool:
    """Whether posting a bank question from *asker_id* to *target_ids* would
    put a no-contact pair in touch.

    The pairing was gated on the draw when the round resolved, but the list
    can change in the hours before the fallback fires, and a question the
    bot posts *for* someone is still that person's question to the target.
    *blocked_pairs* is keyed low-first, as ``no_contact_pairs_among`` returns.
    """
    return any(
        (min(asker_id, t), max(asker_id, t)) in blocked_pairs for t in target_ids
    )


def posted_chase_blocked(
    posted: PostedQuestionState, blocked_pairs: set[tuple[int, int]]
) -> bool:
    """Whether the answerer re-ping for a posted question must be skipped.

    Same reasoning as `fallback_blocked`: the pairing was gated on the draw,
    but the list can change in the hours before the chase fires, and the
    chase pings the answerer(s) with the asker's question attached. It is
    one message to everyone who may reply, so **any** answerer on the list
    with the asker skips the whole chase — a deck question included, since
    the reply still goes to the asker. *blocked_pairs* is keyed low-first.
    """
    return fallback_blocked(posted.asker_id, posted.allowed_replier_ids, blocked_pairs)


def build_history_payload(state: RiskyRollState) -> dict:
    """The ``games_game_history`` payload for a resolved round.

    ``players`` is what ``game_roster`` reads back for the unique-players
    count; the rest is the roll summary a report can show. Keys are strings
    because the payload is JSON — ids round-trip as text like every other
    game's.
    """
    return {
        "players": sorted(state.rolls),
        "rolls": {str(uid): roll for uid, roll in sorted(state.rolls.items())},
        "highest_user": state.highest_user,
        "lowest_user": state.lowest_user,
        "second_lowest_user": state.second_lowest_user,
        "second_highest_user": state.second_highest_user,
    }


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


def effective_min_game_seconds(
    configured: dict[int, int],
    guild_id: int,
    skip_min_game_time: bool = False,
) -> int:
    """How long a round must stay open in *guild_id*, in seconds.

    One lookup for both close paths — the host's Close Round button and the
    auto-close that fires once enough people have rolled. They used to disagree:
    auto-close fell back to a 30-minute default when no value was stored, while
    the dashboard shows an unset dial as 0 and promises "0 lets the host close a
    round the moment it opens". A guild reading 0 on the panel then waited half
    an hour for a full round to close. Absent config means 0 here, so the panel
    tells the truth for both paths.

    ``skip_min_game_time`` is the per-round override and wins outright.
    """
    if skip_min_game_time:
        return 0
    return int(configured.get(guild_id, 0))


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


def possible_directed_edges(rolls: dict[int, int]) -> set[tuple[int, int]]:
    """Every ``(asker, answerer)`` this roll set could produce.

    Mirrors what :meth:`RiskyRollState.resolve` plus
    :func:`build_main_prompt_state` and :func:`build_one_rule_prompt_state`
    would decide — but **pessimistically**, taking the union over every way an
    unresolved tie could break, because the tie-break is a hidden roll-off the
    caller has not run yet and must not have to.

    A 69 in the round returns no edges at all: that roller asks the *room*, in
    a thread, and a question put to everyone is not directed contact between
    two people (docs/no_contact_spec.md, Risky Rolls). The same is true of a
    round too small to resolve.

    This restates ``resolve``'s seat rules, which is a drift risk; the contract
    is pinned by a property test that runs the real resolution over randomised
    rolls and asserts the edges it actually produces are a subset of these.
    """
    if len(rolls) < 2:
        return set()
    if any(roll == 69 for roll in rolls.values()):
        return set()

    max_value = max(rolls.values())
    winners = [uid for uid, roll in rolls.items() if roll == max_value]

    edges: set[tuple[int, int]] = set()
    for winner in winners:
        # A tie for highest is settled first and the winner is then out of the
        # running for lowest; a clean win leaves the whole roster in it.
        pool = (
            {uid: r for uid, r in rolls.items() if uid != winner}
            if len(winners) > 1
            else rolls
        )
        if not pool:
            continue
        min_value = min(pool.values())
        for loser in [uid for uid, r in pool.items() if r == min_value]:
            if loser == winner:
                continue
            edges.add((winner, loser))

            rest = {
                uid: r for uid, r in rolls.items()
                if uid != winner and uid != loser
            }
            if not rest:
                continue
            # The 100 rule hands the winner a second answerer…
            if rolls[winner] == 100:
                second_lowest = min(rest.values())
                edges.update(
                    (winner, uid) for uid, r in rest.items() if r == second_lowest
                )
            # …and the 1 rule hands the loser a second asker.
            if rolls[loser] == 1:
                second_highest = max(rest.values())
                edges.update(
                    (uid, loser) for uid, r in rest.items() if r == second_highest
                )
    return edges


def has_blocked_edge(
    rolls: dict[int, int], blocked_pairs: set[tuple[int, int]]
) -> bool:
    """Whether this roll set could put a no-contact pair in touch.

    *blocked_pairs* comes from ``no_contact_service.no_contact_pairs_among``
    and is keyed low-first; the edges are directed, so both orderings are
    tested. Direction never matters to the list — it separates two people
    both ways — but it very much matters to who ends up asking.
    """
    if not blocked_pairs:
        return False
    return any(
        (min(a, b), max(a, b)) in blocked_pairs
        for a, b in possible_directed_edges(rolls)
    )


def choose_roll(
    rolls: dict[int, int],
    roller_id: int,
    blocked_pairs: set[tuple[int, int]],
) -> int:
    """Roll 1–100 for *roller_id*, avoiding a value that pairs a blocked couple.

    The die is drawn honestly first and kept if it is safe, so a round with no
    no-contact pair in it — every round, nearly always — takes the same
    ``randint`` it always did. Only a natural roll that *would* create a
    directed edge is redrawn, uniformly over the values that would not.

    **69 is excluded from the redraw pool**, and that exclusion is the whole
    reason this is a keep-or-redraw rather than a pick-from-safe. When the two
    members of a pair are the only players so far, 69 is the *only* safe value
    — a room question has no directed edge — so picking uniformly from the safe
    set would make the second of them to roll come up 69 essentially every
    time, which is a far louder tell than the thing being hidden. Drawing
    naturally first leaves 69 at its honest 1-in-100 and lets the round fall
    through to the close-time check instead.

    Returns the natural roll when no value is safe (a round that is only the
    two of them, say). The round is then unresolvable, and refusing to close it
    is the close path's job — see ``views.close_button``.
    """
    natural = random.randint(1, 100)
    if not blocked_pairs:
        return natural
    if not has_blocked_edge({**rolls, roller_id: natural}, blocked_pairs):
        return natural

    safe = [
        value
        for value in range(1, 101)
        if value != 69
        and not has_blocked_edge({**rolls, roller_id: value}, blocked_pairs)
    ]
    if not safe:
        return natural
    return random.choice(safe)
