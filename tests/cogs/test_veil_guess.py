"""Cog-level tests for the Veil guess flow."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.veil_models import VeilRound
from tests.fakes import FakeMember, fake_interaction

VEIL_ROLE_ID = 7001
ROUND_ID = 99


def _make_round(
    *,
    round_id: int = ROUND_ID,
    submitter_id: int = 1001,
    answer_id: int = 2001,
    solved_at: float | None = None,
) -> VeilRound:
    return VeilRound(
        id=round_id,
        guild_id=9001,
        submitter_id=submitter_id,
        answer_id=answer_id,
        channel_id=8001,
        message_id=12345,
        crop_path="/tmp/fake.jpg",
        crop_url="https://cdn.discord.com/fake.jpg",
        original_path="",
        difficulty="medium",
        candidate_count=1,
        reroll_count=0,
        allow_reuse=False,
        is_reuse=False,
        original_round_id=None,
        reuse_blocked=False,
        created_at=1000.0,
        solved_at=solved_at,
        solver_id=None,
        guesses_to_solve=None,
        unique_guessers_to_solve=None,
        answer_optout=False,
        deleted_at=None,
    )


def _make_select_view(
    bot=None,
    veil_members=None,
    game_message=None,
    round_id: int = ROUND_ID,
    cooldown_seconds: int = 0,
):
    from cogs.veil_cog import GuessSelectView

    if bot is None:
        bot = MagicMock()
        bot.ctx.db_path = ":memory:"
    if veil_members is None:
        veil_members = [FakeMember(id=2001, display_name="Alice")]
    if game_message is None:
        game_message = MagicMock()
        game_message.edit = AsyncMock()
        game_message.guild = MagicMock()

    return GuessSelectView(
        bot, round_id, veil_members, game_message,  # type: ignore[arg-type]
        cooldown_seconds=cooldown_seconds,
    )


# ── GuessSelectView tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_correct_first_guess_marks_solved_and_edits_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()  # not yet solved

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(3, 2)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_called_once()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "correct" in call_content.lower()


@pytest.mark.asyncio
async def test_correct_guess_already_solved_does_not_edit_game_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round(solved_at=1234.0)

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "already" in call_content.lower() or "someone" in call_content.lower()


@pytest.mark.asyncio
async def test_wrong_guess_sends_not_it_message():
    game_msg = MagicMock()
    game_msg.edit = AsyncMock()
    game_msg.guild = MagicMock()
    view = _make_select_view(game_message=game_msg)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()

    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(3333)])):
        await view._on_select(interaction)

    game_msg.edit.assert_not_called()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "not it" in call_content.lower()


@pytest.mark.asyncio
async def test_select_is_disabled_after_guess():
    view = _make_select_view()
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()
    with patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    assert view._select.disabled is True


# ── Cooldown tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_select_rejects_guess_within_cooldown():
    import time as _time
    from services.veil_models import VeilGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    recent_guess = VeilGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 5,  # 5s ago, within 30s cooldown
    )
    insert_mock = MagicMock()
    with patch("cogs.veil_cog._do_get_last_guess", return_value=recent_guess), \
         patch("cogs.veil_cog._do_insert_guess", insert_mock), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_not_called()
    call_content = interaction.response.edit_message.call_args.kwargs.get("content", "")
    assert "cooldown" in call_content.lower() or "try again" in call_content.lower()


@pytest.mark.asyncio
async def test_on_select_allows_guess_after_cooldown_expires():
    import time as _time
    from services.veil_models import VeilGuess

    view = _make_select_view(cooldown_seconds=30)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    old_guess = VeilGuess(
        id=1, round_id=ROUND_ID, guesser_id=9999,
        guessed_user_id=2001, correct=False,
        created_at=_time.time() - 60,  # 60s ago, past 30s cooldown
    )
    round_row = _make_round()
    with patch("cogs.veil_cog._do_get_last_guess", return_value=old_guess), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess") as insert_mock, \
         patch("cogs.veil_cog._do_mark_solved", return_value=(2, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    insert_mock.assert_called_once()


@pytest.mark.asyncio
async def test_on_select_cooldown_zero_disables_check():
    """cooldown_seconds=0 means no cooldown enforcement."""
    view = _make_select_view(cooldown_seconds=0)
    interaction = fake_interaction(user=FakeMember(id=9999))
    interaction.response.edit_message = AsyncMock()

    round_row = _make_round()
    get_last_mock = MagicMock()
    with patch("cogs.veil_cog._do_get_last_guess", get_last_mock), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row), \
         patch("cogs.veil_cog._do_insert_guess"), \
         patch("cogs.veil_cog._do_mark_solved", return_value=(1, 1)), \
         patch.object(type(view._select), "values", new=property(lambda _: [str(2001)])):
        await view._on_select(interaction)

    get_last_mock.assert_not_called()


# ── GameView tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_guess_callback_message_mentions_timer():
    """The ephemeral 'Who do you think this is?' message must surface the
    countdown so users know the prompt is time-limited."""
    from cogs.veil_cog import GameView
    from services.veil_models import VeilConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    guesser = FakeMember(id=9999)
    interaction = fake_interaction(user=guesser)
    interaction.response.send_message = AsyncMock()
    veil_role = MagicMock()
    veil_role.members = [FakeMember(id=2001, display_name="Alice")]
    interaction.guild.get_role = MagicMock(return_value=veil_role)

    round_row = _make_round(submitter_id=1001)
    config = VeilConfig(guild_id=9001, veil_role_id=VEIL_ROLE_ID, guess_cooldown_seconds=30)

    with patch("cogs.veil_cog._load_config", return_value=config), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "60" in msg or "second" in msg.lower(), f"expected timer hint in: {msg!r}"


@pytest.mark.asyncio
async def test_game_view_rejects_submitter_guessing_own_round():
    from cogs.veil_cog import GameView
    from services.veil_models import VeilConfig

    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    view = GameView(bot, ROUND_ID)

    submitter = FakeMember(id=1001)
    interaction = fake_interaction(user=submitter)
    interaction.response.send_message = AsyncMock()

    round_row = _make_round(submitter_id=1001)
    config = VeilConfig(guild_id=9001, veil_role_id=VEIL_ROLE_ID)

    with patch("cogs.veil_cog._load_config", return_value=config), \
         patch("cogs.veil_cog._do_load_round", return_value=round_row):
        await view._guess_callback(interaction)

    msg = interaction.response.send_message.call_args.args[0]
    assert "can't guess" in msg.lower() or "own round" in msg.lower()


# ── SubmitPreviewView re-roll tests ───────────────────────────────────────────

GUILD_ID = 9001
VEIL_CHANNEL_ID = 8001


def _make_preview_view(bot, crops):
    from cogs.veil_cog import SubmitPreviewView
    return SubmitPreviewView(
        bot, crops, GUILD_ID, VEIL_CHANNEL_ID,
        submitter_id=1001, answer_id=2001,
        difficulty="medium", candidate_count=len(crops),
    )


@pytest.mark.asyncio
async def test_reroll_cycles_to_next_crop():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"

    view = _make_preview_view(bot, [b"crop0", b"crop1", b"crop2"])
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    await view._on_reroll(interaction)

    assert view.crop_index == 1
    interaction.response.edit_message.assert_called_once()


@pytest.mark.asyncio
async def test_reroll_wraps_around_to_first_crop():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"

    view = _make_preview_view(bot, [b"c0", b"c1", b"c2"])
    interaction = fake_interaction()
    interaction.response.edit_message = AsyncMock()

    for _ in range(3):
        await view._on_reroll(interaction)

    # After 3 rerolls on 3 crops we're back at index 0
    assert view.crop_index == 0
    assert view.reroll_btn.disabled is False


@pytest.mark.asyncio
async def test_reroll_button_disabled_when_only_one_crop():
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"

    view = _make_preview_view(bot, [b"only-crop"])

    assert view.reroll_btn.disabled is True
