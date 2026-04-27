"""Per-guild TTS playback coordinator.

Sits between the TTSCog (which produces MP3 files) and the wavelink player
(which is shared with the music cog). Pauses music when TTS arrives, queues
multiple TTS clips back-to-back, then resumes music from the saved position.

The ``is_interrupting`` flag is the gate music_cog reads to skip its own
track_start/track_end handlers while TTS is playing -- otherwise the music
queue would advance on the REPLACED event when player.play(tts) interrupts
the current music track.

Sequencing rules to avoid races:
  * Set per-guild state BEFORE calling player.play(tts) so the music
    track's REPLACED track_end sees is_interrupting=True.
  * On final TTS end with no more queued, drop the guild state BEFORE
    calling player.play(saved_track) so the music track's track_start
    fires with is_interrupting=False (music_cog handles it normally).
  * A per-guild asyncio.Lock serializes enqueue and on_track_end so a
    second /tts during the resume window can't snapshot a stale player.current.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import contextlib

import discord
import wavelink
from discord.ext import commands

from services.tts_service import TTSService

log = logging.getLogger("dungeonkeeper.tts")


@dataclass
class TTSGuildState:
    tts_queue: deque[Path] = field(default_factory=deque)
    interrupted_track: wavelink.Playable | None = None
    interrupted_position_ms: int | None = None
    current_tts_path: Path | None = None
    current_tts_identifier: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TTSPlaybackService:
    def __init__(self, tts_service: TTSService) -> None:
        self._tts = tts_service
        self._state: dict[int, TTSGuildState] = {}

    def _state_for(self, guild_id: int) -> TTSGuildState:
        st = self._state.get(guild_id)
        if st is None:
            st = TTSGuildState()
            self._state[guild_id] = st
        return st

    def is_interrupting(self, guild_id: int) -> bool:
        """True while a TTS clip is playing or about to play in this guild.

        Music_cog uses this to suppress its track_start/track_end handlers
        so the music queue isn't churned by TTS playback events.
        """
        return guild_id in self._state

    def queue_length(self, guild_id: int) -> int:
        st = self._state.get(guild_id)
        return len(st.tts_queue) if st else 0

    async def _resolve_local(self, mp3_path: Path) -> wavelink.Playable | None:
        """Load an absolute local mp3 path as a wavelink Playable.

        Lavalink's local source accepts plain absolute paths. If that fails
        (older Lavalink builds or non-default config), retry with the
        ``local:`` prefix.
        """
        abs_path = str(mp3_path.resolve())
        try:
            result = await wavelink.Playable.search(abs_path)
        except Exception as exc:
            log.warning("local search failed for %s: %s; retrying with local: prefix", abs_path, exc)
            result = None
        if not result:
            try:
                result = await wavelink.Playable.search(f"local:{abs_path}")
            except Exception as exc:
                log.exception("local: prefix search failed for %s: %s", abs_path, exc)
                return None
        if not result:
            return None
        if isinstance(result, wavelink.Playlist):
            return result.tracks[0] if result.tracks else None
        return result[0]

    async def enqueue(
        self, player: wavelink.Player, mp3_path: Path
    ) -> tuple[bool, int]:
        """Queue a TTS clip for playback.

        Returns ``(started_immediately, queue_position)``. ``queue_position``
        is the number of clips ahead of the new one (0 means it just started).
        """
        if player.guild is None:
            raise RuntimeError("player has no guild")
        guild_id = player.guild.id
        st = self._state_for(guild_id)

        async with st.lock:
            if st.current_tts_identifier is not None:
                st.tts_queue.append(mp3_path)
                position = len(st.tts_queue)
                log.info(
                    "tts queued for guild %s (position=%d)", guild_id, position
                )
                return False, position

            track = await self._resolve_local(mp3_path)
            if track is None:
                # Couldn't load the file -- drop state and surface error.
                self._state.pop(guild_id, None)
                self._tts.cleanup(mp3_path)
                raise RuntimeError("Lavalink could not load the TTS audio.")

            if player.current is not None:
                st.interrupted_track = player.current
                st.interrupted_position_ms = int(player.position or 0)
                log.info(
                    "tts interrupting music in guild %s at %dms",
                    guild_id,
                    st.interrupted_position_ms,
                )

            st.current_tts_path = mp3_path
            st.current_tts_identifier = track.identifier

            try:
                await player.play(track)
            except Exception:
                log.exception("tts initial play failed")
                self._state.pop(guild_id, None)
                self._tts.cleanup(mp3_path)
                raise

            return True, 0

    def is_tts_track_end(
        self, guild_id: int, ended_track: wavelink.Playable
    ) -> bool:
        """Whether this track_end belongs to the active TTS clip."""
        st = self._state.get(guild_id)
        if st is None or st.current_tts_identifier is None:
            return False
        return ended_track.identifier == st.current_tts_identifier

    async def on_tts_track_end(self, player: wavelink.Player) -> None:
        """Called by TTSCog after confirming the ended track was TTS.

        Cleans up the file, then either plays the next queued TTS clip
        or restores the interrupted music track.
        """
        if player.guild is None:
            return
        guild_id = player.guild.id
        st = self._state.get(guild_id)
        if st is None:
            return

        async with st.lock:
            finished = st.current_tts_path
            if finished is not None:
                self._tts.cleanup(finished)
            st.current_tts_path = None
            st.current_tts_identifier = None

            if st.tts_queue:
                next_path = st.tts_queue.popleft()
                track = await self._resolve_local(next_path)
                if track is None:
                    log.error("could not resolve queued tts file %s", next_path)
                    self._tts.cleanup(next_path)
                    # fall through and treat as queue-empty
                else:
                    st.current_tts_path = next_path
                    st.current_tts_identifier = track.identifier
                    try:
                        await player.play(track)
                    except Exception:
                        log.exception("tts queued play failed")
                        st.current_tts_path = None
                        st.current_tts_identifier = None
                        self._tts.cleanup(next_path)
                    else:
                        return

            saved_track = st.interrupted_track
            saved_position = st.interrupted_position_ms

            # Drop the guild state BEFORE resuming music so the music
            # track_start fires with is_interrupting=False.
            self._state.pop(guild_id, None)

            if saved_track is None:
                log.info("tts complete for guild %s; no music to resume", guild_id)
                return

            try:
                await player.play(
                    saved_track,
                    start=int(saved_position or 0),
                )
                log.info(
                    "music resumed in guild %s from %dms",
                    guild_id,
                    saved_position or 0,
                )
            except Exception as exc:
                log.exception("could not resume music after tts: %s", exc)
                await _notify_music_resume_failure(player)


async def _notify_music_resume_failure(player: wavelink.Player) -> None:
    """Best-effort post to the now-playing channel after a resume failure."""
    if player.guild is None:
        return
    bot = player.client
    if not isinstance(bot, commands.Bot):
        return
    music_cog = bot.get_cog("MusicCog")
    if music_cog is None:
        return
    queue = getattr(music_cog, "_queues", {}).get(player.guild.id)
    text_id = getattr(queue, "text_channel_id", None) if queue else None
    if text_id is None:
        return
    channel = player.guild.get_channel(text_id) or player.guild.get_thread(text_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return
    with contextlib.suppress(discord.HTTPException):
        await channel.send("Couldn't resume music after TTS. Use /play to continue.")
