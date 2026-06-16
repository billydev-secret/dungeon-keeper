"""Voice transcription cog — auto-transcribes Discord voice notes using local faster-whisper.

Configuration (enable, model, per-channel allowlist) lives in the web dashboard
under Config → Voice Transcription, not in slash commands.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.services.voice_transcription_service import (
    VoiceTranscriptionConfig,
    get_config,
    is_available,
    transcribe_file,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.voice_transcription")

# Discord IS_VOICE_MESSAGE flag (bit 13)
_VOICE_MSG_FLAG = 1 << 13


def _is_voice_message(message: discord.Message) -> bool:
    return bool(message.flags.value & _VOICE_MSG_FLAG) and bool(message.attachments)


class VoiceTranscriptionCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx

    def _read_config(self, guild_id: int) -> VoiceTranscriptionConfig | None:
        with open_db(self.ctx.db_path) as conn:
            return get_config(conn, guild_id)

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.author.bot:
            return
        if not _is_voice_message(message):
            return

        cfg = await asyncio.to_thread(self._read_config, message.guild.id)
        if cfg is None or not cfg.enabled:
            return
        # Empty allowlist = every channel; otherwise restrict to listed channels.
        if cfg.channel_ids and message.channel.id not in cfg.channel_ids:
            return

        attachment = message.attachments[0]
        suffix = Path(attachment.filename).suffix or ".ogg"

        try:
            async with message.channel.typing():
                data = await attachment.read()

                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                    f.write(data)
                    tmp_path = Path(f.name)

                try:
                    text = await asyncio.to_thread(
                        transcribe_file, tmp_path, cfg.model_name
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)
        except Exception:
            log.warning("Voice transcription failed", exc_info=True)
            return

        if not text:
            return

        await message.reply(f"📝 {text}", mention_author=False)


async def setup(bot: Bot) -> None:
    if not is_available():
        log.warning(
            "faster-whisper not installed — VoiceTranscriptionCog skipped. "
            "Install it with: pip install faster-whisper"
        )
        return
    await bot.add_cog(VoiceTranscriptionCog(bot, bot.ctx))
