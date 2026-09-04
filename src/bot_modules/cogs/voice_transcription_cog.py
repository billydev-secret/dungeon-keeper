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
from discord import app_commands
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.services.voice_transcription_service import (
    DEFAULT_MODEL,
    VoiceTranscriptionConfig,
    fit_transcript,
    get_config,
    MAX_UPLOAD_PARTS,
    is_available,
    split_transcript,
    transcribe_file,
    was_truncated,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.voice_transcription")

# Discord IS_VOICE_MESSAGE flag (bit 13)
_VOICE_MSG_FLAG = 1 << 13


def _is_voice_message(message: discord.Message) -> bool:
    return bool(message.flags.value & _VOICE_MSG_FLAG) and bool(message.attachments)


def _audio_attachment(message: discord.Message) -> discord.Attachment | None:
    """The clip a manual transcribe should use, or None.

    Wider than :func:`_is_voice_message` on purpose. The automatic listener only
    ever wants true voice messages, but someone reaching for the context menu
    has picked a specific message and means it — an uploaded .m4a or .mp3 is a
    reasonable thing to ask about, and refusing it because the IS_VOICE_MESSAGE
    flag is absent would be pedantry.
    """
    for att in message.attachments:
        if (att.content_type or "").startswith("audio/"):
            return att
    if _is_voice_message(message):
        return message.attachments[0]
    return None


def _quest_channel_ids(message: discord.Message) -> tuple[int, ...]:
    """Where the note was posted, for a channel-scoped "post a voice message".

    The thread's parent rides along so a note dropped in a thread still counts
    for a quest scoped to the channel the thread hangs off — same rule the
    message/media triggers use in ``events_cog``.
    """
    parent_id = getattr(message.channel, "parent_id", None)
    return tuple(c for c in (message.channel.id, parent_id) if c is not None)


async def _transcribe_attachment(
    attachment: discord.Attachment, model_name: str
) -> str:
    """Download, transcribe, and leave nothing behind.

    The audio lands in a temp file only because faster-whisper reads from a
    path, and it is removed in a ``finally`` whether or not the transcribe
    succeeded. Nothing about the clip or its text is written to the database by
    this function or its callers — the transcript's only home is the Discord
    message that carries it.
    """
    data = await attachment.read()
    suffix = Path(attachment.filename).suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        tmp_path = Path(f.name)
    try:
        return await asyncio.to_thread(transcribe_file, tmp_path, model_name)
    finally:
        tmp_path.unlink(missing_ok=True)


def transcript_prefix(speaker: str) -> str:
    """What the standalone post opens with, and what its budget must pay for.

    It carries the speaker's name because it is no longer a reply: once the
    audio can be deleted, a reply would render as a dangling "original message
    was deleted" stub, and an unattributed line in a busy channel does not say
    whose note it was. The name is markdown-escaped -- a display name holding
    ``*`` or ``_`` would otherwise reformat the transcript after it.
    """
    return f"\U0001f4dd **{discord.utils.escape_markdown(speaker)}:** "


class VoiceTranscriptionCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        # A message context menu ("Apps → Transcribe Voice Note", or a long
        # press on mobile). Installed to *users* as well as guilds, which is
        # what lets it run in a personal DM: a bot cannot read or post in a DM
        # it is not part of, but a user-installed command travels with the
        # person who installed it and answers through the interaction.
        self.ctx_menu = app_commands.ContextMenu(
            name="Transcribe Voice Note",
            callback=self._transcribe_context_menu,
        )
        app_commands.allowed_installs(guilds=True, users=True)(self.ctx_menu)
        app_commands.allowed_contexts(
            guilds=True, dms=True, private_channels=True
        )(self.ctx_menu)
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def _transcribe_context_menu(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """Transcribe the picked message on demand, and store nothing.

        Deliberately *not* gated on the per-guild config: that dial governs
        which channels get transcribed automatically, and a DM has no guild to
        read a dial from. This is an explicit request by the person pressing it,
        so the only gates are "is there audio here" and "is the model loadable".

        The reply is public rather than ephemeral because the point is to leave
        the text behind in the conversation. Errors are ephemeral — a failure
        is for the presser, not the channel.
        """
        if not is_available():
            await interaction.response.send_message(
                "Transcription isn't available on this bot right now.",
                ephemeral=True,
            )
            return

        attachment = _audio_attachment(message)
        if attachment is None:
            await interaction.response.send_message(
                "That message has no voice note or audio file to transcribe.",
                ephemeral=True,
            )
            return

        # Loading a model and transcribing takes longer than the 3s Discord
        # allows before the interaction token dies.
        await interaction.response.defer(thinking=True)
        try:
            text = await _transcribe_attachment(attachment, self._menu_model(interaction))
        except Exception:
            log.warning("Context-menu transcription failed", exc_info=True)
            await interaction.followup.send(
                "Couldn't transcribe that one — the audio may be unreadable.",
                ephemeral=True,
            )
            return

        if not text:
            await interaction.followup.send(
                "That recording came back empty — no speech was detected.",
                ephemeral=True,
            )
            return

        # An explicit press asks for the whole note, so a long one spans as
        # many messages as it takes rather than being cut. Only the first
        # carries the speaker header; the rest read as one continued note.
        #
        # A real voice note is uncapped. An *uploaded* audio file is not: the
        # menu accepts any audio/* attachment on purpose, and an hour-long
        # podcast would flood the channel and outlive the interaction token, so
        # it stops at MAX_UPLOAD_PARTS with the truncation note.
        speaker = getattr(message.author, "display_name", None) or str(message.author)
        parts = split_transcript(
            text,
            prefix=transcript_prefix(speaker),
            max_parts=None if _is_voice_message(message) else MAX_UPLOAD_PARTS,
        )
        for part in parts:
            await interaction.followup.send(part)

    def _menu_model(self, interaction: discord.Interaction) -> str:
        """The guild's chosen model in a guild, the default in a DM.

        A DM has no guild row to read, and the menu must work there — that is
        the whole point of it — so it falls back rather than refusing.
        """
        guild = interaction.guild
        if guild is None:
            return DEFAULT_MODEL
        try:
            with open_db(self.bot.ctx.db_path) as conn:
                cfg = get_config(conn, guild.id)
        except Exception:
            return DEFAULT_MODEL
        return cfg.model_name if cfg else DEFAULT_MODEL

    def _read_config(self, guild_id: int) -> VoiceTranscriptionConfig | None:
        with open_db(self.bot.ctx.db_path) as conn:
            return get_config(conn, guild_id)

    @commands.Cog.listener("on_message")
    async def _on_message(self, message: discord.Message) -> None:
        if not message.guild:
            return
        if message.author.bot:
            return
        if not _is_voice_message(message):
            return

        # Quest hook: fires on the post itself, before the transcription
        # config gate — the quest is "post a voice message", not "have it
        # transcribed". Guarded, never raised into the listener.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            self.bot, message.guild.id, message.author.id, "voice_message",
            occurrence=str(message.id),
            channel_ids=_quest_channel_ids(message),
        )

        cfg = await asyncio.to_thread(self._read_config, message.guild.id)
        if cfg is None or not cfg.enabled:
            return
        # Empty allowlist = every channel; otherwise restrict to listed channels.
        if cfg.channel_ids and message.channel.id not in cfg.channel_ids:
            return

        try:
            async with message.channel.typing():
                text = await _transcribe_attachment(
                    message.attachments[0], cfg.model_name
                )
        except Exception:
            log.warning("Voice transcription failed", exc_info=True)
            return

        if not text:
            return

        # Unlike the on-demand press, an automatic post stays to one message:
        # nobody asked for it, so it should not be able to fill a channel. The
        # fit is what keeps it postable at all -- sending the raw text made
        # Discord reject any note over the 2000-character cap outright, so a
        # long note auto-transcribed to nothing at all.
        speaker = getattr(message.author, "display_name", None) or str(message.author)
        posted = fit_transcript(text, prefix=transcript_prefix(speaker))
        await message.channel.send(posted)

        # Only after the transcript is safely posted, and only on success: a
        # failed transcribe returns above, so the audio is never destroyed
        # without something to show for it.
        if not cfg.delete_after_transcribe:
            return

        # A truncated transcript is not something to show for all of it. The
        # clip is the only copy of what the cut removed, so a note too long for
        # one message keeps its audio rather than losing its tail for good.
        if was_truncated(posted):
            log.info(
                "Voice message in #%s kept: the transcript did not fit one "
                "message, and the clip is the only copy of the rest",
                getattr(message.channel, "name", message.channel.id),
            )
            return

        try:
            await message.delete()
        except discord.Forbidden:
            log.warning(
                "Cannot delete voice message in #%s — the bot needs Manage "
                "Messages there; transcript posted, audio left in place",
                getattr(message.channel, "name", message.channel.id),
            )
        except discord.NotFound:
            pass  # already gone; the transcript still stands
        except discord.HTTPException:
            log.warning("Deleting the voice message failed", exc_info=True)


async def setup(bot: Bot) -> None:
    if not is_available():
        log.warning(
            "faster-whisper not installed — VoiceTranscriptionCog skipped. "
            "Install it with: pip install faster-whisper"
        )
        return
    await bot.add_cog(VoiceTranscriptionCog(bot))
