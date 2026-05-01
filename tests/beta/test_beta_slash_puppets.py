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


async def test_puppets_reload_handler_happy_path(three_personas, monkeypatch):
    """Reload reads YAML, updates handle personas, calls apply_personas."""
    from beta_tools.slash import puppets as puppets_module
    from beta_tools.slash.puppets import _puppets_reload_handler
    from beta_tools.puppet_manager import PuppetHandle

    new_personas = [
        Persona(key="alice", display_name="NewAlice", avatar_url="https://x/a2.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="short"),
        Persona(key="bob", display_name="NewBob", avatar_url="https://x/b2.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=False, message_length_bias="medium"),
        Persona(key="clara", display_name="NewClara", avatar_url="https://x/c2.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="long"),
    ]
    monkeypatch.setattr(puppets_module, "load_puppet_personas", lambda _path: new_personas)

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.handles = [
        PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1),
        PuppetHandle(key="bob", persona=three_personas[1], token="t2", expected_id=2),
        PuppetHandle(key="clara", persona=three_personas[2], token="t3", expected_id=3),
    ]
    bot.puppet_manager.apply_personas = AsyncMock()

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()

    await _puppets_reload_handler(bot, interaction)

    bot.puppet_manager.apply_personas.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "Reloaded 3" in args[0]
    assert kwargs.get("ephemeral") is True


async def test_puppets_reload_handler_count_mismatch(three_personas, monkeypatch):
    """If the YAML has != current puppet count, reload reports the mismatch."""
    from beta_tools.slash import puppets as puppets_module
    from beta_tools.slash.puppets import _puppets_reload_handler
    from beta_tools.puppet_manager import PuppetHandle

    new_personas = [
        Persona(key="alice", display_name="NewAlice", avatar_url="https://x/a2.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=True, message_length_bias="short"),
        Persona(key="bob", display_name="NewBob", avatar_url="https://x/b2.png",
                activity_weight=1.0, channel_affinities={"general": 1.0},
                voice_likely=False, message_length_bias="medium"),
    ]
    monkeypatch.setattr(puppets_module, "load_puppet_personas", lambda _path: new_personas)

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.handles = [
        PuppetHandle(key="alice", persona=three_personas[0], token="t1", expected_id=1),
        PuppetHandle(key="bob", persona=three_personas[1], token="t2", expected_id=2),
        PuppetHandle(key="clara", persona=three_personas[2], token="t3", expected_id=3),
    ]
    bot.puppet_manager.apply_personas = AsyncMock()

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()

    await _puppets_reload_handler(bot, interaction)

    bot.puppet_manager.apply_personas.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "Reload failed" in args[0]
    assert "2 personas" in args[0]
    assert "3 puppets" in args[0]


async def test_puppets_reconnect_handler_rejects_unknown_key():
    """Reconnect with a missing key sends a clear error."""
    from beta_tools.slash.puppets import _puppets_reconnect_handler

    bot = MagicMock()
    bot.puppet_manager = MagicMock()
    bot.puppet_manager.get_handle = MagicMock(side_effect=KeyError("nobody"))

    interaction = _mod_interaction()
    interaction.response.defer = AsyncMock()

    await _puppets_reconnect_handler(bot, interaction, key="nobody")

    bot.puppet_manager.get_handle.assert_called_once_with("nobody")
    interaction.followup.send.assert_awaited_once()
    args, kwargs = interaction.followup.send.call_args
    assert "Unknown puppet" in args[0] or "nobody" in args[0]
    assert kwargs.get("ephemeral") is True
