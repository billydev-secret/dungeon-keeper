"""Tests for /beta-ambient-* command handlers."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord


def _mod_interaction():
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Mod"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_sim(*, running: bool = False, posts: int = 0):
    from beta_tools.ambient_sim import AmbientSim
    sim = MagicMock(spec=AmbientSim)
    sim.is_running = running
    sim.posts_since_start = posts
    sim.last_post = ("alice", "general", 1000.0) if running else None
    sim.start = MagicMock()
    sim.stop = AsyncMock()
    sim._base_interval = MagicMock(return_value=15.0)
    sim._chain = MagicMock()
    sim._chain.corpus_size = 500
    sim._chain.vocab_size = 200
    return sim


async def test_start_handler_starts_sim():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)
    interaction = _mod_interaction()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_called_once()
    interaction.response.send_message.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "started" in msg.lower()


async def test_start_handler_already_running():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True)
    interaction = _mod_interaction()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "already" in msg.lower()


async def test_stop_handler_stops_sim():
    from beta_tools.slash.ambient import _ambient_stop_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True, posts=42)
    interaction = _mod_interaction()

    await _ambient_stop_handler(bot, interaction)

    bot.ambient_sim.stop.assert_awaited_once()
    msg = interaction.response.send_message.call_args.args[0]
    assert "stopped" in msg.lower()
    assert "42" in msg


async def test_stop_handler_not_running():
    from beta_tools.slash.ambient import _ambient_stop_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)
    interaction = _mod_interaction()

    await _ambient_stop_handler(bot, interaction)

    bot.ambient_sim.stop.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "not running" in msg.lower()


async def test_status_handler_shows_running_state():
    from beta_tools.slash.ambient import _ambient_status_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=True, posts=7)
    interaction = _mod_interaction()

    await _ambient_status_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    msg = interaction.response.send_message.call_args.args[0]
    assert "7" in msg


async def test_status_handler_shows_stopped_state():
    from beta_tools.slash.ambient import _ambient_status_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False, posts=0)
    interaction = _mod_interaction()

    await _ambient_status_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True


async def test_start_handler_rejects_non_mod():
    from beta_tools.slash.ambient import _ambient_start_handler
    bot = MagicMock()
    bot.ambient_sim = _make_sim(running=False)

    interaction = MagicMock()
    role = MagicMock()
    role.name = "Member"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    await _ambient_start_handler(bot, interaction)

    bot.ambient_sim.start.assert_not_called()
    msg = interaction.response.send_message.call_args.args[0]
    assert "moderator" in msg.lower()
