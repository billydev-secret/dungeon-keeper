"""Pressure Cooker cog — slash commands and BaseDuel implementation."""
from __future__ import annotations

import json
import logging
import random

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.duels.base_duel import BaseDuel
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
                value=f"**{loser_name}** is now known as **{imposed_nick}** for 24 hours.",
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

    @pressure.command(name="cancel", description="Cancel your pending challenge in this channel")
    async def pressure_cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        game = await pdb.get_pending_game_for_challenger(
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
        await pdb.set_game_state(self.db, game.id, "EXPIRED_PENDING")
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

    @pressure.command(name="stats", description="View Pressure Cooker stats")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def pressure_stats(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        stats = await pdb.get_stats(self.db, interaction.guild.id, target.id)
        accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
        embed = discord.Embed(
            title=f"🔥 Pressure Cooker — {target.display_name}",
            color=accent,
        )
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Losses", value=str(stats["losses"]), inline=True)
        embed.add_field(name="Total Games", value=str(stats["total_games"]), inline=True)
        if stats["highest_gauge_win"] is not None:
            embed.add_field(
                name="Highest Gauge (Win)", value=f"{stats['highest_gauge_win']}/100", inline=True
            )
        await interaction.response.send_message(embed=embed)

    @pressure.command(name="revert", description="Request early revert of your Pressure Cooker nickname")
    async def pressure_revert(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        cfg = await pdb.get_config(self.db, interaction.guild.id)
        if not cfg.get("allow_early_revert"):
            await interaction.response.send_message(
                "Early revert isn't enabled on this server. Ask a mod.", ephemeral=True
            )
            return
        nick = await pdb.get_active_nick_for_user(
            self.db, interaction.guild.id, interaction.user.id
        )
        if not nick:
            await interaction.response.send_message(
                "You don't have an active nickname sentence.", ephemeral=True
            )
            return
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            try:
                await member.edit(nick=nick["original_nick"], reason="Early revert requested by user")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I couldn't revert your nickname — I may not have permission.", ephemeral=True
                )
                return
        await pdb.mark_nick_reverted(self.db, nick["id"], "early_revert")
        await interaction.response.send_message(
            "Your nickname has been restored early.", ephemeral=True
        )

    @pressure.command(name="config", description="Configure Pressure Cooker (mods only)")
    @app_commands.describe(
        cooldown_hours="Hours before the same pair can play again (default 48)",
        sentence_hours="Hours the imposed nickname lasts (default 24)",
        allow_early_revert="Allow losers to request early nick revert: 0=no, 1=yes",
        channel_allowlist="JSON array of allowed channel IDs, or '[]' for all channels",
        max_nick_length="Maximum nickname character count (default 32)",
        max_stakes_length="Maximum stakes text character count (default 200)",
    )
    async def pressure_config(
        self,
        interaction: discord.Interaction,
        cooldown_hours: int | None = None,
        sentence_hours: int | None = None,
        allow_early_revert: int | None = None,
        channel_allowlist: str | None = None,
        max_nick_length: int | None = None,
        max_stakes_length: int | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "You need the Manage Server permission to configure Pressure Cooker.",
                ephemeral=True,
            )
            return

        updates: dict = {}
        if cooldown_hours is not None:
            updates["cooldown_hours"] = max(0, cooldown_hours)
        if sentence_hours is not None:
            updates["sentence_hours"] = max(1, sentence_hours)
        if allow_early_revert is not None:
            updates["allow_early_revert"] = 1 if allow_early_revert else 0
        if channel_allowlist is not None:
            try:
                json.loads(channel_allowlist)
                updates["channel_allowlist"] = channel_allowlist
            except json.JSONDecodeError:
                await interaction.response.send_message(
                    "channel_allowlist must be a valid JSON array, e.g. `[123456789, 987654321]`",
                    ephemeral=True,
                )
                return
        if max_nick_length is not None:
            updates["max_nick_length"] = max(1, min(32, max_nick_length))
        if max_stakes_length is not None:
            updates["max_stakes_length"] = max(1, min(2000, max_stakes_length))

        if not updates:
            cfg = await pdb.get_config(self.db, interaction.guild.id)
            accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
            embed = discord.Embed(title="🔧 Pressure Cooker Config", color=accent)
            for k, v in cfg.items():
                if k not in ("guild_id", "game_type"):
                    embed.add_field(name=k, value=str(v), inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await pdb.upsert_config(self.db, interaction.guild.id, **updates)
        lines = [f"**{k}** → `{v}`" for k, v in updates.items()]
        await interaction.response.send_message(
            "Config updated:\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    cog = PressureCookerDuel(bot)
    await bot.add_cog(cog)
    for name in ("cancel", "revert", "stats", "config"):
        cog.pressure.remove_command(name)
    bot.tree.remove_command("pressure")
    games.add_command(cog.pressure)
