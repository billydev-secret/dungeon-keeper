"""Hot Potato cog — pass-the-bomb nickname duel."""
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
from bot_modules.duels import db as duels_db
from bot_modules.economy.game_rewards import pay_game_rewards
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.games.command_groups import games
from bot_modules.duels.views import ResultView
from bot_modules.services.embeds import COLOR_RED, COLOR_YELLOW

from . import db as hpdb
from .game import HotPotatoGame, compute_style_points
from .views import PassView

log = logging.getLogger("dungeonkeeper.hot_potato")


class HotPotatoDuel(BaseDuel, name="HotPotatoCog"):

    GAME_KEY = "hot_potato"
    GAME_DISPLAY_NAME = "Hot Potato"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}

    hot_potato = app_commands.Group(
        name="hotpotato",
        description="Hot Potato — pass the bomb before it blows!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_create_game(
        self,
        guild_id: int,
        channel_id: int,
        challenger_id: int,
        target_id: int,
        stakes_text: str | None,
    ) -> int:
        return await hpdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text
        )

    async def _db_get_game(self, game_id: int) -> HotPotatoGame | None:
        return await hpdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> HotPotatoGame | None:
        return await hpdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> HotPotatoGame | None:
        return await hpdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_set_state(self, game_id: int, state: str, **kw) -> None:
        await hpdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[HotPotatoGame]:
        return await hpdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[HotPotatoGame]:
        return await hpdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[HotPotatoGame]:
        return await hpdb.fetch_sweepable_games(self.db, now)

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_explode_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._explode(game_id)
        except asyncio.CancelledError:
            pass

    async def _explode(self, game_id: int) -> None:
        """Timer callback: determine loser, award style points, post result. All inside lock."""
        async with self._get_lock(game_id):
            game = await hpdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.holder_id is None:
                return

            loser_id = game.holder_id
            winner_id = (
                game.challenger_id if loser_id == game.target_id else game.target_id
            )
            now = time.time()

            new_log = list(game.pass_log)
            if new_log and new_log[-1]["passed_at"] is None:
                new_log[-1] = {**new_log[-1], "passed_at": now}

            if game.started_at and game.timer_seconds:
                style_pts = compute_style_points(
                    new_log, game.started_at, game.timer_seconds, loser_id, winner_id
                )
                for uid, pts in style_pts.items():
                    if pts > 0:
                        await hpdb.add_style_points(self.db, game.guild_id, uid, pts)
            else:
                style_pts = {}

            game.winner_id = winner_id
            game.loser_id = loser_id
            game.pass_log = new_log
            game.resolved_at = now

            guild = self.bot.get_guild(game.guild_id)
            if guild:
                disabled_view = self.build_game_view(game.id)
                disabled_view.disable()
                await self._edit_message_silent(
                    game.channel_id,
                    game.message_id,
                    self.render_game_state(game, guild),
                    disabled_view,
                )

            result_view = ResultView(game.id, winner_id, loser_id, self._handle_set_nick)
            channel = self.bot.get_channel(game.channel_id)
            result_message_id = None
            if channel and guild:
                winner_m = guild.get_member(winner_id)
                loser_m = guild.get_member(loser_id)
                ping_content = " ".join(m.mention for m in (winner_m, loser_m) if m)
                result_embed = self.render_result_state(game, guild)
                try:
                    result_msg = await channel.send(  # type: ignore[union-attr]
                        content=ping_content, embed=result_embed, view=result_view
                    )
                    self.bot.add_view(result_view, message_id=result_msg.id)
                    result_message_id = result_msg.id
                except (discord.Forbidden, discord.HTTPException):
                    pass

            await hpdb.set_game_state(
                self.db, game_id, "RESOLVED",
                winner_id=winner_id,
                loser_id=loser_id,
                pass_log=json.dumps(new_log),
                result_message_id=result_message_id,
                resolved_at=now,
                last_action_at=now,
            )
            await pay_game_rewards(
                self.bot, game.guild_id,
                [game.challenger_id, game.target_id], [winner_id], self.GAME_KEY,
                occurrence=str(game.id),
            )

        self._cancel_timer(game_id)
        self._game_locks.pop(game_id, None)
        await self.on_game_resolved(game_id)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: HotPotatoGame) -> None:
        cfg = await hpdb.get_config(self.db, game.guild_id)
        timer = random.uniform(cfg["min_timer"], cfg["max_timer"])
        now = time.time()
        initial_log = json.dumps(
            [{"holder_id": game.challenger_id, "received_at": now, "passed_at": None}]
        )
        await hpdb.set_game_state(
            self.db, game.id, "ACTIVE",
            holder_id=game.challenger_id,
            timer_seconds=timer,
            started_at=now,
            pass_log=initial_log,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_explode_timer(game.id, timer))
        self._timers[game.id] = task

    async def on_game_resume(self, game: HotPotatoGame) -> None:
        if not game.started_at or not game.timer_seconds:
            asyncio.create_task(self._explode(game.id))
            return

        now = time.time()
        remaining = (game.started_at + game.timer_seconds) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Hot Potato game resuming. "
                    f"<@{game.challenger_id}> <@{game.target_id}>"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if remaining <= 0:
            asyncio.create_task(self._explode(game.id))
        else:
            task = asyncio.create_task(self._run_explode_timer(game.id, remaining))
            self._timers[game.id] = task

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)

    def render_game_state(
        self, game: HotPotatoGame, guild: discord.Guild
    ) -> discord.Embed:
        challenger = guild.get_member(game.challenger_id)
        target = guild.get_member(game.target_id)
        c_name = challenger.display_name if challenger else str(game.challenger_id)
        t_name = target.display_name if target else str(game.target_id)

        pass_count = max(0, len(game.pass_log) - 1)

        if game.winner_id:
            embed = discord.Embed(
                title="💥 BOOM!",
                description=f"**{c_name}** vs **{t_name}** — the potato exploded!",
                color=COLOR_RED,
            )
            embed.add_field(name="🥔 Passes", value=str(pass_count), inline=True)
        else:
            holder = guild.get_member(game.holder_id) if game.holder_id else None
            holder_name = (
                holder.display_name if holder
                else (str(game.holder_id) if game.holder_id else "?")
            )
            embed = discord.Embed(
                title="🥔 Hot Potato",
                description=(
                    f"**{c_name}** vs **{t_name}**\n\n"
                    f"**{holder_name}** is holding the potato! Quick — PASS it!"
                ),
                color=COLOR_YELLOW,
            )
            embed.add_field(name="🥔 Passes", value=str(pass_count), inline=True)

        stakes = game.stakes_text or "Loser surrenders their nickname."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        return embed

    def render_result_state(
        self,
        game: HotPotatoGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        winner = guild.get_member(game.winner_id)  # type: ignore[arg-type]
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        winner_name = winner.display_name if winner else str(game.winner_id)
        loser_name = loser.display_name if loser else str(game.loser_id)

        pass_count = max(0, len(game.pass_log) - 1)

        embed = discord.Embed(
            title="💥 Hot Potato!",
            description=f"**{loser_name}** was holding it when it blew!",
            color=COLOR_RED,
        )
        embed.add_field(name="🏆 Winner", value=winner_name, inline=True)
        embed.add_field(name="💀 Loser", value=loser_name, inline=True)
        embed.add_field(name="🥔 Passes", value=str(pass_count), inline=True)

        stakes_text = game.stakes_text or "24-hour nickname surrender."
        embed.add_field(name="📋 Stakes", value=stakes_text, inline=False)

        if (
            game.started_at
            and game.timer_seconds
            and game.winner_id is not None
            and game.loser_id is not None
        ):
            style_pts = compute_style_points(
                game.pass_log,
                game.started_at,
                game.timer_seconds,
                game.loser_id,
                game.winner_id,
            )
            lines = []
            for uid, pts in style_pts.items():
                if pts > 0:
                    m = guild.get_member(uid)
                    name = m.display_name if m else str(uid)
                    lines.append(f"**{name}**: +{pts} pts")
            if lines:
                embed.add_field(
                    name="✨ Style Points (danger zone)",
                    value="\n".join(lines),
                    inline=False,
                )

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
                    f"**{winner_name}**, press **Name the loser** within 5 minutes. "
                    "The nickname lasts 24 hours."
                ),
                inline=False,
            )

        return embed

    def build_game_view(self, game_id: int) -> PassView:
        return PassView(game_id, self._handle_game_button)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: HotPotatoGame
    ) -> tuple[str, int | None]:
        player_id = interaction.user.id

        if player_id not in (game.challenger_id, game.target_id):
            await interaction.followup.send("You're not in this game.", ephemeral=True)
            return ("rejected", None)

        if player_id != game.holder_id:
            await interaction.followup.send(
                "You're not holding the potato!", ephemeral=True
            )
            return ("rejected", None)

        now = time.time()
        new_holder = (
            game.challenger_id if player_id == game.target_id else game.target_id
        )
        new_log = list(game.pass_log)
        if new_log and new_log[-1]["passed_at"] is None:
            new_log[-1] = {**new_log[-1], "passed_at": now}
        new_log.append({"holder_id": new_holder, "received_at": now, "passed_at": None})

        await hpdb.set_game_state(
            self.db, game.id, "ACTIVE",
            holder_id=new_holder,
            pass_log=json.dumps(new_log),
            last_action_at=now,
        )
        game.holder_id = new_holder
        game.pass_log = new_log
        return ("continue", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @hot_potato.command(name="challenge", description="Challenge someone to Hot Potato")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
    )
    async def hp_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes)

    @hot_potato.command(name="cancel", description="Cancel your pending Hot Potato challenge")
    async def hp_cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        game = await hpdb.get_pending_game_for_challenger(
            self.db,
            interaction.guild.id,
            interaction.channel_id,  # type: ignore[arg-type]
            interaction.user.id,
        )
        if not game:
            await interaction.response.send_message(
                "You don't have a pending challenge in this channel.", ephemeral=True
            )
            return
        await hpdb.set_game_state(self.db, game.id, "EXPIRED_PENDING")
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="🚫 Challenge Cancelled",
                description=f"{interaction.user.mention} cancelled the challenge.",
                color=COLOR_YELLOW,
            ),
            view=None,
        )
        await interaction.response.send_message("Challenge cancelled.", ephemeral=True)

    @hot_potato.command(name="stats", description="View Hot Potato stats")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def hp_stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        stats = await hpdb.get_stats(self.db, interaction.guild.id, target.id)  # type: ignore[arg-type]
        accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
        embed = discord.Embed(
            title=f"🥔 Hot Potato — {target.display_name}",
            color=accent,
        )
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Losses", value=str(stats["losses"]), inline=True)
        embed.add_field(name="Total Games", value=str(stats["total_games"]), inline=True)
        embed.add_field(name="✨ Style Points", value=str(stats["style_points"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @hot_potato.command(name="config", description="Configure Hot Potato (mods only)")
    @app_commands.describe(
        cooldown_hours="Hours before the same pair can play again (default 48)",
        sentence_hours="Hours the imposed nickname lasts (default 24)",
        allow_early_revert="Allow losers to request early nick revert: 0=no, 1=yes",
        min_timer="Minimum seconds before explosion (default 10.0)",
        max_timer="Maximum seconds before explosion (default 45.0)",
    )
    async def hp_config(
        self,
        interaction: discord.Interaction,
        cooldown_hours: int | None = None,
        sentence_hours: int | None = None,
        allow_early_revert: int | None = None,
        min_timer: float | None = None,
        max_timer: float | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "You need the Manage Server permission to configure Hot Potato.",
                ephemeral=True,
            )
            return

        shared_updates: dict = {}
        game_updates: dict = {}

        if cooldown_hours is not None:
            shared_updates["cooldown_hours"] = max(0, cooldown_hours)
        if sentence_hours is not None:
            shared_updates["sentence_hours"] = max(1, sentence_hours)
        if allow_early_revert is not None:
            shared_updates["allow_early_revert"] = 1 if allow_early_revert else 0
        if min_timer is not None:
            game_updates["min_timer"] = max(5.0, min_timer)
        if max_timer is not None:
            game_updates["max_timer"] = max(10.0, max_timer)

        if not shared_updates and not game_updates:
            shared_cfg = await duels_db.get_config(self.db, interaction.guild.id, self.GAME_KEY)
            game_cfg = await hpdb.get_config(self.db, interaction.guild.id)
            accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
            embed = discord.Embed(title="🔧 Hot Potato Config", color=accent)
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
            await hpdb.upsert_config(self.db, interaction.guild.id, **game_updates)

        all_updates = {**shared_updates, **game_updates}
        lines = [f"**{k}** → `{v}`" for k, v in all_updates.items()]
        await interaction.response.send_message(
            "Config updated:\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: Bot) -> None:
    cog = HotPotatoDuel(bot)
    await bot.add_cog(cog)
    for name in ("cancel", "stats", "config"):
        cog.hot_potato.remove_command(name)
    bot.tree.remove_command("hotpotato")
    games.add_command(cog.hot_potato)
