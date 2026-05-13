"""Cog-level tests for the /veil optin command — self-add veil role."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.services.veil_models import VeilConfig
from tests.fakes import FakeGuild, FakeMember, FakeRole, fake_interaction

VEIL_ROLE_ID = 7001
GUILD_ID = 9001


def _make_cog(db_path: str = ":memory:"):
    from bot_modules.cogs.veil_cog import VeilCog
    bot = MagicMock()
    bot.ctx.db_path = db_path
    return VeilCog(bot)


def _config(*, veil_role_id: int = VEIL_ROLE_ID) -> VeilConfig:
    return VeilConfig(guild_id=GUILD_ID, veil_role_id=veil_role_id)


async def _optin(cog, interaction):
    await cog.veil_optin.callback(cog, interaction)


@pytest.mark.asyncio
async def test_optin_adds_role_when_not_present():
    role = FakeRole(id=VEIL_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "veil pool" in sent.lower() or "welcome" in sent.lower()


@pytest.mark.asyncio
async def test_optin_skips_when_already_in_pool():
    role = FakeRole(id=VEIL_ROLE_ID)
    member = FakeMember(id=1001, roles=[role])
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "already" in sent.lower()


@pytest.mark.asyncio
async def test_optin_rejects_when_role_not_configured():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID, members={member.id: member})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_config(veil_role_id=0)):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "configured" in sent.lower() or "setup" in sent.lower()


@pytest.mark.asyncio
async def test_optin_handles_role_deleted_from_guild():
    member = FakeMember(id=1001)
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_not_awaited()
    sent = interaction.followup.send.call_args.args[0]
    assert "no longer exists" in sent.lower() or "re-run" in sent.lower()


@pytest.mark.asyncio
async def test_optin_handles_forbidden():
    role = FakeRole(id=VEIL_ROLE_ID)
    member = FakeMember(id=1001, roles=[])
    member.add_roles = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "missing perms")
    )
    guild = FakeGuild(id=GUILD_ID, members={member.id: member}, roles={role.id: role})
    interaction = fake_interaction(user=member, guild=guild)
    cog = _make_cog()

    with patch("bot_modules.cogs.veil_cog._load_config", return_value=_config()):
        await _optin(cog, interaction)

    member.add_roles.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "permission" in sent.lower()
