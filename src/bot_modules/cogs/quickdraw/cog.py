"""Quickdraw cog — slash commands and BaseDuel implementation."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

import asyncio
import logging
import random
import time

import discord
from discord import app_commands

from bot_modules.core.branding import resolve_accent_color
from bot_modules.duels import db as duels_db
from bot_modules.duels.base_duel import BaseDuel
from bot_modules.games.command_groups import games
from bot_modules.services.embeds import COLOR_GOLD, COLOR_GREEN, COLOR_RED, COLOR_YELLOW

from . import db as qdb
from .game import QuickdrawGame
from .views import FireView

log = logging.getLogger("dungeonkeeper.quickdraw")


class QuickdrawDuel(BaseDuel, name="QuickdrawCog"):

    GAME_KEY = "quickdraw"
    GAME_DISPLAY_NAME = "Quickdraw"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}

    quickdraw = app_commands.Group(
        name="quickdraw",
        description="Quickdraw — first to react wins, false-starters lose",
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
        return await qdb.create_game(
            self.db, guild_id, channel_id, challenger_id, target_id, stakes_text
        )

    async def _db_get_game(self, game_id: int) -> QuickdrawGame | None:
        return await qdb.get_game(self.db, game_id)

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> QuickdrawGame | None:
        return await qdb.get_active_game_for_pair(self.db, guild_id, user_a, user_b)

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> QuickdrawGame | None:
        return await qdb.get_pending_game_for_challenger(self.db, guild_id, channel_id, user_id)

    async def _db_set_state(self, game_id: int, state: str, **kw) -> None:
        await qdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_fetch_active_games(self) -> list[QuickdrawGame]:
        return await qdb.fetch_active_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[QuickdrawGame]:
        return await qdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[QuickdrawGame]:
        return await qdb.fetch_sweepable_games(self.db, now)

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_game_timer(
        self, game_id: int, delay: float, draw_window: float
    ) -> None:
        """Sleep `delay` seconds then fire the draw signal."""
        try:
            await asyncio.sleep(delay)
            await self._fire_draw(game_id, draw_window)
        except asyncio.CancelledError:
            pass

    async def _run_drawwindow_timer(self, game_id: int, draw_window: float) -> None:
        """Sleep `draw_window` seconds then void if nobody fired."""
        try:
            await asyncio.sleep(draw_window)
            await self._fire_void(game_id)
        except asyncio.CancelledError:
            pass

    async def _fire_draw(self, game_id: int, draw_window: float) -> None:
        """Transition WAITING → DRAW; edit message to show draw signal."""
        fired_at = time.time()
        async with self._get_lock(game_id):
            game = await qdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.qd_state != "WAITING":
                return

            await qdb.set_game_state(
                self.db, game_id, "ACTIVE",
                qd_state="DRAW",
                fired_at=fired_at,
                last_action_at=fired_at,
            )
            game = await qdb.get_game(self.db, game_id)
            if not game:
                return

            guild = self.bot.get_guild(game.guild_id)
            if guild:
                embed = self.render_game_state(game, guild)
                view = self.build_game_view(game_id)
                await self._edit_message_silent(game.channel_id, game.message_id, embed, view)

        # Start the draw window timer outside the lock (no await between lock
        # release and task creation, so no race with concurrent button presses)
        task = asyncio.create_task(self._run_drawwindow_timer(game_id, draw_window))
        self._timers[game_id] = task

    async def _fire_void(self, game_id: int) -> None:
        """Draw window expired. Void if nobody fired; if the winner fired but the
        opponent never did, resolve winner-only (no loser reaction time)."""
        async with self._get_lock(game_id):
            game = await qdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE":
                return

            guild = self.bot.get_guild(game.guild_id)

            if game.qd_state == "DRAW":
                # Nobody fired in time — void, no consequence.
                await qdb.set_game_state(self.db, game_id, "VOID")
                void_embed = discord.Embed(
                    title="🌵 Nobody Drew",
                    description=(
                        "Both players took too long to fire. "
                        "Round voided — no nickname penalty."
                    ),
                    color=COLOR_YELLOW,
                )
                if guild:
                    p1 = guild.get_member(game.challenger_id)
                    p2 = guild.get_member(game.target_id)
                    p1_name = p1.display_name if p1 else str(game.challenger_id)
                    p2_name = p2.display_name if p2 else str(game.target_id)
                    void_embed.description = (
                        f"**{p1_name}** vs **{p2_name}** — nobody fired in time. "
                        "Round voided — no nickname penalty."
                    )
                await self._edit_message_silent(
                    game.channel_id, game.message_id, void_embed, None
                )

            elif game.qd_state == "WINNER_FIRED":
                # Winner drew; the opponent never fired. Resolve with no loser
                # time (loser_fired_at stays NULL → result shows "didn't draw").
                await qdb.set_game_state(
                    self.db, game_id, "ACTIVE",
                    qd_state="COMPLETE",
                    last_action_at=time.time(),
                )
                game.qd_state = "COMPLETE"
                if guild:
                    dview = self.build_game_view(game_id)
                    dview.disable()
                    await self._edit_message_silent(
                        game.channel_id, game.message_id,
                        self.render_game_state(game, guild), dview,
                    )
                channel = self.bot.get_channel(game.channel_id)
                if channel is not None:
                    await self._finalize_result(
                        game, game.winner_id, game.loser_id,  # type: ignore[arg-type]
                        send=channel.send,  # type: ignore[union-attr]
                    )
            else:
                return

        self._cancel_timer(game_id)
        self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: QuickdrawGame) -> None:
        cfg = await qdb.get_config(self.db, game.guild_id)
        delay = random.uniform(cfg["min_delay"], cfg["max_delay"])
        draw_window = cfg["draw_window"]
        now = time.time()
        await qdb.set_game_state(
            self.db, game.id, "ACTIVE",
            qd_state="WAITING",
            draw_delay=delay,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_game_timer(game.id, delay, draw_window))
        self._timers[game.id] = task

    async def on_game_resume(self, game: QuickdrawGame) -> None:
        """Called on cog_load for each ACTIVE game. Resume timers."""
        cfg = await qdb.get_config(self.db, game.guild_id)
        draw_window = cfg["draw_window"]

        if game.qd_state == "WAITING":
            # Re-roll delay and restart from scratch; let players know
            delay = random.uniform(cfg["min_delay"], cfg["max_delay"])
            now = time.time()
            await qdb.set_game_state(
                self.db, game.id, "ACTIVE",
                draw_delay=delay,
                last_action_at=now,
            )
            task = asyncio.create_task(self._run_game_timer(game.id, delay, draw_window))
            self._timers[game.id] = task
            await self._send_restart_notice(game)

        elif game.qd_state == "DRAW":
            now = time.time()
            fired_at = game.fired_at or now
            remaining = fired_at + draw_window - now
            if remaining <= 0:
                # Window has already passed — void immediately
                asyncio.create_task(self._fire_void(game.id))
            else:
                task = asyncio.create_task(self._run_drawwindow_timer(game.id, remaining))
                self._timers[game.id] = task
                await self._send_restart_notice(game)

        elif game.qd_state == "WINNER_FIRED":
            # Restarted after the winner fired but before the opponent did. The
            # blind-reaction window can't be resumed fairly — resolve winner-only.
            asyncio.create_task(self._fire_void(game.id))

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)

    async def _send_restart_notice(self, game: QuickdrawGame) -> None:
        channel = self.bot.get_channel(game.channel_id)
        if not channel:
            return
        try:
            await channel.send(  # type: ignore[union-attr]
                f"🔄 Bot restarted — Quickdraw round resuming. "
                f"<@{game.challenger_id}> <@{game.target_id}>"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    def render_game_state(
        self, game: QuickdrawGame, guild: discord.Guild
    ) -> discord.Embed:
        p1 = guild.get_member(game.challenger_id)
        p2 = guild.get_member(game.target_id)
        p1_name = p1.display_name if p1 else str(game.challenger_id)
        p2_name = p2.display_name if p2 else str(game.target_id)

        if game.qd_state in ("DRAW", "WINNER_FIRED"):
            # WINNER_FIRED renders identically to DRAW: the first shooter has
            # already fired, but we don't reveal that — the opponent must stay
            # blind so their click is a genuine reaction we can time.
            embed = discord.Embed(
                title="🔫 DRAW!!!",
                description=(
                    f"**{p1_name}** vs **{p2_name}**\n\n"
                    "**FIRE NOW — first to click wins!**"
                ),
                color=COLOR_RED,
            )
        elif game.qd_state == "COMPLETE":
            winner = guild.get_member(game.winner_id)  # type: ignore[arg-type]
            loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
            w_name = winner.display_name if winner else str(game.winner_id)
            l_name = loser.display_name if loser else str(game.loser_id)
            if game.fired_at is None:
                # False start
                embed = discord.Embed(
                    title="💀 False Start!",
                    description=f"**{l_name}** fired before the signal!",
                    color=COLOR_RED,
                )
            else:
                # Clean draw
                embed = discord.Embed(
                    title="⚡ Draw!",
                    description=f"**{w_name}** fires first!",
                    color=COLOR_GREEN,
                )
        else:
            # WAITING
            embed = discord.Embed(
                title="🤠 Quickdraw",
                description=(
                    f"**{p1_name}** vs **{p2_name}**\n\n"
                    "⏳ Waiting for the draw signal... **Don't fire early!**"
                ),
                color=COLOR_GOLD,
            )

        stakes = game.stakes_text or "Loser surrenders their nickname."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        return embed

    def render_result_state(
        self,
        game: QuickdrawGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        winner = guild.get_member(game.winner_id)  # type: ignore[arg-type]
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        winner_name = winner.display_name if winner else str(game.winner_id)
        loser_name = loser.display_name if loser else str(game.loser_id)

        winner_field = winner_name
        loser_field = loser_name
        if game.fired_at is None:
            title = "💀 False Start!"
            desc = f"**{loser_name}** fired before the draw signal and lost."
        else:
            title = "⚡ Quickdraw!"
            desc = f"**{winner_name}** drew first!"
            w_react = (
                game.resolved_at - game.fired_at
                if game.resolved_at is not None
                else None
            )
            l_react = (
                game.loser_fired_at - game.fired_at
                if game.loser_fired_at is not None
                else None
            )
            if w_react is not None:
                winner_field = f"{winner_name} — **{w_react:.3f}s**"
            if l_react is not None:
                loser_field = f"{loser_name} — **{l_react:.3f}s**"
                if w_react is not None:
                    desc += f"\n⚡ Won by **{l_react - w_react:.3f}s**"
            else:
                loser_field = f"{loser_name} — *didn't draw*"

        embed = discord.Embed(title=title, description=desc, color=COLOR_RED)
        embed.add_field(name="🏆 Winner", value=winner_field, inline=True)
        embed.add_field(name="💀 Loser", value=loser_field, inline=True)

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
                    "The nickname lasts 24 hours."
                ),
                inline=False,
            )

        return embed

    def build_game_view(self, game_id: int) -> FireView:
        return FireView(game_id, self._handle_game_button)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: QuickdrawGame
    ) -> tuple[str, int | None]:
        player_id = interaction.user.id

        if player_id not in (game.challenger_id, game.target_id):
            await interaction.followup.send("You're not in this game.", ephemeral=True)
            return ("rejected", None)

        if game.qd_state == "WAITING":
            # False start — the presser loses
            loser_id = player_id
            winner_id = (
                game.challenger_id if player_id == game.target_id else game.target_id
            )
            now = time.time()
            await qdb.set_game_state(
                self.db, game.id, "ACTIVE",
                qd_state="COMPLETE",
                winner_id=winner_id,
                loser_id=loser_id,
                resolved_at=now,
                last_action_at=now,
            )
            game.winner_id = winner_id
            game.loser_id = loser_id
            game.qd_state = "COMPLETE"
            # game.fired_at remains None → signals false start to render

            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            view = self.build_game_view(game.id)
            view.disable()
            await interaction.edit_original_response(
                embed=self.render_game_state(game, guild), view=view
            )
            return ("done", loser_id)

        if game.qd_state == "DRAW":
            # First to click wins — but the round isn't over. Record the winner
            # and move to WINNER_FIRED so the opponent can still fire and log
            # their own reaction time for the result delta.
            winner_id = player_id
            loser_id = (
                game.challenger_id if player_id == game.target_id else game.target_id
            )
            now = time.time()
            await qdb.set_game_state(
                self.db, game.id, "ACTIVE",
                qd_state="WINNER_FIRED",
                winner_id=winner_id,
                loser_id=loser_id,
                resolved_at=now,
                last_action_at=now,
            )
            game.winner_id = winner_id
            game.loser_id = loser_id
            game.qd_state = "WINNER_FIRED"
            game.resolved_at = now
            # game.fired_at is already set (by _fire_draw) → signals clean draw

            reaction = now - game.fired_at if game.fired_at is not None else 0.0
            await interaction.followup.send(
                f"🔫 You drew in **{reaction:.3f}s**! "
                "Hang on — seeing if they can beat it...",
                ephemeral=True,
            )
            # "continue" keeps the FIRE button live (blind) for the opponent.
            return ("continue", None)

        if game.qd_state == "WINNER_FIRED":
            if player_id == game.winner_id:
                await interaction.followup.send(
                    "You already drew — waiting on your opponent.", ephemeral=True
                )
                return ("rejected", None)

            # Opponent fired second: record their reaction time and resolve.
            now = time.time()
            await qdb.set_game_state(
                self.db, game.id, "ACTIVE",
                qd_state="COMPLETE",
                loser_fired_at=now,
                last_action_at=now,
            )
            game.qd_state = "COMPLETE"
            game.loser_fired_at = now

            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            view = self.build_game_view(game.id)
            view.disable()
            await interaction.edit_original_response(
                embed=self.render_game_state(game, guild), view=view
            )
            return ("done", game.loser_id)

        # qd_state == "COMPLETE" — round already fully resolved
        await interaction.followup.send("Too slow — this round is already over.", ephemeral=True)
        return ("rejected", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @quickdraw.command(name="challenge", description="Challenge someone to a Quickdraw")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
    )
    async def quickdraw_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
    ) -> None:
        await self._base_challenge(interaction, user, stakes)

    @quickdraw.command(name="cancel", description="Cancel your pending Quickdraw challenge")
    async def quickdraw_cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        game = await qdb.get_pending_game_for_challenger(
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
        await qdb.set_game_state(self.db, game.id, "EXPIRED_PENDING")
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

    @quickdraw.command(name="stats", description="View Quickdraw stats")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def quickdraw_stats(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        stats = await qdb.get_stats(self.db, interaction.guild.id, target.id)
        accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
        embed = discord.Embed(
            title=f"🤠 Quickdraw — {target.display_name}",
            color=accent,
        )
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Losses", value=str(stats["losses"]), inline=True)
        embed.add_field(name="Total Games", value=str(stats["total_games"]), inline=True)
        await interaction.response.send_message(embed=embed)

    @quickdraw.command(name="revert", description="Request early revert of your Quickdraw nickname")
    async def quickdraw_revert(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        cfg = await duels_db.get_config(self.db, interaction.guild.id, self.GAME_KEY)
        if not cfg.get("allow_early_revert"):
            await interaction.response.send_message(
                "Early revert isn't enabled on this server. Ask a mod.", ephemeral=True
            )
            return
        nick = await duels_db.get_active_nick_for_user(
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
        await duels_db.mark_nick_reverted(self.db, nick["id"], "early_revert")
        await interaction.response.send_message(
            "Your nickname has been restored early.", ephemeral=True
        )

    @quickdraw.command(name="config", description="Configure Quickdraw (mods only)")
    @app_commands.describe(
        cooldown_hours="Hours before the same pair can play again (default 48)",
        sentence_hours="Hours the imposed nickname lasts (default 24)",
        allow_early_revert="Allow losers to request early nick revert: 0=no, 1=yes",
        min_delay="Minimum seconds before draw signal (default 3.0)",
        max_delay="Maximum seconds before draw signal (default 8.0)",
        draw_window="Seconds to fire after draw signal before void (default 5.0)",
    )
    async def quickdraw_config(
        self,
        interaction: discord.Interaction,
        cooldown_hours: int | None = None,
        sentence_hours: int | None = None,
        allow_early_revert: int | None = None,
        min_delay: float | None = None,
        max_delay: float | None = None,
        draw_window: float | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "You need the Manage Server permission to configure Quickdraw.",
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
        if min_delay is not None:
            game_updates["min_delay"] = max(0.5, min_delay)
        if max_delay is not None:
            game_updates["max_delay"] = max(1.0, max_delay)
        if draw_window is not None:
            game_updates["draw_window"] = max(1.0, draw_window)

        if not shared_updates and not game_updates:
            shared_cfg = await duels_db.get_config(self.db, interaction.guild.id, self.GAME_KEY)
            game_cfg = await qdb.get_config(self.db, interaction.guild.id)
            accent = await resolve_accent_color(self.bot.ctx.db_path, interaction.guild)
            embed = discord.Embed(title="🔧 Quickdraw Config", color=accent)
            for k, v in shared_cfg.items():
                if k not in ("guild_id", "game_type"):
                    embed.add_field(name=k, value=str(v), inline=True)
            for k, v in game_cfg.items():
                if k != "guild_id":
                    embed.add_field(name=k, value=str(v), inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if shared_updates:
            await duels_db.upsert_config(self.db, interaction.guild.id, self.GAME_KEY, **shared_updates)
        if game_updates:
            await qdb.upsert_config(self.db, interaction.guild.id, **game_updates)

        all_updates = {**shared_updates, **game_updates}
        lines = [f"**{k}** → `{v}`" for k, v in all_updates.items()]
        await interaction.response.send_message(
            "Config updated:\n" + "\n".join(lines), ephemeral=True
        )


async def setup(bot: Bot) -> None:
    cog = QuickdrawDuel(bot)
    await bot.add_cog(cog)
    for name in ("cancel", "revert", "stats", "config"):
        cog.quickdraw.remove_command(name)
    bot.tree.remove_command("quickdraw")
    games.add_command(cog.quickdraw)
