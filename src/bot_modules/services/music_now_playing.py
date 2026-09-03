"""Music cog - now-playing embed and persistent button view.

The view's custom_ids are stable strings so discord.py routes button presses
to the registered NowPlayingView class even after a bot restart. Callbacks
look up the cog via ``interaction.client.get_cog("MusicCog")`` rather than
holding a reference, since the view can outlive the cog instance after a
hot reload.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord

from bot_modules.services.music_queue import GuildQueue, LoopMode

if TYPE_CHECKING:
    from bot_modules.cogs.music_cog import MusicCog

log = logging.getLogger("dungeonkeeper.music.np")

EMBED_COLOR = 0xC9A961  # warm gold (brand palette)

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
    color: "discord.Color | None" = None,
) -> discord.Embed:
    if color is None:
        color = discord.Color(EMBED_COLOR)
    title = getattr(track, "title", "Unknown title")
    author = getattr(track, "author", None) or getattr(track, "artist", "Unknown")
    uri = getattr(track, "uri", None)
    artwork = getattr(track, "artwork", None) or getattr(track, "thumbnail", None)
    length_ms = int(getattr(track, "length", 0) or 0)

    # A masked link does not render in a title — the reader would see the
    # literal "[Song](https://…)". embed.url is the slot that makes a title
    # clickable. → embed_style_guide.md § Card anatomy
    embed = discord.Embed(title=title, url=uri or None, color=color)
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
        embed.set_footer(text=" • ".join(state_bits))

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
                f"❌ You need to be in {bot_channel.mention} to use these controls.",
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


# ----------------------------------------------------------------------
# The one card per guild
# ----------------------------------------------------------------------

#: Quiet period between two renders of one guild's card. Every track start
#: asks for a refresh, so a member leaning on Skip asks for several a second,
#: and message edits share the channel's rate-limit bucket. Coalescing is also
#: what the listeners want: the card should show the track that won the burst,
#: not flicker through the ones nobody heard.
CARD_REFRESH_INTERVAL = 2.0


async def render_card(
    channel: Any, queue: GuildQueue, *, embed: discord.Embed, view: discord.ui.View
) -> None:
    """Put the guild's one now-playing card in front of the listeners.

    Edits the existing card in place. Track change used to ``send`` a fresh
    card every time, so a ten-track queue left ten cards in the channel — nine
    of them naming a track that had already finished, and all nine still
    carrying working buttons, because the view is persistent and every card
    shares its custom_ids. From the channel that reads exactly as the report
    put it: the card you are watching never changes, another one just appears
    under it.

    Reposts only when there is no card, or when the one on record is gone
    because somebody deleted it. A transient edit failure keeps the ids, so
    the next track change aims at the same message instead of starting a pile.
    """
    message_id = queue.now_playing_message_id
    if message_id and queue.now_playing_channel_id == channel.id:
        try:
            await channel.get_partial_message(message_id).edit(embed=embed, view=view)
            return
        except discord.NotFound:
            queue.now_playing_message_id = None
            queue.now_playing_channel_id = None
        except discord.HTTPException:
            log.warning("now-playing edit failed in #%s", channel.id)
            return
    try:
        message = await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        log.warning("failed to post now-playing in #%s", channel.id)
        return
    queue.now_playing_message_id = message.id
    queue.now_playing_channel_id = channel.id


async def retire_card(
    guild: Any, channel_id: int | None, message_id: int | None
) -> None:
    """Delete one card, best-effort.

    Takes ids rather than the queue because both callers have to *capture* the
    old card before the new one is recorded. ``/nowplaying`` posts its
    replacement first and deletes second — the same post-before-delete ordering
    ``core.sticky`` uses — so clearing the queue's ids up front would leave a
    window where a track change, finding no card on record, posts a third one.
    """
    if not channel_id or not message_id:
        return
    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None or not hasattr(channel, "get_partial_message"):
        return
    with contextlib.suppress(discord.HTTPException):
        await channel.get_partial_message(message_id).delete()


class CardRefresher:
    """At most one card render per guild per interval; newest state wins.

    Trailing-edge coalescing rather than a plain cooldown: a refresh that
    arrives inside the quiet window is not dropped, it *replaces* whatever was
    queued and runs when the window closes. So a burst of skips costs one edit
    and that edit shows the track actually playing when the dust settles.
    """

    def __init__(self, interval: float = CARD_REFRESH_INTERVAL) -> None:
        self._interval = interval
        self._last: dict[int, float] = {}
        self._pending: dict[int, Callable[[], Awaitable[None]]] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._inflight: dict[int, asyncio.Future[None]] = {}

    async def submit(
        self, guild_id: int, render: Callable[[], Awaitable[None]]
    ) -> None:
        now = asyncio.get_running_loop().time()
        last = self._last.get(guild_id)
        if last is not None and now - last < self._interval:
            self._pending[guild_id] = render
            self._arm(guild_id, self._interval - (now - last))
            return
        self._last[guild_id] = now
        await self._render_once(guild_id, render)

    async def _render_once(
        self, guild_id: int, render: Callable[[], Awaitable[None]]
    ) -> None:
        """One render for one guild — serialized, and atomic to cancellation.

        **Serialized**, because ``submit`` renders inline while ``_flush``
        renders from a task. A slow inline render — a rate-limited edit during
        a burst of skips, which is exactly when two are in flight — could
        otherwise finish *after* the coalesced one and overwrite it, leaving
        the card naming the previous track until the next change.

        **Atomic**, because a cancel from ``drain`` landing inside
        ``channel.send`` would let the message reach Discord with its id never
        recorded: a card nobody can edit and nobody will delete. ``core.sticky``
        shields its placements for the same reason.

        The two don't compose perfectly: a cancelled render outlives the lock,
        since unwinding the ``async with`` releases it while the shielded task
        runs on. That is why ``drain`` waits for the in-flight render rather
        than only cancelling — every caller that cancels goes through it.
        """
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            task = asyncio.ensure_future(render())
            self._inflight[guild_id] = task
            try:
                await asyncio.shield(task)
            finally:
                if self._inflight.get(guild_id) is task and task.done():
                    self._inflight.pop(guild_id, None)

    def forget(self, guild_id: int) -> None:
        """Drop a guild's coalescing state and cancel its pending refresh.

        Does **not** wait for a render already in flight — see ``drain`` for
        the caller that has to.
        """
        self._pending.pop(guild_id, None)
        self._last.pop(guild_id, None)
        task = self._tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    async def drain(self, guild_id: int) -> None:
        """``forget``, then wait for any render already under way to land.

        Callers that go on to *read* the card's ids — retiring it at the end of
        a session, replacing it from ``/nowplaying`` — have to, or they read
        ids a shielded render is about to overwrite and strand the card it
        posted.
        """
        inflight = self._inflight.get(guild_id)
        self.forget(guild_id)
        if inflight is not None:
            # ``asyncio.wait``, not ``await`` under a suppress: suppressing
            # CancelledError around a bare await swallows *our own*
            # cancellation too, so a shutdown landing here would be ignored and
            # the caller would carry on past it. wait() reports the task's
            # outcome without raising it, and still propagates ours.
            await asyncio.wait([inflight])
            if self._inflight.get(guild_id) is inflight:
                self._inflight.pop(guild_id, None)

    async def cancel_all(self) -> None:
        """Stop everything — a cog unload or a shutdown.

        The in-flight renders are cancelled too, not just the coalescers.
        Leaving them to finish would post a card and record its id on a
        ``GuildQueue`` the reloaded cog no longer holds: an orphan with live
        buttons that nothing will ever edit or retire, which is the pile-up
        this module exists to prevent. ``asyncio.shield`` protects a render
        from its *waiter's* cancellation, not from being cancelled directly.
        """
        for task in self._tasks.values():
            task.cancel()
        inflight = [t for t in self._inflight.values() if not t.done()]
        for task in inflight:
            task.cancel()
        if inflight:
            await asyncio.wait(inflight)
        self._tasks.clear()
        self._pending.clear()
        self._last.clear()
        self._locks.clear()
        self._inflight.clear()

    def _arm(self, guild_id: int, delay: float) -> None:
        task = self._tasks.get(guild_id)
        if task is not None and not task.done():
            return
        self._tasks[guild_id] = asyncio.create_task(self._flush(guild_id, delay))

    async def _flush(self, guild_id: int, delay: float) -> None:
        try:
            while True:
                await asyncio.sleep(delay)
                render = self._pending.pop(guild_id, None)
                if render is None:
                    return
                self._last[guild_id] = asyncio.get_running_loop().time()
                try:
                    await self._render_once(guild_id, render)
                except Exception:
                    log.exception("now-playing refresh failed in guild %s", guild_id)
                delay = self._interval
        except asyncio.CancelledError:
            raise
        finally:
            # Only clear the slot if it is still *this* task's. A cancel from
            # ``forget``/``cancel_all`` unwinds asynchronously, so a new flush
            # can be armed for the same guild before this one finishes — and an
            # unconditional pop would evict the newcomer, leaving it running
            # while the next ``_arm`` sees an empty slot and starts a second
            # coalescer editing the same card.
            if self._tasks.get(guild_id) is asyncio.current_task():
                self._tasks.pop(guild_id, None)


def cycle_loop_mode(current: LoopMode) -> LoopMode:
    return _LOOP_NEXT[current]
