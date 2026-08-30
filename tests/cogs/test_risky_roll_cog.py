"""Cog-level: /risky start channel game cap and the per-guild enable switch."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


def _make_cog(*, enabled: bool = True):
    from bot_modules.cogs.risky_roll_cog import RiskyRollCog
    bot = MagicMock()
    bot.ctx = MagicMock()
    # `_start_game` consults the games_game_config enable switch first; no row
    # means enabled, which is what an unconfigured guild looks like.
    bot.games_db.fetchone = AsyncMock(return_value=None if enabled else (0,))
    return RiskyRollCog(bot)


@pytest.fixture(autouse=True)
def _clear_risky_state():
    yield
    rr_state.active_games.clear()
    rr_state.max_games_per_channel.clear()


@pytest.mark.asyncio
async def test_start_refused_when_game_disabled_on_the_server():
    """The dashboard's Available on This Server switch has to actually stop it."""
    guild = FakeGuild(id=GUILD_ID)
    guild.me = MagicMock()  # type: ignore[attr-defined]
    channel = _make_channel()
    guild.channels[channel.id] = channel
    interaction = fake_interaction(user=FakeMember(id=1001), guild=guild, channel=channel)

    cog = _make_cog(enabled=False)
    await cog._start_game(
        interaction, auto_close_players=None, auto_close_minutes=None,
        ping=False, skip_min_game_time=True,
    )

    msg = interaction.response.send_message.call_args.args[0]
    assert "disabled" in msg.lower()
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


# ── closing a channel's rounds from outside ──────────────────────────────────
#
# The feature rotation ends a room's game when the room stops being the
# featured one. Risky Rolls keeps rounds in rr_state.active_games rather than
# the shared games_active_games table, so this closer is the only way the
# rotation can reach them.


@pytest.mark.asyncio
async def test_closing_a_channel_resolves_its_round_rather_than_dropping_it(
    monkeypatch,
):
    """Routed through auto_close_round on purpose: that is the round's real
    resolution, so a winner is picked and the no-contact gate is consulted.
    Dropping the state would strand the players with no result."""
    from bot_modules.cogs import risky_roll_cog as rr

    resolved = []

    async def fake_auto_close(client, game_id):
        resolved.append(game_id)
        rr_state.active_games.pop(game_id, None)

    monkeypatch.setattr(rr, "auto_close_round", fake_auto_close)

    rr_state.active_games["live"] = RiskyRollState(
        game_id="live", channel_id=CHANNEL_ID, guild_id=GUILD_ID, opener_id=2002,
    )

    cog = _make_cog()
    assert await cog.close_channel_rounds(CHANNEL_ID) is True
    assert resolved == ["live"]


@pytest.mark.asyncio
async def test_closing_a_channel_leaves_other_channels_rounds_alone(monkeypatch):
    from bot_modules.cogs import risky_roll_cog as rr

    resolved = []

    async def fake_auto_close(client, game_id):
        resolved.append(game_id)

    monkeypatch.setattr(rr, "auto_close_round", fake_auto_close)

    rr_state.active_games["mine"] = RiskyRollState(
        game_id="mine", channel_id=CHANNEL_ID, guild_id=GUILD_ID, opener_id=2002,
    )
    rr_state.active_games["theirs"] = RiskyRollState(
        game_id="theirs", channel_id=CHANNEL_ID + 1, guild_id=GUILD_ID, opener_id=2002,
    )

    cog = _make_cog()
    await cog.close_channel_rounds(CHANNEL_ID)
    assert resolved == ["mine"]


@pytest.mark.asyncio
async def test_closing_a_channel_with_no_round_reports_nothing_closed():
    cog = _make_cog()
    assert await cog.close_channel_rounds(CHANNEL_ID) is False


@pytest.mark.asyncio
async def test_closing_cancels_the_pending_auto_close_timer(monkeypatch):
    """Otherwise the sleeping timer fires later on a round already resolved."""
    import asyncio
    import contextlib

    from bot_modules.cogs import risky_roll_cog as rr

    async def fake_auto_close(client, game_id):
        rr_state.active_games.pop(game_id, None)

    monkeypatch.setattr(rr, "auto_close_round", fake_auto_close)

    async def _sleep_forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_sleep_forever())
    rr_state.active_games["live"] = RiskyRollState(
        game_id="live", channel_id=CHANNEL_ID, guild_id=GUILD_ID, opener_id=2002,
    )
    rr_state.auto_close_tasks["live"] = task

    cog = _make_cog()
    await cog.close_channel_rounds(CHANNEL_ID)

    assert "live" not in rr_state.auto_close_tasks
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()
