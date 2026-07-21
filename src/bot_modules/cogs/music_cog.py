"""Music cog -- YouTube and Spotify playback via wavelink + Lavalink.

Spec: docs/music_spec.md (overrides documented in
~/.claude/plans/take-a-look-at-zippy-lemur.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Literal

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.music.embeds import build_247_status_embed, build_queue_embed
from bot_modules.music.logic import (
    format_247_status_line,
    format_247_toggle_message,
    format_spotify_summary,
    is_search_url,
    paginate_queue,
    should_idle_disconnect,
    shuffled_autoplay_pool,
    track_summary_from_object,
)
from bot_modules.services.lavalink_manager import LavalinkManager
from bot_modules.services.music_now_playing import (
    NowPlayingView,
    build_embed,
    cycle_loop_mode,
)
from bot_modules.services.music_queue import GuildQueue, LoopMode
from bot_modules.services.music_settings import (
    ChannelSettings,
    clear_channel,
    get_channel_settings,
    list_all_always_on,
    list_always_on_channels,
    set_always_on,
    set_autoplay_playlist,
)
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolver,
    SpotifyTrack,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.music")

_IDLE_DISCONNECT_S = 60
_AUTOPLAY_QUEUE_BATCH = 50
_REJOIN_NODE_WAIT_S = 30
# Initial volume on every fresh voice connect. New users often don't know how
# to lower it; 20% is a friendly default that nobody complains about.
_DEFAULT_VOLUME = 20


class MusicCog(commands.Cog):
    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self._lavalink: LavalinkManager | None = None
        self._spotify: SpotifyResolver | None = None
        self._queues: dict[int, GuildQueue] = {}
        self._disabled = False
        self._starting = True
        self._startup_task: asyncio.Task[None] | None = None
        # alone-in-channel watchers: guild_id -> Task
        self._idle_tasks: dict[int, asyncio.Task[None]] = {}
        super().__init__()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        lavalink = LavalinkManager()
        self._lavalink = lavalink
        self._spotify = SpotifyResolver(db_path=self.ctx.db_path)
        self._startup_task = asyncio.create_task(self._start_lavalink(lavalink))

    async def _start_lavalink(self, lavalink: LavalinkManager) -> None:
        try:
            await lavalink.start()
            node = wavelink.Node(
                uri=f"http://{lavalink.host}:{lavalink.port}",
                password=lavalink.password,
            )
            await wavelink.Pool.connect(client=self.bot, nodes=[node])
        except Exception as exc:
            log.error("Lavalink failed to start -- music commands disabled (%s)", exc)
            self._disabled = True
            with contextlib.suppress(Exception):
                await lavalink.stop()
            return
        finally:
            self._starting = False

        self.bot.add_view(NowPlayingView())
        self.bot.startup_task_factories.append(
            lambda: self._rejoin_always_on_channels()
        )
        log.info("Music cog ready (Lavalink %s:%d)", lavalink.host, lavalink.port)

    async def cog_unload(self) -> None:
        log.info("Music cog unloading")
        if self._startup_task and not self._startup_task.done():
            self._startup_task.cancel()
            with contextlib.suppress(Exception):
                await self._startup_task
        for task in self._idle_tasks.values():
            task.cancel()
        self._idle_tasks.clear()

        for guild in list(self.bot.guilds):
            vc = guild.voice_client
            if vc is not None:
                with contextlib.suppress(Exception):
                    await vc.disconnect(force=True)

        with contextlib.suppress(Exception):
            await wavelink.Pool.close()

        if self._lavalink is not None:
            with contextlib.suppress(Exception):
                await self._lavalink.stop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _queue(self, guild_id: int) -> GuildQueue:
        q = self._queues.get(guild_id)
        if q is None:
            q = GuildQueue(guild_id=guild_id)
            self._queues[guild_id] = q
        return q

    def _player(self, guild: discord.Guild) -> wavelink.Player | None:
        vc = guild.voice_client
        return vc if isinstance(vc, wavelink.Player) else None

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
        """Ensure the bot is in the same voice channel as the user; return Player."""
        if self._starting:
            await self._ephemeral(interaction, "Music is warming up, try again in a moment.")
            return None
        if self._disabled:
            await self._ephemeral(interaction, "Music is currently unavailable.")
            return None
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await self._ephemeral(interaction, "Use this command in a server.")
            return None
        if member.voice is None or member.voice.channel is None:
            await self._ephemeral(interaction, "Join a voice channel first.")
            return None

        existing = self._player(guild)
        if existing is not None and existing.channel is not None:
            if existing.channel.id != member.voice.channel.id:
                await self._ephemeral(
                    interaction,
                    f"I'm currently in {existing.channel.mention}. "
                    "Join me there or wait for the queue to finish.",
                )
                return None
            return existing

        log.info("connecting to voice channel %s in guild %s", member.voice.channel.id, guild.id)
        try:
            player = await member.voice.channel.connect(cls=wavelink.Player)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            log.warning("voice connect failed: %s", exc)
            await self._ephemeral(interaction, f"Couldn't join voice: {exc}")
            return None
        await player.set_volume(_DEFAULT_VOLUME)
        q = self._queue(guild.id)
        q.voice_channel_id = member.voice.channel.id
        log.info(
            "voice connected: player.connected=%s player.channel=%s",
            getattr(player, "connected", "?"),
            getattr(player.channel, "id", "?"),
        )
        return player

    def _same_voice(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Guild, wavelink.Player] | None:
        """Verify caller and bot share a voice channel; return (guild, player)."""
        guild = interaction.guild
        if guild is None:
            return None
        player = self._player(guild)
        if player is None or player.channel is None:
            return None
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if (
            member is None
            or member.voice is None
            or member.voice.channel is None
            or member.voice.channel.id != player.channel.id
        ):
            return None
        return guild, player

    # ------------------------------------------------------------------
    # /play
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play a YouTube or Spotify URL or search.")
    @app_commands.describe(query="YouTube URL, Spotify URL/playlist, or search terms.")
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if self._starting:
            await self._ephemeral(interaction, "Music is warming up, try again in a moment.")
            return
        if self._disabled:
            await self._ephemeral(interaction, "Music is currently unavailable.")
            return
        player = await self._ensure_voice(interaction)
        if player is None:
            return
        guild = interaction.guild
        assert guild is not None
        await interaction.response.defer(thinking=True)

        queue = self._queue(guild.id)
        queue.text_channel_id = interaction.channel_id
        requester_id = interaction.user.id

        try:
            if self._spotify is not None and self._spotify.is_spotify_url(query):
                tracks_added, summary = await self._enqueue_spotify(
                    query, queue, requester_id
                )
            else:
                tracks_added, summary = await self._enqueue_search(
                    query, queue, requester_id
                )
        except Exception as exc:
            log.exception("play failed for query=%r", query)
            await interaction.followup.send(f"Error: {exc}", ephemeral=True)
            return

        if tracks_added == 0:
            await interaction.followup.send(
                "Nothing found for that query.", ephemeral=True
            )
            return

        log.info(
            "play: added=%d player.playing=%s player.connected=%s queue=%d",
            tracks_added,
            getattr(player, "playing", "?"),
            getattr(player, "connected", "?"),
            len(queue.tracks),
        )

        # Quest hook: one successful /play per guild-local day counts — the
        # day-keyed occurrence means a 30-track playlist and 30 separate
        # requests look the same, so queue spam never multi-pays. Guarded.
        from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

        await fire_member_trigger(
            self.bot, guild.id, requester_id, "music_request",
            daily_occurrence=True,
        )
        if not player.playing:
            await self._play_next(player, queue)
            await interaction.followup.send(summary)
        else:
            await interaction.followup.send(summary)

    async def _enqueue_spotify(
        self, url: str, queue: GuildQueue, requester_id: int
    ) -> tuple[int, str]:
        assert self._spotify is not None
        try:
            result = await self._spotify.resolve(url)
        except SpotifyResolveError as exc:
            return 0, f"Spotify error: {exc}"

        added = 0
        first_summary = ""
        for s_track in result.tracks:
            wt = await self._search_one(self._spotify.to_search_query(s_track))
            if wt is None:
                log.warning(
                    "no YouTube match for spotify track %s -- %s",
                    s_track.title,
                    s_track.spotify_url,
                )
                continue
            queue.add(wt, requester_id)
            added += 1
            if added == 1:
                first_summary = self._track_summary(wt, s_track)

        summary = format_spotify_summary(
            kind=result.kind,
            name=result.name,
            added=added,
            truncated=result.truncated,
            first_summary=first_summary,
            page_size=len(result.tracks),
        )
        return added, summary

    async def _enqueue_search(
        self, query: str, queue: GuildQueue, requester_id: int
    ) -> tuple[int, str]:
        # Pass URLs verbatim; for plain text, let wavelink add the source prefix
        # (defaults to ytmsearch:). Do NOT prepend ytsearch: ourselves -- the
        # doubled prefix returns garbage.
        is_url = is_search_url(query)
        result = await wavelink.Playable.search(query) if is_url else \
            await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTube)
        log.info("search %r -> %s", query, type(result).__name__)

        if not result:
            return 0, "No results."

        # Search results ALSO arrive as a Playlist (e.g. "Search results for X").
        # Take only the first hit for plain-text searches; queue every track
        # only when the user actually pasted a playlist/album URL.
        if isinstance(result, wavelink.Playlist):
            if not is_url:
                track = result.tracks[0]
                queue.add(track, requester_id)
                return 1, f"Queued: {self._track_summary(track)}"
            for t in result.tracks:
                queue.add(t, requester_id)
            return len(result.tracks), (
                f"Queued **{len(result.tracks)}** tracks from playlist "
                f"**{result.name}**."
            )

        track = result[0]
        queue.add(track, requester_id)
        return 1, f"Queued: {self._track_summary(track)}"

    async def _search_one(self, query: str) -> wavelink.Playable | None:
        try:
            result = await wavelink.Playable.search(query)
        except Exception as exc:
            log.warning("wavelink search failed for %r: %s", query, exc)
            return None
        if not result:
            return None
        if isinstance(result, wavelink.Playlist):
            return result.tracks[0] if result.tracks else None
        return result[0]

    @staticmethod
    def _track_summary(
        track: wavelink.Playable, spotify: SpotifyTrack | None = None
    ) -> str:
        fallback = spotify.primary_artist if spotify else None
        return track_summary_from_object(track, fallback_author=fallback)

    # ------------------------------------------------------------------
    # /skip /shuffle /loop /queue /pause /resume /stop /nowplaying /disconnect
    # ------------------------------------------------------------------

    @app_commands.command(name="skip", description="Skip the current track.")
    async def skip(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        guild, player = sv
        queue = self._queue(guild.id)
        queue.skip()
        if queue.current is None:
            await player.stop()
            await interaction.response.send_message("Skipped. Queue empty.")
            return
        await player.play(queue.current)
        await interaction.response.send_message("Skipped.")

    @app_commands.command(name="shuffle", description="Shuffle the queue (current track unaffected).")
    async def shuffle_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        guild, _player = sv
        queue = self._queue(guild.id)
        queue.shuffle()
        await interaction.response.send_message(
            f"Shuffled {len(queue.tracks)} tracks."
        )

    @app_commands.command(name="loop", description="Set loop mode.")
    @app_commands.describe(mode="off / track / queue")
    async def loop_cmd(
        self,
        interaction: discord.Interaction,
        mode: Literal["off", "track", "queue"],
    ) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        guild, _player = sv
        queue = self._queue(guild.id)
        queue.set_loop(LoopMode(mode))
        await interaction.response.send_message(f"Loop: {mode}.")

    @app_commands.command(name="queue", description="Show the current queue.")
    @app_commands.describe(page="Page number (10 tracks per page)")
    async def queue_cmd(
        self, interaction: discord.Interaction, page: int = 1
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self._ephemeral(interaction, "Use in a server.")
            return
        queue = self._queue(guild.id)
        total = len(queue.tracks)
        start, end, total_pages, normalized_page = paginate_queue(total, page)
        items = list(queue.tracks)[start:end]

        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_queue_embed(
            current_summary=(
                self._track_summary(queue.current)
                if queue.current is not None
                else None
            ),
            item_summaries=[self._track_summary(t) for t in items],
            start_index=start,
            total_in_queue=total,
            page=normalized_page,
            total_pages=total_pages,
            loop_mode_value=queue.loop_mode.value,
            color=accent,
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pause", description="Pause playback.")
    async def pause_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        _guild, player = sv
        await player.pause(True)
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume playback.")
    async def resume_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        _guild, player = sv
        await player.pause(False)
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="stop", description="Clear the queue and stop.")
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        guild, player = sv
        queue = self._queue(guild.id)
        queue.clear()
        queue.current = None
        await player.stop()

        is_247 = self._channel_is_247(guild.id, player.channel.id if player.channel else 0)
        if not is_247:
            with contextlib.suppress(Exception):
                await player.disconnect()
            self._queues.pop(guild.id, None)
            await interaction.response.send_message("Stopped and disconnected.")
        else:
            await interaction.response.send_message(
                "Stopped. Staying in channel (24/7 mode)."
            )

    @app_commands.command(name="nowplaying", description="Repost the now-playing embed.")
    async def now_playing_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._ephemeral(interaction, "Use in a server.")
            return
        queue = self._queue(guild.id)
        player = self._player(guild)
        if queue.current is None or player is None:
            await self._ephemeral(interaction, "Nothing playing right now.")
            return
        requester = (
            guild.get_member(queue.requester_for(queue.current) or 0)
            if queue.requester_for(queue.current)
            else None
        )
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_embed(
            queue.current, queue, requester, paused=player.paused, color=accent
        )
        view = NowPlayingView()
        view.refresh_for(queue, paused=player.paused)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        queue.now_playing_message_id = msg.id

    @app_commands.command(name="disconnect", description="Force-disconnect from voice.")
    async def disconnect_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "Join the bot's voice channel first.")
            return
        guild, player = sv
        ch_id = player.channel.id if player.channel else None
        queue = self._queue(guild.id)
        queue.clear()
        queue.current = None
        await player.stop()
        with contextlib.suppress(Exception):
            await player.disconnect()
        self._queues.pop(guild.id, None)

        # If 24/7 was on for this channel, disable it.
        if ch_id is not None:
            settings = self._get_settings(guild.id, ch_id)
            if settings and settings.always_on:
                _guild_id = guild.id
                _ch_id = ch_id
                _user_id = interaction.user.id

                def _do_disable_always_on():
                    with self.ctx.open_db() as conn:
                        set_always_on(conn, _guild_id, _ch_id, False, _user_id)

                await asyncio.to_thread(_do_disable_always_on)
                await interaction.response.send_message(
                    "Disconnected. 24/7 disabled for this channel."
                )
                return
        await interaction.response.send_message("Disconnected.")

    # ------------------------------------------------------------------
    # /247 /247_status (mod-only)
    # ------------------------------------------------------------------

    @app_commands.command(name="247", description="(Mod) Toggle 24/7 mode for your voice channel.")
    @app_commands.describe(
        enabled="Turn 24/7 on or off",
        autoplay_playlist="Optional Spotify playlist URL for autoplay when queue is idle",
    )
    @app_commands.default_permissions(manage_channels=True)
    async def cmd_247(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        autoplay_playlist: str | None = None,
    ) -> None:
        if not self.ctx.is_mod(interaction):
            await self._ephemeral(interaction, "You need mod permissions.")
            return
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await self._ephemeral(interaction, "Use in a server.")
            return
        if member.voice is None or member.voice.channel is None:
            await self._ephemeral(interaction, "Join the voice channel you want to configure first.")
            return

        ch_id = member.voice.channel.id
        if autoplay_playlist and self._spotify is not None and not self._spotify.is_spotify_url(autoplay_playlist):
            await self._ephemeral(interaction, "autoplay_playlist must be a Spotify URL.")
            return

        _guild_id = guild.id
        _user_id = interaction.user.id

        def _do_247_toggle():
            with self.ctx.open_db() as conn:
                _previous = list_always_on_channels(conn, _guild_id)
                _cleared = [s for s in _previous if s.voice_channel_id != ch_id and s.always_on]
                for s in _cleared:
                    set_always_on(conn, _guild_id, s.voice_channel_id, False, _user_id)
                set_always_on(conn, _guild_id, ch_id, enabled, _user_id)
                if autoplay_playlist:
                    set_autoplay_playlist(conn, _guild_id, ch_id, autoplay_playlist, _user_id)
            return _cleared

        cleared = await asyncio.to_thread(_do_247_toggle)
        cleared_mentions: list[str] = []
        join_error: str | None = None
        if enabled:
            for s in cleared:
                ch = guild.get_channel(s.voice_channel_id)
                cleared_mentions.append(
                    ch.mention if ch else f"<#{s.voice_channel_id}>"
                )
            # If we're not already in the channel, join it now.
            if guild.voice_client is None:
                try:
                    player = await member.voice.channel.connect(cls=wavelink.Player)
                    await player.set_volume(_DEFAULT_VOLUME)
                except Exception as exc:
                    log.warning("24/7 join failed: %s", exc)
                    join_error = str(exc)
        msg = format_247_toggle_message(
            enabled=enabled,
            channel_mention=member.voice.channel.mention,
            cleared_mentions=cleared_mentions,
            autoplay_saved=bool(autoplay_playlist),
            join_error=join_error,
        )
        await interaction.response.send_message(msg)

    @app_commands.command(name="247_status", description="Show 24/7-enabled channels in this server.")
    async def cmd_247_status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._ephemeral(interaction, "Use in a server.")
            return
        _guild_id = guild.id

        def _do_list_always_on():
            with self.ctx.open_db() as conn:
                return list_always_on_channels(conn, _guild_id)

        entries = await asyncio.to_thread(_do_list_always_on)
        if not entries:
            await interaction.response.send_message("No 24/7 channels configured.")
            return
        lines: list[str] = []
        for s in entries:
            ch = guild.get_channel(s.voice_channel_id)
            mention = ch.mention if ch else f"<#{s.voice_channel_id}>"
            lines.append(
                format_247_status_line(mention, bool(s.autoplay_playlist_url))
            )
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_247_status_embed(lines, color=accent)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # Wavelink event handlers
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_wavelink_track_start(
        self, payload: wavelink.TrackStartEventPayload
    ) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        queue = self._queue(player.guild.id)
        queue.current = payload.track
        await self._post_now_playing(player, payload.track)

    @commands.Cog.listener()
    async def on_wavelink_track_end(
        self, payload: wavelink.TrackEndEventPayload
    ) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        queue = self._queue(player.guild.id)
        next_track = queue.next()
        if next_track is None:
            await self._on_queue_empty(player, queue)
            return
        try:
            await player.play(next_track)
        except Exception:
            log.exception("track_end: failed to play next track")

    @commands.Cog.listener()
    async def on_wavelink_track_exception(
        self, payload: wavelink.TrackExceptionEventPayload
    ) -> None:
        log.warning("track exception: %s", payload.exception)
        await self._notify_text(
            payload.player, f"Track error: {payload.exception}. Skipping."
        )
        await self._advance_after_failure(payload.player)

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(
        self, payload: wavelink.TrackStuckEventPayload
    ) -> None:
        log.warning("track stuck: threshold=%s", payload.threshold)
        await self._notify_text(payload.player, "Track stuck. Skipping.")
        await self._advance_after_failure(payload.player)

    async def _advance_after_failure(self, player: wavelink.Player | None) -> None:
        if player is None or player.guild is None:
            return
        queue = self._queue(player.guild.id)
        next_track = queue.skip()
        if next_track is None:
            await self._on_queue_empty(player, queue)
            return
        try:
            await player.play(next_track)
        except Exception:
            log.exception("failed to advance after track failure")

    async def _on_queue_empty(
        self, player: wavelink.Player, queue: GuildQueue
    ) -> None:
        guild = player.guild
        if guild is None or player.channel is None:
            return
        channel_id = player.channel.id
        settings = self._get_settings(guild.id, channel_id)

        if settings and settings.always_on and settings.autoplay_playlist_url:
            try:
                added = await self._autoplay_refill(queue, settings.autoplay_playlist_url)
            except Exception:
                log.exception("autoplay refill failed")
                added = 0
            if added > 0:
                next_track = queue.next()
                if next_track is not None:
                    with contextlib.suppress(Exception):
                        await player.play(next_track)
                    return
            await self._notify_text(
                player,
                "Autoplay playlist couldn't be refreshed. Pausing autoplay; "
                "use /247 to update the playlist.",
            )

        if settings and settings.always_on:
            return  # 24/7 with no autoplay -- stay idle in voice

        # Schedule 60s idle disconnect
        self._schedule_idle_disconnect(guild, _IDLE_DISCONNECT_S)

    async def _autoplay_refill(
        self, queue: GuildQueue, playlist_url: str
    ) -> int:
        if self._spotify is None:
            return 0
        result = await self._spotify.resolve(playlist_url)
        # Shuffle the full candidate pool; cap on _added_ tracks (not
        # candidates), since some Spotify entries fail to mirror to YouTube
        # and we want the queue refill to still land near the batch target.
        candidates = shuffled_autoplay_pool(result.tracks)
        added = 0
        for s_track in candidates:
            if added >= _AUTOPLAY_QUEUE_BATCH:
                break
            wt = await self._search_one(self._spotify.to_search_query(s_track))
            if wt is None:
                continue
            queue.add(wt, requester_id=self.bot.user.id if self.bot.user else 0)
            added += 1
        return added

    async def _post_now_playing(
        self, player: wavelink.Player, track: wavelink.Playable
    ) -> None:
        guild = player.guild
        if guild is None:
            return
        queue = self._queue(guild.id)
        text_id = queue.text_channel_id
        if text_id is None:
            return
        channel = guild.get_channel(text_id) or guild.get_thread(text_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        requester_id = queue.requester_for(track)
        requester = guild.get_member(requester_id) if requester_id else None
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_embed(
            track, queue, requester, paused=player.paused, color=accent
        )
        view = NowPlayingView()
        view.refresh_for(queue, paused=player.paused)
        try:
            msg = await channel.send(embed=embed, view=view)
            queue.now_playing_message_id = msg.id
        except discord.HTTPException:
            log.warning("failed to post now-playing in #%s", text_id)

    async def _notify_text(
        self, player: wavelink.Player | None, message: str
    ) -> None:
        if player is None or player.guild is None:
            return
        queue = self._queue(player.guild.id)
        text_id = queue.text_channel_id
        if text_id is None:
            return
        channel = player.guild.get_channel(text_id) or player.guild.get_thread(text_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        with contextlib.suppress(discord.HTTPException):
            await channel.send(message)

    async def _play_next(self, player: wavelink.Player, queue: GuildQueue) -> None:
        track = queue.next()
        if track is None:
            log.info("_play_next: no track to play")
            return
        log.info(
            "_play_next: playing %r (uri=%s, length=%s) node=%s session_id=%s",
            getattr(track, "title", "?"),
            getattr(track, "uri", "?"),
            getattr(track, "length", "?"),
            getattr(getattr(player, "node", None), "identifier", "?"),
            getattr(getattr(player, "node", None), "session_id", "?"),
        )
        try:
            returned = await player.play(track)
            log.info(
                "_play_next: player.play returned %r playing=%s current=%s",
                returned,
                getattr(player, "playing", "?"),
                getattr(getattr(player, "current", None), "title", None),
            )
        except Exception:
            log.exception("_play_next failed")

    # ------------------------------------------------------------------
    # Voice state listener (alone-in-channel disconnect)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        guild = member.guild
        player = self._player(guild)
        if player is None or player.channel is None:
            return

        humans = [m for m in player.channel.members if not m.bot]
        if not humans:
            self._schedule_idle_disconnect(guild, _IDLE_DISCONNECT_S)
        else:
            # Cancel any pending disconnect
            task = self._idle_tasks.pop(guild.id, None)
            if task and not task.done():
                task.cancel()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        guild = channel.guild
        _guild_id = guild.id
        _channel_id = channel.id

        def _do_clear_channel():
            with self.ctx.open_db() as conn:
                _settings = get_channel_settings(conn, _guild_id, _channel_id)
                if _settings:
                    clear_channel(conn, _guild_id, _channel_id)
                    log.info(
                        "cleared music settings for deleted voice channel %s", _channel_id
                    )

        await asyncio.to_thread(_do_clear_channel)
        player = self._player(guild)
        if player is not None and player.channel and player.channel.id == channel.id:
            with contextlib.suppress(Exception):
                await player.disconnect(force=True)
            self._queues.pop(guild.id, None)

    def _schedule_idle_disconnect(self, guild: discord.Guild, after_s: int) -> None:
        existing = self._idle_tasks.pop(guild.id, None)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._idle_disconnect(guild, after_s))
        self._idle_tasks[guild.id] = task

    async def _idle_disconnect(self, guild: discord.Guild, after_s: int) -> None:
        try:
            await asyncio.sleep(after_s)
        except asyncio.CancelledError:
            return
        player = self._player(guild)
        if player is None or player.channel is None:
            return
        settings = self._get_settings(guild.id, player.channel.id)
        queue = self._queue(guild.id)
        if not should_idle_disconnect(
            humans_present=any(not m.bot for m in player.channel.members),
            playing=player.playing,
            paused=player.paused,
            has_current=queue.current is not None,
            always_on=bool(settings and settings.always_on),
        ):
            return
        log.info(
            "idle disconnect for guild=%s channel=%s", guild.id, player.channel.id
        )
        with contextlib.suppress(Exception):
            await player.disconnect()
        self._queues.pop(guild.id, None)
        self._idle_tasks.pop(guild.id, None)

    def _channel_is_247(self, guild_id: int, channel_id: int) -> bool:
        settings = self._get_settings(guild_id, channel_id)
        return bool(settings and settings.always_on)

    def _get_settings(self, guild_id: int, channel_id: int) -> ChannelSettings | None:
        with self.ctx.open_db() as conn:
            return get_channel_settings(conn, guild_id, channel_id)

    # ------------------------------------------------------------------
    # 24/7 rejoin task (background)
    # ------------------------------------------------------------------

    async def _rejoin_always_on_channels(self) -> None:
        await self.bot.wait_until_ready()

        # Wait for at least one wavelink node to be connected before issuing
        # voice.connect(cls=wavelink.Player), or wavelink will raise.
        deadline = time.monotonic() + _REJOIN_NODE_WAIT_S
        while time.monotonic() < deadline:
            try:
                if any(node.status == wavelink.NodeStatus.CONNECTED for node in wavelink.Pool.nodes.values()):
                    break
            except Exception:
                log.exception("wavelink node status check")
            await asyncio.sleep(1.0)
        else:
            log.warning("no wavelink node connected after %ss; aborting 24/7 rejoin", _REJOIN_NODE_WAIT_S)
            return

        def _do_list_all_always_on():
            with self.ctx.open_db() as conn:
                return list_all_always_on(conn)

        entries = await asyncio.to_thread(_do_list_all_always_on)
        for s in entries:
            try:
                guild = self.bot.get_guild(s.guild_id)
                if guild is None:
                    log.info("24/7 rejoin: guild %s not in cache; skipping", s.guild_id)
                    continue
                channel = guild.get_channel(s.voice_channel_id)
                if not isinstance(channel, discord.VoiceChannel):
                    log.warning(
                        "24/7 rejoin: channel %s in guild %s not a voice channel",
                        s.voice_channel_id,
                        s.guild_id,
                    )
                    continue
                me = guild.me
                if me is None:
                    continue
                perms = channel.permissions_for(me)
                if not (perms.connect and perms.speak):
                    log.warning(
                        "24/7 rejoin: missing Connect/Speak in guild=%s channel=%s",
                        s.guild_id,
                        s.voice_channel_id,
                    )
                    continue
                if guild.voice_client is None:
                    try:
                        player = await channel.connect(cls=wavelink.Player)
                    except Exception as exc:
                        log.warning("24/7 rejoin connect failed: %s", exc)
                        continue
                    await player.set_volume(_DEFAULT_VOLUME)
                    queue = self._queue(s.guild_id)
                    queue.voice_channel_id = s.voice_channel_id
                    queue.autoplay_playlist_url = s.autoplay_playlist_url
                    if s.autoplay_playlist_url:
                        try:
                            added = await self._autoplay_refill(queue, s.autoplay_playlist_url)
                        except Exception:
                            log.exception("autoplay refill on rejoin failed")
                            added = 0
                        if added > 0:
                            track = queue.next()
                            if track is not None:
                                with contextlib.suppress(Exception):
                                    await player.play(track)
            except Exception:
                log.exception("24/7 rejoin: error for entry %s", s)

    # ------------------------------------------------------------------
    # View callback handlers (called by NowPlayingView buttons)
    # ------------------------------------------------------------------

    async def handle_view_pause_resume(
        self, interaction: discord.Interaction, view: NowPlayingView
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        player = self._player(guild)
        if player is None:
            return
        new_paused = not player.paused
        await player.pause(new_paused)
        queue = self._queue(guild.id)
        view.refresh_for(queue, paused=new_paused)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_embed(
            queue.current,
            queue,
            guild.get_member(queue.requester_for(queue.current) or 0),
            paused=new_paused,
            color=accent,
        ) if queue.current else None
        if embed is not None:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.edit_message(view=view)

    async def handle_view_skip(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        player = self._player(guild)
        if player is None:
            return
        queue = self._queue(guild.id)
        next_track = queue.skip()
        if next_track is None:
            await player.stop()
            await interaction.response.send_message("Skipped. Queue empty.", ephemeral=True)
            return
        await player.play(next_track)
        await interaction.response.send_message("Skipped.", ephemeral=True)

    async def handle_view_stop(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        player = self._player(guild)
        if player is None:
            return
        queue = self._queue(guild.id)
        queue.clear()
        queue.current = None
        await player.stop()
        if player.channel and not self._channel_is_247(guild.id, player.channel.id):
            with contextlib.suppress(Exception):
                await player.disconnect()
            self._queues.pop(guild.id, None)
        await interaction.response.send_message("Stopped.", ephemeral=True)

    async def handle_view_shuffle(
        self, interaction: discord.Interaction, view: NowPlayingView
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        queue = self._queue(guild.id)
        queue.shuffle()
        player = self._player(guild)
        paused = bool(player and player.paused)
        view.refresh_for(queue, paused=paused)
        await interaction.response.send_message(
            f"Shuffled {len(queue.tracks)} tracks.", ephemeral=True
        )

    async def handle_view_loop(
        self, interaction: discord.Interaction, view: NowPlayingView
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        queue = self._queue(guild.id)
        queue.set_loop(cycle_loop_mode(queue.loop_mode))
        player = self._player(guild)
        paused = bool(player and player.paused)
        view.refresh_for(queue, paused=paused)
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        embed = build_embed(
            queue.current,
            queue,
            guild.get_member(queue.requester_for(queue.current) or 0),
            paused=paused,
            color=accent,
        ) if queue.current else None
        if embed is not None:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.edit_message(view=view)


async def setup(bot: "Bot") -> None:
    await bot.add_cog(MusicCog(bot, bot.ctx))
