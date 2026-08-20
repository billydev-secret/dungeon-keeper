"""Wiring tests: the no-contact gate is actually connected to Guess Who.

The gate's *decisions* are covered at the service layer
(tests/test_no_contact_service.py). What this file proves is the glue —
that the cog calls the gate at all, and that when the gate says "blocked"
the flow diverges the way the design requires. For a safety feature the
glue is the enforcement: a gate nobody calls protects nobody.

Kept to that one job. Nothing here re-proves service behaviour through
Discord mocks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.cogs.test_guess_guess import _make_round, _make_select_view
from tests.fakes import FakeMember, fake_interaction


@pytest.fixture(autouse=True)
def _stub_accent_color(monkeypatch):
    import discord

    monkeypatch.setattr(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )


@pytest.mark.asyncio
async def test_blocked_guess_is_rate_limited_like_any_other():
    """A guess on her round must be written, so the cap and cooldown apply.

    Discarding it left him uncapped and never on cooldown: he could hold the
    button down on her round and get "Not it" forever while every other round
    put him on cooldown after one guess — a tell found in about a minute. The
    picker filter is what actually protects her (he cannot name her, so the
    guess is genuinely wrong); the write is what keeps him indistinguishable.

    Staff still get one event for the round, on the first guess only.
    """
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    view._select = MagicMock()
    view._select.values = ["3001"]  # not the answer — she isn't selectable

    with (
        patch("bot_modules.cogs.guess_cog._do_count_user_guesses", return_value=0),
        patch(
            "bot_modules.cogs.guess_cog._do_load_round",
            return_value=_make_round(answer_id=2001),
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.no_contact_partners",
            return_value={2001},
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.record_event"
        ) as record,
        patch("bot_modules.cogs.guess_cog._do_insert_guess") as insert,
        patch("bot_modules.cogs.guess_cog._do_count_guesses_for_round", return_value=1),
        patch("bot_modules.cogs.guess_cog._do_mark_solved") as solved,
    ):
        await view._on_select(interaction)

    insert.assert_called_once()
    solved.assert_not_called()
    record.assert_called_once()


@pytest.mark.asyncio
async def test_repeat_blocked_guess_does_not_reflood_the_log():
    """Only the first guess on a round records an attempt."""
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    view._select = MagicMock()
    view._select.values = ["3001"]

    with (
        patch("bot_modules.cogs.guess_cog._do_count_user_guesses", return_value=2),
        patch(
            "bot_modules.cogs.guess_cog._do_load_round",
            return_value=_make_round(answer_id=2001),
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.no_contact_partners",
            return_value={2001},
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.record_event"
        ) as record,
        patch("bot_modules.cogs.guess_cog._do_insert_guess"),
        patch("bot_modules.cogs.guess_cog._do_count_guesses_for_round", return_value=3),
    ):
        await view._on_select(interaction)

    record.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_guess_gets_the_ordinary_wrong_answer_reply():
    """The response must be the one every wrong guess gets.

    Anything else — an error, a silence, a differently-worded success — is a
    signal he can compare against guesses on other rounds.
    """
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    view._select = MagicMock()
    view._select.values = ["3001"]

    with (
        patch("bot_modules.cogs.guess_cog._do_count_user_guesses", return_value=0),
        patch(
            "bot_modules.cogs.guess_cog._do_load_round",
            return_value=_make_round(answer_id=2001),
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.no_contact_partners",
            return_value={2001},
        ),
        patch("bot_modules.cogs.guess_cog.no_contact_service.record_event"),
        patch("bot_modules.cogs.guess_cog._do_insert_guess"),
        patch("bot_modules.cogs.guess_cog._do_count_guesses_for_round", return_value=1),
    ):
        await view._on_select(interaction)

    interaction.edit_original_response.assert_awaited()
    assert (
        interaction.edit_original_response.await_args.kwargs["content"]
        == "❌ Not it. Keep trying!"
    )


@pytest.mark.asyncio
async def test_candidate_picker_drops_no_contact_partners():
    """She must not be selectable in his picker.

    This is what makes the silent discard undetectable: he cannot guess her
    correctly, so he can never notice a correct guess going unrecorded.
    """
    from bot_modules.cogs.guess_cog import GameView

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, 99, solved=False, guess_count=0)

    alice = FakeMember(id=2001, display_name="Alice")
    carol = FakeMember(id=3001, display_name="Carol")
    role = MagicMock()
    role.members = [alice, carol]

    guild = MagicMock()
    guild.id = 9001
    guild.get_role.return_value = role
    interaction = fake_interaction(user=FakeMember(id=9999), guild=guild)
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.message = MagicMock()

    cfg = MagicMock()
    cfg.guess_role_id = 7001
    cfg.guess_cooldown_seconds = 0
    cfg.max_guesses_per_round = 5

    with (
        patch("bot_modules.cogs.guess_cog._load_config", return_value=cfg),
        patch(
            "bot_modules.cogs.guess_cog._do_load_round",
            return_value=_make_round(answer_id=2001),
        ),
        patch(
            "bot_modules.cogs.guess_cog.no_contact_service.no_contact_partners",
            return_value={alice.id},
        ),
    ):
        await view._guess_callback(interaction)

    interaction.followup.send.assert_awaited()
    sent_view = interaction.followup.send.await_args.kwargs["view"]
    offered = {m.id for m in sent_view._all_members}
    assert alice.id not in offered
    assert carol.id in offered
