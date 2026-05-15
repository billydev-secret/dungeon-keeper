import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
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
from bot_modules.games.utils.live_bar import LiveBarUpdater, build_bar
from bot_modules.games.utils.question_source import get_nhie_statement

log = logging.getLogger(__name__)

DEFAULT_LIVES = 3


def build_nhie_embed(
    host_name: str,
    statement: str,
    guilty: list,
    innocent: list,
    round_num: int,
    closed: bool = False,
    lives: dict[int, int] | None = None,
    eliminated: set[int] | None = None,
    guild=None,
    max_lives: int = DEFAULT_LIVES,
) -> discord.Embed:
    total = len(guilty) + len(innocent)
    bar_g, pct_g = build_bar(len(guilty), total)
    bar_i, pct_i = build_bar(len(innocent), total)

    title = f"{GAME_ICONS['nhie']} NEVER HAVE I EVER"
    if closed:
        title += " — ROUND OVER"
    embed = discord.Embed(title=title, color=PHASE_RESULTS if closed else PHASE_PLAYING)
    embed.add_field(name="Round", value=str(round_num), inline=False)
    embed.add_field(name="Statement", value=discord.utils.escape_markdown(statement), inline=False)
    embed.add_field(
        name="Votes",
        value=(
            f"😈 {bar_g} {pct_g} ({len(guilty)})\n"
            f"😇 {bar_i} {pct_i} ({len(innocent)})"
        ),
        inline=False,
    )

    # Lives display
    if lives:
        alive_lines = []
        elim = eliminated or set()
        for uid, hp in sorted(lives.items(), key=lambda x: -x[1]):
            if uid in elim:
                continue
            name = resolve_name(guild, uid) if guild else str(uid)
            hearts = "❤️" * hp + "🖤" * (max_lives - hp)
            alive_lines.append(f"{hearts} **{discord.utils.escape_markdown(name)}**")
        if alive_lines:
            embed.add_field(name=f"Still Standing ({len(alive_lines)})", value="\n".join(alive_lines), inline=False)

        elim_names = []
        for uid in elim:
            name = resolve_name(guild, uid) if guild else str(uid)
            elim_names.append(discord.utils.escape_markdown(name))
        if elim_names:
            embed.add_field(name="💀 Eliminated", value=", ".join(elim_names), inline=False)

    embed.set_footer(text=f"{GAME_ICONS['nhie']} Never Have I Ever • Round {round_num}")
    return embed


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
        return build_nhie_embed(
            self.host_name,
            self.statement,
            self.guilty,
            self.innocent,
            self.round_num,
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
        changed = uid in self.innocent
        if changed:
            self.innocent.remove(uid)
        if uid not in self.guilty:
            self.guilty.append(uid)
        # Track this player in lives if not already
        if self.max_lives > 0 and uid not in self.lives:
            self.lives[uid] = self.max_lives
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
        changed = uid in self.guilty
        if changed:
            self.guilty.remove(uid)
        if uid not in self.innocent:
            self.innocent.append(uid)
        if self.max_lives > 0 and uid not in self.lives:
            self.lives[uid] = self.max_lives
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
                embed = self._build_embed(closed=True)
                embed.title = f"{GAME_ICONS['nhie']} NEVER HAVE I EVER — CLOSED"
                embed.colour = PHASE_RECAP
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
        log.info("%s used /nhie in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "nhie", interaction.guild_id or 0):
            await interaction.response.send_message("Never Have I Ever is currently disabled on this server.", ephemeral=True)
            return

        lives = max(0, min(lives, 10))

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "nhie",
            state="playing",
            payload={"rounds": {}, "guilt_scores": {}, "lives": {}, "eliminated": [], "max_lives": lives},
        )
        log.info("Game %s (nhie) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        await interaction.response.defer()
        await self._run_round(
            interaction=interaction,
            game_id=game_id,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            round_num=1,
            channel=interaction.channel,
            guild=interaction.guild,
            custom_statement=question.strip() or None,
            max_lives=lives,
        )
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

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
            lives_raw = payload.get("lives", {})
            lives = {int(k): v for k, v in lives_raw.items()}
            eliminated = set(int(x) for x in payload.get("eliminated", []))
            max_lives = payload.get("max_lives", DEFAULT_LIVES)
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
            for uid in view.guilty:
                guilt_scores[str(uid)] = guilt_scores.get(str(uid), 0) + 1

            # Deduct lives from guilty voters
            current_lives = {int(k): v for k, v in payload.get("lives", {}).items()}
            current_eliminated = set(int(x) for x in payload.get("eliminated", []))
            newly_eliminated = []

            if max_lives > 0:
                for uid in view.guilty:
                    if uid in current_eliminated:
                        continue
                    if uid not in current_lives:
                        current_lives[uid] = max_lives
                    current_lives[uid] -= 1
                    if current_lives[uid] <= 0:
                        current_eliminated.add(uid)
                        newly_eliminated.append(uid)
                # Also add innocent voters to lives tracker if not present
                for uid in view.innocent:
                    if uid not in current_lives and uid not in current_eliminated:
                        current_lives[uid] = max_lives

            payload["lives"] = {str(k): v for k, v in current_lives.items()}
            payload["eliminated"] = [str(x) for x in current_eliminated]
            await update_game_payload(self.db, game_id, payload)

            # Announce eliminations
            for uid in newly_eliminated:
                name = resolve_name(guild, uid)
                try:
                    await channel.send(f"💀 **{discord.utils.escape_markdown(name)}** has been eliminated!")
                except Exception:
                    pass

            # Check for winner (1 or fewer players remaining)
            if max_lives > 0:
                alive = [uid for uid, hp in current_lives.items() if hp > 0 and uid not in current_eliminated]
                if len(alive) <= 1:
                    if alive:
                        winner_name = resolve_name(guild, alive[0])
                        try:
                            embed = discord.Embed(
                                title=f"{GAME_ICONS['nhie']} NEVER HAVE I EVER — GAME OVER",
                                description=f"🏆 **{discord.utils.escape_markdown(winner_name)}** is the last one standing!",
                                color=PHASE_RECAP,
                            )
                            embed.add_field(
                                name="Final Guilt Scores",
                                value="\n".join(
                                    f"**{resolve_name(guild, int(uid))}** — {score} guilty votes"
                                    for uid, score in sorted(guilt_scores.items(), key=lambda x: -x[1])
                                ) or "—",
                                inline=False,
                            )
                            await channel.send(embed=embed)
                        except Exception:
                            pass
                    else:
                        try:
                            await channel.send(f"{GAME_ICONS['nhie']} Everyone's been eliminated! No winner this time.")
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
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            try:
                await interaction.followup.send(
                    "❌ I don't have permission to send messages in that channel. "
                    "Please grant me **Send Messages** and **Embed Links** permissions.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return
        await update_game_message(self.db, game_id, msg.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(NHIECog(bot))
