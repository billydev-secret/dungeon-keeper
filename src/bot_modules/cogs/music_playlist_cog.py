"""Music playlist cog — a watched channel becomes a rolling Spotify playlist.

Thin by design: parsing/matching live in ``music_playlist_logic``, storage in
``music_playlist_store``, and the whole pipeline (settings gates, resolve,
dedupe, write, trim) in ``music_playlist_service``. This cog wires two
gateway events into the service, reacts on the source message (✅ added,
🔁 duplicate, ❓ queued for review), and hosts the one ephemeral member
panel. Every admin dial lives on the dashboard (music-playlist panel) —
no config commands here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Mapping

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.core.branding import DEFAULT_ACCENT_COLOR, safe_resolve_accent
from bot_modules.core.db_utils import open_db
from bot_modules.music_playlist import music_playlist_store as store
from bot_modules.music_playlist.embeds import (
    build_my_posts_embed,
    build_my_review_embed,
    build_window_embed,
)
from bot_modules.music_playlist.music_playlist_logic import extract_links
from bot_modules.music_playlist.music_playlist_service import (
    MusicPlaylistService,
    ProcessingSummary,
)
from bot_modules.services.spotify_resolver import SpotifyResolver

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.music_playlist")

# Source-message feedback — the only non-ephemeral surface this feature has.
REACTION_ADDED = "✅"
REACTION_DUPLICATE = "🔁"
REACTION_QUEUED = "❓"

_PANEL_TIMEOUT_S = 300
# How far back the dashboard's Re-scan sweep reads channel history. Enough to
# refill a 30-song window several times over; the ledger keeps it idempotent.
# Fallback only — the real depth is the ``rescan_depth`` dial, which bounds
# the recovery path for messages whose tracks never landed.
_RESCAN_HISTORY_LIMIT = 200


class MusicPlaylistPanelView(discord.ui.View):
    """The one ephemeral member panel: the window, your songs, your reviews.

    Ephemeral and short-lived, so no persistent ``custom_id`` dance — a timed
    view whose buttons re-render the same message.
    """

    def __init__(
        self,
        cog: "MusicPlaylistCog",
        *,
        guild_id: int,
        playlist_id: str,
        window_size: int,
        user_id: int,
        color: discord.Color,
    ) -> None:
        super().__init__(timeout=_PANEL_TIMEOUT_S)
        self.cog = cog
        self.guild_id = guild_id
        self.playlist_id = playlist_id
        self.window_size = window_size
        self.user_id = user_id
        self.color = color

    @discord.ui.button(label="Playlist", emoji="🎶", style=discord.ButtonStyle.secondary)
    async def show_window(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        rows = await asyncio.to_thread(
            self.cog.window_rows, self.guild_id, self.playlist_id
        )
        embed = build_window_embed(
            rows, window_size=self.window_size, color=self.color
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="My Songs", emoji="🎧", style=discord.ButtonStyle.secondary)
    async def show_my_songs(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        rows = await asyncio.to_thread(
            self.cog.window_rows, self.guild_id, self.playlist_id
        )
        mine = [r for r in rows if int(r["added_by"]) == self.user_id]
        embed = build_my_posts_embed(
            mine, window_size=self.window_size, color=self.color
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="In Review", emoji="❓", style=discord.ButtonStyle.secondary)
    async def show_my_reviews(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        items = await asyncio.to_thread(
            self.cog.pending_rows_for, self.guild_id, self.user_id
        )
        embed = build_my_review_embed(items, color=self.color)
        await interaction.response.edit_message(embed=embed, view=self)


class MusicPlaylistCog(commands.Cog):
    """Listener-first cog; configuration lives on the web dashboard."""

    def __init__(self, bot: "Bot", ctx: "AppContext") -> None:
        self.bot = bot
        self.ctx = ctx
        self.service = MusicPlaylistService(
            ctx.db_path, SpotifyResolver(db_path=ctx.db_path)
        )
        super().__init__()

    async def cog_load(self) -> None:
        self._retry_loop.start()

    async def cog_unload(self) -> None:
        self._retry_loop.cancel()

    # ------------------------------------------------------------------
    # Auto-retry sweep — re-fires retryable ledger rows (write_failed /
    # resolve_failed) so a transient Spotify outage heals itself instead of
    # waiting for someone to press Re-scan. Policy (backoff, attempt cap,
    # batch size) lives in the service; this is only the Discord edge.
    # ------------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def _retry_loop(self) -> None:
        try:
            guild_ids = await self.service.guilds_with_pending_retries()
            for guild_id in guild_ids:
                await self._retry_sweep_guild(guild_id)
        except Exception:
            log.exception("music playlist: retry sweep failed")

    @_retry_loop.before_loop
    async def _before_retry_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _retry_sweep_guild(self, guild_id: int) -> None:
        fetched: dict[int, discord.Message] = {}

        async def _fetch(channel_id: int, message_id: int) -> tuple[str, int] | None:
            channel = self.bot.get_channel(channel_id)
            fetch_message = getattr(channel, "fetch_message", None)
            if fetch_message is None:
                # Channel deleted (or not text-ish) — its messages are gone.
                return None
            try:
                message = await fetch_message(message_id)
            except discord.NotFound:
                return None
            # Other HTTPExceptions propagate: transient, retried next sweep.
            fetched[message_id] = message
            return (message.content or "", message.author.id)

        result = await self.service.retry_failed_messages(guild_id, _fetch)
        # The original failure swallowed the poster's feedback; deliver it now.
        for _channel_id, message_id, summary in result.results:
            message = fetched.get(message_id)
            if message is not None:
                await self._react(message, summary)
        if result.attempted:
            log.info(
                "music playlist retry sweep: guild %s attempted=%d "
                "recovered=%d still_failing=%d gone=%d",
                guild_id, result.attempted, result.recovered,
                result.still_failing, result.gone,
            )

    # ------------------------------------------------------------------
    # Store reads for the panel (sync, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def window_rows(
        self, guild_id: int, playlist_id: str
    ) -> list[Mapping[str, Any]]:
        with open_db(self.ctx.db_path) as conn:
            return store.live_window(conn, guild_id, playlist_id)

    def pending_rows_for(
        self, guild_id: int, user_id: int
    ) -> list[Mapping[str, Any]]:
        with open_db(self.ctx.db_path) as conn:
            return [
                r for r in store.list_pending(conn, guild_id)
                if int(r["added_by"]) == user_id
            ]

    # ------------------------------------------------------------------
    # Gateway events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        # Cheapest bail-out for the overwhelming majority of messages: no
        # music link, no settings read, no DB touch. The service re-parses —
        # this is purely the per-message cost gate (the ledger only ever
        # needs messages a re-scan would also act on).
        if not extract_links(message.content or ""):
            return
        try:
            summary = await self.service.process_message(
                message.guild.id,
                message.channel.id,
                message.id,
                message.content or "",
                message.author.id,
            )
        except Exception:
            log.exception("music playlist: processing failed for %s", message.id)
            return
        if summary.skipped:
            return
        await self._react(message, summary)
        for error in summary.errors:
            log.warning("music playlist: %s (message %s)", error, message.id)

    async def _react(
        self, message: discord.Message, summary: ProcessingSummary
    ) -> None:
        reactions = [
            emoji
            for emoji, applies in (
                (REACTION_ADDED, bool(summary.added_track_ids)),
                (REACTION_DUPLICATE, summary.duplicate_count > 0),
                (REACTION_QUEUED, bool(summary.unmatched_ids)),
            )
            if applies
        ]
        for emoji in reactions:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning(
                    "music playlist: couldn't react %s on %s", emoji, message.id
                )

    @commands.Cog.listener()
    async def on_raw_message_delete(
        self, payload: discord.RawMessageDeleteEvent
    ) -> None:
        # Raw, not the cached variant: old posts are almost never in the
        # cache, and deleting an old post is the common case. The service
        # gates on the remove_on_delete dial and is a no-op for messages
        # that never added a track.
        if payload.guild_id is None:
            return
        try:
            result = await self.service.handle_message_deleted(
                payload.guild_id, payload.message_id
            )
        except Exception:
            log.exception(
                "music playlist: delete handling failed for %s",
                payload.message_id,
            )
            return
        for error in result.errors:
            log.warning(
                "music playlist: %s (deleted message %s)",
                error, payload.message_id,
            )

    # ------------------------------------------------------------------
    # Dashboard maintenance hooks (routes/music_playlist.py delegates here
    # by name; the returned dict is rendered by the panel verbatim)
    # ------------------------------------------------------------------

    async def rescan_channel(self, guild_id: int) -> dict[str, Any]:
        """Re-run the pipeline over the watched channel's recent history.

        The processed-message ledger makes this idempotent — already-seen
        messages are per-message no-ops — so it exists to catch posts made
        while the bot was down. Oldest first, so the rolling window ends up
        holding the newest posts. No reactions: reacting to old messages
        during a sweep would be noise.
        """
        settings = await self.service.load_settings(guild_id)
        if not settings.channel_id or not settings.playlist_id:
            return {
                "ok": False,
                "error": "No watched channel or playlist configured.",
            }
        channel = self.bot.get_channel(settings.channel_id)
        # Duck-typed: only text-ish channels have history(); a category or a
        # stale id reports itself instead of raising.
        history = getattr(channel, "history", None)
        if history is None:
            return {
                "ok": False,
                "error": "Watched channel not found — check the channel dial.",
            }
        depth = settings.rescan_depth or _RESCAN_HISTORY_LIMIT
        messages = [m async for m in history(limit=depth)]
        scanned = added = duplicates = queued = errors = 0
        for message in reversed(messages):
            if message.author.bot or not extract_links(message.content or ""):
                continue
            scanned += 1
            try:
                summary = await self.service.process_message(
                    guild_id,
                    settings.channel_id,
                    message.id,
                    message.content or "",
                    message.author.id,
                )
            except Exception:
                log.exception(
                    "music playlist: rescan failed on message %s", message.id
                )
                errors += 1
                continue
            if summary.skipped:
                continue
            added += len(summary.added_track_ids)
            duplicates += summary.duplicate_count
            queued += len(summary.unmatched_ids)
        return {
            "ok": True,
            "scanned": scanned,
            "added": added,
            "duplicates": duplicates,
            "queued": queued,
            "errors": errors,
        }

    async def reconcile_playlist(
        self, guild_id: int, *, confirm_removals: bool = False
    ) -> dict[str, Any]:
        """Square the actual Spotify playlist with the DB's window.

        Bulk removals are withheld until confirmed — see the service.
        """
        return await self.service.reconcile(
            guild_id, confirm_removals=confirm_removals
        )

    # ------------------------------------------------------------------
    # The member panel
    # ------------------------------------------------------------------

    @app_commands.command(
        name="playlist",
        description="See the rolling playlist and your songs in it.",
    )
    async def playlist_panel(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "❌ Use this command in a server.", ephemeral=True
            )
            return
        settings = await self.service.load_settings(guild.id)
        if not settings.enabled or not settings.playlist_id:
            await interaction.response.send_message(
                "❌ The music playlist isn't set up on this server yet.",
                ephemeral=True,
            )
            return
        color = await safe_resolve_accent(self.ctx, guild, log_label="music playlist", default=DEFAULT_ACCENT_COLOR)
        rows = await asyncio.to_thread(
            self.window_rows, guild.id, settings.playlist_id
        )
        view = MusicPlaylistPanelView(
            self,
            guild_id=guild.id,
            playlist_id=settings.playlist_id,
            window_size=settings.window_size,
            user_id=interaction.user.id,
            color=color,
        )
        await interaction.response.send_message(
            embed=build_window_embed(
                rows, window_size=settings.window_size, color=color
            ),
            view=view,
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(MusicPlaylistCog(bot, bot.ctx))
