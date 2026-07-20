"""Tests for rules_watch/scorer.py boundary detection.

Regression cover for the inverted boundary gate: `check_boundary_token` used to
run on the *author's own* message, so the author saying "no" ("no worries",
"oh no", "no milk") registered as a boundary event. Across the 1,292 historical
events that fired on the bare token "no" 71.5% of the time, and on the safeword
"red" matching colour words a further 6.3%.

A boundary event means *the target* signalled stop and the author continued.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.rules_watch.scorer import (
    check_boundary_token,
    detect_boundary_crossing,
)
from migrations import apply_migrations_sync

GUILD = 123
CHANNEL = 456
AUTHOR = 1001
TARGET = 1002
BYSTANDER = 1003

BASE_TS = 1_700_000_000


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _msg(conn, message_id, author_id, content, ts, reply_to=None, channel=CHANNEL):
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, "
        "content, reply_to_id, ts) VALUES (?,?,?,?,?,?,?)",
        (message_id, GUILD, channel, author_id, content, reply_to, ts),
    )


# ── check_boundary_token: pure text ───────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "no milk, extra butter in the pan",
        "No thanks 😂😂😂",
        "Oh no I'm poor",
        "Wait no",
        "I have no idea what I'm saying",
        "no fr 😂",
    ],
)
def test_casual_no_is_not_a_boundary_token(text):
    """Bare "no" in ordinary speech must not register.

    Every one of these is a real message the live guard flagged as a "slur".
    """
    assert check_boundary_token(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "The red and black combo 😍 omg! And your liiiiips 😍",
        "I love the red one",
        "yellow is such a good colour on you",
    ],
)
def test_colour_words_are_not_safewords(text):
    """Bare red/yellow matched colour words in the photo channels."""
    assert check_boundary_token(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "please stop",
        "Stop it",
        "I said no",
        "not interested",
        "leave me alone",
        "back off",
        "cut it out",
        "go away",
        "I'm not comfortable with that",
    ],
)
def test_explicit_stop_signals_are_boundary_tokens(text):
    assert check_boundary_token(text) is True


# ── detect_boundary_crossing: relational ──────────────────────────────


def test_author_saying_no_is_not_a_crossing(db):
    """The core inversion bug: the author's own "no" must not fire."""
    with open_db(db) as conn:
        _msg(conn, 1, TARGET, "what are you having for dinner", BASE_TS)
        _msg(conn, 2, AUTHOR, "no milk, extra butter in the pan", BASE_TS + 60)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is False
        )


def test_target_stop_signal_after_author_message_is_a_crossing(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "come to voice with me", BASE_TS)
        _msg(conn, 2, TARGET, "please stop asking", BASE_TS + 60)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is True
        )


def test_target_bare_no_counts_only_as_a_direct_reply(db):
    """A terse "no" is a refusal when it replies to the author, not otherwise."""
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "would you send me one", BASE_TS)
        _msg(conn, 2, TARGET, "No", BASE_TS + 30, reply_to=1)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 60
            )
            is True
        )


def test_target_bare_no_in_open_chat_is_not_a_crossing(db):
    """Narration, not refusal — ~90% of "no" hits in this corpus are this."""
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "morning all", BASE_TS)
        _msg(conn, 2, TARGET, "no way lol that's wild", BASE_TS + 60)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is False
        )


def test_stop_signal_with_no_prior_author_message_is_not_a_crossing(db):
    """A refusal is only a refusal if there was a request from this author."""
    with open_db(db) as conn:
        _msg(conn, 1, BYSTANDER, "hey pretty", BASE_TS)
        _msg(conn, 2, TARGET, "please stop", BASE_TS + 60)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is False
        )


def test_boundary_directed_at_a_third_party_does_not_implicate_author(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "nice pic", BASE_TS)
        _msg(conn, 2, BYSTANDER, "send me more", BASE_TS + 30)
        _msg(conn, 3, TARGET, "back off", BASE_TS + 60, reply_to=2)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is False
        )


def test_stale_boundary_outside_lookback_does_not_fire(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "come to voice", BASE_TS)
        _msg(conn, 2, TARGET, "please stop", BASE_TS + 60)
        far_future = BASE_TS + 60 + (10 * 3600)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, far_future
            )
            is False
        )


def test_crossing_is_scoped_to_the_channel(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "come to voice", BASE_TS, channel=CHANNEL)
        _msg(conn, 2, TARGET, "please stop", BASE_TS + 60, channel=999)
        assert (
            detect_boundary_crossing(
                conn, GUILD, CHANNEL, AUTHOR, TARGET, BASE_TS + 120
            )
            is False
        )


def test_no_target_is_not_a_crossing(db):
    with open_db(db) as conn:
        _msg(conn, 1, AUTHOR, "hello", BASE_TS)
        assert (
            detect_boundary_crossing(conn, GUILD, CHANNEL, AUTHOR, None, BASE_TS + 60)
            is False
        )
