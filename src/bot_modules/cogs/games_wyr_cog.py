import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import GAME_ICONS, HOW_TO_PLAY, PHASE_PLAYING, PHASE_RESULTS, PHASE_RECAP
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    end_game,
    update_session,
    is_game_expired,
    ConfirmCloseView,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater, build_bar
from bot_modules.games.utils.question_source import get_wyr_question

log = logging.getLogger(__name__)


def build_wyr_embed(
    host_name: str,
    option_a: str,
    option_b: str,
    votes_a: list,
    votes_b: list,
    anonymous: bool,
    round_num: int,
    closed: bool = False,
    revealed: bool = False,
) -> discord.Embed:
    total = len(votes_a) + len(votes_b)
    bar_a, pct_a = build_bar(len(votes_a), total)
    bar_b, pct_b = build_bar(len(votes_b), total)

    title = f"{GAME_ICONS['wyr']} WOULD YOU RATHER"
    if closed:
        title += " — ROUND OVER"
    embed = discord.Embed(title=title, color=PHASE_RESULTS if closed else PHASE_PLAYING)
    embed.add_field(name="Round", value=str(round_num), inline=False)
    esc = discord.utils.escape_markdown
    embed.add_field(name="🅰️", value=esc(option_a), inline=True)
    embed.add_field(name="🅱️", value=esc(option_b), inline=True)
    embed.add_field(name="​", value="​", inline=True)

    a_label = f"🅰️ {bar_a} {pct_a} ({len(votes_a)})"
    b_label = f"🅱️ {bar_b} {pct_b} ({len(votes_b)})"

    if revealed:
        a_names = ", ".join(f"<@{uid}>" for uid in votes_a) if votes_a else "—"
        b_names = ", ".join(f"<@{uid}>" for uid in votes_b) if votes_b else "—"
        a_label += f"\n{a_names}"
        b_label += f"\n{b_names}"

    embed.add_field(name="Votes", value=f"{a_label}\n{b_label}", inline=False)
    anon_badge = "  •  👁 Anonymous" if anonymous else ""
    embed.set_footer(text=f"{GAME_ICONS['wyr']} Would You Rather  •  Round {round_num}{anon_badge}")
    return embed


class PoseWYRModal(discord.ui.Modal, title="Pose a Question"):
    option_a = discord.ui.TextInput(
        label="Option A",
        placeholder="e.g. fly",
        style=discord.TextStyle.short,
        max_length=200,
    )
    option_b = discord.ui.TextInput(
        label="Option B",
        placeholder="e.g. be invisible",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Question", interaction.channel.name if interaction.channel else "unknown")
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        a = self.option_a.value.strip()
        b = self.option_b.value.strip()
        if not a or not b:
            await interaction.response.send_message("Both options are required.", ephemeral=True)
            return
        self._view.queued_questions.append((a, b))
        count = len(self._view.queued_questions)
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except Exception:
            pass
        await interaction.response.send_message("✅ Your question has been queued!", ephemeral=True)


class WYRRoundView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        option_a: str,
        option_b: str,
        round_num: int,
        anonymous: bool,
        db,
        bot,
        host_name: str,
        advance_callback,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.option_a = option_a
        self.option_b = option_b
        self.round_num = round_num
        self.anonymous = anonymous
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.advance_callback = advance_callback
        self.votes_a: list[int] = []
        self.votes_b: list[int] = []
        self.revealed = False
        self._updater = LiveBarUpdater()
        self._closed = False
        self.queued_questions: list[tuple[str, str]] = []

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_wyr_embed(
            self.host_name,
            self.option_a,
            self.option_b,
            self.votes_a,
            self.votes_b,
            self.anonymous,
            self.round_num,
            closed=closed,
            revealed=self.revealed,
        )

    @discord.ui.button(label="🅰️ Option A", style=discord.ButtonStyle.primary, custom_id="wyr_a", row=0)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        uid = interaction.user.id
        changed = uid in self.votes_b
        if changed:
            self.votes_b.remove(uid)
        if uid not in self.votes_a:
            self.votes_a.append(uid)
        msg = f"✅ Voted **🅰️ Option A**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="🅱️ Option B", style=discord.ButtonStyle.primary, custom_id="wyr_b", row=0)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        uid = interaction.user.id
        changed = uid in self.votes_a
        if changed:
            self.votes_a.remove(uid)
        if uid not in self.votes_b:
            self.votes_b.append(uid)
        msg = f"✅ Voted **🅱️ Option B**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="✍️ Pose Question", style=discord.ButtonStyle.primary, custom_id="wyr_pose", row=1)
    async def pose_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PoseWYRModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="wyr_next", row=1)
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

    @discord.ui.button(label="👀 Reveal Voters", style=discord.ButtonStyle.secondary, custom_id="wyr_reveal", row=2)
    async def reveal_voters(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can reveal voters.", ephemeral=True)
            return
        self.revealed = True
        button.disabled = True
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="wyr_close", row=2)
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
                embed.title = f"{GAME_ICONS['wyr']} WOULD YOU RATHER — CLOSED"
                embed.colour = PHASE_RECAP
                await game_msg.edit(embed=embed, view=self)
            except Exception:
                pass
            payload = await get_game_payload(self.db, self.game_id)
            payload["rounds"][str(self.round_num)]["a"] = self.votes_a
            payload["rounds"][str(self.round_num)]["b"] = self.votes_b
            await update_game_payload(self.db, self.game_id, payload)
            await end_game(self.db, self.game_id, round_count=self.round_num, payload=payload)
            self.bot.active_views.pop(self.game_id, None)
            await channel.send("🛑 Game ended by host.")

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="wyr_htp", row=3)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["wyr"], ephemeral=True)


class WYRCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="wyr", description="Start a Would You Rather game!")
    @app_commands.describe(
        question="Opening question (format: 'option A | option B') — defaults to question bank",
    )
    async def wyr(
        self,
        interaction: discord.Interaction,
        question: str = "",
    ):
        log.info("%s used /wyr in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/config allow-channel`.",
                ephemeral=True,
            )
            return

        custom_question: tuple[str, str] | None = None
        if question.strip():
            parts = question.split("|", 1)
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
                await interaction.response.send_message(
                    "❌ Question must have two options separated by `|`, e.g. `fly | be invisible`.",
                    ephemeral=True,
                )
                return
            custom_question = (parts[0].strip(), parts[1].strip())

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "wyr",
            state="playing",
            payload={"anonymous": True, "rounds": {}},
        )
        log.info("Game %s (wyr) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")

        await interaction.response.defer()
        try:
            await self._run_round(
                interaction=interaction,
                game_id=game_id,
                host_id=interaction.user.id,
                host_name=interaction.user.display_name,
                round_num=1,
                channel=interaction.channel,
                custom_question=custom_question,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            if game_id in self.bot.active_views:
                del self.bot.active_views[game_id]
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

    async def _run_round(
        self,
        interaction,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        custom_question: tuple[str, str] | None = None,
        carry_over_queue: list[tuple[str, str]] | None = None,
    ):
        if custom_question:
            option_a, option_b = custom_question
        else:
            question = await get_wyr_question(self.db)
            if not question:
                await channel.send(
                    "❌ The question bank is empty! Use **✍️ Pose Question** to submit your own, "
                    "or ask an admin to add questions with `/bank add`."
                )
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                return
            option_a, option_b = question

        payload = await get_game_payload(self.db, game_id)
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {"a": [], "b": [], "q": f"{option_a} OR {option_b}"}
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
            payload["rounds"][str(round_num)]["a"] = view.votes_a
            payload["rounds"][str(round_num)]["b"] = view.votes_b
            await update_game_payload(self.db, game_id, payload)

            remaining = list(view.queued_questions)
            next_custom = remaining.pop(0) if remaining else None
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    channel=channel,
                    custom_question=next_custom,
                    carry_over_queue=remaining if remaining else None,
                )
            except Exception:
                log.exception("Error advancing WYR game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except Exception:
                    pass

        view = WYRRoundView(
            game_id=game_id,
            host_id=host_id,
            option_a=option_a,
            option_b=option_b,
            round_num=round_num,
            anonymous=True,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            advance_callback=advance,
        )
        if carry_over_queue:
            view.queued_questions = carry_over_queue
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
    await bot.add_cog(WYRCog(bot))
