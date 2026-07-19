"""Pressure Cooker cog — slash commands and BaseDuel implementation."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import logging
import random

import discord
from discord import app_commands

from bot_modules.duels.base_duel import BaseDuel
from bot_modules.economy.game_rewards import pay_game_rewards
from bot_modules.games.command_groups import games
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED, COLOR_YELLOW

from . import db as pdb
from .game import PressureGame, apply_pump
from .views import GameView, gauge_bar

log = logging.getLogger("dungeonkeeper.pressure")


class PressureCookerDuel(BaseDuel, name="PressureCookerCog"):

    GAME_KEY = "pressure"
    GAME_DISPLAY_NAME = "Pressure Cooker"

    pressure = app_commands.Group(
        name="pressure",
        description="Pressure Cooker — a high-stakes nickname duel",
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
        return await pdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text
        )

    async def _db_get_game(self, game_id: int) -> PressureGame | None:
        return await pdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> PressureGame | None:
        return await pdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> PressureGame | None:
        return await pdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_set_state(self, game_id: int, state: str, **kw) -> None:
        await pdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[PressureGame]:
        return await pdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[PressureGame]:
        return await pdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[PressureGame]:
        return await pdb.fetch_sweepable_games(self.db, now)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: PressureGame) -> None:
        first_player = random.choice([game.challenger_id, game.target_id])
        await pdb.set_game_state(self.db, game.id, "ACTIVE", active_player=first_player)

    def render_game_state(
        self, game: PressureGame, guild: discord.Guild
    ) -> discord.Embed:
        if game.gauge >= 75:
            color = COLOR_RED
        elif game.gauge >= 50:
            color = COLOR_YELLOW
        else:
            color = COLOR_GREEN

        embed = discord.Embed(title="🔥 PRESSURE COOKER", color=color)

        p1 = guild.get_member(game.challenger_id)
        p2 = guild.get_member(game.target_id)
        p1_name = p1.display_name if p1 else str(game.challenger_id)
        p2_name = p2.display_name if p2 else str(game.target_id)
        embed.description = f"**{p1_name}** vs **{p2_name}**"

        embed.add_field(name="Gauge", value=gauge_bar(game.gauge), inline=False)

        if game.pumps:
            last_pumps = game.pumps[-5:]
            lines = []
            for entry in last_pumps:
                m = guild.get_member(entry.player_id)
                name = m.display_name if m else str(entry.player_id)
                gauge_after = entry.gauge_before + entry.roll
                bust_marker = " 💥" if gauge_after >= 100 else ""
                lines.append(f"**{name}**: +{entry.roll} → {gauge_after}/100{bust_marker}")
            embed.add_field(name="Recent Pumps", value="\n".join(lines), inline=False)

        if game.active_player and game.state == "ACTIVE":
            active = guild.get_member(game.active_player)
            turn = active.mention if active else str(game.active_player)
            embed.add_field(name="▶️ Turn", value=turn, inline=False)

        return embed

    def render_result_state(
        self,
        game: PressureGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        winner = guild.get_member(game.winner_id)  # type: ignore[arg-type]
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        winner_name = winner.display_name if winner else str(game.winner_id)
        loser_name = loser.display_name if loser else str(game.loser_id)

        embed = discord.Embed(title="💥 BOOM.", color=COLOR_RED)
        embed.description = (
            f"**{loser_name}** pushed the gauge to **{game.gauge}/100** and lost."
        )
        embed.add_field(name="🏆 Winner", value=winner_name, inline=True)
        embed.add_field(name="💀 Loser", value=loser_name, inline=True)

        stakes_text = game.stakes_text or "24-hour nickname surrender."
        embed.add_field(name="📋 Stakes", value=stakes_text, inline=False)

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
                    f"The nickname lasts 24 hours."
                ),
                inline=False,
            )

        if game.pumps:
            last = game.pumps[-10:]
            lines = []
            for entry in last:
                m = guild.get_member(entry.player_id)
                name = m.display_name if m else str(entry.player_id)
                gauge_after = entry.gauge_before + entry.roll
                bust = " 💥" if gauge_after >= 100 else ""
                lines.append(f"{name}: +{entry.roll} → {gauge_after}{bust}")
            embed.add_field(name="Pump Log", value="\n".join(lines), inline=False)

        return embed

    def build_game_view(self, game_id: int) -> GameView:
        return GameView(game_id, self._handle_game_button)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: PressureGame
    ) -> tuple[str, int | None]:
        if interaction.user.id != game.active_player:
            await interaction.followup.send("It's not your turn.", ephemeral=True)
            return ("rejected", None)

        result = apply_pump(game, interaction.user.id)
        await pdb.save_pump(self.db, game)

        if result.busted:
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            bust_embed = self.render_game_state(game, guild)
            game_view = self.build_game_view(game.id)
            game_view.disable()
            await interaction.edit_original_response(embed=bust_embed, view=game_view)
            await pay_game_rewards(
                self.bot, game.guild_id,
                [game.challenger_id, game.target_id],
                [game.winner_id] if game.winner_id is not None else [],
                self.GAME_KEY,
                occurrence=str(game.id),
            )
            return ("done", game.loser_id)

        return ("continue", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @pressure.command(name="challenge", description="Challenge someone to Pressure Cooker")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
    )
    async def pressure_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes)

async def setup(bot: Bot) -> None:
    cog = PressureCookerDuel(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("pressure")
    games.add_command(cog.pressure)
