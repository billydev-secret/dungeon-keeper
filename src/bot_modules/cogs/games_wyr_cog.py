import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.command_groups import play
from bot_modules.games.constants import HOW_TO_PLAY
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
    channel_name,
)
from bot_modules.games.utils.live_bar import LiveBarUpdater
from bot_modules.games.utils.question_source import (
    get_wyr_question,
    has_matching_questions,
    channel_allows_nsfw,
)
from bot_modules.games_wyr.embeds import build_wyr_embed
from bot_modules.games_wyr.logic import (
    next_button_label,
    parse_question_input,
    toggle_vote,
)

log = logging.getLogger(__name__)

# Cap the player-submitted question queue to prevent flooding.
_MAX_QUEUED_QUESTIONS = 15


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
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Question", channel_name(interaction.channel))
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        a = self.option_a.value.strip()
        b = self.option_b.value.strip()
        if not a or not b:
            await interaction.response.send_message("Both options are required.", ephemeral=True)
            return
        if len(self._view.queued_questions) >= _MAX_QUEUED_QUESTIONS:
            await interaction.response.send_message(
                f"The question queue is full ({_MAX_QUEUED_QUESTIONS}). Let some play first!",
                ephemeral=True,
            )
            return
        self._view.queued_questions.append((a, b))
        count = len(self._view.queued_questions)
        self._view.next_btn.label = next_button_label(count)
        try:
            await self._message.edit(view=self._view)
        except discord.HTTPException:
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
        if interaction.guild and isinstance(interaction.user, discord.Member):
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
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        changed = toggle_vote(self.votes_a, self.votes_b, interaction.user.id, "a")
        msg = f"✅ Voted **🅰️ Option A**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="🅱️ Option B", style=discord.ButtonStyle.primary, custom_id="wyr_b", row=0)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        changed = toggle_vote(self.votes_a, self.votes_b, interaction.user.id, "b")
        msg = f"✅ Voted **🅱️ Option B**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=3)
        await self._updater.schedule_update(interaction.message, self._build_embed)

    @discord.ui.button(label="✍️ Pose Question", style=discord.ButtonStyle.primary, custom_id="wyr_pose", row=1)
    async def pose_question(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PoseWYRModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="wyr_next", row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can reveal voters.", ephemeral=True)
            return
        self.revealed = True
        button.disabled = True
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="wyr_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["wyr"], ephemeral=True)


class WYRCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="wyr", description="Start a Would You Rather game!")
    @app_commands.describe(
        question="Opening question (format: 'option A | option B') — defaults to question bank",
        tags="Comma-separated tags to filter the question bank",
    )
    async def wyr(
        self,
        interaction: discord.Interaction,
        question: str = "",
        tags: str = "",
    ):
        log.info("%s used /wyr in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "wyr", interaction.guild_id or 0):
            await interaction.response.send_message("Would You Rather is currently disabled on this server.", ephemeral=True)
            return

        if question.strip() and parse_question_input(question) is None:
            await interaction.response.send_message(
                "❌ Question must have two options separated by `|`, e.g. `fly | be invisible`.",
                ephemeral=True,
            )
            return

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list and not question.strip() and not await has_matching_questions(
            self.db, "wyr", tag_list, allow_nsfw=channel_allows_nsfw(interaction.channel)
        ):
            await interaction.response.send_message(
                f"No questions match tags: {', '.join(tag_list)} for this game.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"question": question, "tags": tag_list},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I don't have access to send messages in that channel. "
                    "Please grant me **View Channel**, **Send Messages**, and **Embed Links**.",
                    ephemeral=True,
                )
            except discord.HTTPException:
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
        custom_question: tuple[str, str] | None = None
        if question:
            custom_question = parse_question_input(question)
            if custom_question is None:
                log.warning("WYR launch: invalid question %r ignored", question)

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "wyr",
            state="playing",
            payload={"anonymous": True, "rounds": {}, "tags": options.get("tags") or []},
        )
        log.info("Game %s (wyr) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        try:
            await self._run_round(
                interaction=None,
                game_id=game_id,
                host_id=host_id,
                host_name=host_name,
                round_num=1,
                channel=channel,
                custom_question=custom_question,
            )
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("WYR launch lacked send perms in channel %s", channel.id)
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
        custom_question: tuple[str, str] | None = None,
        carry_over_queue: list[tuple[str, str]] | None = None,
    ):
        if custom_question:
            option_a, option_b = custom_question
        else:
            tags = (await get_game_payload(self.db, game_id)).get("tags") or None
            question = await get_wyr_question(
                self.db, tags=tags, allow_nsfw=channel_allows_nsfw(channel)
            )
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

        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=round_num,
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=interaction,
        )
        if carry_over_queue:
            view.queued_questions = carry_over_queue
            view.next_btn.label = next_button_label(len(carry_over_queue))
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

    def _build_round_view(
        self,
        *,
        game_id: str,
        host_id: int,
        host_name: str,
        round_num: int,
        channel,
        option_a: str,
        option_b: str,
        anonymous: bool = True,
        interaction=None,
    ) -> "WYRRoundView":
        """Construct a round view with its advance callback wired.

        Shared by _run_round (fresh round) and recover_game (post-restart) so
        round-to-round advancement behaves identically after a crash.
        """

        async def advance(message: discord.Message):
            if view._closed:
                return
            view._closed = True

            final_embed = view._build_embed(closed=True)
            for item in view.children:
                item.disabled = True
            try:
                await message.edit(embed=final_embed, view=view)
            except discord.HTTPException:
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
                except discord.HTTPException:
                    pass

        view = WYRRoundView(
            game_id=game_id,
            host_id=host_id,
            option_a=option_a,
            option_b=option_b,
            round_num=round_num,
            anonymous=anonymous,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            advance_callback=advance,
        )
        return view

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Rebuild the current round's view after a restart, restoring votes."""
        rounds = payload.get("rounds", {})
        if not rounds:
            return False
        cur = max(rounds, key=lambda k: int(k))
        rd = rounds.get(cur, {})
        q = rd.get("q", "") or ""
        option_a, option_b = (q.split(" OR ", 1) + [""])[:2] if " OR " in q else (q, "")

        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"

        view = self._build_round_view(
            game_id=game_id,
            host_id=host_id,
            host_name=host_name,
            round_num=int(cur),
            channel=channel,
            option_a=option_a,
            option_b=option_b,
            anonymous=payload.get("anonymous", True),
            interaction=None,
        )
        view.votes_a = list(rd.get("a", []))
        view.votes_b = list(rd.get("b", []))
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered wyr game %s (round %s) in #%s", game_id, cur, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = WYRCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("wyr")
    play.add_command(cog.wyr, override=True)
    bot.game_launchers["wyr"] = cog.launch
    bot.game_recoverers["wyr"] = cog.recover_game
