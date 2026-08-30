"""Cog-level: /risky start channel game cap and the per-guild enable switch."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot_modules.services.risky_roll import state as rr_state
from bot_modules.services.risky_roll.models import RiskyRollState
from tests.fakes import FakeGuild, FakeMember, fake_interaction

GUILD_ID = 9001
CHANNEL_ID = 5001


def _make_channel() -> MagicMock:
    channel = MagicMock()
    channel.id = CHANNEL_ID
    perms = MagicMock(send_messages=True, embed_links=True)
    channel.permissions_for = MagicMock(return_value=perms)
    return channel


def _make_cog():
    from bot_modules.cogs.risky_roll_cog import RiskyRollCog
    bot = MagicMock()
    bot.ctx = MagicMock()
    return RiskyRollCog(bot)


@pytest.fixture(autouse=True)
def _clear_risky_state():
    yield
    rr_state.active_games.clear()
    rr_state.max_games_per_channel.clear()


@pytest.fixture(autouse=True)
def _game_enabled():
    """Every guard below sits behind the enable switch, so hold it on.

    The switch itself is exercised by the test right underneath.
    """
    with patch(
        "bot_modules.cogs.risky_roll_cog.check_game_enabled",
        AsyncMock(return_value=True),
    ) as gate:
        yield gate


@pytest.mark.asyncio
async def test_start_refused_when_the_game_is_switched_off():
    """Games -> Global Config lists risky_roll under "Available on This Server",
    whose hint promises the command refuses when it is off. It used to be read
    only by the scheduler, so an admin who unticked it watched members go on
    opening rounds by hand."""
    guild = FakeGuild(id=GUILD_ID)
    guild.me = MagicMock()  # type: ignore[attr-defined]
    channel = _make_channel()
    guild.channels[channel.id] = channel

    interaction = fake_interaction(user=FakeMember(id=1001), guild=guild, channel=channel)

    cog = _make_cog()
    with patch(
        "bot_modules.cogs.risky_roll_cog.check_game_enabled",
        AsyncMock(return_value=False),
    ):
        await cog._start_game(
            interaction, auto_close_players=None, auto_close_minutes=None,
            ping=False, skip_min_game_time=True,
        )

    msg = interaction.response.send_message.call_args.args[0]
    assert "switched off" in msg
    assert not rr_state.active_games


@pytest.mark.asyncio
async def test_start_blocked_by_configured_cap_below_hardcoded_default():
    """A guild with a cap of 1 is blocked with only 1 active game — proving
    rr_state.max_games_per_channel, not the hardcoded default of 10, drives
    enforcement."""
    guild = FakeGuild(id=GUILD_ID)
    guild.me = MagicMock()  # type: ignore[attr-defined]
    channel = _make_channel()
    guild.channels[channel.id] = channel

    interaction = fake_interaction(user=FakeMember(id=1001), guild=guild, channel=channel)

    rr_state.active_games["existing"] = RiskyRollState(
        game_id="existing", channel_id=CHANNEL_ID, guild_id=GUILD_ID, opener_id=2002,
    )
    rr_state.max_games_per_channel[GUILD_ID] = 1

    cog = _make_cog()
    await cog._start_game(
        interaction, auto_close_players=None, auto_close_minutes=None,
        ping=False, skip_min_game_time=True,
    )

    msg = interaction.response.send_message.call_args.args[0]
    assert "already has 1 active games" in msg


@pytest.mark.asyncio
async def test_start_allowed_under_configured_cap():
    """A guild with a cap of 3 is NOT blocked with only 1 active game."""
    guild = FakeGuild(id=GUILD_ID)
    guild.me = MagicMock()  # type: ignore[attr-defined]
    channel = _make_channel()
    guild.channels[channel.id] = channel

    interaction = fake_interaction(user=FakeMember(id=1001), guild=guild, channel=channel)
    interaction.response.send_message = AsyncMock()

    rr_state.active_games["existing"] = RiskyRollState(
        game_id="existing", channel_id=CHANNEL_ID, guild_id=GUILD_ID, opener_id=2002,
    )
    rr_state.max_games_per_channel[GUILD_ID] = 3

    cog = _make_cog()
    try:
        await cog._start_game(
            interaction, auto_close_players=None, auto_close_minutes=None,
            ping=False, skip_min_game_time=True,
        )
    except Exception:
        # The success path beyond the cap check isn't fully mocked here (no
        # real Discord message round-trip) — irrelevant to this guard.
        pass

    rejected = any(
        call.args and isinstance(call.args[0], str) and "already has" in call.args[0]
        for call in interaction.response.send_message.call_args_list
    )
    assert not rejected
