"""Tests for /beta-puppets-* command handler logic.

Discord's app_commands decorators make registration hard to unit-test directly.
We extract the handler functions and test them as plain async callables.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from beta_tools.personas import Persona


@pytest.fixture
def three_personas():
    return [
        Persona(key="alice", display_name="Alice", avatar_url="https://x/a.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="short"),
        Persona(key="bob", display_name="Bob", avatar_url="https://x/b.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=False, message_length_bias="medium"),
        Persona(key="clara", display_name="Clara", avatar_url="https://x/c.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="long"),
    ]


def _mod_interaction():
    """Build a fake discord.Interaction whose user has Mod role."""
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Mod"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _regular_interaction():
    interaction = MagicMock()
    role = MagicMock()
    role.name = "Member"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.roles = [role]
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


async def test_puppets_list_handler_lists_handles(three_personas):
    from beta_tools.slash.puppets import _puppets_list_handler
    from beta_tools.puppet_manager import PuppetHandle

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    handle1 = PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1)
    handle1.client = MagicMock()
    handle1.client.user = MagicMock()
    handle1.client.user.__str__ = lambda self: "Alice#0001"
    handle1.client.user.id = 1
    handle1.ready = MagicMock()
    handle1.ready.is_set = MagicMock(return_value=True)

    handle2 = PuppetHandle(key="bob", persona=three_personas[1], token="t2", expected_id=2)
    handle2.client = None
    handle2.ready = MagicMock()
    handle2.ready.is_set = MagicMock(return_value=False)

    bot.puppet_manager.handles = [handle1, handle2]

    interaction = _mod_interaction()
    await _puppets_list_handler(bot, interaction)

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    msg = args[0] if args else kwargs.get("content", "")
    assert "alice" in msg
    assert "bob" in msg
    assert kwargs.get("ephemeral") is True


async def test_puppets_list_handler_rejects_non_mod():
    from beta_tools.slash.puppets import _puppets_list_handler

    bot = MagicMock()
    interaction = _regular_interaction()
    await _puppets_list_handler(bot, interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert "moderator" in args[0].lower() or "moderator" in (kwargs.get("content", "") or "").lower()
    assert kwargs.get("ephemeral") is True


async def test_puppets_impersonate_handler_dispatches_to_puppet(three_personas):
    from beta_tools.slash.puppets import _puppets_impersonate_handler
    from beta_tools.puppet_manager import PuppetHandle

    fake_channel = MagicMock()
    fake_channel.send = AsyncMock()

    handle = PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1)
    fake_puppet_channel = MagicMock()
    fake_puppet_channel.send = AsyncMock()
    handle.client = MagicMock()
    handle.client.get_channel = MagicMock(return_value=fake_puppet_channel)

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.get_handle = MagicMock(return_value=handle)

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()

    await _puppets_impersonate_handler(bot, interaction, key="alice", channel=fake_channel, text="hello")

    handle.client.get_channel.assert_called_once_with(fake_channel.id)
    fake_puppet_channel.send.assert_awaited_once_with("hello")
    interaction.followup.send.assert_awaited_once()


async def test_puppets_impersonate_handler_rejects_unknown_key():
    from beta_tools.slash.puppets import _puppets_impersonate_handler

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.get_handle = MagicMock(side_effect=KeyError("nobody"))

    interaction = _mod_interaction()
    fake_channel = MagicMock()
    await _puppets_impersonate_handler(bot, interaction, key="nobody", channel=fake_channel, text="hello")

    args, kwargs = interaction.response.send_message.call_args
    msg = args[0] if args else kwargs.get("content", "")
    assert "unknown" in msg.lower() or "no puppet" in msg.lower()
    assert kwargs.get("ephemeral") is True


async def test_ghosts_impersonate_handler_uses_webhook_fleet():
    from beta_tools.slash.puppets import _ghosts_impersonate_handler

    bot = MagicMock()
    bot.webhook_fleet = MagicMock()
    bot.webhook_fleet.send = AsyncMock()

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()
    fake_channel = MagicMock()

    await _ghosts_impersonate_handler(
        bot, interaction,
        display_name="ghost_test", avatar_url="https://x/g.png",
        channel=fake_channel, text="hi",
    )

    bot.webhook_fleet.send.assert_awaited_once_with(
        fake_channel, content="hi", username="ghost_test", avatar_url="https://x/g.png",
    )
    interaction.followup.send.assert_awaited_once()
