"""Music cog - now-playing embed and persistent button view.

The view's custom_ids are stable strings so discord.py routes button presses
to the registered NowPlayingView class even after a bot restart. Callbacks
look up the cog via ``interaction.client.get_cog("MusicCog")`` rather than
holding a reference, since the view can outlive the cog instance after a
hot reload.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import discord

from services.music_queue import GuildQueue, LoopMode

if TYPE_CHECKING:
    from cogs.music_cog import MusicCog

log = logging.getLogger("dungeonkeeper.music.np")

EMBED_COLOR = 0xC9A961  # warm gold (Golden Meadow palette)

_LOOP_EMOJI = {
    LoopMode.OFF: "➡️",     # ➡️
    LoopMode.TRACK: "\U0001f502",      # 🔂
    LoopMode.QUEUE: "\U0001f501",      # 🔁
}

_LOOP_NEXT = {
    LoopMode.OFF: LoopMode.TRACK,
    LoopMode.TRACK: LoopMode.QUEUE,
    LoopMode.QUEUE: LoopMode.OFF,
}

_PAUSE_EMOJI = "⏸️"        # ⏸
_PLAY_EMOJI = "▶️"         # ▶
_SKIP_EMOJI = "⏭️"         # ⏭
_STOP_EMOJI = "⏹️"         # ⏹
_SHUFFLE_EMOJI = "\U0001f500"        # 🔀


def _format_duration(ms: int) -> str:
    if not ms or ms < 0:
        return "live"
    total = int(ms // 1000)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def build_embed(
    track: Any,
    queue: GuildQueue,
    requester: discord.abc.User | discord.Member | None,
    *,
    paused: bool = False,
) -> discord.Embed:
    title = getattr(track, "title", "Unknown title")
    author = getattr(track, "author", None) or getattr(track, "artist", "Unknown")
    uri = getattr(track, "uri", None)
    artwork = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
    length_ms = int(getattr(track, "length", 0) or 0)

    embed = discord.Embed(
        title=title if not uri else f"[{title}]({uri})",
        color=EMBED_COLOR,
    )
    embed.set_author(name=str(author))
    if artwork:
        embed.set_thumbnail(url=artwork)

    embed.add_field(
        name="Requested by",
        value=requester.mention if requester else "—",
        inline=True,
    )
    embed.add_field(name="Duration", value=_format_duration(length_ms), inline=True)
    embed.add_field(
        name="In queue",
        value=str(len(queue.tracks)),
        inline=True,
    )

    state_bits: list[str] = []
    if paused:
        state_bits.append("Paused")
    if queue.loop_mode != LoopMode.OFF:
        state_bits.append(f"Loop: {queue.loop_mode.value}")
    if state_bits:
        embed.set_footer(text=" · ".join(state_bits))

    return embed


class NowPlayingView(discord.ui.View):
    """Persistent control panel for the now-playing embed.

    Persistent (timeout=None) + stable custom_ids = survives bot restarts.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cog(interaction: discord.Interaction) -> "MusicCog | None":
        client: Any = interaction.client
        return client.get_cog("MusicCog")

    async def _check_same_voice(
        self, interaction: discord.Interaction
    ) -> bool:
        """Reject the press if user isn't in the bot's current voice channel."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Voice controls only work in a server.", ephemeral=True
            )
            return False
        bot_voice = guild.voice_client
        bot_channel = getattr(bot_voice, "channel", None)
        if bot_voice is None or not isinstance(
            bot_channel, (discord.VoiceChannel, discord.StageChannel)
        ):
            await interaction.response.send_message(
                "I'm not in a voice channel right now.", ephemeral=True
            )
            return False
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if (
            member is None
            or member.voice is None
            or member.voice.channel is None
            or member.voice.channel.id != bot_channel.id
        ):
            await interaction.response.send_message(
                f"You need to be in {bot_channel.mention} to use these controls.",
                ephemeral=True,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Buttons
    # ------------------------------------------------------------------

    @discord.ui.button(
        emoji=_PAUSE_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="music:np:pause",
    )
    async def pause_resume(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = self._cog(interaction)
        if cog is None or not await self._check_same_voice(interaction):
            if cog is None:
                await interaction.response.send_message(
                    "Music session ended. Use /play to start a new one.",
                    ephemeral=True,
                )
            return
        await cog.handle_view_pause_resume(interaction, self)

    @discord.ui.button(
        emoji=_SKIP_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="music:np:skip",
    )
    async def skip(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = self._cog(interaction)
        if cog is None or not await self._check_same_voice(interaction):
            if cog is None:
                await interaction.response.send_message(
                    "Music session ended.", ephemeral=True
                )
            return
        await cog.handle_view_skip(interaction)

    @discord.ui.button(
        emoji=_STOP_EMOJI,
        style=discord.ButtonStyle.danger,
        custom_id="music:np:stop",
    )
    async def stop_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = self._cog(interaction)
        if cog is None or not await self._check_same_voice(interaction):
            if cog is None:
                await interaction.response.send_message(
                    "Music session ended.", ephemeral=True
                )
            return
        await cog.handle_view_stop(interaction)

    @discord.ui.button(
        emoji=_SHUFFLE_EMOJI,
        style=discord.ButtonStyle.secondary,
        custom_id="music:np:shuffle",
    )
    async def shuffle(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = self._cog(interaction)
        if cog is None or not await self._check_same_voice(interaction):
            if cog is None:
                await interaction.response.send_message(
                    "Music session ended.", ephemeral=True
                )
            return
        await cog.handle_view_shuffle(interaction, self)

    @discord.ui.button(
        emoji=_LOOP_EMOJI[LoopMode.OFF],
        style=discord.ButtonStyle.secondary,
        custom_id="music:np:loop",
    )
    async def loop(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        cog = self._cog(interaction)
        if cog is None or not await self._check_same_voice(interaction):
            if cog is None:
                await interaction.response.send_message(
                    "Music session ended.", ephemeral=True
                )
            return
        await cog.handle_view_loop(interaction, self)

    # ------------------------------------------------------------------
    # State refresh helpers (called by cog after mutations)
    # ------------------------------------------------------------------

    def refresh_for(self, queue: GuildQueue, *, paused: bool) -> None:
        """Update button labels/emojis to reflect current state."""
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            cid = child.custom_id or ""
            if cid == "music:np:pause":
                child.emoji = _PLAY_EMOJI if paused else _PAUSE_EMOJI
            elif cid == "music:np:loop":
                child.emoji = _LOOP_EMOJI[queue.loop_mode]


def cycle_loop_mode(current: LoopMode) -> LoopMode:
    return _LOOP_NEXT[current]
