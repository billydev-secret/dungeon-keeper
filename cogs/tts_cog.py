"""TTS cog -- /tts command and TTS-track end listener.

Generates speech via edge-tts and routes it through the existing wavelink
player. When music is playing, the playback service pauses the music,
speaks, then resumes from the saved position. Multiple /tts requests
queue back-to-back before music is restored.

Wires the shared ``TTSPlaybackService`` onto ``bot.tts_playback`` so the
music cog can read it via the gate that suppresses queue-advancement during
TTS playback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from services.tts_playback import TTSPlaybackService
from services.tts_service import (
    DEFAULT_VOICE,
    MAX_TEXT_LEN,
    VOICE_CHOICES,
    TTSGenerationError,
    TTSService,
)

if TYPE_CHECKING:
    from core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.tts")

_DEFAULT_VOLUME = 20  # match music_cog default for fresh connects


class TTSCog(commands.Cog):
    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self._service = TTSService()
        self._playback = TTSPlaybackService(self._service)
        # Expose the playback service so music_cog can gate on it.
        bot.tts_playback = self._playback  # type: ignore[attr-defined]
        super().__init__()

    async def cog_unload(self) -> None:
        if getattr(self.bot, "tts_playback", None) is self._playback:
            try:
                delattr(self.bot, "tts_playback")
            except AttributeError:
                pass

    async def _ephemeral(
        self, interaction: discord.Interaction, msg: str
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> wavelink.Player | None:
        """Connect to (or reuse) the wavelink player in the user's VC.

        Mirrors music_cog._ensure_voice: requires the caller to be in a VC
        and refuses to move the bot when it's already in a different one.
        """
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await self._ephemeral(interaction, "Use this command in a server.")
            return None
        if member.voice is None or member.voice.channel is None:
            await self._ephemeral(interaction, "Join a voice channel first.")
            return None

        existing = guild.voice_client
        if isinstance(existing, wavelink.Player) and existing.channel is not None:
            if existing.channel.id != member.voice.channel.id:
                await self._ephemeral(
                    interaction,
                    f"I'm currently in {existing.channel.mention}. "
                    "Join me there or wait for the queue to finish.",
                )
                return None
            return existing

        log.info(
            "tts: connecting to voice channel %s in guild %s",
            member.voice.channel.id, guild.id,
        )
        try:
            player = await member.voice.channel.connect(cls=wavelink.Player)
        except (discord.ClientException, Exception) as exc:
            log.warning("tts voice connect failed: %s", exc)
            await self._ephemeral(interaction, f"Couldn't join voice: {exc}")
            return None
        try:
            await player.set_volume(_DEFAULT_VOLUME)
        except Exception:
            pass
        return player

    async def _voice_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        cur = current.lower()
        return [
            app_commands.Choice(name=v.label, value=v.value)
            for v in VOICE_CHOICES
            if cur in v.value.lower() or cur in v.label.lower()
        ][:25]

    @app_commands.command(
        name="tts",
        description="Speak text in your voice channel using TTS.",
    )
    @app_commands.describe(
        text="What to say (max 500 characters).",
        voice="Voice to use (default: Aria, US female).",
    )
    @app_commands.autocomplete(voice=_voice_autocomplete)
    async def tts(
        self,
        interaction: discord.Interaction,
        text: str,
        voice: str = DEFAULT_VOICE,
    ) -> None:
        if len(text) > MAX_TEXT_LEN:
            await self._ephemeral(
                interaction,
                f"Text must be {MAX_TEXT_LEN} characters or fewer (got {len(text)}).",
            )
            return
        if not TTSService.is_valid_voice(voice):
            await self._ephemeral(
                interaction,
                f"Unknown voice {voice!r}. Pick from the autocomplete list.",
            )
            return

        player = await self._ensure_voice(interaction)
        if player is None:
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            mp3_path = await self._service.generate(text, voice)
        except TTSGenerationError as exc:
            await interaction.followup.send(f"TTS failed: {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("unexpected tts generation error")
            await interaction.followup.send(
                f"TTS failed unexpectedly: {exc}", ephemeral=True
            )
            return

        try:
            started, position = await self._playback.enqueue(player, mp3_path)
        except RuntimeError as exc:
            await interaction.followup.send(
                f"Couldn't queue TTS: {exc}", ephemeral=True
            )
            return
        except Exception as exc:
            log.exception("unexpected tts enqueue error")
            await interaction.followup.send(
                f"TTS playback failed: {exc}", ephemeral=True
            )
            return

        if started:
            msg = (
                "Speaking now. Music (if any) will resume when TTS finishes; "
                "it may rewind a few seconds due to position-update granularity."
            )
        else:
            msg = f"Queued -- position #{position} in TTS queue."
        await interaction.followup.send(msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_wavelink_track_end(
        self, payload: wavelink.TrackEndEventPayload
    ) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        if not self._playback.is_tts_track_end(player.guild.id, payload.track):
            return
        try:
            await self._playback.on_tts_track_end(player)
        except Exception:
            log.exception("on_tts_track_end failed")


async def setup(bot: "Bot") -> None:
    await bot.add_cog(TTSCog(bot, bot.ctx))
