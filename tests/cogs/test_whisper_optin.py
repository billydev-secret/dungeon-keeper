"""Cog-level: optin / optout."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.whisper_models import WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

ROLE_ID = 7001


def _cog_with_role():
    from cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx
    return cog


@pytest.mark.asyncio
async def test_optin_grants_role():
    cog = _cog_with_role()
    role = FakeRole(id=ROLE_ID, name="Whisper")
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock()
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.get_role = MagicMock(return_value=role)

    cfg = WhisperConfig(guild_id=9001, role_id=ROLE_ID, channel_id=8001, log_channel_id=8002)
    with patch("cogs.whisper_cog._load_config", return_value=cfg):
        await cog._optin_impl(interaction)

    member.add_roles.assert_awaited_once_with(role, reason="Whisper opt-in")
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_optin_not_configured():
    cog = _cog_with_role()
    member = FakeMember(id=1001, roles=[])
    interaction = fake_interaction(user=member)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001

    cfg = WhisperConfig(guild_id=9001)  # no role configured
    with patch("cogs.whisper_cog._load_config", return_value=cfg):
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
    with patch("cogs.whisper_cog._load_config", return_value=cfg):
        await cog._optout_impl(interaction)

    member.remove_roles.assert_awaited_once()
