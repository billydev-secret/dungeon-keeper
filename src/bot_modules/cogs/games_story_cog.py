import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
    resolve_name,
    resolve_names,
)
from bot_modules.games_story.embeds import (
    build_attribution_embed,
    build_complete_story_embed,
    build_lobby_embed,
    build_turn_embed,
)
from bot_modules.games_story.logic import (
    add_player,
    append_sentence,
    assemble_story_text,
    build_attribution_lines,
    build_context,
    build_turn_order,
    chunk_attribution_lines,
    clamp_max_sentences,
    pick_current_player,
    remove_player,
    resolve_starter,
    should_end_after_skip,
)

log = logging.getLogger(__name__)

_TURN_TIMEOUT = 300  # seconds per turn


class StorySentenceModal(discord.ui.Modal, title="Add Your Sentence"):
    context_field = discord.ui.TextInput(
        label="Context (for reference)",
        style=discord.TextStyle.paragraph,
        required=False,
    )
    sentence = discord.ui.TextInput(
        label="Your Sentence",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Continue the story...",
    )

    def __init__(self, game_id: str, player_id: int, context_text: str = ""):
        super().__init__()
        self.game_id = game_id
        self.player_id = player_id
        self._submitted = False
        self._value: str | None = None
        if context_text:
            self.context_field._underlying.value = context_text[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted story sentence in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        self._submitted = True
        self._value = self.sentence.value
        await interaction.response.send_message("✅ Your sentence has been added!", ephemeral=True)


class StoryTurnView(discord.ui.View):
    """Per-turn view with Write and Skip buttons."""

    def __init__(self, game_id: str, host_id: int, current_player_id: int, context_text: str, db, bot):
        super().__init__(timeout=_TURN_TIMEOUT)
        self.game_id = game_id
        self.host_id = host_id
        self.current_player_id = current_player_id
        self.context_text = context_text
        self.db = db
        self.bot = bot
        self._submitted_event = asyncio.Event()
        self._submitted_text: str | None = None
        self._skipped = False

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="✍️ Write Your Sentence", style=discord.ButtonStyle.primary, custom_id="story_write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if interaction.user.id != self.current_player_id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return
        modal = StorySentenceModal(self.game_id, self.current_player_id, self.context_text)
        await interaction.response.send_modal(modal)
        timed_out = await modal.wait()
        if modal._submitted and modal._value:
            self._submitted_text = modal._value
            self._submitted_event.set()
            self.stop()
        elif timed_out:
            # Modal was closed or timed out without submitting — unblock the loop
            self._skipped = True
            self._submitted_event.set()
            self.stop()

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="story_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can skip.", ephemeral=True)
            return
        self._skipped = True
        self._submitted_event.set()
        self.stop()
        await interaction.response.send_message("⏩ Player skipped.", ephemeral=True)

    @discord.ui.button(label="🛑 Close Game", style=discord.ButtonStyle.danger, custom_id="story_turn_close")
    async def close_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can close.", ephemeral=True)
            return
        game_msg = interaction.message
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self._skipped = True  # unblock the loop
            self._submitted_event.set()
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(view=self)
            except Exception:
                pass
            await end_game(self.db, self.game_id)
            if self.game_id in self.bot.active_views:
                del self.bot.active_views[self.game_id]
            await channel.send("🛑 Story ended by host.")

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message("⚠️ Are you sure you want to end this game?", view=view, ephemeral=True)


class StoryJoinView(discord.ui.View):
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

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="story_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id

        def _add(payload):
            add_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        log.info("%s joined game %s", interaction.user.display_name, self.game_id)

        players = payload.get("players", [])
        names = resolve_names(interaction.guild, players)
        embed = interaction.message.embeds[0]
        embed.set_field_at(0, name=f"Writers ({len(players)})", value=", ".join(names) or "—", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="story_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id

        def _remove(payload):
            remove_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        log.info("%s left game %s", interaction.user.display_name, self.game_id)

        players = payload.get("players", [])
        names = resolve_names(interaction.guild, players)
        embed = interaction.message.embeds[0]
        embed.set_field_at(0, name=f"Writers ({len(players)})", value=", ".join(names) or "—", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've left.", ephemeral=True)

    @discord.ui.button(label="Start Story", style=discord.ButtonStyle.primary, custom_id="story_start")
    async def start_story(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if len(players) < 2:
            await interaction.response.send_message("❌ Need at least 2 writers to start!", ephemeral=True)
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
                    f"📖 **Story Builder is starting!** {' '.join(mentions)} — get ready to write!",
                    delete_after=15,
                )

        payload["host_id"] = interaction.user.id
        await self.cog._run_story(interaction, self.game_id, payload, interaction.channel)

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="story_cancel")
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

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="story_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        await interaction.response.send_message(HOW_TO_PLAY["story"], ephemeral=True)


class StoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    @app_commands.command(name="story", description="Start a Story Builder (Exquisite Corpse) game!")
    @app_commands.describe(
        max_sentences="Total sentences in the story (max 30)",
        visibility="blind = only see previous sentence, full = see whole story",
        starter="Opening sentence (blank = use default)",
    )
    @app_commands.choices(
        visibility=[
            app_commands.Choice(name="Blind", value="blind"),
            app_commands.Choice(name="Full", value="full"),
        ],
    )
    async def story(
        self,
        interaction: discord.Interaction,
        max_sentences: int = 10,
        visibility: str = "blind",
        starter: str = "",
    ):
        log.info("%s used /games play story in #%s", interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it with `/games config allow-channel`.",
                ephemeral=True,
            )
            return
        max_sentences = clamp_max_sentences(max_sentences)

        game_id = await create_game(
            self.db,
            interaction.channel_id,
            interaction.user.id,
            "story",
            state="joining",
            payload={
                "max_sentences": max_sentences,
                "visibility": visibility,
                "starter": starter,
                "players": [],
                "sentences": [],
            },
        )

        embed = build_lobby_embed(
            host_name=interaction.user.display_name,
            visibility=visibility,
            max_sentences=max_sentences,
        )

        log.info("Game %s (story) created by %s in #%s", game_id, interaction.user.display_name, interaction.channel.name if interaction.channel else "unknown")
        view = StoryJoinView(game_id, interaction.user.id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, interaction.channel_id, game_id, [interaction.user.id])

    async def _run_story(self, interaction, game_id: str, payload: dict, channel):
        guild = channel.guild if hasattr(channel, "guild") else None
        host_id = payload.get("host_id", 0)

        players = payload["players"]
        max_sentences = payload.get("max_sentences", 10)
        visibility = payload.get("visibility", "blind")
        starter = resolve_starter(payload.get("starter", ""))

        sentences: list[dict] = [{"author_id": None, "text": starter}]
        payload["sentences"] = sentences
        await update_game_payload(self.db, game_id, payload)

        await channel.send(f"📖 **The story begins:**\n> *{starter}*")

        turn_order = build_turn_order(players)

        sentence_count = 1  # starter already counted
        turn_index = 0
        consecutive_skips = 0

        def _name_for(pid: int) -> str:
            if guild is None:
                return str(pid)
            m = guild.get_member(pid)
            return m.display_name if m else str(pid)

        while sentence_count < max_sentences:
            # Check if game was closed
            if game_id not in self.bot.active_views:
                break

            current_player_id = pick_current_player(turn_order, turn_index)
            current_member = guild.get_member(current_player_id) if guild else None
            player_name = current_member.display_name if current_member else str(current_player_id)

            # Build context for the modal
            context_text = build_context(sentences, visibility)

            # Single turn message: ping + buttons
            mention = current_member.mention if current_member else f"**{player_name}**"
            turn_view = StoryTurnView(game_id, host_id, current_player_id, context_text, self.db, self.bot)

            turn_embed = build_turn_embed(
                sentence_count=sentence_count,
                max_sentences=max_sentences,
                current_player_id=current_player_id,
                turn_order=turn_order,
                name_resolver=_name_for,
            )

            timeout_min = _TURN_TIMEOUT // 60
            turn_msg = await channel.send(
                content=f"{mention} — it's your turn! You have **{timeout_min} minutes** to write. Click below to start.",
                embed=turn_embed,
                view=turn_view,
            )

            # Wait for submission, skip, or timeout
            try:
                await asyncio.wait_for(turn_view._submitted_event.wait(), timeout=_TURN_TIMEOUT)
            except asyncio.TimeoutError:
                turn_view._skipped = True

            # Disable turn buttons
            for item in turn_view.children:
                item.disabled = True
            try:
                await turn_msg.edit(view=turn_view)
            except Exception:
                pass

            # Check if game was closed via the close button
            if game_id not in self.bot.active_views:
                break

            if turn_view._skipped and not turn_view._submitted_text:
                await channel.send(f"⏩ {player_name} was skipped.", delete_after=15)
                consecutive_skips += 1
                turn_index += 1
                # If every player in the rotation was skipped, end the story
                if should_end_after_skip(consecutive_skips, len(turn_order)):
                    await channel.send("📖 All writers were skipped — ending the story.")
                    break
                continue

            consecutive_skips = 0  # reset on successful submission
            new_sentence = turn_view._submitted_text
            append_sentence(payload, current_player_id, new_sentence)
            sentences = payload["sentences"]
            await update_game_payload(self.db, game_id, payload)

            await channel.send(f"> *{discord.utils.escape_markdown(new_sentence)}*", allowed_mentions=discord.AllowedMentions.none())
            sentence_count += 1
            turn_index += 1

        # If game was closed by host, skip final reveal
        if game_id not in self.bot.active_views:
            return

        await self._reveal_story(channel, game_id, sentences, players, guild)

    async def _reveal_story(self, channel, game_id: str, sentences: list, players: list, guild):
        def _name_for(author_id: int) -> str:
            return resolve_name(guild, author_id)

        # Send the full story embed first
        story_text = assemble_story_text(sentences)
        complete_embed = build_complete_story_embed(
            story_text=story_text,
            player_count=len(players),
            sentence_count=len(sentences),
        )
        await channel.send(embed=complete_embed)

        # Send attributed breakdown — split across messages if needed
        lines = build_attribution_lines(sentences, _name_for)
        chunks = chunk_attribution_lines(lines)
        attr_embed = build_attribution_embed(chunks)
        await channel.send(embed=attr_embed)

        payload = await get_game_payload(self.db, game_id)
        log.info("Game %s ended — %d players", game_id, len(players))
        await end_game(
            self.db, game_id,
            player_count=len(players),
            round_count=len(sentences),
            payload=payload,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: commands.Bot):
    cog = StoryCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("story")
    play.add_command(cog.story)
