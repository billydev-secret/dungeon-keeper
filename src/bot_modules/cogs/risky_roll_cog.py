from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.risky_roll import state as rr_state
from bot_modules.services.risky_roll.formatters import build_embed
from bot_modules.services.risky_roll.logic import (
    collect_channel_state_ids,
    normalize_auto_close_options,
)
from bot_modules.services.risky_roll.models import RiskyRollState
from bot_modules.services.risky_roll.store import MAX_GAMES_PER_CHANNEL, StateStore
from bot_modules.services.risky_roll.views import (
    QuestionReplyView,
    RiskyRollView,
    SixtyNineQuestionView,
    disable_pending_question_message,
    disable_round_message,
    schedule_auto_close,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.risky_roll")


class RiskyRollCog(commands.Cog):
    risky = app_commands.Group(name="risky", description="Risky Rolls game commands")

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        rr_state.store = StateStore(self.ctx.db_path)

        swept = await rr_state.store.sweep_old_posted_questions()
        if swept:
            log.info("Swept %d old posted questions.", swept)

        ping_roles, min_times, active_rounds, pending_questions, posted_questions = await asyncio.gather(
            rr_state.store.load_ping_roles(),
            rr_state.store.load_min_game_times(),
            rr_state.store.load_active_rounds(),
            rr_state.store.load_pending_questions(),
            rr_state.store.load_posted_questions(),
        )
        rr_state.ping_roles.update(ping_roles)
        rr_state.min_game_seconds.update(min_times)

        now = time.time()
        for state in active_rounds:
            rr_state.active_games[state.game_id] = state
            if state.message_id is not None:
                self.bot.add_view(RiskyRollView(state.game_id), message_id=state.message_id)
            if state.auto_close_minutes and state.auto_close_minutes > 0:
                elapsed = now - state.created_at
                delay = max(0.0, state.auto_close_minutes * 60 - elapsed)
                rr_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                    schedule_auto_close(self.bot, state.game_id, delay)
                )

        for pq in pending_questions:
            rr_state.pending_questions[pq.game_id] = pq
            if pq.prompt_message_id is not None:
                self.bot.add_view(SixtyNineQuestionView(pq.game_id), message_id=pq.prompt_message_id)

        for posted in posted_questions:
            rr_state.posted_questions[posted.message_id] = posted
            self.bot.add_view(QuestionReplyView(), message_id=posted.message_id)

        log.info(
            "Risky Rolls loaded: %d active, %d pending, %d posted",
            len(active_rounds), len(pending_questions), len(posted_questions),
        )

    async def cog_unload(self) -> None:
        tasks = list(rr_state.auto_close_tasks.values())
        rr_state.auto_close_tasks.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        rr_state.active_games.clear()
        rr_state.pending_questions.clear()
        rr_state.posted_questions.clear()
        rr_state.ping_roles.clear()
        rr_state.min_game_seconds.clear()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You do not have permission to use that command."
        else:
            log.exception("Unhandled risky command error", exc_info=error)
            msg = "The command failed. Check the bot logs for details."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    # Game commands
    # ------------------------------------------------------------------

    @risky.command(name="start", description="Open a new Risky Rolls round in this channel")
    @app_commands.describe(
        auto_close_players="Auto-close when this many players have rolled (default 25)",
        auto_close_minutes="Auto-close after this many minutes (default 120)",
    )
    async def risky_start(
        self,
        interaction: discord.Interaction,
        auto_close_players: int | None = 25,
        auto_close_minutes: int | None = 120,
    ) -> None:
        await self._start_game(
            interaction,
            auto_close_players=auto_close_players,
            auto_close_minutes=auto_close_minutes,
            ping=True,
            skip_min_game_time=False,
        )

    @risky.command(
        name="start_no_ping",
        description="Open a new round without pinging and without a minimum game time",
    )
    @app_commands.describe(
        auto_close_players="Auto-close when this many players have rolled (default 25)",
        auto_close_minutes="Auto-close after this many minutes (default 120)",
    )
    async def risky_start_no_ping(
        self,
        interaction: discord.Interaction,
        auto_close_players: int | None = 25,
        auto_close_minutes: int | None = 120,
    ) -> None:
        await self._start_game(
            interaction,
            auto_close_players=auto_close_players,
            auto_close_minutes=auto_close_minutes,
            ping=False,
            skip_min_game_time=True,
        )

    async def _start_game(
        self,
        interaction: discord.Interaction,
        auto_close_players: int | None,
        auto_close_minutes: int | None,
        ping: bool,
        skip_min_game_time: bool,
    ) -> None:
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message(
                "This command can only be used in a server channel.", ephemeral=True
            )
            return

        me = interaction.guild.me
        perms = interaction.channel.permissions_for(me)
        missing = [
            name for allowed, name in [
                (perms.send_messages, "Send Messages"),
                (perms.embed_links, "Embed Links"),
            ]
            if not allowed
        ]
        if missing:
            await interaction.response.send_message(
                f"I'm missing permissions in this channel: {', '.join(missing)}. "
                "Please fix my permissions before starting a round.",
                ephemeral=True,
            )
            return

        async with rr_state.get_channel_lock(interaction.channel.id):
            active_in_channel = sum(
                1 for s in rr_state.active_games.values()
                if s.channel_id == interaction.channel.id
            )
            if active_in_channel >= MAX_GAMES_PER_CHANNEL:
                await interaction.response.send_message(
                    f"This channel already has {MAX_GAMES_PER_CHANNEL} active games. "
                    "Close one before starting another.",
                    ephemeral=True,
                )
                return

            normalized_players, normalized_minutes = normalize_auto_close_options(
                auto_close_players, auto_close_minutes
            )
            state = RiskyRollState(
                channel_id=interaction.channel.id,
                guild_id=interaction.guild.id,
                opener_id=interaction.user.id,
                auto_close_players=normalized_players,
                auto_close_minutes=normalized_minutes,
                skip_min_game_time=skip_min_game_time,
            )
            rr_state.active_games[state.game_id] = state

            content = None
            allowed_mentions = discord.AllowedMentions.none()

            if ping:
                role_id = rr_state.ping_roles.get(interaction.guild.id)
                if role_id:
                    content = f"# <@&{role_id}> A new Risky Rolls round has begun!"
                    allowed_mentions = discord.AllowedMentions(roles=True)

            view = RiskyRollView(state.game_id)
            try:
                await interaction.response.send_message(
                    content=content,
                    embed=build_embed(state),
                    view=view,
                    allowed_mentions=allowed_mentions,
                )
                message = await interaction.original_response()
                state.message_id = message.id
                if rr_state.store is not None:
                    await rr_state.store.save_round(state)

                if state.auto_close_minutes:
                    rr_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                        schedule_auto_close(interaction.client, state.game_id, state.auto_close_minutes * 60)
                    )
            except Exception:
                rr_state.active_games.pop(state.game_id, None)
                if rr_state.store is not None:
                    await rr_state.store.delete_round(state.game_id)
                state.is_open = False

                if interaction.response.is_done():
                    try:
                        message = await interaction.original_response()
                    except (discord.NotFound, discord.HTTPException):
                        pass
                    else:
                        failed_view = RiskyRollView(state.game_id)
                        failed_view.disable_all_items()
                        try:
                            await message.edit(
                                content="Risky Rolls could not finish setup. Start a new round.",
                                embed=build_embed(state),
                                view=failed_view,
                                allowed_mentions=discord.AllowedMentions.none(),
                            )
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                            pass
                raise

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch for the scheduler. Returns game_id, or None."""
        me = channel.guild.me
        perms = channel.permissions_for(me)
        if not (perms.send_messages and perms.embed_links):
            log.warning("risky_roll launch: missing perms in channel %s", channel.id)
            return None

        auto_close_players = options.get("auto_close_players", 25)
        auto_close_minutes = options.get("auto_close_minutes", 120)

        async with rr_state.get_channel_lock(channel.id):
            active_in_channel = sum(
                1 for s in rr_state.active_games.values()
                if s.channel_id == channel.id
            )
            if active_in_channel >= 1:
                log.warning(
                    "risky_roll launch: channel %s already has an active round", channel.id
                )
                return None

            normalized_players, normalized_minutes = normalize_auto_close_options(
                auto_close_players, auto_close_minutes
            )
            state = RiskyRollState(
                channel_id=channel.id,
                guild_id=guild_id,
                opener_id=host_id,
                auto_close_players=normalized_players,
                auto_close_minutes=normalized_minutes,
                skip_min_game_time=True,
            )
            rr_state.active_games[state.game_id] = state

            view = RiskyRollView(state.game_id)
            try:
                msg = await channel.send(
                    embed=build_embed(state),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden:
                rr_state.active_games.pop(state.game_id, None)
                log.warning("risky_roll launch: Forbidden in channel %s", channel.id)
                return None
            except Exception:
                rr_state.active_games.pop(state.game_id, None)
                log.exception("risky_roll launch: failed to send in channel %s", channel.id)
                return None

            state.message_id = msg.id
            if rr_state.store is not None:
                await rr_state.store.save_round(state)

            if state.auto_close_minutes:
                rr_state.auto_close_tasks[state.game_id] = asyncio.create_task(
                    schedule_auto_close(self.bot, state.game_id, state.auto_close_minutes * 60)
                )

        return state.game_id

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @risky.command(
        name="reset_state",
        description="Clear all active rounds and pending prompts in this channel",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def risky_reset_state(self, interaction: discord.Interaction) -> None:
        if interaction.channel is None:
            await interaction.response.send_message(
                "This command can only be used in a server channel.", ephemeral=True
            )
            return

        async with rr_state.get_channel_lock(interaction.channel.id):
            channel_id = interaction.channel.id

            game_ids, question_ids, posted_message_ids = collect_channel_state_ids(
                rr_state.active_games,
                rr_state.pending_questions,
                rr_state.posted_questions,
                channel_id,
            )

            if not game_ids and not question_ids and not posted_message_ids:
                await interaction.response.send_message(
                    "No active or pending Risky Rolls state was found in this channel.",
                    ephemeral=True,
                )
                return

            for game_id in game_ids:
                task = rr_state.auto_close_tasks.pop(game_id, None)
                if task:
                    task.cancel()
                state = rr_state.active_games.pop(game_id, None)
                if state is not None:
                    state.is_open = False
                    await disable_round_message(state, interaction.channel)
                if rr_state.store is not None:
                    await rr_state.store.delete_round(game_id)

            for game_id in question_ids:
                pending_state = rr_state.pending_questions.pop(game_id, None)
                if pending_state is not None:
                    await disable_pending_question_message(
                        interaction.client,
                        pending_state,
                        "The pending question prompt was cleared by an administrator.",
                    )
                if rr_state.store is not None:
                    await rr_state.store.delete_pending_question(game_id)

            for message_id in posted_message_ids:
                rr_state.posted_questions.pop(message_id, None)
                if rr_state.store is not None:
                    await rr_state.store.delete_posted_question(message_id)

            await interaction.response.send_message(
                "Reset the Risky Rolls state for this channel.", ephemeral=True
            )


async def setup(bot: Bot) -> None:
    cog = RiskyRollCog(bot, bot.ctx)
    await bot.add_cog(cog)
    bot.game_launchers["risky_roll"] = cog.launch  # type: ignore[attr-defined]
