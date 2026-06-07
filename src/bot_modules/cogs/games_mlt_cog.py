import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
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
    resolve_names,
    ConfirmCloseView,
)
from bot_modules.games.utils.question_source import get_mlt_prompt
from bot_modules.games_mlt.embeds import (
    build_closed_embed,
    build_join_embed,
    build_results_embed,
    build_round_embed,
)
from bot_modules.games_mlt.logic import (
    MIN_PLAYERS,
    add_player,
    apply_vote,
    bump_crowns,
    can_start,
    encode_round_votes,
    find_round_winners,
    is_eligible_voter,
    pop_next_prompt,
    queue_prompt,
    remove_player,
    tally_votes,
)

log = logging.getLogger(__name__)


class MLTJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="mlt_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        log.info("%s joined game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        add_player(players, interaction.user.id)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host", names
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined the pool!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="mlt_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        log.info("%s left game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.setdefault("players", [])
        remove_player(players, interaction.user.id)
        await update_game_payload(self.db, self.game_id, payload)

        guild = interaction.guild
        names = resolve_names(guild, players)
        host_member = guild.get_member(self.host_id) if guild else None
        embed = build_join_embed(
            host_member.display_name if host_member else "Host", names
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've left the pool.", ephemeral=True)

    @discord.ui.button(label="Start Game", style=discord.ButtonStyle.primary, custom_id="mlt_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if not can_start(players):
            await interaction.response.send_message(
                f"❌ Need at least {MIN_PLAYERS} players to start!", ephemeral=True
            )
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        # Ping joined players
        if interaction.guild:
            mentions = [
                interaction.guild.get_member(uid).mention
                for uid in players
                if interaction.guild.get_member(uid)
            ]
            if mentions:
                await interaction.channel.send(
                    f"👑 **Most Likely To is starting!** {' '.join(mentions)} — get ready!",
                    delete_after=15,
                )

        await self.cog._run_round(
            interaction=interaction,
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=interaction.user.display_name,
            round_num=1,
            players=players,
            channel=interaction.channel,
            custom_prompt=payload.get("opening_prompt"),
        )

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="mlt_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Game cancelled.", view=self)
        await end_game(self.db, self.game_id)
        if self.game_id in self.bot.active_views:
            del self.bot.active_views[self.game_id]

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="mlt_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class PoseMLTModal(discord.ui.Modal, title="Pose a Prompt"):
    prompt = discord.ui.TextInput(
        label="Most likely to...",
        placeholder="e.g. win a staring contest",
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, view, message: discord.Message):
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted '%s' modal in #%s", interaction.user.display_name, "Pose a Prompt", interaction.channel.name if interaction.channel else "unknown")
        if self._view._closed:
            await interaction.response.send_message("This round already ended.", ephemeral=True)
            return
        count = queue_prompt(self._view.queued_prompts, self.prompt.value)
        self._view.next_btn.label = f"⏭️ Next ({count} queued)"
        try:
            await self._message.edit(view=self._view)
        except Exception:
            pass
        await interaction.response.send_message("✅ Your prompt has been queued!", ephemeral=True)


class MLTVoteView(discord.ui.View):
    def __init__(
        self,
        game_id: str,
        host_id: int,
        prompt: str,
        round_num: int,
        players: list[int],
        db,
        bot,
        host_name: str,
        guild,
        advance_callback,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.prompt = prompt
        self.round_num = round_num
        self.players = players
        self.db = db
        self.bot = bot
        self.host_name = host_name
        self.guild = guild
        self.advance_callback = advance_callback
        self.votes: dict[int, int] = {}
        self._closed = False
        self.queued_prompts: list[str] = []

        options = []
        for uid in players:
            member = guild.get_member(uid) if guild else None
            name = member.display_name if member else str(uid)
            options.append(discord.SelectOption(label=name, value=str(uid)))
        self.select = discord.ui.Select(
            placeholder="🗳️ Vote: Select a player",
            options=options,
            custom_id="mlt_vote_select",
        )
        self.select.callback = self._vote_select_callback
        self.add_item(self.select)

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def _vote_select_callback(self, interaction: discord.Interaction):
        log.info("%s voted in game %s in #%s", interaction.user.display_name, self.game_id, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        if not is_eligible_voter(interaction.user.id, self.players):
            await interaction.response.send_message("You're not in the player pool.", ephemeral=True)
            return
        target_id = int(interaction.data["values"][0])
        changed = apply_vote(self.votes, interaction.user.id, target_id)
        member = self.guild.get_member(target_id) if self.guild else None
        name = member.display_name if member else str(target_id)
        msg = f"✅ Voted for **{name}**{' (changed)' if changed else ''}"
        await interaction.response.send_message(msg, ephemeral=True)

    def _build_embed(self, closed=False) -> discord.Embed:
        return build_round_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            vote_count=len(self.votes),
            closed=closed,
        )

    def _build_results_embed(self, tally: dict) -> discord.Embed:
        return build_results_embed(
            prompt=self.prompt,
            round_num=self.round_num,
            tally=tally,
            guild=self.guild,
        )

    @discord.ui.button(label="✍️ Pose Prompt", style=discord.ButtonStyle.primary, custom_id="mlt_pose", row=1)
    async def pose_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if self._closed:
            await interaction.response.send_message("This round is over.", ephemeral=True)
            return
        await interaction.response.send_modal(PoseMLTModal(self, interaction.message))

    @discord.ui.button(label="⏭️ Next", style=discord.ButtonStyle.secondary, custom_id="mlt_next", row=1)
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

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="mlt_close", row=2)
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = interaction.message

        async def _confirmed(confirm_interaction):
            self._closed = True
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                embed = build_closed_embed(
                    prompt=self.prompt,
                    round_num=self.round_num,
                    vote_count=len(self.votes),
                )
                await game_msg.edit(embed=embed, view=self)
            except Exception:
                pass
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="mlt_htp2", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["mlt"], ephemeral=True)


class MLTCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="mlt", description="Start a Most Likely To game!")
    @app_commands.describe(
        question="Opening prompt (e.g. 'win a staring contest') — defaults to question bank",
    )
    async def mlt(
        self,
        interaction: discord.Interaction,
        question: str = "",
    ):
        log.info("%s used /games play mlt in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "mlt", interaction.guild_id or 0):
            await interaction.response.send_message("Most Likely To is currently disabled on this server.", ephemeral=True)
            return
        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "mlt",
            state="joining",
            payload={"opening_prompt": question.strip() or None, "rounds": {}, "crowns": {}, "players": []},
        )

        log.info("Game %s (mlt) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        embed = build_join_embed(interaction.user.display_name, [])
        view = MLTJoinView(game_id, interaction.user.id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
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
        players: list[int],
        channel,
        custom_prompt: str | None = None,
        carry_over_queue: list[str] | None = None,
    ):
        if custom_prompt:
            prompt = custom_prompt
        else:
            prompt = await get_mlt_prompt(self.db)
        if not prompt:
            await channel.send(
                "❌ The prompt bank is empty! Use **✍️ Pose Prompt** to submit your own, "
                "or ask an admin to add prompts with `/bank add`."
            )
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            return

        payload = await get_game_payload(self.db, game_id)
        rounds_data = payload.setdefault("rounds", {})
        rounds_data[str(round_num)] = {"votes": {}, "prompt": prompt}
        await update_game_payload(self.db, game_id, payload)

        async def advance(message: discord.Message):
            if view._closed:
                return
            view._closed = True

            tally = tally_votes(view.votes, players)

            results_embed = view._build_results_embed(tally)
            for item in view.children:
                item.disabled = True
            try:
                await message.edit(embed=view._build_embed(closed=True), view=view)
            except Exception:
                pass
            await channel.send(embed=results_embed)

            if await is_game_expired(self.db, game_id):
                await end_game(self.db, game_id)
                if game_id in self.bot.active_views:
                    del self.bot.active_views[game_id]
                return

            payload = await get_game_payload(self.db, game_id)
            crowns = payload.setdefault("crowns", {})
            bump_crowns(crowns, find_round_winners(tally))
            payload["rounds"][str(round_num)]["votes"] = encode_round_votes(view.votes)
            await update_game_payload(self.db, game_id, payload)

            next_custom, remaining = pop_next_prompt(view.queued_prompts)
            try:
                await self._run_round(
                    interaction=interaction,
                    game_id=game_id,
                    host_id=host_id,
                    host_name=host_name,
                    round_num=round_num + 1,
                    players=players,
                    channel=channel,
                    custom_prompt=next_custom,
                    carry_over_queue=remaining if remaining else None,
                )
            except Exception:
                log.exception("Error advancing MLT game %s to round %d", game_id, round_num + 1)
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                try:
                    await channel.send("❌ Something went wrong advancing the round. Game ended.")
                except Exception:
                    pass

        guild = channel.guild if hasattr(channel, "guild") else None
        view = MLTVoteView(
            game_id=game_id,
            host_id=host_id,
            prompt=prompt,
            round_num=round_num,
            players=players,
            db=self.db,
            bot=self.bot,
            host_name=host_name,
            guild=guild,
            advance_callback=advance,
        )
        if carry_over_queue:
            view.queued_prompts = carry_over_queue
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
    cog = MLTCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("mlt")
    play.add_command(cog.mlt)
