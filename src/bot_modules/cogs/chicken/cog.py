"""Chicken cog — brinkmanship for 2..N players. Hold your nerve or bail before the crash."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import json
import logging
import time

import discord
from discord import app_commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.duels import db as duels_db
from bot_modules.duels.base_game import BaseGame
from bot_modules.games.command_groups import games
from bot_modules.services.embeds import COLOR_GOLD, COLOR_RED, COLOR_YELLOW

from . import db as chdb
from .game import ChickenGame, bravest_bailer, meter_pct, resolve_crash
from .views import ChickenView

log = logging.getLogger("dungeonkeeper.chicken")

_TICK_INTERVAL = 2.0
_BAR_WIDTH = 16


def _meter_bar(pct: float) -> str:
    filled = int(round(pct / 100.0 * _BAR_WIDTH))
    filled = max(0, min(_BAR_WIDTH, filled))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


class ChickenCog(BaseGame, name="ChickenCog"):

    GAME_KEY = "chicken"
    GAME_DISPLAY_NAME = "Chicken"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        # Chicken runs TWO concurrent tasks per game: the crash deadline + the meter
        # ticker that edits the embed as the bar climbs.
        self._timers: dict[int, list[asyncio.Task]] = {}

    chicken = app_commands.Group(
        name="chicken",
        description="Chicken — hold your nerve, or bail before the meter crashes!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_get_game(self, game_id: int) -> ChickenGame | None:
        return await chdb.get_game(self.db, game_id)

    async def _db_set_state(self, game_id: int, state: str, **kw) -> None:
        await chdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None
    ) -> int:
        return await chdb.create_lobby(self.db, guild_id, channel_id, host_id, stakes_text)

    async def _db_fetch_active_games(self) -> list[ChickenGame]:
        return await chdb.fetch_active_games(self.db)

    async def _db_fetch_lobby_games(self) -> list[ChickenGame]:
        return await chdb.fetch_lobby_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[ChickenGame]:
        return await chdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[ChickenGame]:
        return await chdb.fetch_sweepable_games(self.db, now)

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int, float]:
        cfg = await chdb.get_config(self.db, guild_id)
        return int(cfg["min_players"]), int(cfg["max_players"]), float(cfg["lobby_timeout"])

    # ── Timer helpers (multiple tasks per game) ───────────────────────────────

    def _cancel_timers(self, game_id: int) -> None:
        for task in self._timers.pop(game_id, []):
            if not task.done():
                task.cancel()

    def _add_timer(self, game_id: int, task: asyncio.Task) -> None:
        self._timers.setdefault(game_id, []).append(task)

    async def _run_crash_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._crash(game_id)

    async def _run_ticker(self, game_id: int, interval: float, total: float) -> None:
        elapsed = 0.0
        try:
            while elapsed < total:
                await asyncio.sleep(interval)
                elapsed += interval
                async with self._get_lock(game_id):
                    game = await chdb.get_game(self.db, game_id)
                    if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                        return
                    guild = self.bot.get_guild(game.guild_id)
                    if guild and game.message_id:
                        # Embed-only edit: never re-send the view mid-climb, or an
                        # in-flight BAIL click can be invalidated ("interaction failed").
                        await self._edit_embed_silent(
                            game.channel_id, game.message_id,
                            self.render_game_state(game, guild),
                        )
        except asyncio.CancelledError:
            return

    async def _crash(self, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await chdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                return
            crashers = list(game.alive)
            winner, loser = resolve_crash(crashers, game.bail_log)
            if loser is not None and winner is not None:
                await self._post_group_result(game, winner, loser)
            else:
                # total wipeout (nobody bailed) → cosmetic, no nick
                await self._resolve_cosmetic(game, winner)
        self._cancel_timers(game_id)
        self._game_locks.pop(game_id, None)

    async def _resolve_cosmetic(self, game: ChickenGame, winner_id: int | None) -> None:
        """Resolve with no nickname stake (everyone bailed, or total wipeout)."""
        now = time.time()
        game.winner_id = winner_id
        guild = self.bot.get_guild(game.guild_id)
        for uid in game.roster:
            await duels_db.set_group_cooldown(self.db, game.guild_id, self.GAME_KEY, uid)

        if guild and game.message_id:
            dv = self.build_game_view(game.id)
            dv.disable()
            await self._edit_message_silent(
                game.channel_id, game.message_id, self.render_game_state(game, guild), dv
            )

        result_message_id = None
        channel = self.bot.get_channel(game.channel_id)
        if channel and guild:
            embed = self.render_result_state(game, guild)
            ping = ""
            if winner_id is not None:
                wm = guild.get_member(winner_id)
                ping = wm.mention if wm else ""
            try:
                msg = await channel.send(content=ping, embed=embed)  # type: ignore[union-attr]
                result_message_id = msg.id
            except (discord.Forbidden, discord.HTTPException):
                pass

        await chdb.set_game_state(
            self.db, game.id, "RESOLVED_NO_NICK",
            winner_id=winner_id,
            result_message_id=result_message_id,
            resolved_at=now,
            last_action_at=now,
        )
        await self.on_game_resolved(game.id)

    # ── Button handler ────────────────────────────────────────────────────────

    async def _on_bail(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer()
        resolved = False
        async with self._get_lock(game_id):
            game = await chdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.phase != "CLIMBING":
                await interaction.followup.send(
                    "This game is no longer active.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid not in game.alive:
                await interaction.followup.send(
                    "You've already bailed (or you're not in this game).", ephemeral=True
                )
                return

            now = time.time()
            pct = meter_pct(now, game.climb_started_at, game.climb_duration)
            new_alive = [u for u in game.alive if u != uid]
            new_bail = list(game.bail_log) + [
                {"player_id": uid, "bail_ts": now, "meter_pct": pct}
            ]
            await chdb.set_game_state(
                self.db, game_id, "ACTIVE",
                alive=json.dumps(new_alive),
                bail_log=json.dumps(new_bail),
                last_action_at=now,
            )
            game.alive = new_alive
            game.bail_log = new_bail

            if not new_alive:
                # everyone blinked → the last to bail (this presser) wins, no nick
                await self._resolve_cosmetic(game, winner_id=uid)
                resolved = True
            else:
                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                await interaction.edit_original_response(
                    embed=self.render_game_state(game, guild)
                )
        if resolved:
            self._cancel_timers(game_id)
            self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: ChickenGame) -> None:
        cfg = await chdb.get_config(self.db, game.guild_id)
        duration = float(cfg["climb_duration"])
        now = time.time()
        await chdb.set_game_state(
            self.db, game.id, "ACTIVE",
            phase="CLIMBING",
            climb_started_at=now,
            climb_duration=duration,
            bail_log="[]",
            last_action_at=now,
        )
        self._schedule(game.id, duration, duration)

    def _schedule(self, game_id: int, crash_in: float, total: float) -> None:
        crash = asyncio.create_task(self._run_crash_timer(game_id, crash_in))
        ticker = asyncio.create_task(self._run_ticker(game_id, _TICK_INTERVAL, total))
        self._add_timer(game_id, crash)
        self._add_timer(game_id, ticker)

    async def on_game_resume(self, game: ChickenGame) -> None:
        if game.climb_started_at is None or game.climb_duration is None:
            asyncio.create_task(self._crash(game.id))
            return
        now = time.time()
        remaining = (game.climb_started_at + game.climb_duration) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            mentions = " ".join(f"<@{u}>" for u in game.alive)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Chicken resuming. {mentions}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if remaining <= 0:
            asyncio.create_task(self._crash(game.id))
        else:
            self._schedule(game.id, remaining, remaining)

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timers(game_id)

    def _name(self, guild: discord.Guild, uid: int) -> str:
        m = guild.get_member(uid)
        return m.display_name if m else str(uid)

    def render_game_state(self, game: ChickenGame, guild: discord.Guild) -> discord.Embed:
        holders = ", ".join(self._name(guild, u) for u in game.alive) or "—"
        bailed = ", ".join(
            f"{self._name(guild, b['player_id'])} ({b['meter_pct']:.0f}%)"
            for b in game.bail_log
        ) or "—"
        pct = meter_pct(time.time(), game.climb_started_at, game.climb_duration)

        embed = discord.Embed(
            title="🐔 CHICKEN",
            description="First to bail is safe — but ride to 100% and you **crash**.",
            color=COLOR_YELLOW,
        )
        embed.add_field(name="Still holding", value=holders, inline=False)
        embed.add_field(name="Bailed", value=bailed, inline=False)
        embed.add_field(
            name=f"⚡ METER — {pct:.0f}%",
            value=f"`{_meter_bar(pct)}`\n↑ crash at 100%. blink first or ride it out.",
            inline=False,
        )
        stakes = game.stakes_text or "Whoever's still holding at the crash surrenders their nickname for 24h."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        return embed

    def render_result_state(
        self,
        game: ChickenGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        if game.loser_id is not None:
            # crash with a nick loser
            crashers = ", ".join(self._name(guild, u) for u in game.alive) or "—"
            loser_name = self._name(guild, game.loser_id)
            embed = discord.Embed(
                title="💥 CRASH at 100%!",
                description=f"😵 Still holding when it blew: {crashers}",
                color=COLOR_RED,
            )
            if game.winner_id is not None:
                best = bravest_bailer(game.bail_log)
                pct = best["meter_pct"] if best else 0.0
                embed.add_field(
                    name="🐔 Nerves of steel",
                    value=f"**{self._name(guild, game.winner_id)}** bailed last at {pct:.0f}%",
                    inline=False,
                )
            embed.add_field(name="💀 Takes the stake", value=loser_name, inline=False)
            stakes = game.stakes_text or "24-hour nickname surrender."
            embed.add_field(name="📋 Stakes", value=stakes, inline=False)
            if imposed_nick:
                embed.add_field(
                    name="🏷️ Nickname Applied",
                    value=f"**{loser_name}** is now known as **{imposed_nick}** for 24 hours.",
                    inline=False,
                )
            elif game.stakes_text is None:
                embed.add_field(
                    name="⏳ Awaiting Nickname",
                    value=(
                        f"**{self._name(guild, game.winner_id) if game.winner_id else 'Winner'}**, "
                        "press **Name the loser** within 5 minutes."
                    ),
                    inline=False,
                )
            return embed

        # cosmetic: everyone bailed, or total wipeout
        if game.winner_id is not None:
            ranked = sorted(game.bail_log, key=lambda b: b["meter_pct"], reverse=True)
            lines = [
                f"**{self._name(guild, b['player_id'])}** — {b['meter_pct']:.0f}%"
                for b in ranked
            ]
            embed = discord.Embed(
                title="🐔 Everyone blinked!",
                description=f"🏆 **{self._name(guild, game.winner_id)}** held longest. No nicknames today.",
                color=COLOR_GOLD,
            )
            if lines:
                embed.add_field(name="Chicken ranking", value="\n".join(lines), inline=False)
            return embed

        embed = discord.Embed(
            title="💥 Total wipeout!",
            description="Nobody blinked — everyone rode it straight into the crash. No winner, no nicknames.",
            color=COLOR_RED,
        )
        return embed

    def build_game_view(self, game_id: int) -> ChickenView:
        return ChickenView(game_id, self._on_bail)

    # ── Slash commands ────────────────────────────────────────────────────────

    @chicken.command(name="start", description="Open a Chicken lobby")
    @app_commands.describe(stakes="Optional custom stakes text (max 200 chars)")
    async def ch_start(
        self, interaction: discord.Interaction, stakes: str | None = None
    ) -> None:
        await self._base_lobby(interaction, stakes)

    @chicken.command(name="stats", description="View Chicken stats")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def ch_stats(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        stats = await chdb.get_stats(self.db, interaction.guild.id, target.id)  # type: ignore[arg-type]
        accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
        embed = discord.Embed(title=f"🐔 Chicken — {target.display_name}", color=accent)
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Crashes", value=str(stats["losses"]), inline=True)
        embed.add_field(name="Games", value=str(stats["total_games"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @chicken.command(name="config", description="Configure Chicken (mods only)")
    @app_commands.describe(
        cooldown_hours="Hours before a player can join another game (default 48)",
        sentence_hours="Hours the imposed nickname lasts (default 24)",
        climb_duration="Seconds for the meter to climb 0→100 (default 25)",
        min_players="Minimum players to start (default 2)",
        max_players="Maximum players in a lobby (default 8)",
    )
    async def ch_config(
        self,
        interaction: discord.Interaction,
        cooldown_hours: int | None = None,
        sentence_hours: int | None = None,
        climb_duration: float | None = None,
        min_players: int | None = None,
        max_players: int | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "You need the Manage Server permission to configure this game.", ephemeral=True
            )
            return

        shared_updates: dict = {}
        game_updates: dict = {}
        if cooldown_hours is not None:
            shared_updates["cooldown_hours"] = max(0, cooldown_hours)
        if sentence_hours is not None:
            shared_updates["sentence_hours"] = max(1, sentence_hours)
        if climb_duration is not None:
            game_updates["climb_duration"] = max(5.0, climb_duration)
        if min_players is not None:
            game_updates["min_players"] = max(2, min_players)
        if max_players is not None:
            game_updates["max_players"] = max(2, max_players)

        if not shared_updates and not game_updates:
            shared_cfg = await duels_db.get_config(self.db, interaction.guild.id, self.GAME_KEY)
            game_cfg = await chdb.get_config(self.db, interaction.guild.id)
            accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
            embed = discord.Embed(title="🔧 Chicken Config", color=accent)
            for k, v in shared_cfg.items():
                if k not in ("guild_id", "game_type"):
                    embed.add_field(name=k, value=str(v), inline=True)
            for k, v in game_cfg.items():
                if k != "guild_id":
                    embed.add_field(name=k, value=str(v), inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if shared_updates:
            await duels_db.upsert_config(
                self.db, interaction.guild.id, self.GAME_KEY, **shared_updates
            )
        if game_updates:
            await chdb.upsert_config(self.db, interaction.guild.id, **game_updates)

        all_updates = {**shared_updates, **game_updates}
        lines = [f"**{k}** → `{v}`" for k, v in all_updates.items()]
        await interaction.response.send_message(
            "Config updated:\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: Bot) -> None:
    cog = ChickenCog(bot)
    await bot.add_cog(cog)
    for name in ("stats", "config"):
        cog.chicken.remove_command(name)
    bot.tree.remove_command("chicken")
    games.add_command(cog.chicken)
