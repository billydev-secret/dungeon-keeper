"""Music cog -- YouTube and Spotify playback via wavelink + Lavalink.

Spec: docs/music_spec.md (overrides documented in
~/.claude/plans/take-a-look-at-zippy-lemur.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal

import discord
import wavelink
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.music.embeds import build_queue_embed
from bot_modules.music.logic import (
    describe_track_failure,
    failure_reason,
    format_spotify_summary,
    is_search_url,
    paginate_queue,
    pick_substitute,
    should_advance_on_track_end,
    should_idle_disconnect,
    substitute_queries,
    substitution_note,
    track_summary_from_object,
)
from bot_modules.services.lavalink_manager import LavalinkManager
from bot_modules.services.music_now_playing import (
    CardRefresher,
    NowPlayingView,
    build_embed,
    cycle_loop_mode,
    render_card,
    retire_card,
)
from bot_modules.services.music_queue import GuildQueue, LoopMode
from bot_modules.services.spotify_resolver import (
    SpotifyResolveError,
    SpotifyResolver,
    SpotifyTrack,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.music")

_IDLE_DISCONNECT_S = 60
# Initial volume on every fresh voice connect. **Must stay 100.** Lavalink
# forwards the source's Opus frames untouched only while volume is exactly 100
# and no filter is active; any other value forces a decode → resample →
# re-encode, which costs a lossy generation and real CPU per stream. This was
# 20 ("friendly default nobody complains about") until 2026-08-16, when that
# re-encode turned out to be throwing away YouTube's ~149kbps Opus on a channel
# running at 384kbps. Members who want it quieter have Discord's own per-user
# volume slider; the bot paying for everyone's comfort in audio quality is the
# wrong trade.
_DEFAULT_VOLUME = 100


class MusicCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self._lavalink: LavalinkManager | None = None
        self._spotify: SpotifyResolver | None = None
        self._queues: dict[int, GuildQueue] = {}
        self._disabled = False
        self._starting = True
        self._startup_task: asyncio.Task[None] | None = None
        # alone-in-channel watchers: guild_id -> Task
        self._idle_tasks: dict[int, asyncio.Task[None]] = {}
        # guild_id -> identifier of the substitute currently covering a
        # blocked track; guards against a failed substitute re-substituting
        self._substituted: dict[int, str] = {}
        # Coalesces the card refreshes a burst of track changes asks for.
        self._card = CardRefresher()
        super().__init__()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        lavalink = LavalinkManager()
        self._lavalink = lavalink
        self._spotify = SpotifyResolver(db_path=self.bot.ctx.db_path)
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
        self._card.cancel_all()

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

    async def _end_session(self, guild: discord.Guild) -> None:
        """Drop everything keyed to a finished session, card included.

        The queue carries the card's ids, so a refresh coalesced behind the old
        session's quiet window would edit a card that belongs to nothing — and
        the card itself has to go, or it sits there naming a track that stopped
        playing, with buttons that still work and answer "I'm not in a voice
        channel right now". The next /play would post a second one beside it,
        which is how the channel filled up with cards before.
        """
        await self._card.drain(guild.id)
        queue = self._queues.pop(guild.id, None)
        if queue is not None:
            await retire_card(
                guild, queue.now_playing_channel_id, queue.now_playing_message_id
            )

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
            await self._ephemeral(interaction, "❌ Music is warming up, try again in a moment.")
            return None
        if self._disabled:
            await self._ephemeral(interaction, "❌ Music is currently unavailable.")
            return None
        guild = interaction.guild
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if guild is None or member is None:
            await self._ephemeral(interaction, "❌ Use this command in a server.")
            return None
        if member.voice is None or member.voice.channel is None:
            await self._ephemeral(interaction, "❌ Join a voice channel first.")
            return None

        existing = self._player(guild)
        if existing is not None and existing.channel is not None:
            if existing.channel.id != member.voice.channel.id:
                await self._ephemeral(
                    interaction,
                    f"❌ I'm currently in {existing.channel.mention}. "
                    "Join me there or wait for the queue to finish.",
                )
                return None
            return existing

        log.info("connecting to voice channel %s in guild %s", member.voice.channel.id, guild.id)
        try:
            player = await member.voice.channel.connect(cls=wavelink.Player)
        except (discord.ClientException, asyncio.TimeoutError) as exc:
            log.warning("voice connect failed: %s", exc)
            await self._ephemeral(interaction, f"❌ Couldn't join voice: {exc}")
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
            await self._ephemeral(interaction, "❌ Music is warming up, try again in a moment.")
            return
        if self._disabled:
            await self._ephemeral(interaction, "❌ Music is currently unavailable.")
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
            await interaction.followup.send(f"❌ Error: {exc}", ephemeral=True)
            return

        if tracks_added == 0:
            await interaction.followup.send(
                "❌ Nothing found for that query.", ephemeral=True
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
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
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
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
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
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
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
            await self._ephemeral(interaction, "❌ Use in a server.")
            return
        queue = self._queue(guild.id)
        total = len(queue.tracks)
        start, end, total_pages, normalized_page = paginate_queue(total, page)
        items = list(queue.tracks)[start:end]

        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="music")
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
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
            return
        _guild, player = sv
        await player.pause(True)
        await interaction.response.send_message("Paused.")

    @app_commands.command(name="resume", description="Resume playback.")
    async def resume_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
            return
        _guild, player = sv
        await player.pause(False)
        await interaction.response.send_message("Resumed.")

    @app_commands.command(name="stop", description="Clear the queue and stop.")
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
            return
        guild, player = sv
        queue = self._queue(guild.id)
        queue.clear()
        queue.current = None
        await player.stop()

        with contextlib.suppress(Exception):
            await player.disconnect()
        await self._end_session(guild)
        await interaction.response.send_message("Stopped and disconnected.")

    @app_commands.command(name="nowplaying", description="Repost the now-playing embed.")
    async def now_playing_cmd(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self._ephemeral(interaction, "❌ Use in a server.")
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
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="music")
        embed = build_embed(
            queue.current, queue, requester, paused=player.paused, color=accent
        )
        view = NowPlayingView()
        view.refresh_for(queue, paused=player.paused)
        # Drop any refresh still queued from a track change in the last couple
        # of seconds. It closed over the *old* channel, so once the ids below
        # move it would stop matching, decide there is no card there and post
        # one — the second card this command exists to get rid of.
        await self._card.drain(guild.id)
        # Post the replacement, record it, and only then delete the old one.
        # Clearing the ids first leaves a window across these two awaits where
        # a fresh track change finds no card on record and posts a third.
        old_card = (queue.now_playing_channel_id, queue.now_playing_message_id)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        queue.now_playing_message_id = msg.id
        queue.now_playing_channel_id = msg.channel.id
        await retire_card(guild, *old_card)

    @app_commands.command(name="disconnect", description="Force-disconnect from voice.")
    async def disconnect_cmd(self, interaction: discord.Interaction) -> None:
        sv = self._same_voice(interaction)
        if sv is None:
            await self._ephemeral(interaction, "❌ Join the bot's voice channel first.")
            return
        guild, player = sv
        queue = self._queue(guild.id)
        queue.clear()
        queue.current = None
        await player.stop()
        with contextlib.suppress(Exception):
            await player.disconnect()
        await self._end_session(guild)

        await interaction.response.send_message("Disconnected.")

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
        await self._refresh_now_playing(player, payload.track)

    @commands.Cog.listener()
    async def on_wavelink_track_end(
        self, payload: wavelink.TrackEndEventPayload
    ) -> None:
        player = payload.player
        if player is None or player.guild is None:
            return
        # 'replaced' (/skip, substitute), 'loadFailed' (exception handler
        # owns recovery), and 'stopped' all end tracks too -- advancing on
        # those double-advances and drops queued tracks.
        if not should_advance_on_track_end(payload.reason):
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
        exc = payload.exception
        # Full detail (multi-KB Java cause included) belongs in the log only;
        # the channel gets one plain line from describe_track_failure.
        log.warning(
            "track exception for %r: severity=%s message=%s cause=%s",
            getattr(payload.track, "title", None),
            exc.get("severity"),
            exc.get("message"),
            exc.get("cause"),
        )
        player = payload.player
        if player is not None and player.guild is not None:
            if await self._try_substitute(player, payload.track, exc):
                return
        await self._notify_text(
            player,
            describe_track_failure(
                getattr(payload.track, "title", None),
                message=exc.get("message"),
                cause=exc.get("cause"),
            ),
        )
        await self._advance_after_failure(player)

    async def _try_substitute(
        self,
        player: wavelink.Player,
        failed: wavelink.Playable,
        exc: Mapping[str, Any],
    ) -> bool:
        """Try to replace a failed track with another upload of it.

        One recovery per track: search YouTube for an alternate upload,
        then SoundCloud, guard each candidate via pick_substitute, and
        play the first survivor with a visible one-line note. Returns
        True when a substitute is now playing.
        """
        guild = player.guild
        assert guild is not None
        failed_key = str(
            getattr(failed, "identifier", None) or getattr(failed, "uri", "") or ""
        )
        prior = self._substituted.pop(guild.id, None)
        if prior is not None and prior == failed_key:
            # The substitute itself failed -- give up rather than loop.
            return False
        queries = substitute_queries(
            getattr(failed, "title", None), getattr(failed, "author", None)
        )
        if not queries:
            return False

        for source, query in (
            (source, query)
            for source in (wavelink.TrackSource.YouTube, wavelink.TrackSource.SoundCloud)
            for query in queries
        ):
            try:
                result = await wavelink.Playable.search(query, source=source)
            except Exception as search_exc:
                # SoundCloud disabled server-side lands here -- degrade to
                # the plain error message rather than blowing up recovery.
                log.warning(
                    "substitute search failed (%s) for %r: %s",
                    source,
                    query,
                    search_exc,
                )
                continue
            tracks = result.tracks if isinstance(result, wavelink.Playlist) else result
            pick = pick_substitute(
                tracks[:5],
                original_title=getattr(failed, "title", None),
                original_author=getattr(failed, "author", None),
                original_length_ms=int(getattr(failed, "length", 0) or 0),
                exclude_identifiers={failed_key},
            )
            if pick is None:
                continue

            queue = self._queue(guild.id)
            queue.adopt_requester(failed, pick)
            self._substituted[guild.id] = str(getattr(pick, "identifier", "") or "")
            await self._notify_text(
                player,
                substitution_note(
                    getattr(failed, "title", None),
                    getattr(pick, "title", None),
                    getattr(pick, "author", None),
                    source=getattr(pick, "source", None),
                    reason=failure_reason(
                        message=exc.get("message"), cause=exc.get("cause")
                    ),
                ),
            )
            try:
                await player.play(pick)
            except Exception:
                log.exception("failed to start substitute for %r", query)
                self._substituted.pop(guild.id, None)
                return False
            return True
        return False

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
        # Schedule 60s idle disconnect
        self._schedule_idle_disconnect(guild, _IDLE_DISCONNECT_S)

    def _card_channel(
        self, guild: discord.Guild, queue: GuildQueue
    ) -> discord.TextChannel | discord.Thread | None:
        """Where this guild's card lives, or where a first one would go.

        The card stays in the channel it was posted in for the life of the
        session, even after someone runs ``/play`` from somewhere else —
        moving it would strand the old one, buttons and all. ``/nowplaying``
        is how you move it: it retires the old card first.
        """
        for channel_id in (queue.now_playing_channel_id, queue.text_channel_id):
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                return channel
        return None

    async def _refresh_now_playing(
        self, player: wavelink.Player, track: wavelink.Playable
    ) -> None:
        """Bring the guild's one card up to date with what is playing now."""
        guild = player.guild
        if guild is None:
            return
        queue = self._queue(guild.id)
        channel = self._card_channel(guild, queue)
        if channel is None:
            return

        async def _render() -> None:
            requester_id = queue.requester_for(track)
            requester = guild.get_member(requester_id) if requester_id else None
            accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="music")
            embed = build_embed(
                track, queue, requester, paused=player.paused, color=accent
            )
            view = NowPlayingView()
            view.refresh_for(queue, paused=player.paused)
            await render_card(channel, queue, embed=embed, view=view)

        await self._card.submit(guild.id, _render)

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
        try:
            # Hard cap as a backstop: a message that trips the 2000-char limit
            # gets silently rejected by Discord, which is how track errors
            # used to vanish without a trace.
            await channel.send(message[:1990])
        except discord.HTTPException:
            log.warning("notify failed in #%s: %.120s", text_id, message)

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
        guild = member.guild
        me = self.bot.user
        if me is not None and member.id == me.id:
            # Dragged out, disconnected by a mod, or the voice connection
            # dropped. Nothing else notices: /stop and /disconnect end the
            # session themselves, and this listener used to ignore every bot.
            # The queue survived with the card's channel still on it, so the
            # next /play in a *different* channel kept editing the card in the
            # old one and the listeners who started the new session saw
            # nothing at all.
            if after.channel is None and before.channel is not None:
                await self._end_session(guild)
            return
        if member.bot:
            return
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
        player = self._player(guild)
        if player is not None and player.channel and player.channel.id == channel.id:
            with contextlib.suppress(Exception):
                await player.disconnect(force=True)
            await self._end_session(guild)

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
        queue = self._queue(guild.id)
        if not should_idle_disconnect(
            humans_present=any(not m.bot for m in player.channel.members),
            playing=player.playing,
            paused=player.paused,
            has_current=queue.current is not None,
        ):
            return
        log.info(
            "idle disconnect for guild=%s channel=%s", guild.id, player.channel.id
        )
        with contextlib.suppress(Exception):
            await player.disconnect()
        await self._end_session(guild)
        self._idle_tasks.pop(guild.id, None)

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
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="music")
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
        with contextlib.suppress(Exception):
            await player.disconnect()
        await self._end_session(guild)
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
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="music")
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
    await bot.add_cog(MusicCog(bot))
