import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    end_game,
    update_session,
    is_game_expired,
    resolve_name,
    ConfirmCloseView,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.question_source import get_nhie_statement
from bot_modules.games_nhie.embeds import (
    build_closed_embed,
    build_recap_embed,
    build_round_embed,
)
from bot_modules.games_nhie.logic import (
    DEFAULT_LIVES,
    apply_round_lives,
    apply_vote,
    bump_guilt_scores,
    encode_round_state,
    find_winner,
    payload_to_round_state,
)

log = logging.getLogger(__name__)


class PoseStatementModal(discord.ui.Modal, title="Pose a Statement"):
    statement = discord.ui.TextInput(
        label="Never have I ever...",
        placeholder="e.g. gone skydiving",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Statement", interaction.channel.name if interaction.channel else "unknown")
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        self._view.queued_statements.append(self.statement.value.strip())
        count = len(self._view.queued_statements)
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except Exception:
            pass
        await interaction.response.send_message("✅ Your statement has been queued!", ephemeral=True)


class NHIERoundView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        statement: str,
        round_num: int,
        db,
        bot,
        host_name: str,
        advance_callback,
        lives: dict[int, int] | None = None,
        eliminated: set[int] | None = None,
        guild=None,
        max_lives: int = DEFAULT_LIVES,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.statement = statement
        self.round_num = round_num
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.guilty: list[int] = []
        self.innocent: list[int] = []
        self.queued_statements: list[str] = []
        self._updater = LiveBarUpdater()
        self._closed = False
        self.lives = lives or {}
        self.eliminated = eliminated or set()
        self.guild = guild
        self.max_lives = max_lives

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_round_embed(
            statement=self.statement,
            guilty=self.guilty,
            innocent=self.innocent,
            round_num=self.round_num,
            closed=closed,
            lives=self.lives,
            eliminated=self.eliminated,
            guild=self.guild,
            max_lives=self.max_lives,
        )

    @discord.ui.button(label="😈 Guilty", style=discord.ButtonStyle.danger, custom_id="nhie_guilty", row=0)
    async def vote_guilty(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in self.eliminated:
            await interaction.response.send_message("💀 You've been eliminated!", ephemeral=True)
            return
        changed = apply_vote(
            self.guilty, self.innocent, self.lives, uid, "guilty", self.max_lives
        )
        msg = f"✅ Voted **😈 Guilty**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="😇 Innocent", style=discord.ButtonStyle.success, custom_id="nhie_innocent", row=0)
    async def vote_innocent(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        uid = interaction.user.id
        if uid in self.eliminated:
            await interaction.response.send_message("💀 You've been eliminated!", ephemeral=True)
            return
        changed = apply_vote(
            self.guilty, self.innocent, self.lives, uid, "innocent", self.max_lives
        )
        msg = f"✅ Voted **😇 Innocent**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="✍️ Pose Statement", style=discord.ButtonStyle.primary, custom_id="nhie_pose", row=1)
    async def pose_statement(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PoseStatementModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="nhie_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        if self._closed:
            await interaction.response.send_message("This round is already over.", ephemeral=True)
            return
        await interaction.response.defer()
        await self.advance_callback(interaction.message)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="nhie_close", row=2)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = interaction.message
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self._closed = True
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                embed = build_closed_embed(
                    statement=self.statement,
                    guilty=self.guilty,
                    innocent=self.innocent,
                    round_num=self.round_num,
                    lives=self.lives,
                    eliminated=self.eliminated,
                    guild=self.guild,
                    max_lives=self.max_lives,
                )
                await game_msg.edit(embed=embed, view=self)
            except Exception:
                pass
            payload = await get_game_payload(self.db, self.game_id)
            payload["rounds"][str(self.round_num)]["guilty"] = self.guilty
            payload["rounds"][str(self.round_num)]["innocent"] = self.innocent
            await update_game_payload(self.db, self.game_id, payload)
            await end_game(self.db, self.game_id, round_count=self.round_num, payload=payload)
            self.bot.active_views.pop(self.game_id, None)
            await channel.send("🛑 Game ended by host.")

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="nhie_htp", row=3)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["nhie"], ephemeral=True)


class NHIECog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="nhie", description="Start a Never Have I Ever game!")
    @app_commands.describe(
        question="Opening statement (e.g. 'gone skydiving') — defaults to question bank",
        lives="Number of lives per player (default 3, 0 = no elimination)",
    )
    async def nhie(
        self,
        interaction: discord.Interaction,
        question: str = "",
        lives: int = DEFAULT_LIVES,
    ):
        log.info("%s used /games play nhie in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "nhie", interaction.guild_id or 0):
            await interaction.response.send_message("Never Have I Ever is currently disabled on this server.", ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"question": question, "lives": lives},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
                pass

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None."""
        question = (options.get("question") or "").strip()
        lives = max(0, min(int(options.get("lives", DEFAULT_LIVES)), 10))
        guild = getattr(channel, "guild", None)

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "nhie",
            state="playing",
            payload={"rounds": {}, "guilt_scores": {}, "lives": {}, "eliminated": [], "max_lives": lives},
        )
        log.info("Game %s (nhie) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        try:
            await self._run_round(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                round_num=1,
                channel=channel,
                guild=guild,
                custom_statement=question or None,
                max_lives=lives,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("nhie launch lacked send perms in channel %s", channel.id)
            return None
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    async def _run_round(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        guild,
        custom_statement: str | None = None,
        carry_over_queue: list[str] | None = None,
        lives: dict[int, int] | None = None,
        eliminated: set[int] | None = None,
        max_lives: int = DEFAULT_LIVES,
    ):
        if custom_statement:
            statement = custom_statement
        else:
            statement = await get_nhie_statement(self.db)
        if not statement:
            await channel.send(
                "❌ The statement bank is empty! Use **✍️ Pose Statement** to submit your own, "
                "or ask an admin to add statements with `/bank add`."
            )
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            return

        if lives is None:
            payload = await get_game_payload(self.db, game_id)
            lives, eliminated, max_lives = payload_to_round_state(payload)
        if eliminated is None:
            eliminated = set()

        payload = await get_game_payload(self.db, game_id)
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {"guilty": [], "innocent": [], "stmt": statement}
        await update_game_payload(self.db, game_id, payload)

        async def advance(message: discord.Message):
            if view._closed:
                return
            view._closed = True

            final_embed = view._build_embed(closed=True)
            for item in view.children:
                item.disabled = True
            try:
                await message.edit(embed=final_embed, view=view)
            except Exception:
                pass

            if await is_game_expired(self.db, game_id):
                await end_game(self.db, game_id)
                if game_id in self.bot.active_views:
                    del self.bot.active_views[game_id]
                return

            payload = await get_game_payload(self.db, game_id)
            payload["rounds"][str(round_num)]["guilty"] = view.guilty
            payload["rounds"][str(round_num)]["innocent"] = view.innocent
            guilt_scores = payload.setdefault("guilt_scores", {})
            bump_guilt_scores(guilt_scores, view.guilty)

            current_lives, current_eliminated, _ = payload_to_round_state(payload)
            newly_eliminated = apply_round_lives(
                current_lives,
                current_eliminated,
                view.guilty,
                view.innocent,
                max_lives,
            )

            lives_serialized, eliminated_serialized = encode_round_state(
                current_lives, current_eliminated
            )
            payload["lives"] = lives_serialized
            payload["eliminated"] = eliminated_serialized
            await update_game_payload(self.db, game_id, payload)

            # Announce eliminations
            for uid in newly_eliminated:
                name = resolve_name(guild, uid)
                try:
                    await channel.send(f"💀 **{discord.utils.escape_markdown(name)}** has been eliminated!")
                except Exception:
                    pass

            if max_lives > 0:
                status, winner_id = find_winner(current_lives, current_eliminated)
                if status != "continue":
                    try:
                        if status == "winner":
                            embed = build_recap_embed(
                                winner_id=winner_id,
                                guilt_scores=guilt_scores,
                                guild=guild,
                            )
                            await channel.send(embed=embed)
                        else:
                            await channel.send(
                                f"{GAME_ICONS['nhie']} Everyone's been eliminated! No winner this time."
                            )
                    except Exception:
                        pass
                    await end_game(self.db, game_id, player_count=len(current_lives), round_count=round_num, payload=payload)
                    if game_id in self.bot.active_views:
                        del self.bot.active_views[game_id]
                    return

            remaining = list(view.queued_statements)
            next_custom = remaining.pop(0) if remaining else None
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    channel=channel,
                    guild=guild,
                    custom_statement=next_custom,
                    carry_over_queue=remaining if remaining else None,
                    lives=current_lives,
                    eliminated=current_eliminated,
                    max_lives=max_lives,
                )
            except Exception:
                log.exception("Error advancing NHIE game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except Exception:
                    pass

        view = NHIERoundView(
            game_id=game_id,
            host_id=host_id,
            statement=statement,
            round_num=round_num,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            advance_callback=advance,
            lives=lives,
            eliminated=eliminated,
            guild=guild,
            max_lives=max_lives,
        )
        if carry_over_queue:
            view.queued_statements = carry_over_queue
            count = len(carry_over_queue)
            view.next_btn.label = f"⏭️ Next ({count} queued)"
        self.bot.active_views[game_id] = view

        embed = view._build_embed()
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            # Clean up and let the caller (slash wrapper / scheduler) report the failure.
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            raise
        await update_game_message(self.db, game_id, msg.id)


async def setup(bot: commands.Bot):
    cog = NHIECog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("nhie")
    play.add_command(cog.nhie)
    bot.game_launchers["nhie"] = cog.launch
