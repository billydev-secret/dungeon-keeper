"""Guards for the Gap 3 command collapses (2026-07-28).

Three pairs of commands became one command with an option:

  /risky start_no_ping        -> /risky start ping:false
  /games config game-end      -> /games end force:true
  /wellness away on|off       -> /wellness away set state:on|off

The risk in every collapse is the same: an option is easier to get wrong than a
separate callback, because one code path now has to honour both behaviours *and*
keep the permission difference the two commands used to encode in their
decorators. These assert the option actually reaches the underlying call, and
that the old gates survived the merge.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── /risky start ping: ───────────────────────────────────────────────


def _risky_cog():
    from bot_modules.cogs.risky_roll_cog import RiskyRollCog

    cog = RiskyRollCog.__new__(RiskyRollCog)
    cog._start_game = AsyncMock()  # type: ignore[method-assign]
    return cog


@pytest.mark.parametrize(
    ("ping", "expect_skip"),
    [
        pytest.param(True, False, id="ping-holds-the-min-game-time"),
        pytest.param(False, True, id="quiet-round-skips-it"),
    ],
)
@pytest.mark.asyncio
async def test_risky_start_ping_drives_the_min_game_time(ping, expect_skip):
    """The retired start_no_ping set both flags together; the minimum exists to
    give pinged members time to arrive, so a quiet round must still skip it."""
    from bot_modules.cogs.risky_roll_cog import RiskyRollCog

    cog = _risky_cog()
    await RiskyRollCog.risky_start.callback(cog, MagicMock(), ping=ping)  # type: ignore[attr-defined]

    kwargs = cog._start_game.await_args.kwargs
    assert kwargs["ping"] is ping
    assert kwargs["skip_min_game_time"] is expect_skip


@pytest.mark.asyncio
async def test_risky_start_pings_by_default():
    from bot_modules.cogs.risky_roll_cog import RiskyRollCog

    cog = _risky_cog()
    await RiskyRollCog.risky_start.callback(cog, MagicMock())  # type: ignore[attr-defined]

    assert cog._start_game.await_args.kwargs["ping"] is True


# ── /games end force: ────────────────────────────────────────────────


def _games_cog(*, host_id: int, caller_id: int, is_mod: bool, monkeypatch):
    import bot_modules.cogs.games_config_cog as mod
    from bot_modules.cogs.games_config_cog import GamesConfigCog

    monkeypatch.setattr(
        mod, "get_active_game", AsyncMock(return_value={
            "host_id": host_id, "game_id": 1, "game_type": "wyr", "message_id": None,
        })
    )
    monkeypatch.setattr(mod, "has_mod_or_admin_permissions", lambda _p: is_mod)
    monkeypatch.setattr(mod, "build_force_end_embed", lambda _t: MagicMock())

    cog = GamesConfigCog.__new__(GamesConfigCog)
    cog.bot = MagicMock()
    cog._teardown_active_game = AsyncMock()  # type: ignore[method-assign]
    return cog


def _games_interaction(caller_id: int):
    import discord

    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = caller_id
    interaction.user.display_name = "Caller"
    interaction.guild = MagicMock()
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.channel.send = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_games_end_force_closes_without_confirming_for_a_mod(monkeypatch):
    from bot_modules.cogs.games_config_cog import GamesConfigCog

    cog = _games_cog(host_id=99, caller_id=1, is_mod=True, monkeypatch=monkeypatch)
    interaction = _games_interaction(1)

    await GamesConfigCog.games_end.callback(cog, interaction, force=True)  # type: ignore[attr-defined]

    cog._teardown_active_game.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_games_end_force_is_refused_for_a_non_mod_host(monkeypatch):
    """The retired command was mod-gated. A host skipping their own confirmation
    would be a capability the merge invented."""
    from bot_modules.cogs.games_config_cog import GamesConfigCog

    cog = _games_cog(host_id=1, caller_id=1, is_mod=False, monkeypatch=monkeypatch)
    interaction = _games_interaction(1)

    await GamesConfigCog.games_end.callback(cog, interaction, force=True)  # type: ignore[attr-defined]

    cog._teardown_active_game.assert_not_awaited()
    assert "moderator" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_games_end_without_force_still_confirms(monkeypatch):
    from bot_modules.cogs.games_config_cog import GamesConfigCog

    cog = _games_cog(host_id=1, caller_id=1, is_mod=False, monkeypatch=monkeypatch)
    interaction = _games_interaction(1)

    await GamesConfigCog.games_end.callback(cog, interaction)  # type: ignore[attr-defined]

    cog._teardown_active_game.assert_not_awaited()
    assert "sure" in interaction.response.send_message.await_args.args[0].lower()
    assert interaction.response.send_message.await_args.kwargs.get("view") is not None


# ── /wellness away set ───────────────────────────────────────────────


def _wellness_cog(monkeypatch, *, write=None):
    import bot_modules.cogs.wellness_cog as mod
    from bot_modules.cogs.wellness_cog import WellnessCog

    monkeypatch.setattr(mod, "_require_active_user", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(mod, "update_away_message", write or MagicMock())
    monkeypatch.setattr(mod, "get_wellness_user", MagicMock(return_value=MagicMock(away_message="saved")))

    cog = WellnessCog.__new__(WellnessCog)
    cog.bot = MagicMock()
    cog.bot.ctx = MagicMock()
    return cog


def _choice(value: str):
    from discord import app_commands

    return app_commands.Choice(name=value, value=value)


def _wellness_interaction():
    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.user.id = 7
    interaction.response.send_message = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_away_off_preserves_the_stored_message(monkeypatch):
    """update_away_message only rewrites away_message when passed one, so off
    must pass None — otherwise turning away mode off would wipe the text the
    member wrote."""
    from bot_modules.cogs.wellness_cog import WellnessCog

    write = MagicMock()
    cog = _wellness_cog(monkeypatch, write=write)
    interaction = _wellness_interaction()

    await WellnessCog.away_set_cmd.callback(cog, interaction, _choice("off"))  # type: ignore[attr-defined]

    assert write.call_args.kwargs["enabled"] is False
    assert write.call_args.kwargs["message"] is None


@pytest.mark.asyncio
async def test_away_on_writes_the_new_message(monkeypatch):
    from bot_modules.cogs.wellness_cog import WellnessCog

    write = MagicMock()
    cog = _wellness_cog(monkeypatch, write=write)

    await WellnessCog.away_set_cmd.callback(  # type: ignore[attr-defined]
        cog, _wellness_interaction(), _choice("on"), "back tuesday"
    )

    assert write.call_args.kwargs["enabled"] is True
    assert write.call_args.kwargs["message"] == "back tuesday"


@pytest.mark.asyncio
async def test_away_off_with_a_message_is_refused(monkeypatch):
    """A message only applies when switching on. Refusing beats silently
    dropping text the member expected to save."""
    from bot_modules.cogs.wellness_cog import WellnessCog

    write = MagicMock()
    cog = _wellness_cog(monkeypatch, write=write)
    interaction = _wellness_interaction()

    await WellnessCog.away_set_cmd.callback(  # type: ignore[attr-defined]
        cog, interaction, _choice("off"), "back tuesday"
    )

    write.assert_not_called()
    assert "only applies" in interaction.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_away_rejects_an_overlong_message(monkeypatch):
    from bot_modules.cogs.wellness_cog import WellnessCog
    from bot_modules.cogs.wellness_cog import AWAY_MESSAGE_MAX

    write = MagicMock()
    cog = _wellness_cog(monkeypatch, write=write)
    interaction = _wellness_interaction()

    await WellnessCog.away_set_cmd.callback(  # type: ignore[attr-defined]
        cog, interaction, _choice("on"), "x" * (AWAY_MESSAGE_MAX + 1)
    )

    write.assert_not_called()
    assert "characters or fewer" in interaction.response.send_message.await_args.args[0]
