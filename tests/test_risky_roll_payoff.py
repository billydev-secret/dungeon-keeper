"""Risky Rolls chases its payoff (games deep review, rotation-rooms-156).

The round's payoff is the winner's question, and most rounds never got
one. Two dashboard dials, both shipping at 0 (off), now chase it:

- **Chase** — one re-ping of whoever owes a question after N hours, and one
  re-ping of the answerer once a question is posted.
- **Fallback** — after N hours with no question, a Truth drawn from the
  Truth or Dare bank is posted as the winner's question, so the loser still
  answers.

The timing decisions live in ``logic.py`` and are tested bare; the store
round-trips the two new columns; the formatters own the copy; and
``views.run_payoff_pass`` is driven once per scenario against a recording
channel and a real sqlite store, since the sending is where a restart or a
no-contact pair could otherwise slip a second ping through.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from bot_modules.services.games_db import GamesDb
from bot_modules.services.risky_roll import state as rr_state
from bot_modules.services.risky_roll import views as rr_views
from bot_modules.services.risky_roll.formatters import (
    build_fallback_question_content,
    build_how_to_play_content,
    build_pending_chase_content,
    build_pending_question_summary,
    build_posted_chase_content,
    build_question_reply_content,
)
from bot_modules.services.risky_roll.logic import (
    PAYOFF_CHASE_HOURS_KEY,
    PAYOFF_FALLBACK_HOURS_KEY,
    PAYOFF_HOURS_MAX,
    PayoffAction,
    PayoffDials,
    fallback_blocked,
    normalize_payoff_hours,
    pending_payoff_action,
    posted_chase_due,
    unasked_questioners,
)
from bot_modules.services.risky_roll.models import (
    PendingQuestionState,
    PostedQuestionState,
    PromptKind,
)
from bot_modules.services.risky_roll.store import StateStore

WINNER, LOSER, SECOND, EXTRA = 10, 20, 30, 11
H = 3600.0
T0 = 1_700_000_000.0


def _pending(**overrides: Any) -> PendingQuestionState:
    kwargs: dict[str, Any] = dict(
        channel_id=100, guild_id=1, winner_id=WINNER,
        participant_user_ids={LOSER}, game_id="g1",
        prompt_kind=PromptKind.DIRECT, created_at=T0, prompt_message_id=4242,
    )
    kwargs.update(overrides)
    return PendingQuestionState(**kwargs)


def _posted(**overrides: Any) -> PostedQuestionState:
    kwargs: dict[str, Any] = dict(
        message_id=5000, channel_id=100, guild_id=1, asker_id=WINNER,
        allowed_replier_ids={LOSER}, question_text="Truth?", created_at=T0,
    )
    kwargs.update(overrides)
    return PostedQuestionState(**kwargs)


# ── logic: the dials ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        pytest.param("5", 5, id="stored text"),
        pytest.param(7, 7, id="int"),
        pytest.param(None, 0, id="unset"),
        pytest.param("nope", 0, id="garbage reads as off"),
        pytest.param("-3", 0, id="negative reads as off"),
        pytest.param(999, PAYOFF_HOURS_MAX, id="clamped to a week"),
    ],
)
def test_normalize_payoff_hours(raw, expected):
    assert normalize_payoff_hours(raw) == expected


def test_dials_enabled_only_when_either_is_set():
    assert PayoffDials().enabled is False
    assert PayoffDials(chase_hours=1).enabled is True
    assert PayoffDials(fallback_hours=1).enabled is True


# ── logic: pending prompts ───────────────────────────────────────────


@pytest.mark.parametrize(
    "pending_kwargs, dials, age_hours, expected",
    [
        pytest.param({}, PayoffDials(), 100, None, id="both dials off: nothing, ever"),
        pytest.param({}, PayoffDials(chase_hours=2), 1, None, id="chase not yet due"),
        pytest.param({}, PayoffDials(chase_hours=2), 2, PayoffAction.CHASE, id="chase due"),
        pytest.param(
            {"chased_at": T0 + H}, PayoffDials(chase_hours=2), 50, None,
            id="chase fires once: already chased",
        ),
        pytest.param({}, PayoffDials(fallback_hours=6), 5, None, id="fallback not yet due"),
        pytest.param({}, PayoffDials(fallback_hours=6), 6, PayoffAction.FALLBACK, id="fallback due"),
        pytest.param(
            {}, PayoffDials(chase_hours=2, fallback_hours=6), 3, PayoffAction.CHASE,
            id="both on, only the chase due",
        ),
        pytest.param(
            {}, PayoffDials(chase_hours=2, fallback_hours=6), 7, PayoffAction.FALLBACK,
            id="both due (restart): the fallback wins, no nag and question",
        ),
        pytest.param(
            {"chased_at": T0 + 2 * H}, PayoffDials(chase_hours=2, fallback_hours=6), 7,
            PayoffAction.FALLBACK, id="chased earlier, fallback now",
        ),
        pytest.param(
            {}, PayoffDials(chase_hours=6, fallback_hours=2), 7, PayoffAction.FALLBACK,
            id="chase window longer than fallback: the fallback is what fires",
        ),
        pytest.param(
            {"created_at": 0.0}, PayoffDials(chase_hours=1, fallback_hours=1), 999, None,
            id="no usable age (pre-migration-173 row) is left alone",
        ),
        pytest.param(
            {"questioners_asked": {WINNER}}, PayoffDials(chase_hours=1, fallback_hours=1), 5, None,
            id="nobody owes a question",
        ),
        pytest.param(
            {
                "prompt_kind": PromptKind.TWO_QUESTIONERS, "extra_questioner_id": EXTRA,
                "questioners_asked": {WINNER},
            },
            PayoffDials(chase_hours=1), 2, PayoffAction.CHASE,
            id="two questioners, the second still owes: chased",
        ),
    ],
)
def test_pending_payoff_action(pending_kwargs, dials, age_hours, expected):
    pending = _pending(**pending_kwargs)
    assert pending_payoff_action(pending, dials, T0 + age_hours * H) is expected


def test_unasked_questioners_lists_the_winner_first():
    pending = _pending(prompt_kind=PromptKind.TWO_QUESTIONERS, extra_questioner_id=EXTRA)
    assert unasked_questioners(pending) == [WINNER, EXTRA]
    pending.questioners_asked.add(WINNER)
    assert unasked_questioners(pending) == [EXTRA]


# ── logic: posted questions ──────────────────────────────────────────


@pytest.mark.parametrize(
    "posted_kwargs, dials, age_hours, expected",
    [
        pytest.param({}, PayoffDials(fallback_hours=1), 50, False, id="fallback alone never chases a reply"),
        pytest.param({}, PayoffDials(chase_hours=2), 1, False, id="not yet due"),
        pytest.param({}, PayoffDials(chase_hours=2), 2, True, id="due"),
        pytest.param({"chased_at": T0 + 2 * H}, PayoffDials(chase_hours=2), 9, False, id="once only"),
    ],
)
def test_posted_chase_due(posted_kwargs, dials, age_hours, expected):
    assert posted_chase_due(_posted(**posted_kwargs), dials, T0 + age_hours * H) is expected


@pytest.mark.parametrize(
    "asker, targets, pairs, expected",
    [
        pytest.param(WINNER, {LOSER}, set(), False, id="no pairs"),
        pytest.param(WINNER, {LOSER}, {(WINNER, LOSER)}, True, id="the pair, low-first"),
        pytest.param(LOSER, {WINNER}, {(WINNER, LOSER)}, True, id="direction never matters"),
        pytest.param(WINNER, {LOSER, SECOND}, {(WINNER, SECOND)}, True, id="any target blocked blocks the post"),
        pytest.param(WINNER, {LOSER}, {(LOSER, SECOND)}, False, id="a pair not involving the asker"),
    ],
)
def test_fallback_blocked(asker, targets, pairs, expected):
    assert fallback_blocked(asker, targets, pairs) is expected


# ── store: dials and the two new columns ─────────────────────────────


@pytest.fixture
def store(sync_db_path: Path) -> StateStore:
    return StateStore(sync_db_path)


def _set_config(db_path: Path, guild_id: int, key: str, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, key) DO UPDATE SET value = excluded.value",
            (guild_id, key, value),
        )


async def test_store_load_payoff_dials_reads_both_keys_per_guild(store: StateStore, sync_db_path: Path):
    _set_config(sync_db_path, 1, PAYOFF_CHASE_HOURS_KEY, "4")
    _set_config(sync_db_path, 1, PAYOFF_FALLBACK_HOURS_KEY, "12")
    _set_config(sync_db_path, 2, PAYOFF_FALLBACK_HOURS_KEY, "garbage")

    dials = await store.load_payoff_dials()

    assert dials[1] == PayoffDials(chase_hours=4, fallback_hours=12)
    # A half-set guild gets 0 for the missing dial; garbage reads as off.
    assert dials[2] == PayoffDials(chase_hours=0, fallback_hours=0)
    assert 3 not in dials


async def test_store_pending_question_round_trips_chased_at(store: StateStore):
    pending = _pending(chased_at=T0 + H)
    await store.save_pending_question(pending)
    (loaded,) = await store.load_pending_questions()
    assert loaded.chased_at == T0 + H

    # Re-saving after the chase keeps the mark (the upsert carries it).
    loaded.chased_at = T0 + 2 * H
    await store.save_pending_question(loaded)
    (again,) = await store.load_pending_questions()
    assert again.chased_at == T0 + 2 * H


async def test_store_posted_question_round_trips_chase_and_bank_flags(store: StateStore):
    posted = _posted(chased_at=T0 + H, from_bank=True)
    await store.save_posted_question(posted)
    (loaded,) = await store.load_posted_questions()
    assert loaded.chased_at == T0 + H
    assert loaded.from_bank is True

    plain = _posted(message_id=5001)
    await store.save_posted_question(plain)
    by_id = {p.message_id: p for p in await store.load_posted_questions()}
    assert by_id[5001].chased_at is None
    assert by_id[5001].from_bank is False


async def test_store_delete_guild_data_clears_the_payoff_dials(store: StateStore, sync_db_path: Path):
    _set_config(sync_db_path, 1, PAYOFF_CHASE_HOURS_KEY, "4")
    _set_config(sync_db_path, 1, PAYOFF_FALLBACK_HOURS_KEY, "12")
    _set_config(sync_db_path, 2, PAYOFF_CHASE_HOURS_KEY, "1")

    await store.delete_guild_data(1)

    dials = await store.load_payoff_dials()
    assert 1 not in dials
    assert dials[2].chase_hours == 1


# ── formatters: the copy ─────────────────────────────────────────────


def test_how_to_play_promises_only_what_is_enforced():
    text = build_how_to_play_content()
    assert "the loser is asked to reply" in text
    assert "must reply" not in text
    assert "Reminders" not in text and "deck" not in text


def test_how_to_play_names_the_windows_when_the_dials_are_on():
    text = build_how_to_play_content(PayoffDials(chase_hours=1, fallback_hours=12))
    assert "after 1 hour." in text
    assert "hasn't asked after 12 hours" in text


def test_pending_chase_content_pings_whoever_still_owes_and_names_the_fallback():
    pending = _pending(
        prompt_kind=PromptKind.TWO_QUESTIONERS, extra_questioner_id=EXTRA,
        questioners_asked={WINNER},
    )
    text = build_pending_chase_content(pending, PayoffDials(chase_hours=2, fallback_hours=6))
    assert f"<@{EXTRA}>" in text and f"<@{WINNER}>" not in text
    assert "Ask Question" in text
    assert "after 6 hours, the deck asks one for you" in text

    quiet = build_pending_chase_content(pending, PayoffDials(chase_hours=2))
    assert "deck" not in quiet


@pytest.mark.parametrize(
    "from_bank, whose",
    [pytest.param(False, f"<@{WINNER}>'s question"), pytest.param(True, "the deck's question")],
)
def test_posted_chase_content_pings_the_answerer(from_bank, whose):
    text = build_posted_chase_content(_posted(from_bank=from_bank))
    assert text.startswith(f"⏰ <@{LOSER}>")
    assert whose in text and "Reply" in text


def test_fallback_question_content_direct_and_room():
    direct = build_fallback_question_content(_pending(), WINNER, "Truth?", {LOSER})
    assert direct == f"<@{LOSER}>\n<@{WINNER}> ran out of time, so the deck asks for them:\nTruth?"

    room = build_fallback_question_content(
        _pending(prompt_kind=PromptKind.ROOM, participant_user_ids={WINNER, LOSER}),
        WINNER, "Truth?", {LOSER},
    )
    assert "rolled 69 but never asked, so the deck asks the room" in room
    assert room.startswith(f"<@{LOSER}>\n")


@pytest.mark.parametrize(
    "kind, expected",
    [
        pytest.param(PromptKind.DIRECT, f"<@{WINNER}> ran out of time — the deck asked <@{LOSER}> for them:"),
        pytest.param(PromptKind.TWO_QUESTIONERS, f"<@{WINNER}> ran out of time — the deck asked <@{LOSER}> for them:"),
        pytest.param(PromptKind.ROOM, f"<@{WINNER}> rolled 69 but never asked — the deck asked the room:"),
    ],
)
def test_pending_summary_says_the_deck_asked(kind, expected):
    text = build_pending_question_summary(_pending(prompt_kind=kind), "Truth?", WINNER, from_bank=True)
    assert text.startswith(expected)
    assert text.endswith("> Truth?")


def test_reply_render_credits_the_deck_for_a_bank_question():
    own = build_question_reply_content(_posted(), LOSER, "Yes.")
    assert f"<@{WINNER}> asks:" in own
    bank = build_question_reply_content(_posted(from_bank=True), LOSER, "Yes.")
    assert f"the deck asks for <@{WINNER}>:" in bank
    assert f"<@{WINNER}> asks:" not in bank


# ── views: one pass of the chaser ────────────────────────────────────


class _Channel:
    """Records sends; ``is_nsfw`` is the age gate the bank draw reads."""

    def __init__(self, nsfw: bool = False) -> None:
        self.nsfw = nsfw
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._next_id = 9000

    def is_nsfw(self) -> bool:
        return self.nsfw

    async def send(self, content=None, **kwargs) -> SimpleNamespace:
        self._next_id += 1
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=self._next_id)

    def get_partial_message(self, message_id: int):
        edits = self.edits

        class _Partial:
            async def edit(self, **kwargs) -> None:
                edits.append({"message_id": message_id, **kwargs})

        return _Partial()

    @property
    def texts(self) -> list[str]:
        return [str(s.get("content") or "") for s in self.sent]


@pytest.fixture
def wired(sync_db_path: Path):
    """The views wired to a real store, a recording channel and a client
    carrying the games db the bank draw reads."""
    rr_state.store = StateStore(sync_db_path)
    rr_state.db_path = sync_db_path
    channel = _Channel()
    client = SimpleNamespace(games_db=GamesDb(sync_db_path))
    with patch.object(rr_views, "get_text_channel", AsyncMock(side_effect=lambda _c, _id: channel)):
        yield SimpleNamespace(channel=channel, client=client, db_path=sync_db_path)
    rr_views.stop_payoff_chaser()
    rr_state.pending_questions.clear()
    rr_state.posted_questions.clear()
    rr_state.store = None
    rr_state.db_path = None


def _set_dials(db_path: Path, guild_id: int, chase: int = 0, fallback: int = 0) -> None:
    _set_config(db_path, guild_id, PAYOFF_CHASE_HOURS_KEY, str(chase))
    _set_config(db_path, guild_id, PAYOFF_FALLBACK_HOURS_KEY, str(fallback))


def _bank(text: str | None):
    """Pin the bank draw (the migrated test db seeds the Truth or Dare bank,
    so a real draw is random) and record what the chaser asked it for."""
    draw = AsyncMock(return_value=None if text is None else ("TRUTH", text))
    return patch.object(rr_views, "get_ffa_prompt", draw), draw


async def _register_pending(pending: PendingQuestionState) -> None:
    rr_state.pending_questions[pending.game_id] = pending
    assert rr_state.store is not None
    await rr_state.store.save_pending_question(pending)


async def _register_posted(posted: PostedQuestionState) -> None:
    rr_state.posted_questions[posted.message_id] = posted
    assert rr_state.store is not None
    await rr_state.store.save_posted_question(posted)


async def test_pass_does_nothing_while_both_dials_are_off(wired):
    """Ships dark: a prompt a week old is left exactly as it was."""
    await _register_pending(_pending())
    await _register_posted(_posted())

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 500 * H) == 0
    assert wired.channel.sent == []
    assert "g1" in rr_state.pending_questions


async def test_pass_chases_the_winner_once(wired):
    _set_dials(wired.db_path, 1, chase=2)
    pending = _pending()
    await _register_pending(pending)

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 2 * H) == 1
    (text,) = wired.channel.texts
    assert text.startswith(f"⏰ <@{WINNER}>")
    assert pending.chased_at == T0 + 2 * H
    assert "g1" in rr_state.pending_questions  # the prompt itself is untouched

    # Persisted, so a restart cannot chase again; and the next pass is quiet.
    assert rr_state.store is not None
    (stored,) = await rr_state.store.load_pending_questions()
    assert stored.chased_at == T0 + 2 * H
    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 40 * H) == 0
    assert len(wired.channel.sent) == 1


async def test_pass_chases_the_answerer_once_a_question_is_posted(wired):
    _set_dials(wired.db_path, 1, chase=2)
    posted = _posted()
    await _register_posted(posted)

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 3 * H) == 1
    (text,) = wired.channel.texts
    assert text.startswith(f"⏰ <@{LOSER}>") and f"<@{WINNER}>'s question" in text
    assert rr_state.store is not None
    (stored,) = await rr_state.store.load_posted_questions()
    assert stored.chased_at == T0 + 3 * H
    assert posted.message_id in rr_state.posted_questions  # still awaiting its reply

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 90 * H) == 0


async def test_draw_fallback_question_is_a_truth_from_the_bank(wired):
    """Against the real (seeded) bank: a Truth comes back as plain text."""
    text = await rr_views.draw_fallback_question(wired.client.games_db, allow_nsfw=False)
    assert isinstance(text, str) and text


async def test_fallback_posts_a_bank_truth_as_the_winners_question(wired):
    _set_dials(wired.db_path, 1, fallback=6)
    pending = _pending(participant_user_ids={LOSER, SECOND})
    await _register_pending(pending)

    patched, draw = _bank("What's your go-to karaoke song?")
    with patched:
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 1
    draw.assert_awaited_once_with(wired.client.games_db, kind="truth", allow_nsfw=False)

    (sent,) = wired.channel.sent
    assert sent["content"] == (
        f"<@{LOSER}> <@{SECOND}>\n<@{WINNER}> ran out of time, so the deck asks for them:\n"
        "What's your go-to karaoke song?"
    )
    assert isinstance(sent["view"], rr_views.QuestionReplyView)

    # The loser still answers: a posted question exists, flagged as the bank's.
    (posted,) = rr_state.posted_questions.values()
    assert posted.asker_id == WINNER
    assert posted.allowed_replier_ids == {LOSER, SECOND}
    assert posted.from_bank is True
    assert posted.asker_rolled_100 is True
    assert rr_state.store is not None
    (stored,) = await rr_state.store.load_posted_questions()
    assert stored.from_bank is True and stored.question_text == "What's your go-to karaoke song?"

    # The pending prompt is gone from memory and disk, and its message says why.
    assert "g1" not in rr_state.pending_questions
    assert await rr_state.store.load_pending_questions() == []
    (edit,) = wired.channel.edits
    assert edit["message_id"] == 4242
    assert "ran out of time — the deck asked" in edit["content"]


async def test_fallback_speaks_for_the_second_questioner_when_the_winner_already_asked(wired):
    _set_dials(wired.db_path, 1, fallback=6)
    pending = _pending(
        prompt_kind=PromptKind.TWO_QUESTIONERS, extra_questioner_id=EXTRA,
        questioners_asked={WINNER},
    )
    await _register_pending(pending)

    patched, _draw = _bank("Bank truth?")
    with patched:
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 1
    (text,) = wired.channel.texts
    assert f"<@{EXTRA}> ran out of time" in text
    (posted,) = rr_state.posted_questions.values()
    assert posted.asker_id == EXTRA and posted.target_rolled_1 is True


async def test_fallback_for_a_1_rule_prompt_speaks_for_the_winner_then_the_second(wired):
    """Neither questioner asked: the deck speaks for the winner first and the
    prompt survives for the second questioner — re-saved with the winner
    marked as asked and its message updated, exactly as when the first of two
    asks by hand — then the next tick speaks for the second and retires it."""
    _set_dials(wired.db_path, 1, fallback=6)
    pending = _pending(prompt_kind=PromptKind.TWO_QUESTIONERS, extra_questioner_id=EXTRA)
    await _register_pending(pending)
    assert rr_state.store is not None

    patched, _draw = _bank("Bank truth?")
    with patched:
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 1
    (text,) = wired.channel.texts
    assert f"<@{WINNER}> ran out of time" in text
    assert rr_state.pending_questions["g1"].questioners_asked == {WINNER}
    (stored,) = await rr_state.store.load_pending_questions()
    assert stored.questioners_asked == {WINNER}
    (edit,) = wired.channel.edits
    assert edit["message_id"] == 4242 and "already asked" in edit["content"]

    with patched:
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H + 300) == 1
    assert f"<@{EXTRA}> ran out of time" in wired.channel.texts[1]
    assert "g1" not in rr_state.pending_questions
    assert await rr_state.store.load_pending_questions() == []
    assert {p.asker_id for p in rr_state.posted_questions.values()} == {WINNER, EXTRA}


@pytest.mark.parametrize("nsfw", [False, True], ids=["sfw channel", "age-restricted channel"])
async def test_fallback_draws_under_the_channel_age_gate(wired, nsfw):
    """The bank is asked for spicy rows only when Discord's own flag says so."""
    _set_dials(wired.db_path, 1, fallback=6)
    wired.channel.nsfw = nsfw
    await _register_pending(_pending())

    patched, draw = _bank("Truth?")
    with patched:
        await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H)
    assert draw.await_args is not None
    assert draw.await_args.kwargs["allow_nsfw"] is nsfw


async def test_fallback_leaves_the_prompt_alone_when_the_bank_is_empty(wired):
    _set_dials(wired.db_path, 1, fallback=6)
    await _register_pending(_pending())

    patched, _draw = _bank(None)
    with patched:
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 0
    assert wired.channel.sent == []
    assert "g1" in rr_state.pending_questions


async def test_fallback_for_a_room_question_pings_everyone_but_the_askers_no_contact_partner(wired):
    _set_dials(wired.db_path, 1, fallback=6)
    pending = _pending(prompt_kind=PromptKind.ROOM, participant_user_ids={WINNER, LOSER, SECOND})
    await _register_pending(pending)

    patched, _draw = _bank("Room truth?")
    with patched, patch(
        "bot_modules.services.no_contact_service.no_contact_partners", return_value={SECOND}
    ):
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 1

    (sent,) = wired.channel.sent
    assert sent["content"].startswith(f"<@{WINNER}> <@{LOSER}>\n🔥 <@{WINNER}> rolled 69")
    assert f"<@{SECOND}>" not in sent["content"].split("\n")[0]
    assert "view" not in sent  # a room question has no Reply button
    assert rr_state.posted_questions == {}
    assert "g1" not in rr_state.pending_questions


async def test_fallback_is_silently_skipped_for_a_pair_the_list_now_forbids(wired):
    """The pairing was gated on the draw, but the list can change in the
    hours before the fallback fires — and a question the bot posts *for*
    the winner is still the winner's question to the loser."""
    _set_dials(wired.db_path, 1, fallback=6)
    await _register_pending(_pending())

    patched, draw = _bank("Bank truth?")
    with patched, patch(
        "bot_modules.services.no_contact_service.no_contact_pairs_among",
        return_value={(WINNER, LOSER)},
    ):
        assert await rr_views.run_payoff_pass(wired.client, now=T0 + 6 * H) == 0
    draw.assert_not_awaited()  # nothing is marked served for a question never posted

    assert wired.channel.sent == []
    assert wired.channel.edits == []
    assert "g1" in rr_state.pending_questions
    assert rr_state.posted_questions == {}


async def test_pass_acts_once_per_channel_per_tick(wired):
    """Flipping the dial over a backlog drains it a message a tick, not all at once."""
    _set_dials(wired.db_path, 1, chase=1)
    await _register_pending(_pending(game_id="a"))
    await _register_pending(_pending(game_id="b"))
    await _register_pending(_pending(game_id="c", channel_id=200))

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 10 * H) == 2
    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 10 * H) == 1
    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 10 * H) == 0
    assert len(wired.channel.sent) == 3


async def test_pass_only_touches_guilds_with_a_dial_set(wired):
    _set_dials(wired.db_path, 2, chase=1)  # a different guild
    await _register_pending(_pending())

    assert await rr_views.run_payoff_pass(wired.client, now=T0 + 10 * H) == 0
    assert wired.channel.sent == []


def test_ensure_payoff_chaser_is_a_no_op_without_a_store():
    rr_state.store = None
    rr_views.ensure_payoff_chaser(SimpleNamespace())  # type: ignore[arg-type]
    assert rr_views._chaser_task is None
