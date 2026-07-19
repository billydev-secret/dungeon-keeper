"""Hot Potato (group) cog — pass-the-bomb for 2..N players with progressive elimination."""
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

from bot_modules.duels.base_game import BaseGame
from bot_modules.economy.game_rewards import pay_game_rewards
from bot_modules.games.command_groups import games
from bot_modules.services.embeds import COLOR_RED, COLOR_YELLOW

from . import db as hpgdb
from .game import (
    HotPotatoGroupGame,
    bravest,
    cumulative_hold_times,
    next_holder_clockwise,
    shake_emoji,
)
from .views import PassGroupView

log = logging.getLogger("dungeonkeeper.hot_potato_group")


class HotPotatoGroupGameCog(BaseGame, name="HotPotatoGroupCog"):

    GAME_KEY = "hot_potato_group"
    GAME_DISPLAY_NAME = "Hot Potato"

    def __init__(self, bot: Bot) -> None:
        super().__init__(bot)
        self._timers: dict[int, asyncio.Task] = {}

    hotpotatogroup = app_commands.Group(
        name="hotpotatogroup",
        description="Hot Potato for groups — pass the bomb before it blows!",
    )

    # ── DB hooks ──────────────────────────────────────────────────────────────

    async def _db_get_game(self, game_id: int) -> HotPotatoGroupGame | None:
        return await hpgdb.get_game(self.db, game_id)

    async def _db_set_state(self, game_id: int, state: str, **kw) -> None:
        await hpgdb.set_game_state(self.db, game_id, state, **kw)

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None
    ) -> int:
        return await hpgdb.create_lobby(self.db, guild_id, channel_id, host_id, stakes_text)

    async def _db_fetch_active_games(self) -> list[HotPotatoGroupGame]:
        return await hpgdb.fetch_active_games(self.db)

    async def _db_fetch_lobby_games(self) -> list[HotPotatoGroupGame]:
        return await hpgdb.fetch_lobby_games(self.db)

    async def _db_fetch_resolved_games(self) -> list[HotPotatoGroupGame]:
        return await hpgdb.fetch_resolved_games(self.db)

    async def _db_fetch_sweepable(self, now: float) -> list[HotPotatoGroupGame]:
        return await hpgdb.fetch_sweepable_games(self.db, now)

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int, float]:
        cfg = await hpgdb.get_config(self.db, guild_id)
        return int(cfg["min_players"]), int(cfg["max_players"]), float(cfg["lobby_timeout"])

    # ── Timer helpers ─────────────────────────────────────────────────────────

    def _cancel_timer(self, game_id: int) -> None:
        task = self._timers.pop(game_id, None)
        if task and not task.done():
            task.cancel()

    async def _run_detonate_timer(self, game_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._detonate(game_id)
        except asyncio.CancelledError:
            pass

    async def _detonate(self, game_id: int) -> None:
        """Fuse expired: current holder is eliminated. Re-round or resolve."""
        resolved = False
        async with self._get_lock(game_id):
            game = await hpgdb.get_game(self.db, game_id)
            if not game or game.state != "ACTIVE" or game.holder_id is None:
                return

            now = time.time()
            loser = game.holder_id

            new_log = list(game.pass_log)
            if new_log and new_log[-1].get("passed_at") is None:
                new_log[-1] = {**new_log[-1], "passed_at": now}

            new_alive = [u for u in game.alive if u != loser]
            new_elim = list(game.elimination_order) + [loser]

            if len(new_alive) <= 1:
                winner = new_alive[0] if new_alive else loser
                game.alive = new_alive
                game.elimination_order = new_elim
                game.pass_log = new_log
                await hpgdb.set_game_state(
                    self.db, game_id, "ACTIVE",
                    alive=json.dumps(new_alive),
                    elimination_order=json.dumps(new_elim),
                    pass_log=json.dumps(new_log),
                    last_action_at=now,
                )
                await self._post_group_result(game, winner, loser)
                await pay_game_rewards(
                    self.bot, game.guild_id, list(game.roster), [winner], self.GAME_KEY,
                    occurrence=str(game.id),
                )
                resolved = True
            else:
                cfg = await hpgdb.get_config(self.db, game.guild_id)
                fuse = random.uniform(cfg["min_fuse"], cfg["max_fuse"])
                next_holder = next_holder_clockwise(game.alive, loser)
                new_log.append(
                    {"holder_id": next_holder, "received_at": now, "passed_at": None}
                )
                await hpgdb.set_game_state(
                    self.db, game_id, "ACTIVE",
                    round=game.round + 1,
                    alive=json.dumps(new_alive),
                    elimination_order=json.dumps(new_elim),
                    holder_id=next_holder,
                    fuse_seconds=fuse,
                    phase_started_at=now,
                    pass_log=json.dumps(new_log),
                    last_action_at=now,
                )
                guild = self.bot.get_guild(game.guild_id)
                game2 = await hpgdb.get_game(self.db, game_id)
                if guild and game2 and game2.message_id:
                    await self._edit_message_silent(
                        game2.channel_id, game2.message_id,
                        self.render_game_state(game2, guild),
                        self.build_game_view(game_id),
                    )
                channel = self.bot.get_channel(game.channel_id)
                if guild and channel:
                    lm = guild.get_member(loser)
                    nh = guild.get_member(next_holder)
                    ln = lm.display_name if lm else str(loser)
                    nn = nh.display_name if nh else str(next_holder)
                    try:
                        await channel.send(  # type: ignore[union-attr]
                            f"💥 BOOM! **{ln}** is out. ({len(new_alive)} left) "
                            f"🔁 **{nn}** is holding now."
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        pass
                task = asyncio.create_task(self._run_detonate_timer(game_id, fuse))
                self._timers[game_id] = task

        if resolved:
            self._cancel_timer(game_id)
            self._game_locks.pop(game_id, None)

    # ── Game hooks ────────────────────────────────────────────────────────────

    async def on_game_start(self, game: HotPotatoGroupGame) -> None:
        cfg = await hpgdb.get_config(self.db, game.guild_id)
        fuse = random.uniform(cfg["min_fuse"], cfg["max_fuse"])
        now = time.time()
        holder = random.choice(game.alive)
        init_log = json.dumps([{"holder_id": holder, "received_at": now, "passed_at": None}])
        await hpgdb.set_game_state(
            self.db, game.id, "ACTIVE",
            round=1,
            holder_id=holder,
            fuse_seconds=fuse,
            phase_started_at=now,
            pass_log=init_log,
            last_action_at=now,
        )
        task = asyncio.create_task(self._run_detonate_timer(game.id, fuse))
        self._timers[game.id] = task

    async def on_game_resume(self, game: HotPotatoGroupGame) -> None:
        if not game.phase_started_at or not game.fuse_seconds:
            asyncio.create_task(self._detonate(game.id))
            return
        now = time.time()
        remaining = (game.phase_started_at + game.fuse_seconds) - now

        channel = self.bot.get_channel(game.channel_id)
        if channel:
            mentions = " ".join(f"<@{u}>" for u in game.alive)
            try:
                await channel.send(  # type: ignore[union-attr]
                    f"🔄 Bot restarted — Hot Potato resuming. {mentions}"
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

        if remaining <= 0:
            asyncio.create_task(self._detonate(game.id))
        else:
            task = asyncio.create_task(self._run_detonate_timer(game.id, remaining))
            self._timers[game.id] = task

    async def on_game_resolved(self, game_id: int) -> None:
        self._cancel_timer(game_id)

    def render_game_state(
        self, game: HotPotatoGroupGame, guild: discord.Guild
    ) -> discord.Embed:
        def name(uid: int) -> str:
            m = guild.get_member(uid)
            return m.display_name if m else str(uid)

        alive_names = ", ".join(name(u) for u in game.alive) or "—"
        holder = name(game.holder_id) if game.holder_id else "?"

        elapsed = max(0.0, time.time() - game.phase_started_at) if game.phase_started_at else 0.0
        emoji = shake_emoji(elapsed, game.fuse_seconds or 0.0)

        embed = discord.Embed(title=f"{emoji} HOT POTATO", color=COLOR_YELLOW)
        embed.add_field(name="Still in", value=alive_names, inline=False)
        if game.elimination_order:
            embed.add_field(
                name="Out", value=", ".join(name(u) for u in game.elimination_order), inline=False
            )
        embed.add_field(
            name="🤲 Holding", value=f"**{holder}** — pass it before it blows!", inline=False
        )
        stakes = game.stakes_text or "Final loser surrenders their nickname for 24h."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        return embed

    def render_result_state(
        self,
        game: HotPotatoGroupGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
        original_name: str | None = None,
        **_kwargs,
    ) -> discord.Embed:
        def name(uid: int | None) -> str:
            if uid is None:
                return "?"
            m = guild.get_member(uid)
            return m.display_name if m else str(uid)

        winner_name = name(game.winner_id)
        loser_name = name(game.loser_id)

        embed = discord.Embed(
            title="💥 Hot Potato — Game Over",
            description=f"**{loser_name}** was holding the final blast!",
            color=COLOR_RED,
        )
        embed.add_field(name="🏆 Winner", value=winner_name, inline=True)
        embed.add_field(name="💀 Final loser", value=loser_name, inline=True)
        embed.add_field(name="🥔 Players", value=str(len(game.roster)), inline=True)

        end_ts = game.resolved_at or time.time()
        holds = cumulative_hold_times(game.pass_log, end_ts)
        bid = bravest(holds)
        if bid is not None:
            embed.add_field(
                name="🫡 Bravest hands",
                value=f"**{name(bid)}** held {holds[bid]:.0f}s total",
                inline=False,
            )

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

    def build_game_view(self, game_id: int) -> PassGroupView:
        return PassGroupView(game_id, self._handle_group_button)

    async def handle_interaction(
        self, interaction: discord.Interaction, game: HotPotatoGroupGame
    ) -> tuple[str, int | None]:
        uid = interaction.user.id

        if uid not in game.alive:
            await interaction.followup.send(
                "You're not in this game (or you're already out).", ephemeral=True
            )
            return ("rejected", None)
        if uid != game.holder_id:
            await interaction.followup.send("You're not holding the bomb!", ephemeral=True)
            return ("rejected", None)

        cfg = await hpgdb.get_config(self.db, game.guild_id)
        now = time.time()
        if game.pass_log:
            last = game.pass_log[-1]
            if last.get("holder_id") == uid and last.get("received_at") is not None:
                if now - last["received_at"] < cfg["min_hold"]:
                    await interaction.followup.send(
                        f"Hold it a moment — you can pass after {cfg['min_hold']:.0f}s.",
                        ephemeral=True,
                    )
                    return ("rejected", None)

        new_holder = next_holder_clockwise(game.alive, uid)
        new_log = list(game.pass_log)
        if new_log and new_log[-1].get("passed_at") is None:
            new_log[-1] = {**new_log[-1], "passed_at": now}
        new_log.append({"holder_id": new_holder, "received_at": now, "passed_at": None})

        await hpgdb.set_game_state(
            self.db, game.id, "ACTIVE",
            holder_id=new_holder,
            pass_log=json.dumps(new_log),
            last_action_at=now,
        )
        game.holder_id = new_holder
        game.pass_log = new_log
        return ("continue", None)

    # ── Slash commands ────────────────────────────────────────────────────────

    @hotpotatogroup.command(name="start", description="Open a Hot Potato lobby")
    @app_commands.describe(stakes="Optional custom stakes text (max 200 chars)")
    async def hpg_start(
        self, interaction: discord.Interaction, stakes: str | None = None
    ) -> None:
        await self._base_lobby(interaction, stakes)

async def setup(bot: Bot) -> None:
    cog = HotPotatoGroupGameCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("hotpotatogroup")
    games.add_command(cog.hotpotatogroup)
