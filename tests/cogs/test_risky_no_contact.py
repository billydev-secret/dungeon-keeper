"""Wiring tests: the no-contact gate is actually connected to Risky Rolls.

The gate's *decisions* live in tests/test_risky_roll_no_contact.py. What
this file proves is the glue — that the views consult the list at all, and
that when a pairing cannot be avoided the round refuses to close with the
ordinary too-few-players line rather than seating them together.

Kept to that one job. Nothing here re-proves logic-layer behaviour through
Discord mocks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.risky_roll import state as rr_state
from bot_modules.services.risky_roll import views as rr_views
from bot_modules.services.risky_roll.models import RiskyRollState
from tests.fakes import FakeMember, fake_interaction

ANN, BOB = 1, 2


@pytest.fixture(autouse=True)
def _clear_risky_state(tmp_path):
    rr_state.db_path = tmp_path / "db.sqlite"
    yield
    rr_state.active_games.clear()
    # Pressing Roll caches the roller's display name globally for the roster
    # embed; leaving it behind renames players in other files' tests.
    rr_state.display_names.clear()
    rr_state.db_path = None


def _open_round(rolls: dict[int, int]) -> RiskyRollState:
    state = RiskyRollState(channel_id=100, guild_id=1, opener_id=ANN)
    state.rolls = dict(rolls)
    rr_state.active_games[state.game_id] = state
    return state


@pytest.mark.asyncio
async def test_the_roll_draw_consults_the_no_contact_list():
    """The gate has to be on the draw, not on the outcome.

    Risky Rolls has no private moment between the pairing and the contact —
    the room watched the dice decide. So the only place to intervene without
    anything to explain away is before the number exists.
    """
    state = _open_round({ANN: 40})
    view = rr_views.RiskyRollView(state.game_id)
    interaction = fake_interaction(user=FakeMember(id=BOB))
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    with (
        patch.object(
            rr_views, "choose_roll", return_value=50
        ) as choose,
        patch(
            "bot_modules.services.no_contact_service.no_contact_pairs_among",
            return_value={(ANN, BOB)},
        ),
        patch.object(rr_views, "resolve_embed_accent", AsyncMock(return_value=None)),
        patch.object(rr_views, "build_embed", MagicMock(return_value=MagicMock())),
        patch("bot_modules.economy.game_rewards.fire_member_trigger", AsyncMock()),
    ):
        await view.roll_button.callback(interaction)

    rolls_seen, roller, pairs = choose.call_args[0]
    assert roller == BOB
    assert pairs == {(ANN, BOB)}
    assert state.rolls[BOB] == 50


@pytest.mark.asyncio
async def test_a_round_that_cannot_be_made_safe_refuses_to_close():
    state = _open_round({ANN: 40, BOB: 90})
    view = rr_views.RiskyRollView(state.game_id)
    interaction = fake_interaction(user=FakeMember(id=ANN))
    interaction.response.send_message = AsyncMock()

    with patch(
        "bot_modules.services.no_contact_service.no_contact_pairs_among",
        return_value={(ANN, BOB)},
    ):
        await view.close_button.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.call_args[0][0] == rr_views.NOT_ENOUGH_TEXT
    # Refused BEFORE resolve(), which would otherwise have closed the round
    # and run the hidden tie roll-offs out from under the refusal.
    assert state.is_open is True
    assert state.highest_user is None
    assert state.game_id in rr_state.active_games


@pytest.mark.asyncio
async def test_the_refusal_is_the_same_string_an_ordinary_short_round_gets():
    """The gated refusal and the ordinary one must not be able to drift apart.

    Both branches read the same module constant. A second copy of the literal
    would let a copy edit to one of them turn the refusal into a tell, with
    nothing failing to say so.
    """
    state = _open_round({ANN: 40})  # one roll: genuinely too few players
    view = rr_views.RiskyRollView(state.game_id)
    interaction = fake_interaction(user=FakeMember(id=ANN))
    interaction.response.send_message = AsyncMock()

    with patch(
        "bot_modules.services.no_contact_service.no_contact_pairs_among",
        return_value=set(),
    ):
        await view.close_button.callback(interaction)

    assert interaction.response.send_message.call_args[0][0] == rr_views.NOT_ENOUGH_TEXT


@pytest.mark.asyncio
async def test_an_unlisted_round_closes_normally():
    state = _open_round({ANN: 40, BOB: 90})
    view = rr_views.RiskyRollView(state.game_id)
    interaction = fake_interaction(user=FakeMember(id=ANN))
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()

    with (
        patch(
            "bot_modules.services.no_contact_service.no_contact_pairs_among",
            return_value=set(),
        ),
        patch.object(rr_views, "resolve_embed_accent", AsyncMock(return_value=None)),
        patch.object(rr_views, "build_embed", MagicMock(return_value=MagicMock())),
        patch.object(rr_views, "_send_question_prompts_followup", AsyncMock()),
    ):
        await view.close_button.callback(interaction)

    assert state.highest_user == BOB
    assert state.lowest_user == ANN
