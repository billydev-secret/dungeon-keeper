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
        "bot_modules.cogs.guess_cog.resolve_accent_color",
        AsyncMock(return_value=discord.Color.default()),
    )


@pytest.mark.asyncio
async def test_blocked_guess_is_never_recorded():
    """His guess on her round is discarded, and nothing touches the round.

    No row, no quest payout, no counter bump — and, crucially, no solve even
    though the guessed id matches the answer.
    """
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    view._select = MagicMock()
    view._select.values = ["2001"]  # the correct answer

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
        patch("bot_modules.cogs.guess_cog._do_mark_solved") as solved,
    ):
        await view._on_select(interaction)

    insert.assert_not_called()
    solved.assert_not_called()
    # The attempt is still logged for staff — silent to him, not to moderators.
    record.assert_called_once()


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
    view._select.values = ["2001"]

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
