import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    check_allowed_channel,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    resolve_names,
    channel_name,
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
        log.info("%s submitted story sentence in #%s", interaction.user.display_name, channel_name(interaction.channel))
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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="✍️ Write Your Sentence", style=discord.ButtonStyle.primary, custom_id="story_write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can skip.", ephemeral=True)
            return
        self._skipped = True
        self._submitted_event.set()
        self.stop()
        await interaction.response.send_message("⏩ Player skipped.", ephemeral=True)


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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="story_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id

        def _add(payload):
            add_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        log.info("%s joined game %s", interaction.user.display_name, self.game_id)

        players = payload.get("players", [])
        names = resolve_names(interaction.guild, players)
        assert interaction.message  # component interactions always carry their message
        embed = interaction.message.embeds[0]
        embed.set_field_at(0, name=f"Writers ({len(players)})", value=", ".join(names) or "—", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="story_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id

        def _remove(payload):
            remove_player(payload, uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        log.info("%s left game %s", interaction.user.display_name, self.game_id)

        players = payload.get("players", [])
        names = resolve_names(interaction.guild, players)
        assert interaction.message  # component interactions always carry their message
        embed = interaction.message.embeds[0]
        embed.set_field_at(0, name=f"Writers ({len(players)})", value=", ".join(names) or "—", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've left.", ephemeral=True)

    @discord.ui.button(label="Start Story", style=discord.ButtonStyle.primary, custom_id="story_start")
    async def start_story(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if len(players) < 2:
            await interaction.response.send_message("❌ Need at least 2 writers to start!", ephemeral=True)
            return

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        # Ping joined players
        if interaction.guild:
            mentions = [
                member.mention
                for uid in players
                if (member := interaction.guild.get_member(uid))
            ]
            if mentions:
                assert isinstance(interaction.channel, discord.abc.Messageable)  # games run in text channels
                await interaction.channel.send(
                    f"📖 **Story Builder is starting!** {' '.join(mentions)} — get ready to write!",
                    delete_after=15,
                )

        payload["host_id"] = interaction.user.id
        await self.cog._run_story(interaction, self.game_id, payload, interaction.channel)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="story_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["story"], ephemeral=True)


class StoryCog(commands.Cog):
    def __init__(self, bot: "Bot"):
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
        log.info("%s used /games play story in #%s", interaction.user.display_name, channel_name(interaction.channel))
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "max_sentences": max_sentences,
                "visibility": visibility,
                "starter": starter,
            },
        )
        await finish_launch_response(interaction, game_id)

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
        max_sentences = clamp_max_sentences(options.get("max_sentences", 10))
        visibility = options.get("visibility", "blind")
        if visibility not in ("blind", "full"):
            visibility = "blind"
        starter = options.get("starter", "")

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
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

        guild = getattr(channel, "guild", None)
        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
        embed = build_lobby_embed(
            host_name=host_name,
            visibility=visibility,
            max_sentences=max_sentences,
            color=color,
        )

        log.info("Game %s (story) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))
        view = StoryJoinView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("story launch lacked send perms in channel %s", channel.id)
            return None
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

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

            turn_color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None
            turn_embed = build_turn_embed(
                sentence_count=sentence_count,
                max_sentences=max_sentences,
                current_player_id=current_player_id,
                turn_order=turn_order,
                name_resolver=_name_for,
                color=turn_color,
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
            disable_all_items(turn_view)
            try:
                await turn_msg.edit(view=turn_view)
            except discord.HTTPException:
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
            assert new_sentence is not None  # not skipped ⇒ a sentence was submitted
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

        color = await resolve_accent_color(self.bot.ctx.db_path, guild) if guild else None

        # Send the full story embed first
        story_text = assemble_story_text(sentences)
        complete_embed = build_complete_story_embed(
            story_text=story_text,
            player_count=len(players),
            sentence_count=len(sentences),
            color=color,
        )
        await channel.send(embed=complete_embed)

        # Send attributed breakdown — split across messages if needed
        lines = build_attribution_lines(sentences, _name_for)
        chunks = chunk_attribution_lines(lines)
        attr_embed = build_attribution_embed(chunks, color=color)
        await channel.send(embed=attr_embed)

        payload = await get_game_payload(self.db, game_id)
        log.info("Game %s ended — %d players", game_id, len(players))
        await end_game(
            self.db, game_id,
            player_count=len(players),
            round_count=len(sentences),
            payload=payload,
            bot=self.bot, player_ids=list(players),
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover after a restart.

        The join lobby re-registers cleanly. Once the story is underway, play
        runs in a blocking per-turn loop whose turn messages aren't tracked, so
        it can't be resumed — end it gracefully so players aren't left waiting on
        a dead turn prompt.
        """
        if payload.get("sentences"):
            try:
                await channel.send(
                    "📖 This Story game was interrupted by a bot restart and can't be "
                    "resumed — start a new one with `/games play story`."
                )
            except discord.HTTPException:
                pass
            await end_game(self.db, row["game_id"])
            self.bot.active_views.pop(row["game_id"], None)
            log.info("story game %s was mid-play at restart; ended gracefully.", row["game_id"])
            return True
        game_id = row["game_id"]
        view = StoryJoinView(game_id, int(row["host_id"]), self.db, self.bot, self)
        self.bot.active_views[game_id] = view
        self.bot.add_view(view, message_id=message.id)
        log.info("Recovered story game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
        return True


async def setup(bot: "Bot"):
    cog = StoryCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("story")
    play.add_command(cog.story, override=True)
    bot.game_launchers["story"] = cog.launch
    bot.game_recoverers["story"] = cog.recover_game
