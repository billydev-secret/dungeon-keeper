"""Cog-level: optin / optout."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.whisper_models import WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

ROLE_ID = 7001


def _cog_with_role():
    from bot_modules.cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx
    return cog


@pytest.mark.asyncio
async def test_optin_sends_confirmation_view():
    """/whisper optin shows a consent message + confirm/cancel View
    rather than granting the role immediately."""
    from bot_modules.cogs.whisper_cog import WhisperOptinConfirmView

    cog = _cog_with_role()
    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    cfg = WhisperConfig(guild_id=9001, role_id=ROLE_ID, channel_id=8001, log_channel_id=8002)
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await cog._optin_impl(interaction)

    # Role is NOT granted yet — only after confirm.
    member.add_roles.assert_not_awaited()
    # Send_message was called with a view + ephemeral consent text.
    interaction.response.send_message.assert_awaited()
    kwargs = interaction.response.send_message.call_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert isinstance(kwargs.get("view"), WhisperOptinConfirmView)
    args = interaction.response.send_message.call_args.args
    body = args[0] if args else kwargs.get("content", "")
    assert "opt" in body.lower()


def _find_button_by_label(view: discord.ui.View, label: str) -> discord.ui.Button:
    for item in view.children:
        if isinstance(item, discord.ui.Button) and item.label == label:
            return item
    raise AssertionError(f"button {label!r} not found on view")


@pytest.mark.asyncio
async def test_optin_confirm_button_grants_role():
    """The Confirm button on the consent view actually grants the role."""
    from bot_modules.cogs.whisper_cog import WhisperOptinConfirmView

    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.response.edit_message = AsyncMock()

    bot = MagicMock()
    view = WhisperOptinConfirmView(bot, role)  # type: ignore[arg-type]
    confirm_button = _find_button_by_label(view, "Confirm")
    await confirm_button.callback(interaction)

    member.add_roles.assert_awaited_once_with(role, reason="Whisper opt-in")
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_optin_cancel_button_does_not_grant_role():
    """The Cancel button on the consent view does not grant the role."""
    from bot_modules.cogs.whisper_cog import WhisperOptinConfirmView

    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.response.edit_message = AsyncMock()

    bot = MagicMock()
    view = WhisperOptinConfirmView(bot, role)  # type: ignore[arg-type]
    cancel_button = _find_button_by_label(view, "Cancel")
    await cancel_button.callback(interaction)

    member.add_roles.assert_not_awaited()
    interaction.response.edit_message.assert_awaited()


@pytest.mark.asyncio
async def test_optin_not_configured():
    cog = _cog_with_role()
    member = FakeMember(id=1001, roles=[])
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    cfg = WhisperConfig(guild_id=9001)  # no role configured
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await cog._optin_impl(interaction)

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_optout_removes_role():
    cog = _cog_with_role()
    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[role])
    member.remove_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    cfg = WhisperConfig(guild_id=9001, role_id=ROLE_ID, channel_id=8001, log_channel_id=8002)
    with patch("bot_modules.cogs.whisper_cog._load_config", return_value=cfg):
        await cog._optout_impl(interaction)

    member.remove_roles.assert_awaited_once()
