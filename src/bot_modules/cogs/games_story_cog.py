import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items, is_host_or_mod
from discord.ext import commands
from discord import app_commands
from bot_modules.games.constants import HOW_TO_PLAY, play_description
from bot_modules.games.command_groups import play
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.game_start_ping_service import resolve_start_epoch
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
    create_game,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    resolve_name,
    resolve_names,
    channel_name,
)
from bot_modules.games.utils.launch_guard import launch_refusal
from bot_modules.games_story.embeds import (
    build_attribution_embed,
    build_complete_story_embed,
    build_lobby_embed,
    build_turn_embed,
)
from bot_modules.games_story.logic import (
    DEFAULT_TURN_SECONDS,
    add_player,
    append_sentence,
    assemble_story_text,
    build_attribution_lines,
    build_context,
    build_turn_order,
    chunk_attribution_lines,
    clamp_max_sentences,
    format_drop_notice,
    format_leave_notice,
    format_skip_notice,
    format_story_opening,
    note_turn_outcome,
    pick_current_player,
    remove_player,
    resolve_starter,
    rotation_after_turn,
    roster_for_payout,
    should_drop_writer,
    should_end_after_skip,
    writer_may_skip,
    writer_skip_unlock_at,
)

log = logging.getLogger(__name__)

_TURN_TIMEOUT = DEFAULT_TURN_SECONDS  # seconds per turn


class StorySentenceModal(discord.ui.Modal, title="Add Your Sentence"):
    """The sentence box. Submitting hands the sentence straight to the turn
    view (``on_submit`` sets its event); the modal is never awaited, so a
    box the writer dismisses leaks nothing — it simply times out with the
    turn (anon-tail-78: each dismissal used to park a coroutine on
    ``modal.wait()`` for the life of the process).
    """

    context_field = discord.ui.TextInput(
        label="Context (for reference)",
        style=discord.TextStyle.paragraph,
        required=False,
    )
    sentence = discord.ui.TextInput(
        label="Your Sentence",
        style=discord.TextStyle.paragraph,
        max_length=500,
        placeholder="Continue the story…",
    )

    def __init__(
        self, game_id: str, player_id: int, context_text: str = "",
        turn_view: "StoryTurnView | None" = None,
    ):
        super().__init__(timeout=_TURN_TIMEOUT)
        self.game_id = game_id
        self.player_id = player_id
        self.turn_view = turn_view
        self._submitted = False
        self._value: str | None = None
        if context_text:
            self.context_field._underlying.value = context_text[:4000]

    async def on_submit(self, interaction: discord.Interaction):
        log.info("%s submitted story sentence in #%s", interaction.user.display_name, channel_name(interaction.channel))
        self._submitted = True
        self._value = self.sentence.value
        view = self.turn_view
        if view is not None and self._value and not view._submitted_event.is_set():
            view._submitted_text = self._value
            view._submitted_event.set()
            view.stop()
        await interaction.response.send_message("✅ Your sentence has been added!", ephemeral=True)


class StoryTurnView(discord.ui.View):
    """Per-turn view with Write, Skip and Leave buttons.

    Skip is the host's or a mod's at any time, and **any writer's** once the
    turn has been open :data:`WRITER_SKIP_AFTER_SECONDS` (anon-tail-73: only
    the host could skip, so an AFK host stalled the story). Leave takes the
    presser out of the rotation after this turn.
    """

    def __init__(
        self, game_id: str, host_id: int, current_player_id: int, context_text: str, db, bot,
        turn_order: list[int] | None = None,
    ):
        super().__init__(timeout=_TURN_TIMEOUT)
        self.game_id = game_id
        self.host_id = host_id
        self.current_player_id = current_player_id
        self.context_text = context_text
        self.db = db
        self.bot = bot
        self.turn_order = list(turn_order or [])
        self.opened_at = time.time()
        self._submitted_event = asyncio.Event()
        self._submitted_text: str | None = None
        self._skipped = False
        self._left: set[int] = set()

    @discord.ui.button(label="✍️ Write Your Sentence", style=discord.ButtonStyle.primary, custom_id="story_write")
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if interaction.user.id != self.current_player_id:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return
        modal = StorySentenceModal(self.game_id, self.current_player_id, self.context_text, turn_view=self)
        await interaction.response.send_modal(modal)

    def _skip_now(self) -> None:
        self._skipped = True
        self._submitted_event.set()
        self.stop()

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="story_skip")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not is_host_or_mod(interaction, self.host_id) and not writer_may_skip(
            interaction.user.id, turn_order=self.turn_order, opened_at=self.opened_at, now=time.time(),
        ):
            if interaction.user.id in self.turn_order:
                unlock = writer_skip_unlock_at(self.opened_at)
                await interaction.response.send_message(
                    f"❌ Only the host or a mod can skip right now — any writer can <t:{unlock}:R>.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message("❌ Only the host, a mod, or a writer can skip.", ephemeral=True)
            return
        self._skip_now()
        await interaction.response.send_message("⏩ Player skipped.", ephemeral=True)

    @discord.ui.button(label="🚪 Leave", style=discord.ButtonStyle.secondary, custom_id="story_leave_turn")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        if uid not in self.turn_order or uid in self._left:
            await interaction.response.send_message("You're not in this story's rotation.", ephemeral=True)
            return
        self._left.add(uid)
        await interaction.response.send_message(
            "✅ You've left the story — you'll be dropped from the rotation after this turn.", ephemeral=True,
        )
        # The current writer leaving is also a skip: nothing is coming.
        if uid == self.current_player_id:
            self._skip_now()


class StoryJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog

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
        if not is_host_or_mod(interaction, self.host_id):
            await interaction.response.send_message("❌ Only the host or a mod can start.", ephemeral=True)
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

        # The row must stop reading as an open lobby — the start-ping sweep
        # polls state='joining' and a story outlives its countdown. The host
        # stays the row's host: a mod pressing Start on the host's behalf used
        # to become the host for Skip purposes (anon-tail-77).
        await update_game_state(self.db, self.game_id, "playing")
        await self.cog._run_story(
            interaction, self.game_id, payload, interaction.channel, host_id=self.host_id,
        )

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

    @app_commands.command(name="story", description=play_description("story"))
    @app_commands.describe(
        max_sentences="Total sentences in the story (max 30)",
        visibility="blind = only see previous sentence, full = see whole story",
        starter="Opening sentence (blank = use default)",
        start_in="Show a lobby countdown — game starts in this many minutes (host still clicks Start Story)",
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
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info("%s used /games play story in #%s", interaction.user.display_name, channel_name(interaction.channel))
        # The one launch guard every door shares: allowed channel, enabled
        # dial, and no game already running in this channel.
        refusal = await launch_refusal(
            self.db, "story", interaction.channel_id, interaction.guild_id or 0,
        )
        if refusal:
            await interaction.response.send_message(refusal, ephemeral=True)
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
                "start_in": start_in,
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
        start_epoch = resolve_start_epoch(options)

        payload = {
            "max_sentences": max_sentences,
            "visibility": visibility,
            "starter": starter,
            "players": [],
            "sentences": [],
        }
        if start_epoch:
            payload["start_epoch"] = start_epoch
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "story",
            state="joining",
            payload=payload,
        )

        guild = getattr(channel, "guild", None)
        color = await safe_resolve_accent(self.bot, guild, log_label="story")
        embed = build_lobby_embed(
            host_name=host_name,
            visibility=visibility,
            max_sentences=max_sentences,
            color=color,
            start_at=start_epoch,
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

    async def _run_story(
        self, interaction, game_id: str, payload: dict, channel, *, host_id: int | None = None,
    ):
        """The turn loop. ``host_id`` is the lobby's host (the row's, not
        whoever pressed Start); a caller without one falls back to the
        payload's legacy ``host_id`` key.

        Pacing (anon-tail-73): a turn is :data:`_TURN_TIMEOUT` seconds; a
        writer who misses two turns in a row (timed out, dismissed the box or
        was skipped) is dropped from the rotation with a notice; a writer who
        pressed Leave is dropped after the turn; an all-miss lap still ends
        the story.
        """
        guild = channel.guild if hasattr(channel, "guild") else None
        if host_id is None:
            host_id = int(payload.get("host_id", 0) or 0)

        players = payload["players"]
        max_sentences = payload.get("max_sentences", 10)
        visibility = payload.get("visibility", "blind")
        starter = resolve_starter(payload.get("starter", ""))

        sentences: list[dict] = [{"author_id": None, "text": starter}]
        payload["sentences"] = sentences
        await update_game_payload(self.db, game_id, payload)

        await channel.send(
            format_story_opening(starter),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        turn_order = build_turn_order(players)

        sentence_count = 1  # starter already counted
        turn_index = 0
        consecutive_skips = 0
        misses: dict[int, int] = {}   # writer -> consecutive missed turns
        left: set[int] = set()        # writers who pressed Leave

        def _name_for(pid: int) -> str:
            if guild is None:
                return str(pid)
            m = guild.get_member(pid)
            return m.display_name if m else str(pid)

        while sentence_count < max_sentences and turn_order:
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
            turn_view = StoryTurnView(
                game_id, host_id, current_player_id, context_text, self.db, self.bot,
                turn_order=turn_order,
            )

            turn_color = await safe_resolve_accent(self.bot, guild, log_label="story")
            turn_embed = build_turn_embed(
                sentence_count=sentence_count,
                max_sentences=max_sentences,
                current_player_id=current_player_id,
                turn_order=turn_order,
                name_resolver=_name_for,
                color=turn_color,
            )

            timeout_min = max(1, _TURN_TIMEOUT // 60)
            turn_msg = await channel.send(
                content=(
                    f"{mention} — it's your turn! You have **{timeout_min} minute{'s' if timeout_min != 1 else ''}** "
                    "to write. Click below to start."
                ),
                embed=turn_embed,
                view=turn_view,
            )

            # Wait for submission, skip, or timeout
            try:
                await asyncio.wait_for(turn_view._submitted_event.wait(), timeout=_TURN_TIMEOUT)
            except asyncio.TimeoutError:
                turn_view._skipped = True

            # The turn is spent, so the panel goes rather than lingering as
            # a dead disabled copy — Price is Right already deletes its host
            # prompt this way. Story used to leave one behind per player per
            # round, so a 5-player 4-round game buried its own story text
            # under twenty exhausted panels.
            try:
                await turn_msg.delete()
            except discord.HTTPException:
                pass

            # Check if game was closed via the close button
            if game_id not in self.bot.active_views:
                break

            # Writers who pressed Leave during the turn go after it resolves.
            leaving = set(turn_view._left) - left
            for pid in leaving:
                left.add(pid)
                await channel.send(
                    format_leave_notice(_name_for(pid)),
                    delete_after=15,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if leaving:
                # Persist the leavers: the sweep and ``/games end`` rebuild
                # the roster from the payload (``game_roster._story``) and
                # must drop a never-wrote leaver the way the reveal does.
                payload["left"] = sorted(left)
                await update_game_payload(self.db, game_id, payload)
            drop: set[int] = set(leaving)

            missed = turn_view._skipped and not turn_view._submitted_text
            if missed:
                await channel.send(
                    format_skip_notice(player_name),
                    delete_after=15,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                consecutive_skips += 1
                note_turn_outcome(misses, current_player_id, missed=True)
                if current_player_id not in drop and should_drop_writer(misses, current_player_id):
                    drop.add(current_player_id)
                    await channel.send(
                        format_drop_notice(player_name),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                if drop:
                    # A shrunken rotation starts a fresh lap: the missed turns
                    # were the leaver's, not the writers who remain.
                    consecutive_skips = 0
                turn_order, turn_index = rotation_after_turn(turn_order, turn_index, drop)
                # If every writer in the rotation was skipped, end the story
                if not turn_order or should_end_after_skip(consecutive_skips, len(turn_order)):
                    await channel.send("📖 All writers were skipped — ending the story.")
                    break
                continue

            consecutive_skips = 0  # reset on successful submission
            note_turn_outcome(misses, current_player_id, missed=False)
            new_sentence = turn_view._submitted_text
            assert new_sentence is not None  # not skipped ⇒ a sentence was submitted
            append_sentence(payload, current_player_id, new_sentence)
            sentences = payload["sentences"]
            await update_game_payload(self.db, game_id, payload)

            await channel.send(f"> *{discord.utils.escape_markdown(new_sentence)}*", allowed_mentions=discord.AllowedMentions.none())
            sentence_count += 1
            turn_order, turn_index = rotation_after_turn(turn_order, turn_index, drop)

        # If game was closed by host, skip final reveal
        if game_id not in self.bot.active_views:
            return

        await self._reveal_story(
            channel, game_id, sentences, roster_for_payout(players, sentences, left), guild,
        )

    async def _reveal_story(self, channel, game_id: str, sentences: list, players: list, guild):
        def _name_for(author_id: int) -> str:
            return resolve_name(guild, author_id)

        color = await safe_resolve_accent(self.bot, guild, log_label="story")

        # Send the full story embed first
        story_text = assemble_story_text(sentences)
        complete_embed = build_complete_story_embed(
            story_text=story_text,
            player_count=len(players),
            sentence_count=len(sentences),
            color=color,
        )
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, complete_embed, guild.id, "story")
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
