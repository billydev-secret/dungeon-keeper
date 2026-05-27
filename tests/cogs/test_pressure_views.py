"""Tests for pressure_cooker views and modals."""
from __future__ import annotations

from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.pressure_cooker.views import ChallengeView, GameView, ResultView, gauge_bar
from bot_modules.cogs.pressure_cooker.modals import NicknameModal
from tests.fakes import FakeUser, fake_interaction


# ── gauge_bar ─────────────────────────────────────────────────────────────────

def test_gauge_bar_empty():
    result = gauge_bar(0)
    assert "0/100" in result
    assert "█" not in result


def test_gauge_bar_half():
    result = gauge_bar(50)
    assert "50/100" in result
    assert "█" in result
    assert "░" in result


def test_gauge_bar_full():
    result = gauge_bar(100)
    assert "100/100" in result
    assert "░" not in result


def test_gauge_bar_over_ceiling_clamps():
    result = gauge_bar(115)
    assert "115/100" in result  # shows actual value
    assert "░" not in result    # bar is full


# ── ChallengeView ─────────────────────────────────────────────────────────────

async def test_challenge_accept_by_target():
    on_accept = AsyncMock()
    on_decline = AsyncMock()
    view = ChallengeView(game_id=1, target_id=42, on_accept=on_accept, on_decline=on_decline)

    interaction = fake_interaction(user=FakeUser(id=42))
    # Find the accept button and call its callback
    accept_btn = next(b for b in view.children if isinstance(b, discord.ui.Button) and b.emoji and "✅" in str(b.emoji))
    await accept_btn.callback(interaction)

    on_accept.assert_awaited_once_with(interaction, 1)
    on_decline.assert_not_awaited()


async def test_challenge_accept_by_wrong_user_rejected():
    on_accept = AsyncMock()
    view = ChallengeView(game_id=1, target_id=42, on_accept=on_accept, on_decline=AsyncMock())

    interaction = fake_interaction(user=FakeUser(id=99))
    accept_btn = next(b for b in view.children if isinstance(b, discord.ui.Button) and b.emoji and "✅" in str(b.emoji))
    await accept_btn.callback(interaction)

    on_accept.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args
    assert call_kwargs.kwargs.get("ephemeral") is True


async def test_challenge_decline_by_target():
    on_decline = AsyncMock()
    view = ChallengeView(game_id=1, target_id=42, on_accept=AsyncMock(), on_decline=on_decline)

    interaction = fake_interaction(user=FakeUser(id=42))
    decline_btn = next(b for b in view.children if isinstance(b, discord.ui.Button) and b.emoji and "❌" in str(b.emoji))
    await decline_btn.callback(interaction)

    on_decline.assert_awaited_once_with(interaction, 1)


async def test_challenge_decline_by_wrong_user_rejected():
    on_decline = AsyncMock()
    view = ChallengeView(game_id=1, target_id=42, on_accept=AsyncMock(), on_decline=on_decline)

    interaction = fake_interaction(user=FakeUser(id=77))
    decline_btn = next(b for b in view.children if isinstance(b, discord.ui.Button) and b.emoji and "❌" in str(b.emoji))
    await decline_btn.callback(interaction)

    on_decline.assert_not_awaited()


# ── GameView ──────────────────────────────────────────────────────────────────

async def test_game_view_pump_fires_callback():
    on_pump = AsyncMock()
    view = GameView(game_id=5, on_pump=on_pump)

    interaction = fake_interaction(user=FakeUser(id=10))
    pump_btn = view.children[0]
    assert isinstance(pump_btn, discord.ui.Button)
    await pump_btn.callback(interaction)

    on_pump.assert_awaited_once_with(interaction, 5)


def test_game_view_custom_id_encodes_game_id():
    view = GameView(game_id=99, on_pump=AsyncMock())
    pump_btn = view.children[0]
    assert pump_btn.custom_id == "pump:99"


def test_game_view_disable_disables_button():
    view = GameView(game_id=1, on_pump=AsyncMock())
    view.disable()
    assert all(b.disabled for b in view.children if isinstance(b, discord.ui.Button))


# ── ResultView ────────────────────────────────────────────────────────────────

def _make_result_view(game_id=7, winner_id=1, loser_id=2):
    return ResultView(
        game_id=game_id,
        winner_id=winner_id,
        loser_id=loser_id,
        on_set_nick=AsyncMock(),
        on_honor=AsyncMock(),
        on_rematch=AsyncMock(),
    )


def _get_btn(view: ResultView, emoji_char: str) -> discord.ui.Button:
    return next(
        b for b in view.children
        if isinstance(b, discord.ui.Button) and b.emoji and emoji_char in str(b.emoji)
    )


async def test_result_set_nick_by_winner():
    on_set_nick = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=on_set_nick, on_honor=AsyncMock(), on_rematch=AsyncMock())
    interaction = fake_interaction(user=FakeUser(id=1))
    await _get_btn(view, "📝").callback(interaction)
    on_set_nick.assert_awaited_once_with(interaction, 7)


async def test_result_set_nick_by_wrong_user():
    on_set_nick = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=on_set_nick, on_honor=AsyncMock(), on_rematch=AsyncMock())
    interaction = fake_interaction(user=FakeUser(id=99))
    await _get_btn(view, "📝").callback(interaction)
    on_set_nick.assert_not_awaited()
    assert interaction.response.send_message.called


async def test_result_honor_by_loser():
    on_honor = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=AsyncMock(), on_honor=on_honor, on_rematch=AsyncMock())
    interaction = fake_interaction(user=FakeUser(id=2))
    await _get_btn(view, "🤝").callback(interaction)
    on_honor.assert_awaited_once_with(interaction, 7)


async def test_result_honor_by_winner_rejected():
    on_honor = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=AsyncMock(), on_honor=on_honor, on_rematch=AsyncMock())
    interaction = fake_interaction(user=FakeUser(id=1))
    await _get_btn(view, "🤝").callback(interaction)
    on_honor.assert_not_awaited()


async def test_result_rematch_by_winner():
    on_rematch = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=AsyncMock(), on_honor=AsyncMock(), on_rematch=on_rematch)
    interaction = fake_interaction(user=FakeUser(id=1))
    await _get_btn(view, "🔁").callback(interaction)
    on_rematch.assert_awaited_once_with(interaction, 7)


async def test_result_rematch_by_loser():
    on_rematch = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=AsyncMock(), on_honor=AsyncMock(), on_rematch=on_rematch)
    interaction = fake_interaction(user=FakeUser(id=2))
    await _get_btn(view, "🔁").callback(interaction)
    on_rematch.assert_awaited_once_with(interaction, 7)


async def test_result_rematch_by_third_party_rejected():
    on_rematch = AsyncMock()
    view = ResultView(7, winner_id=1, loser_id=2, on_set_nick=AsyncMock(), on_honor=AsyncMock(), on_rematch=on_rematch)
    interaction = fake_interaction(user=FakeUser(id=99))
    await _get_btn(view, "🔁").callback(interaction)
    on_rematch.assert_not_awaited()


def test_result_custom_ids_encode_game_id():
    view = _make_result_view(game_id=42)
    custom_ids = {b.custom_id for b in view.children if isinstance(b, discord.ui.Button)}
    assert "set_nick:42" in custom_ids
    assert "honor:42" in custom_ids
    assert "rematch:42" in custom_ids


# ── NicknameModal ─────────────────────────────────────────────────────────────

async def test_nickname_modal_on_submit_calls_callback():
    on_submit = AsyncMock()
    modal = NicknameModal(game_id=3, on_submit=on_submit)
    # Simulate the TextInput value being set
    modal.nick_input._value = "LoserFace"
    interaction = fake_interaction(user=FakeUser(id=1))
    await modal.on_submit(interaction)
    on_submit.assert_awaited_once_with(interaction, 3, "LoserFace")


async def test_nickname_modal_passes_raw_value():
    submitted = []

    async def capture(interaction, game_id, raw):
        submitted.append(raw)

    modal = NicknameModal(game_id=1, on_submit=capture)
    modal.nick_input._value = "  SpaceyNick  "
    await modal.on_submit(fake_interaction())
    assert submitted == ["  SpaceyNick  "]
