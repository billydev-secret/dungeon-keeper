"""Musical Chairs cog — reflex elimination for 3..N players."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import json
import logging
import random
import time

import discord
from discord import app_commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.duels.base_game import BaseGame
from bot_modules.games.command_groups import games
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED, COLOR_YELLOW

from . import db as mcdb
from .game import MusicalChairsGame, chairs_for, resolve_round
from .views import SitView

log = logging.getLogger("dungeonkeeper.musical_chairs")


class MusicalChairsCog(BaseGame, name="MusicalChairsCog"):

    GAME_KEY = "musical_chairs"
    GAME_DISPLAY_NAME = "Musical Chairs"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}
        # game_id -> resolved guild accent, so a game's round-active embeds share
        # one branding lookup across every MUSIC↔SCRAMBLE message edit.
        self._accents: dict[int, discord.Color] = {}

    async def _resolve_accent(
        self, game_id: int, guild: discord.Guild | None
    ) -> discord.Color:
        """Return the guild accent for this game's round-active embeds, resolved
        once and cached for reuse across the game's message edits. Falls back to
        the old COLOR_YELLOW on any failure (no guild / no app context / branding
        error) so a branding hiccup never crashes a live game."""
        cached = self._accents.get(game_id)
        if cached is not None:
            return cached
        ctx = getattr(self.bot, "ctx", None)
        if guild is None or ctx is None:
            return discord.Color(COLOR_YELLOW)
        try:
            color = await resolve_accent_color(ctx.db_path, guild)
        except Exception:
            log.debug("accent resolution failed for game %s", game_id, exc_info=True)
            return discord.Color(COLOR_YELLOW)
        self._accents[game_id] = color
        return color

    musicalchairs = app_commands.Group(
        name="musicalchairs",
        description="Musical Chairs — grab a seat when the music stops!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_get_game(self, game_id: int) -> MusicalChairsGame | None:
        return await mcdb.get_game(self.db, game_id)

    async def _db_write_state(self, game_id: int, state: str, **kw) -> None:
        await mcdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None
    ) -> int:
        return await mcdb.create_lobby(self.db, guild_id, channel_id, host_id, stakes_text)

    async def _db_fetch_active_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_active_games(self.db)

    async def _db_fetch_lobby_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_lobby_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[MusicalChairsGame]:
        return await mcdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[MusicalChairsGame]:
        return await mcdb.fetch_sweepable_games(self.db, now)

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int, float]:
        cfg = await mcdb.get_config(self.db, guild_id)
        return int(cfg["min_players"]), int(cfg["max_players"]), float(cfg["lobby_timeout"])

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_music_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._start_scramble(game_id)

    async def _run_scramble_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        resolved = False
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "SCRAMBLE":
                return
            resolved = await self._close_round_locked(game)
        if resolved:
            self._game_locks.pop(game_id, None)

    async def _start_scramble(self, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "MUSIC":
                return
            now = time.time()
            cfg = await mcdb.get_config(self.db, game.guild_id)
            await self._db_set_state(
                game_id, "ACTIVE",
                phase="SCRAMBLE",
                seated="[]",
                chairs=chairs_for(len(game.alive)),
                phase_started_at=now,
                phase_duration=float(cfg["scramble_window"]),
                last_action_at=now,
            )
            game2 = await mcdb.get_game(self.db, game_id)
            guild = self.bot.get_guild(game.guild_id)
            if guild and game2 and game2.message_id:
                await self._edit_message_silent(
                    game2.channel_id, game2.message_id,
                    self.render_game_state(game2, guild),
                    self.build_game_view(game_id),
                )
            task = asyncio.create_task(
                self._run_scramble_timer(game_id, float(cfg["scramble_window"]))
            )
            self._timers[game_id] = task

    async def _close_round_locked(self, game: MusicalChairsGame) -> bool:
        """Resolve a SCRAMBLE: seat the fastest, eliminate the rest. Returns True if the
        game ended. Caller holds the lock; any pending scramble timer is cancelled/done."""
        now = time.time()
        chairs = chairs_for(len(game.alive))
        survivors, eliminated = resolve_round(game.alive, game.seated, chairs)
        new_elim = list(game.elimination_order) + eliminated

        guild = self.bot.get_guild(game.guild_id)
        channel = self.bot.get_channel(game.channel_id)

        if len(survivors) <= 1:
            winner = survivors[0] if survivors else (eliminated[-1] if eliminated else None)
            loser = new_elim[-1] if new_elim else None
            game.alive = survivors
            game.elimination_order = new_elim
            await self._db_set_state(
                game.id, "ACTIVE",
                alive=json.dumps(survivors),
                elimination_order=json.dumps(new_elim),
                seated="[]",
                last_action_at=now,
            )
            if winner is not None and loser is not None:
                await self._post_group_result(game, winner, loser)
            return True

        cfg = await mcdb.get_config(self.db, game.guild_id)
        music = random.uniform(cfg["min_music"], cfg["max_music"])
        new_chairs = chairs_for(len(survivors))
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="MUSIC",
            round=game.round + 1,
            chairs=new_chairs,
            alive=json.dumps(survivors),
            elimination_order=json.dumps(new_elim),
            seated="[]",
            phase_started_at=now,
            phase_duration=music,
            last_action_at=now,
        )
        game.alive = survivors
        game.elimination_order = new_elim
        game.phase = "MUSIC"
        game.chairs = new_chairs

        if guild and channel and eliminated:
            out = ", ".join(self._name(guild, u) for u in eliminated)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"❌ **{out}** didn't find a chair. {len(survivors)} left. "
                    f"🎵 the music starts again…"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        game2 = await mcdb.get_game(self.db, game.id)
        if guild and game2 and game2.message_id:
            await self._resolve_accent(game.id, guild)
            await self._edit_message_silent(
                game2.channel_id, game2.message_id,
                self.render_game_state(game2, guild),
                self.build_game_view(game.id),
            )
        task = asyncio.create_task(self._run_music_timer(game.id, music))
        self._timers[game.id] = task
        return False

    # ── Button handler ────────────────────────────────────────────────────────

    async def _on_sit(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer()
        resolved = False
        async with self._get_lock(game_id):
            game = await mcdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE":
                await interaction.followup.send(
                    "This game is no longer active.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid not in game.alive:
                await interaction.followup.send(
                    "You're not in this game (or you're already out).", ephemeral=True
                )
                return

            if game.phase == "MUSIC":
                cfg = await mcdb.get_config(self.db, game.guild_id)
                if cfg["false_start_elim"]:
                    await interaction.followup.send(
                        "🚫 You sat too early — you're out this round!", ephemeral=True
                    )
                    await self._group_eliminate(game, uid, interaction=interaction)
                else:
                    await interaction.followup.send(
                        "Not yet — wait for **SIT!**", ephemeral=True
                    )
                return

            if game.phase == "SCRAMBLE":
                if uid in game.seated:
                    await interaction.followup.send(
                        "You already grabbed a chair!", ephemeral=True
                    )
                    return
                new_seated = list(game.seated) + [uid]
                await self._db_set_state(
                    game_id, "ACTIVE",
                    seated=json.dumps(new_seated), last_action_at=time.time(),
                )
                game.seated = new_seated
                chairs = chairs_for(len(game.alive))
                if len(new_seated) >= chairs:
                    self._cancel_timer(game_id)
                    resolved = await self._close_round_locked(game)
                else:
                    guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                    await interaction.edit_original_response(
                        embed=self.render_game_state(game, guild)
                    )
                return
        if resolved:
            self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: MusicalChairsGame) -> None:
        cfg = await mcdb.get_config(self.db, game.guild_id)
        await self._resolve_accent(game.id, self.bot.get_guild(game.guild_id))
        music = random.uniform(cfg["min_music"], cfg["max_music"])
        now = time.time()
        await self._db_set_state(
            game.id, "ACTIVE",
            phase="MUSIC",
            round=1,
            chairs=chairs_for(len(game.alive)),
            seated="[]",
            phase_started_at=now,
            phase_duration=music,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_music_timer(game.id, music))
        self._timers[game.id] = task

    async def on_game_resume(self, game: MusicalChairsGame) -> None:
        await self._resolve_accent(game.id, self.bot.get_guild(game.guild_id))
        if not game.phase or game.phase_started_at is None or game.phase_duration is None:
            asyncio.create_task(self._start_scramble(game.id))
            return
        now = time.time()
        remaining = (game.phase_started_at + game.phase_duration) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            mentions = " ".join(f"<@{u}>" for u in game.alive)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Musical Chairs resuming. {mentions}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if game.phase == "MUSIC":
            delay = max(0.0, remaining)
            task = asyncio.create_task(self._run_music_timer(game.id, delay))
            self._timers[game.id] = task
        else:  # SCRAMBLE
            delay = max(0.0, remaining)
            task = asyncio.create_task(self._run_scramble_timer(game.id, delay))
            self._timers[game.id] = task

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)
        self._accents.pop(game_id, None)

    def _name(self, guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        return m.display_name if m else str(uid)

    def render_game_state(
        self, game: MusicalChairsGame, guild: discord.Guild
    ) -> discord.Embed:
        alive_names = ", ".join(self._name(guild, u) for u in game.alive) or "—"
        chairs = game.chairs if game.chairs is not None else chairs_for(len(game.alive))

        if game.phase == "SCRAMBLE":
            embed = discord.Embed(
                title="🪑 SIT!!! — grab a chair!",
                description="The music stopped — **press SIT now!**",
                color=COLOR_RED,
            )
            embed.add_field(name="Chairs left", value=str(chairs), inline=True)
            embed.add_field(name="Still in", value=alive_names, inline=False)
        else:  # MUSIC
            # Round-active embeds follow the guild accent (resolved once per game
            # into ``_accents``); fall back to the old COLOR_YELLOW if unresolved.
            embed = discord.Embed(
                title=f"🎵 MUSICAL CHAIRS — Round {game.round}",
                description="🎶 …the music is playing… **don't sit yet** (sit early and you're out).",
                color=self._accents.get(game.id, COLOR_YELLOW),
            )
            embed.add_field(name="🪑 Chairs", value=str(chairs), inline=True)
            embed.add_field(name="👥 Still in", value=alive_names, inline=False)

        stakes = game.stakes_text or "Last seated wins; the runner-up surrenders their nickname for 24h."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        return embed

    def render_result_state(
        self,
        game: MusicalChairsGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        winner_name = self._name(guild, game.winner_id) if game.winner_id else "?"
        loser_name = self._name(guild, game.loser_id) if game.loser_id else "?"

        embed = discord.Embed(
            title="🪑 Musical Chairs — Game Over",
            description=f"**{winner_name}** takes the last chair!",
            color=COLOR_GREEN,
        )
        embed.add_field(name="🏆 Winner", value=winner_name, inline=True)
        embed.add_field(name="🥈 Runner-up", value=loser_name, inline=True)
        embed.add_field(name="👥 Players", value=str(len(game.roster)), inline=True)

        stakes = game.stakes_text or "24-hour nickname surrender."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)

        if imposed_nick:
            embed.add_field(
                name="🏷️ Nickname Applied",
                value=f"**{original_name or loser_name}** is now known as **{imposed_nick}** for 24 hours.",
                inline=False,
            )
        elif game.stakes_text is None:
            embed.add_field(
                name="⏳ Awaiting Nickname",
                value=(
                    f"**{winner_name}**, press **Name the loser** within 5 minutes. "
                    "The nickname lasts 24 hours."
                ),
                inline=False,
            )
        return embed

    def build_game_view(self, game_id: int) -> SitView:
        # SIT routes through _on_sit (not _handle_group_button) because Musical Chairs'
        # button has phase-dependent behavior (false-start vs seat-claim) and can
        # eliminate multiple players at once on round close.
        return SitView(game_id, self._on_sit)

    # ── Slash commands ────────────────────────────────────────────────────────

    @musicalchairs.command(name="start", description="Open a Musical Chairs lobby")
    @app_commands.describe(
        stakes="Optional custom stakes text (max 200 chars)",
        wager="Optional coin wager — every player antes this; winner takes the pot",
    )
    async def mc_start(
        self,
        interaction: discord.Interaction,
        stakes: str | None = None,
        wager: int | None = None,
    ) -> None:
        await self._base_lobby(interaction, stakes, wager)

async def setup(bot: Bot) -> None:
    cog = MusicalChairsCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("musicalchairs")
    games.add_command(cog.musicalchairs)
