"""The Ask panel's transcript mechanics.

The chat has no database behind it: what the member can see in the ephemeral
message *is* the conversation state, and pressing Reply reads it back out. That
makes the round trip load-bearing in a way a stored history never is — a bug
here doesn't lose a row, it silently feeds the model a different conversation
from the one on screen. Hence the property tests below rather than a couple of
happy-path assertions.
"""

from __future__ import annotations

import pytest

from bot_modules.services.advisor_chat_logic import (
    BOT_MARKER,
    EMBED_TOTAL_LIMIT,
    FIELD_BUDGET,
    FIELD_VALUE_LIMIT,
    MAX_EXCHANGES,
    MAX_FIELDS,
    REPLY_COOLDOWN_SECONDS,
    USER_MARKER,
    ReplyCooldown,
    exchange_count,
    footer_tail,
    history_from_fields,
    is_full,
    transcript_fields,
    turn_field,
)

NAME = "Billy-bot"


def _history(pairs: list[tuple[str, str]]) -> list[dict]:
    return [{"role": r, "content": c} for r, c in pairs]


# ── the round trip ───────────────────────────────────────────────────


def test_a_rendered_conversation_reads_back_as_the_same_conversation():
    """The whole design rests on this: what is drawn is what the model is given."""
    history = _history([
        ("user", "how do I earn coins?"),
        ("assistant", "Chatting earns XP, and XP pays out daily."),
        ("user", "what about the casino?"),
        ("assistant", "`/casino` has three games. Start with coinflip."),
    ])

    assert history_from_fields(transcript_fields(history, NAME)) == history


def test_turns_are_ordered_oldest_first_so_the_chat_reads_downwards():
    history = _history([("user", "first"), ("assistant", "second"), ("user", "third")])

    assert [v for _, v in transcript_fields(history, NAME)] == [
        "first",
        "second",
        "third",
    ]


@pytest.mark.parametrize(
    ("role", "marker", "expected_who"),
    [
        ("user", USER_MARKER, "You"),
        ("assistant", BOT_MARKER, NAME),
    ],
)
def test_a_field_name_carries_its_role_marker_and_who_said_it(
    role, marker, expected_who
):
    name, value = turn_field(role, "hello", NAME)

    assert name.startswith(marker)
    assert expected_who in name
    assert value == "hello"


def test_the_role_survives_the_assistant_being_renamed_mid_chat():
    """The branded name is a dial an admin can flip at any moment, including
    while somebody is mid-conversation. Reading the role off the name would
    reclass every earlier assistant turn as a user turn the instant it changed —
    handing the model a conversation in which it never spoke."""
    history = _history([("user", "hi"), ("assistant", "hello there")])
    fields = transcript_fields(history, "Billy-bot")

    # The dial changes; the *already rendered* message still says Billy-bot.
    assert history_from_fields(fields) == history

    # And a chat rendered under the new name reads back identically too.
    assert history_from_fields(transcript_fields(history, "Poppy")) == history


def test_a_value_longer_than_discord_allows_is_clipped_and_marked():
    long_answer = "x" * (FIELD_VALUE_LIMIT + 500)
    history = _history([("user", "go on"), ("assistant", long_answer)])

    fields = transcript_fields(history, NAME)
    value = fields[-1][1]

    assert len(value) <= FIELD_VALUE_LIMIT
    assert value.endswith("…")
    # Lossy, but consistently so: the model is fed the clipped text the member
    # is actually looking at, never a fuller version they can't see.
    assert history_from_fields(fields)[-1]["content"] == value


def test_a_very_long_chat_keeps_its_most_recent_turns():
    """Discord rejects an embed with more than 25 fields outright, so something
    has to give; the opening is the part no longer being discussed."""
    history = _history([("user", f"q{i}") for i in range(MAX_FIELDS + 6)])

    fields = transcript_fields(history, NAME)

    assert len(fields) == MAX_FIELDS
    assert fields[-1][1] == f"q{MAX_FIELDS + 5}"


def test_a_full_chat_of_long_turns_still_fits_in_one_embed():
    """Regression: the per-field caps do not imply Discord's 6000-character
    whole-embed limit. Five near-limit answers and five long questions run past
    it, and the failure compounds — the edit 400s, the window never updates, and
    every later turn rebuilds an equal-or-larger embed that fails identically,
    so the chat can never progress."""
    history = _history(
        [
            t
            for i in range(MAX_EXCHANGES)
            for t in (("user", "q" * 500), ("assistant", "a" * FIELD_VALUE_LIMIT))
        ]
    )

    fields = transcript_fields(history, NAME)
    total = sum(len(n) + len(v) for n, v in fields)

    assert total <= FIELD_BUDGET < EMBED_TOTAL_LIMIT


def test_the_whole_embed_budget_drops_the_oldest_turns_first():
    """What is still being discussed is the far end of the chat."""
    history = _history([("assistant", f"{i}" * FIELD_VALUE_LIMIT) for i in range(9)])

    values = [v for _, v in transcript_fields(history, NAME)]

    assert values[-1].startswith("8")
    assert len(values) < 9  # something was dropped, and it was the opening
    assert not any(v.startswith("0") for v in values)


def test_one_oversized_turn_is_still_shown_rather_than_dropped_entirely():
    """Budgeting must never render an empty transcript — a member would see a
    chat window with nothing in it and no way to tell why."""
    history = _history([("assistant", "x" * FIELD_VALUE_LIMIT)] )

    assert len(transcript_fields(history, NAME)) == 1


# ── parsing anything that isn't ours ─────────────────────────────────


def test_fields_without_a_role_marker_contribute_nothing():
    """A field added to this embed later, or a click arriving from some other
    message, must not become a phantom turn in somebody's conversation."""
    assert history_from_fields([("Pending change 1", "grant_message → hi")]) == []
    assert history_from_fields([("", "orphaned text")]) == []
    assert history_from_fields([(None, None)]) == []


def test_blank_turns_are_dropped_from_both_directions():
    assert transcript_fields(_history([("user", "   ")]), NAME) == []
    assert history_from_fields([(f"{USER_MARKER} You", "   ")]) == []


def test_an_empty_chat_round_trips_as_empty():
    assert transcript_fields([], NAME) == []
    assert history_from_fields([]) == []


# ── the budget ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("questions", "full"),
    [
        (0, False),
        (1, False),
        (MAX_EXCHANGES - 1, False),
        (MAX_EXCHANGES, True),
        (MAX_EXCHANGES + 1, True),
    ],
)
def test_a_chat_is_full_at_its_exchange_cap(questions, full):
    history = _history(
        [t for i in range(questions) for t in (("user", f"q{i}"), ("assistant", f"a{i}"))]
    )

    assert exchange_count(history) == questions
    assert is_full(history) is full


def test_an_unanswered_question_still_counts_against_the_budget():
    """Counting user turns, not pairs — otherwise a failed round-trip that left
    a question with no answer would hand back a free one."""
    assert exchange_count(_history([("user", "q")])) == 1


def test_the_footer_tells_the_member_where_they_stand():
    assert f"1 of {MAX_EXCHANGES}" in footer_tail(_history([("user", "q")]))

    spent = _history([("user", f"q{i}") for i in range(MAX_EXCHANGES)])
    assert "limit reached" in footer_tail(spent)


# ── the reply cooldown ───────────────────────────────────────────────


def test_the_first_press_is_always_allowed():
    assert ReplyCooldown().remaining(7, now=100.0) == 0.0


def test_a_second_press_inside_the_window_has_to_wait():
    cd = ReplyCooldown()
    cd.mark(7, now=100.0)

    assert cd.remaining(7, now=104.0) == pytest.approx(REPLY_COOLDOWN_SECONDS - 4.0)


def test_the_window_expires():
    cd = ReplyCooldown()
    cd.mark(7, now=100.0)

    assert cd.remaining(7, now=100.0 + REPLY_COOLDOWN_SECONDS) == 0.0


def test_one_member_waiting_does_not_hold_up_another():
    cd = ReplyCooldown()
    cd.mark(7, now=100.0)

    assert cd.remaining(8, now=100.5) == 0.0


def test_expired_members_are_pruned_so_the_table_tracks_who_is_chatting_now():
    """It lives for the whole process lifetime; without the prune it would grow
    by one entry per member who ever pressed Reply."""
    cd = ReplyCooldown()
    cd.mark(1, now=100.0)
    cd.mark(2, now=100.0)

    cd.mark(3, now=100.0 + REPLY_COOLDOWN_SECONDS + 1)

    assert set(cd._last) == {3}
