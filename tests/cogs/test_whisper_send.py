"""Cog-level: /whisper send command."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from services.whisper_models import WhisperConfig
from tests.fakes import FakeMember, FakeRole, fake_interaction

ROLE = 7001
FEED = 8001
LOG = 8002
SENDER_ID = 1001
TARGET_ID = 2001


def _cfg() -> WhisperConfig:
    return WhisperConfig(guild_id=9001, role_id=ROLE, channel_id=FEED, log_channel_id=LOG)


def _make_cog():
    from cogs.whisper_cog import WhisperCog
    bot = MagicMock()
    bot.ctx.db_path = ":memory:"
    cog = WhisperCog.__new__(WhisperCog)
    cog.bot = bot
    cog.ctx = bot.ctx
    return cog


def _make_target_dmable():
    target = FakeMember(id=TARGET_ID, display_name="Target", roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(return_value=MagicMock(id=99999))  # type: ignore[attr-defined]
    return target


@pytest.mark.asyncio
async def test_send_happy_path():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, display_name="Sender", roles=[FakeRole(id=ROLE)])
    target = _make_target_dmable()

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock(return_value=MagicMock(id=88888))
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_whisper", return_value=42), \
         patch("cogs.whisper_cog._do_set_message_ids") as set_ids:
        await cog._send_impl(interaction, target=target, message="hello world")  # type: ignore[arg-type]

    target.send.assert_awaited_once()  # type: ignore[attr-defined]
    feed_channel.send.assert_awaited_once()
    log_channel.send.assert_awaited_once()
    set_ids.assert_called_once_with(":memory:", 42, channel_msg_id=88888, dm_msg_id=99999)
    interaction.response.send_message.assert_awaited()


@pytest.mark.asyncio
async def test_send_rejects_when_sender_lacks_role():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[])  # no role
    target = _make_target_dmable()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    assert "role" in args[0].lower()


@pytest.mark.asyncio
async def test_send_rejects_self_target():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        await cog._send_impl(interaction, target=sender, message="hi")  # type: ignore[arg-type]

    args, kwargs = interaction.response.send_message.call_args
    assert "yourself" in args[0].lower()


@pytest.mark.asyncio
async def test_target_autocomplete_only_returns_role_members():
    """Autocomplete restricts target choices to members holding the whisper role."""
    cog = _make_cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER_ID))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    role = MagicMock()
    role.members = [
        FakeMember(id=2001, display_name="Alice", name="alice"),
        FakeMember(id=2002, display_name="Bob", name="bob"),
    ]
    interaction.guild.get_role = MagicMock(return_value=role)
    with patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        results = await cog._target_autocomplete(interaction, "")
    assert {r.value for r in results} == {"2001", "2002"}


@pytest.mark.asyncio
async def test_target_autocomplete_filters_by_prefix():
    """Autocomplete filters by typed prefix against display_name and name."""
    cog = _make_cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER_ID))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    role = MagicMock()
    role.members = [
        FakeMember(id=2001, display_name="Alice", name="alice"),
        FakeMember(id=2002, display_name="Bob", name="bob"),
        FakeMember(id=2003, display_name="Charlie", name="charlie"),
    ]
    interaction.guild.get_role = MagicMock(return_value=role)
    with patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        results = await cog._target_autocomplete(interaction, "ali")
    assert {r.value for r in results} == {"2001"}


@pytest.mark.asyncio
async def test_target_autocomplete_excludes_self():
    """Autocomplete must not offer the calling user as a target."""
    cog = _make_cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER_ID))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    role = MagicMock()
    role.members = [
        FakeMember(id=SENDER_ID, display_name="Me", name="me"),
        FakeMember(id=2002, display_name="Bob", name="bob"),
    ]
    interaction.guild.get_role = MagicMock(return_value=role)
    with patch("cogs.whisper_cog._load_config", return_value=_cfg()):
        results = await cog._target_autocomplete(interaction, "")
    assert {r.value for r in results} == {"2002"}


@pytest.mark.asyncio
async def test_target_autocomplete_returns_empty_when_role_unset():
    """Autocomplete returns empty list when whisper role isn't configured."""
    cog = _make_cog()
    interaction = fake_interaction(user=FakeMember(id=SENDER_ID))
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    cfg_no_role = WhisperConfig(guild_id=9001, role_id=0, channel_id=FEED, log_channel_id=LOG)
    with patch("cogs.whisper_cog._load_config", return_value=cfg_no_role):
        results = await cog._target_autocomplete(interaction, "")
    assert results == []


@pytest.mark.asyncio
async def test_send_dm_forbidden_does_not_persist():
    cog = _make_cog()
    sender = FakeMember(id=SENDER_ID, roles=[FakeRole(id=ROLE)])
    target = FakeMember(id=TARGET_ID, roles=[FakeRole(id=ROLE)])
    target.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no dms"))  # type: ignore[attr-defined]

    feed_channel = MagicMock(spec=discord.TextChannel)
    feed_channel.send = AsyncMock()
    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.send = AsyncMock()

    interaction = fake_interaction(user=sender)
    interaction.guild = MagicMock()
    interaction.guild.id = 9001
    interaction.guild.name = "Test"
    interaction.guild.get_channel = MagicMock(side_effect=lambda cid: {FEED: feed_channel, LOG: log_channel}.get(cid))
    interaction.response.send_message = AsyncMock()

    with patch("cogs.whisper_cog._load_config", return_value=_cfg()), \
         patch("cogs.whisper_cog._do_insert_whisper", return_value=42), \
         patch("cogs.whisper_cog._do_delete_whisper") as mocked_delete:
        await cog._send_impl(interaction, target=target, message="hi")  # type: ignore[arg-type]
        mocked_delete.assert_called_once()

    feed_channel.send.assert_not_called()
    args, kwargs = interaction.response.send_message.call_args
    assert "DM" in args[0] or "deliver" in args[0].lower()
