"""Survivor cog — member self-service only (spec §2, stage 3).

Thin glue over ``survivor/logic.py``: `/survivor pick|status|board` plus the
persistent Join button — the feature's ENTIRE command footprint. Everything
admin, including manual settle and the Reckoning preview, lives on the
dashboard (decided 2026-08-17/18); this cog deliberately registers no admin
commands.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.services.survivor_service import (
    get_active_season,
    panel_ids,
    set_panel_ids,
)
from bot_modules.survivor import logic
from bot_modules.survivor.embeds import (
    build_board_embed,
    build_status_embed,
)
from bot_modules.survivor.views import (
    HistoryButton,
    JoinSeasonButton,
    PickPanel,
    SlatePickButton,
    build_live_panel,
    kickoff_label,
    panel_view,
    submit_pick,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.survivor")


class SurvivorCog(commands.Cog):
    """NFL pick'em survival pool — the member surface."""

    survivor = app_commands.Group(
        name="survivor",
        description="NFL pick'em survival pool — one team a week, no team twice.",
        guild_only=True,
    )

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        # The channel panel is sticky (Billy, 2026-08-18: "the game panel
        # doesn't drop to the bottom") — chat under it reposts it below,
        # debounced by the house machinery. restick_on_bot because the
        # bot's own posts (Reckoning, last call) are the panel's main
        # buriers in a dedicated Survivor channel; the machinery's placed
        # registry and at-bottom check stop it chasing the Wednesday
        # repost_panel, which stays separate (it carries the ping content
        # and the pin, which sticky placements don't).
        self.panel = StickyPanel(
            "survivor panel",
            bot,
            load_ids=self._read_panel_ids,
            save_ids=self._write_panel_ids,
            build=self._build_panel,
            restick_on_bot=True,
        )

    async def cog_unload(self) -> None:
        self.panel.cancel_all()

    # ── sticky plumbing ────────────────────────────────────────────────

    def _read_panel_ids(self, guild_id: int) -> tuple[int, int]:
        with open_db(self.bot.ctx.db_path) as conn:
            return panel_ids(conn, guild_id)

    def _write_panel_ids(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        with open_db(self.bot.ctx.db_path) as conn:
            set_panel_ids(conn, guild_id, channel_id, message_id)
            conn.commit()

    async def _build_panel(self, guild: discord.Guild) -> PanelContent:
        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                return get_active_season(conn, guild.id)

        season = await asyncio.to_thread(_q)
        built = (
            await build_live_panel(self.bot, self.bot.ctx.db_path, season["id"])
            if season is not None else None
        )
        if built is None:
            # Season ended between the trigger and the debounce — the ids
            # read (0, 0) on the next pass, so this is a one-off, not a loop.
            raise RuntimeError("no live season to restick")
        _season, embed, join_open = built
        return PanelContent(
            embed=embed, view=panel_view(_season["id"], join_open=join_open)
        )

    @commands.Cog.listener("on_message")
    async def _sticky_on_message(self, message: discord.Message) -> None:
        await self.panel.on_message(message)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _sticky_on_channel_delete(self, channel) -> None:
        await self.panel.on_channel_delete(channel)

    async def cog_load(self) -> None:
        # The Join button on the pinned announcement and the slate's pick
        # button must survive restarts.
        self.bot.add_dynamic_items(JoinSeasonButton)
        self.bot.add_dynamic_items(SlatePickButton)
        self.bot.add_dynamic_items(HistoryButton)
        # The §4.2 poll/settle loop: 10-min cadence inside game windows,
        # one daily full refresh, settle sweep after any ingest.
        from bot_modules.services.survivor_loop import survivor_poll_loop

        bot = self.bot
        db_path = self.bot.ctx.db_path
        self.bot.startup_task_factories.append(
            lambda: survivor_poll_loop(bot, db_path)
        )

    # ── helpers ───────────────────────────────────────────────────────

    async def _season_and_offset(self, guild_id: int) -> tuple[dict | None, float]:
        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
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
            with open_db(self.bot.ctx.db_path) as conn:
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
                "No Survivor season is running here.", ephemeral=True
            )
            return
        now = self._now()

        def _week_and_games():
            with open_db(self.bot.ctx.db_path) as conn:
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
                "No eligible team left this week — everything still to play "
                f"is already used. You survive the week; Week {week + 1} "
                "is next.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Week {week} — pick a team to **win**. Locks at that game's "
            "kickoff; hidden until the results post.",
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
                "No Survivor season is running here.", ephemeral=True
            )
            return
        now = self._now()

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                return logic.player_status(conn, season, interaction.user.id, now)

        st = await asyncio.to_thread(_q)
        if st is None:
            await interaction.response.send_message(
                "You're not in this season — use the Join button on the "
                "season post.",
                ephemeral=True,
            )
            return
        color = await safe_resolve_accent(self.bot.ctx, interaction.guild, log_label="survivor")
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
                "No Survivor season is running here.", ephemeral=True
            )
            return
        now = self._now()

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                from bot_modules.services.economy_service import (
                    load_econ_settings,
                )

                return (
                    logic.board_data(conn, season, now),
                    load_econ_settings(conn, season["guild_id"]),
                )

        board, settings = await asyncio.to_thread(_q)
        guild = interaction.guild

        def name_of(user_id: int) -> str:
            member = guild.get_member(user_id)
            if member is None:
                return f"soul {user_id}"
            # Style guide: member text is escaped before it enters an embed —
            # a name like "**everyone** ·" must not reformat the board.
            return discord.utils.escape_markdown(member.display_name)

        color = await safe_resolve_accent(self.bot.ctx, guild, log_label="survivor")
        await interaction.response.send_message(
            embed=build_board_embed(
                board,
                name_of,
                season_name=season["name"],
                strikes_allowed=int(season["config"]["strikes"]),
                settings=settings,
                color=color,
            )
        )


    # ── /survivor history ─────────────────────────────────────────────

    @survivor.command(
        name="history", description="Revealed picks only — nothing current leaks."
    )
    @app_commands.describe(member="Whose road to read (default: yours).")
    async def history(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        assert interaction.guild is not None
        season, _ = await self._season_and_offset(interaction.guild.id)
        if season is None:
            await interaction.response.send_message(
                "No Survivor season is running here.", ephemeral=True
            )
            return
        target = member or interaction.user
        revealed = int(season["config"].get("last_reckoned_week") or 0)

        def _q():
            with open_db(self.bot.ctx.db_path) as conn:
                return logic.history_rows(
                    conn, season, target.id, through_week=revealed
                )

        rows = await asyncio.to_thread(_q)
        if not rows:
            await interaction.response.send_message(
                f"Nothing revealed for {discord.utils.escape_markdown(target.display_name)} "
                "yet — picks appear here after the weekly results post.",
                ephemeral=True,
            )
            return
        from bot_modules.survivor.embeds import build_history_embed

        color = await safe_resolve_accent(self.bot.ctx, interaction.guild, log_label="survivor")
        await interaction.response.send_message(
            embed=build_history_embed(
                rows,
                display_name=discord.utils.escape_markdown(target.display_name),
                revealed_week=revealed,
                own=False,
                color=color,
            )
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(SurvivorCog(bot))
