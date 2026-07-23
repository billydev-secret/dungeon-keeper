"""Tests for Greeting Watch — detection heuristic + watch/verdict DB helpers."""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.greeting_watch_service import (
    has_pending_greeting,
    is_greeting,
    list_due_greetings,
    mark_resolved,
    parse_notify_ids,
    pending_greetings_for,
    record_greeting,
    was_acknowledged,
)
from bot_modules.services.interaction_graph import record_interactions
from migrations import apply_migrations_sync


# ── Detection heuristic ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "good morning",
        "Good Morning everyone!",
        "goodmorning",
        "gm",
        "gm all",
        "morning",
        "mornin'",
        "hello",
        "hellooo",
        "hi",
        "hi everyone",
        "hey",
        "hey all",
        "heyyy",
        "hiya 👋",
        "howdy folks",
        "good afternoon",
        "good evening all",
        "yo",
        "sup",
        "what's up",
        "greetings",
        "good timezone",
        "Good Timezone!",
        "how's everyone's evening",
        "how's everyone's evening going",
        "how is your morning",
        "how's your day",
        "how's y'all's evening",
    ],
)
def test_is_greeting_positive(text):
    assert is_greeting(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "history channel is cool",  # starts with "hi"-ish but not a greeting
        "gaming tonight?",  # not "gm"
        "morningstar is my favorite weapon",  # "morning" prefix, but a sentence
        "does anyone know when the store opens",
        "himalayan salt lamps are great",  # "hi" prefix only
        "I said good morning earlier",  # greeting not at the start
        "hey can someone explain this whole ranked system to me in detail please",  # >8 words
        "how's this bug even possible",  # "how's" without a check-in subject
        "how's the weather where you live today at all",  # not everyone/your + time word
        "how do I fix this",
    ],
)
def test_is_greeting_negative(text):
    assert is_greeting(text) is False


# ── notify-id parsing (multi-subscriber) ─────────────────────────────


def test_parse_notify_ids_csv_multiple():
    assert parse_notify_ids("111,222,333") == [111, 222, 333]


def test_parse_notify_ids_dedups_and_drops_junk_preserving_order():
    # blanks, zeros, non-numeric and duplicates are all discarded; order kept.
    assert parse_notify_ids(" 111 , , 0, abc, 222, 111 ") == [111, 222]


def test_parse_notify_ids_empty_is_empty_list():
    assert parse_notify_ids("") == []
    assert parse_notify_ids(None) == []


def test_parse_notify_ids_legacy_fallback_when_csv_blank():
    # A guild configured before multi-notify keeps its single subscriber.
    assert parse_notify_ids("", legacy="555") == [555]
    assert parse_notify_ids(None, legacy="555") == [555]


def test_parse_notify_ids_csv_wins_over_legacy():
    # Once the CSV holds ids the legacy single value is ignored.
    assert parse_notify_ids("111,222", legacy="555") == [111, 222]


def test_parse_notify_ids_ignores_zero_legacy():
    assert parse_notify_ids("", legacy="0") == []
    assert parse_notify_ids("", legacy="") == []


# ── DB fixture ───────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "gw.db"
    apply_migrations_sync(path)
    return path


GUILD = 1000
CHANNEL = 2000
GREETER = 3000
OTHER = 4000


# ── record / dedup ───────────────────────────────────────────────────


def test_record_greeting_inserts_and_lists_due(db_path):
    with open_db(db_path) as conn:
        assert record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100) is True
    with open_db(db_path) as conn:
        due = list_due_greetings(conn, GUILD, cutoff_ts=200)
        assert len(due) == 1
        assert due[0].message_id == 1
        assert due[0].author_id == GREETER


def test_record_greeting_dedups_pending_author(db_path):
    """A second greeting from the same author in the same channel is a no-op
    while the first is still unresolved."""
    with open_db(db_path) as conn:
        assert record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100) is True
        assert has_pending_greeting(conn, GUILD, CHANNEL, GREETER) is True
        assert record_greeting(conn, GUILD, 2, CHANNEL, GREETER, created_ts=105) is False
        due = list_due_greetings(conn, GUILD, cutoff_ts=200)
        assert len(due) == 1


def test_record_greeting_allowed_again_after_resolution(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        # First one resolved → a fresh greeting is tracked again.
        assert record_greeting(conn, GUILD, 2, CHANNEL, GREETER, created_ts=1100) is True


def test_list_due_respects_cutoff(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        # cutoff before the greeting → not yet due
        assert list_due_greetings(conn, GUILD, cutoff_ts=50) == []
        # cutoff at/after → due
        assert len(list_due_greetings(conn, GUILD, cutoff_ts=100)) == 1


# ── acknowledgment ───────────────────────────────────────────────────


def test_was_acknowledged_true_when_someone_replies_or_mentions(db_path):
    with open_db(db_path) as conn:
        # Someone else interacts with the greeter inside the window.
        record_interactions(conn, GUILD, OTHER, [GREETER], ts=150, message_id=9)
        assert was_acknowledged(conn, GUILD, GREETER, since_ts=100, until_ts=700) is True


def test_was_acknowledged_false_when_silence(db_path):
    with open_db(db_path) as conn:
        assert was_acknowledged(conn, GUILD, GREETER, since_ts=100, until_ts=700) is False


def test_was_acknowledged_ignores_greeter_own_interactions(db_path):
    """The greeter greeting someone by name records a from-greeter edge, which
    must NOT count as being answered."""
    with open_db(db_path) as conn:
        record_interactions(conn, GUILD, GREETER, [OTHER], ts=150, message_id=9)
        assert was_acknowledged(conn, GUILD, GREETER, since_ts=100, until_ts=700) is False


def test_was_acknowledged_ignores_out_of_window(db_path):
    with open_db(db_path) as conn:
        # Interaction after the window closes doesn't count.
        record_interactions(conn, GUILD, OTHER, [GREETER], ts=800, message_id=9)
        assert was_acknowledged(conn, GUILD, GREETER, since_ts=100, until_ts=700) is False


# ── resolution ───────────────────────────────────────────────────────


def test_mark_resolved_removes_from_due(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "acknowledged", now_ts=1000)
        assert list_due_greetings(conn, GUILD, cutoff_ts=2000) == []
        row = conn.execute(
            "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = 1"
        ).fetchone()
        assert row["resolved_at"] == 1000
        assert row["outcome"] == "acknowledged"


def test_mark_resolved_is_idempotent(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        # A second resolve must not overwrite the first verdict.
        mark_resolved(conn, GUILD, 1, "acknowledged", now_ts=2000)
        row = conn.execute(
            "SELECT resolved_at, outcome FROM greeting_watch WHERE message_id = 1"
        ).fetchone()
        assert row["resolved_at"] == 1000
        assert row["outcome"] == "unanswered"


# ── pending_greetings_for (the greeting_answered quest detector) ─────


def test_pending_greetings_for_matches_channel_and_target(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert pending_greetings_for(
            conn, GUILD, (CHANNEL,), (GREETER,)
        ) == [(1, GREETER)]
        # Wrong channel or wrong target: no match.
        assert pending_greetings_for(conn, GUILD, (9999,), (GREETER,)) == []
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (OTHER,)) == []


def test_pending_greetings_for_ignores_resolved(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        mark_resolved(conn, GUILD, 1, "unanswered", now_ts=1000)
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER,)) == []


def test_pending_greetings_for_empty_inputs(db_path):
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        assert pending_greetings_for(conn, GUILD, (), (GREETER,)) == []
        assert pending_greetings_for(conn, GUILD, (CHANNEL,), ()) == []


def test_pending_greetings_for_multiple_targets(db_path):
    # A message replying to one greeter while mentioning another answers both.
    with open_db(db_path) as conn:
        record_greeting(conn, GUILD, 1, CHANNEL, GREETER, created_ts=100)
        record_greeting(conn, GUILD, 2, CHANNEL, OTHER, created_ts=110)
        got = pending_greetings_for(conn, GUILD, (CHANNEL,), (GREETER, OTHER))
        assert sorted(got) == [(1, GREETER), (2, OTHER)]
