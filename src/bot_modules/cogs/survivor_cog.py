"""Survivor cog — member self-service only (spec §2, stage 3).

Thin glue over ``survivor/logic.py``: `/survivor pick|status|board` plus the
persistent Join button. Admin configuration lives on the dashboard (the
2026-08-17 decision); the two live admin commands (settle, preview-reckoning)
arrive with the settle engine in stage 4.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.services.survivor_service import get_active_season
from bot_modules.survivor import logic
from bot_modules.survivor.embeds import (
    build_board_embed,
    build_status_embed,
)
from bot_modules.survivor.views import (
    JoinSeasonButton,
    PickPanel,
    kickoff_label,
    submit_pick,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.survivor")


class SurvivorCog(commands.Cog):
    """NFL pick'em survival pool — the member surface."""

    survivor = app_commands.Group(
        name="survivor",
        description="NFL pick'em survival pool — one team a week, no team twice.",
        guild_only=True,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx

    async def cog_load(self) -> None:
        # The Join button on the pinned announcement must survive restarts.
        self.bot.add_dynamic_items(JoinSeasonButton)

    # ── helpers ───────────────────────────────────────────────────────

    async def _season_and_offset(self, guild_id: int) -> tuple[dict | None, float]:
        def _q():
            with open_db(self.ctx.db_path) as conn:
                return (
                    get_active_season(conn, guild_id),
                    get_tz_offset_hours(conn, guild_id),
                )

        return await asyncio.to_thread(_q)

    @staticmethod
    def _now() -> float:
        return discord.utils.utcnow().timestamp()

    # ── /survivor pick ────────────────────────────────────────────────

    async def _team_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        guild_id = interaction.guild.id
        now = self._now()

        def _q():
            with open_db(self.ctx.db_path) as conn:
                season = get_active_season(conn, guild_id)
                if season is None:
                    return [], 0.0
                week = logic.pick_week(conn, season["season_year"], now)
                if week is None:
                    return [], 0.0
                games = logic.legal_teams(
                    conn, season, interaction.user.id, week, now
                )
                offset = get_tz_offset_hours(conn, guild_id)
            return games, offset

        games, offset = await asyncio.to_thread(_q)
        needle = current.upper()
        out = []
        for g in games:
            if needle and needle not in g.team and needle not in g.opponent:
                continue
            vs = "vs" if g.is_home else "at"
            out.append(app_commands.Choice(
                name=f"{g.team} ({vs} {g.opponent} · "
                f"{kickoff_label(g.kickoff_ts, offset)})",
                value=g.team,
            ))
        return out[:25]

    @survivor.command(name="pick", description="Pick this week's team to win.")
    @app_commands.describe(team="Your team — leave empty for the pick panel.")
    @app_commands.autocomplete(team=_team_autocomplete)
    async def pick(
        self, interaction: discord.Interaction, team: str | None = None
    ) -> None:
        assert interaction.guild is not None
        season, offset = await self._season_and_offset(interaction.guild.id)
        if season is None:
            await interaction.response.send_message(
                "No Survivor season is running here. 🌾", ephemeral=True
            )
            return
        now = self._now()

        def _week_and_games():
            with open_db(self.ctx.db_path) as conn:
                week = logic.pick_week(conn, season["season_year"], now)
                games = (
                    logic.legal_teams(
                        conn, season, interaction.user.id, week, now
                    )
                    if week is not None
                    else []
                )
            return week, games

        week, games = await asyncio.to_thread(_week_and_games)
        if week is None:
            await interaction.response.send_message(
                "No games left to pick — the season is settling.",
                ephemeral=True,
            )
            return

        if team is not None:
            await submit_pick(
                interaction, season, interaction.user.id, week,
                team.strip().upper(), edit=False,
            )
            return
        if not games:
            await interaction.response.send_message(
                "Nothing legal left this week — every open team is burned "
                "or playing. Rare, survivable, and week "
                f"{week + 1} awaits.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"week {week} — pick a team to **win**. locks at each game's "
            "kickoff; secret until Tuesday.",
            view=PickPanel(
                self.bot, season, interaction.user.id, week, games, offset
            ),
            ephemeral=True,
        )

    # ── /survivor status ──────────────────────────────────────────────

    @survivor.command(name="status", description="Your pick, satchel, and strikes.")
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        season, _ = await self._season_and_offset(interaction.guild.id)
        if season is None:
            await interaction.response.send_message(
                "No Survivor season is running here. 🌾", ephemeral=True
            )
            return
        now = self._now()

        def _q():
            with open_db(self.ctx.db_path) as conn:
                return logic.player_status(conn, season, interaction.user.id, now)

        st = await asyncio.to_thread(_q)
        if st is None:
            await interaction.response.send_message(
                "You're not in this season — the Join button in the "
                "announcement is the door. 🌾",
                ephemeral=True,
            )
            return
        color = await resolve_accent_color(self.ctx.db_path, interaction.guild)
        await interaction.response.send_message(
            embed=build_status_embed(st, season_name=season["name"], color=color),
            ephemeral=True,
        )

    # ── /survivor board ───────────────────────────────────────────────

    @survivor.command(name="board", description="The living, the dead, the pot.")
    async def board(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        season, _ = await self._season_and_offset(interaction.guild.id)
        if season is None:
            await interaction.response.send_message(
                "No Survivor season is running here. 🌾", ephemeral=True
            )
            return
        now = self._now()

        def _q():
            with open_db(self.ctx.db_path) as conn:
                return logic.board_data(conn, season, now)

        board = await asyncio.to_thread(_q)
        guild = interaction.guild

        def name_of(user_id: int) -> str:
            member = guild.get_member(user_id)
            return member.display_name if member else f"soul {user_id}"

        color = await resolve_accent_color(self.ctx.db_path, guild)
        await interaction.response.send_message(
            embed=build_board_embed(
                board,
                name_of,
                season_name=season["name"],
                strikes_allowed=int(season["config"]["strikes"]),
                color=color,
            )
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(SurvivorCog(bot, bot.ctx))
