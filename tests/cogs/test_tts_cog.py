"""Callback tests for cogs.tts_cog (Tier 3)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Stub edge_tts so importing the cog doesn't require the package.
if "edge_tts" not in sys.modules:
    sys.modules["edge_tts"] = MagicMock()

import discord  # noqa: E402

from tests.fakes import fake_interaction  # noqa: E402


@pytest.fixture
def cog(tmp_path, monkeypatch):
    """Construct a TTSCog with the cache dir redirected at tmp_path.

    We don't run discord.ext.commands.Cog.__init__ side effects through
    the bot here; we just instantiate the cog and call its callbacks.
    """
    monkeypatch.setattr(
        "services.tts_service._CACHE_DIR", tmp_path / "tts_cache"
    )
    from cogs.tts_cog import TTSCog
    from services.tts_service import TTSService

    bot = MagicMock()
    ctx = MagicMock()
    instance = TTSCog.__new__(TTSCog)
    instance.bot = bot
    instance.ctx = ctx
    instance._service = TTSService(cache_dir=tmp_path / "tts_cache")
    from services.tts_playback import TTSPlaybackService

    instance._playback = TTSPlaybackService(instance._service)
    bot.tts_playback = instance._playback
    return instance


@pytest.mark.asyncio
async def test_text_over_limit_rejects(cog):
    interaction = fake_interaction()
    callback = cog.tts.callback  # underlying coroutine of the app_command
    await callback(cog, interaction, text="x" * 600)
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert "characters or fewer" in args[0]
    assert kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_invalid_voice_rejects(cog):
    interaction = fake_interaction()
    callback = cog.tts.callback
    await callback(cog, interaction, text="hello", voice="zz-XX-NotARealVoice")
    interaction.response.send_message.assert_awaited_once()
    args, _ = interaction.response.send_message.call_args
    assert "voice" in args[0].lower()


@pytest.mark.asyncio
async def test_user_not_in_vc_rejects(cog):
    """If the caller has no voice channel, _ensure_voice should refuse."""
    user = MagicMock(spec=discord.Member)
    user.voice = None
    guild = MagicMock(spec=discord.Guild)
    guild.id = 9001
    guild.voice_client = None

    interaction = fake_interaction(user=user, guild=guild)
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    callback = cog.tts.callback
    await callback(cog, interaction, text="hello")
    interaction.response.send_message.assert_awaited_once()
    args, _ = interaction.response.send_message.call_args
    assert "voice channel" in args[0].lower()


@pytest.mark.asyncio
async def test_bot_in_different_vc_rejects(cog, monkeypatch):
    """If the bot is in a different VC than the caller, refuse to move."""
    import wavelink

    user_channel = MagicMock()
    user_channel.id = 100
    user_channel.mention = "<#100>"

    bot_channel = MagicMock()
    bot_channel.id = 999
    bot_channel.mention = "<#999>"

    user = MagicMock(spec=discord.Member)
    user.voice = MagicMock()
    user.voice.channel = user_channel

    guild = MagicMock(spec=discord.Guild)
    guild.id = 9001
    fake_player = MagicMock(spec=wavelink.Player)
    fake_player.channel = bot_channel
    guild.voice_client = fake_player

    interaction = fake_interaction(user=user, guild=guild)
    interaction.response.send_message = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)

    callback = cog.tts.callback
    await callback(cog, interaction, text="hello")
    interaction.response.send_message.assert_awaited_once()
    args, _ = interaction.response.send_message.call_args
    assert "currently in" in args[0]


@pytest.mark.asyncio
async def test_happy_path_calls_generate_and_enqueue(cog, tmp_path, monkeypatch):
    """When validation passes, generate() runs and enqueue() is invoked."""
    import wavelink

    user_channel = MagicMock()
    user_channel.id = 100

    user = MagicMock(spec=discord.Member)
    user.voice = MagicMock()
    user.voice.channel = user_channel

    guild = MagicMock(spec=discord.Guild)
    guild.id = 9001
    fake_player = MagicMock(spec=wavelink.Player)
    fake_player.channel = user_channel  # bot already in user's VC
    guild.voice_client = fake_player

    interaction = fake_interaction(user=user, guild=guild)
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    fake_path = tmp_path / "tts_cache" / "result.mp3"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"\xFF\xFB" + b"\x00" * 256)

    cog._service.generate = AsyncMock(return_value=fake_path)
    cog._playback.enqueue = AsyncMock(return_value=(True, 0))

    callback = cog.tts.callback
    await callback(cog, interaction, text="hello world")

    cog._service.generate.assert_awaited_once()
    cog._playback.enqueue.assert_awaited_once_with(fake_player, fake_path)
    interaction.followup.send.assert_awaited_once()
