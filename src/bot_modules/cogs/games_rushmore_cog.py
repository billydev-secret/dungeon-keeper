"""
Mt. Rushmore Draft — game cog.

A topic is chosen and players snake-draft their top 4 picks.  No duplicates
across any player.  After 4 rounds everyone's board is revealed, and the room
votes on the best Mt. Rushmore.

Pure logic (snake order, duplicate checks, recap stats, vote tally) lives in
``bot_modules.games_rushmore.logic``; embed/text builders live in
``bot_modules.games_rushmore.embeds``. This cog is just the Discord glue.
"""

import asyncio
import logging
import random
import time as _time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import HOW_TO_PLAY
from bot_modules.games.command_groups import play
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    create_game,
    modify_payload,
    update_game_message,
    update_game_payload,
    update_game_state,
    get_game_payload,
    get_game_options,
    end_game,
    update_session,
    resolve_name,
    channel_name,
)
from bot_modules.games.utils.question_source import get_rushmore_topic, channel_allows_nsfw
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games_rushmore.logic import (
    DRAFT_ROUNDS,
    SKIPPED_MARKER,
    clamp_settings,
    compute_recap_stats,
    eligible_voters,
    find_who_picked,
    generate_snake_order,
    tally_votes,
)
from bot_modules.games_rushmore.embeds import (
    build_draft_embed,
    build_final_boards_embed,
    build_join_embed,
    build_recap_embed,
    build_vote_embed,
    build_winner_embed,
)

log = logging.getLogger(__name__)

# ── AI prompts ───────────────────────────────────────────────────────────────

RUSHMORE_SYSTEM_PROMPT = (
    "You are generating fun, debatable 'Mt. Rushmore' draft topics for an adult "
    "party game in this Discord community. A Mt. Rushmore topic is "
    "a category where players will draft their top 4 picks. The best topics are "
    "ones where there are many valid options and people will disagree on the best 4."
)

RUSHMORE_USER_PROMPT = (
    "Generate a single Mt. Rushmore topic. "
    "Examples: 'Snacks', 'Movie Villains', 'Excuses to Leave a Party', "
    "'Songs to Play at the End of the World', 'Fast Food Menu Items', "
    "'Things You'd Save in a House Fire', 'Guilty Pleasure TV Shows', "
    "'Underrated Superpowers', 'Worst First Date Ideas'. "
    "Return only the topic — a short noun phrase, no preamble, no quotes."
)


# ── Modals ───────────────────────────────────────────────────────────────────

class HostTopicModal(discord.ui.Modal, title="Choose a Topic"):
    topic_input = discord.ui.TextInput(
        label="Topic",
        placeholder="e.g. Snacks, Movie Villains, Excuses to Leave a Party",
        required=True,
        max_length=100,
    )

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._event = asyncio.Event()

    async def on_submit(self, interaction: discord.Interaction):
        self._result = self.topic_input.value.strip()
        await interaction.response.send_message(
            f"Topic set: **{discord.utils.escape_markdown(self._result)}**", ephemeral=True, delete_after=5
        )
        self._event.set()

    async def wait_for_result(self, timeout: float = 60.0) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return self._result


class PickModal(discord.ui.Modal):
    pick = discord.ui.TextInput(
        label="Pick",
        required=True,
        max_length=100,
    )

    def __init__(self, round_num: int, topic: str, draft_view: "RushmoreDraftView"):
        title = f"Your Pick — Round {round_num}"
        if len(title) > 45:
            title = f"Round {round_num} Pick"
        super().__init__(title=title)
        self.pick.placeholder = f"Enter your pick for Mt. Rushmore of {topic}"[:100]
        self._draft_view = draft_view

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted pick modal in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        view = self._draft_view
        if view._closed:
            await interaction.response.send_message("This draft has ended.", ephemeral=True)
            return
        pick_text = self.pick.value.strip()
        if not pick_text:
            await interaction.response.send_message("Pick can't be empty.", ephemeral=True)
            return

        # Duplicate check
        who = find_who_picked(pick_text, view.boards)
        if who:
            who_name = resolve_name(interaction.guild, int(who))
            await interaction.response.send_message(
                f"❌ **{discord.utils.escape_markdown(pick_text)}** was already taken by **{discord.utils.escape_markdown(who_name)}**! Try again.",
                ephemeral=True,
            )
            return

        # Valid pick — store it
        view.accept_pick(pick_text, interaction.user.id)
        await interaction.response.send_message(
            f"✅ Picked **{discord.utils.escape_markdown(pick_text)}**!", ephemeral=True, delete_after=5,
        )


# ── Vote select menu ────────────────────────────────────────────────────────

class RushmoreVoteSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="\U0001f5f3️ Select the winner",
            options=options,
            custom_id="rushmore_vote_select",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        view: RushmoreVoteView = self.view
        uid = interaction.user.id
        target = int(self.values[0])
        if target == uid:
            await interaction.response.send_message("You can't vote for yourself!", ephemeral=True)
            return
        changed = uid in view.votes
        view.votes[uid] = target
        target_name = resolve_name(interaction.guild, target)
        msg = f"✅ Voted for **{discord.utils.escape_markdown(target_name)}**"
        if changed:
            msg += " (changed)"
        await interaction.response.send_message(msg, ephemeral=True, delete_after=5)
        if view.all_voted():
            view.skip_timer()


# ── Views ────────────────────────────────────────────────────────────────────

class RushmoreJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, host_name: str, topic: str | None, source: str, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.source = source
        self.db = db
        self.bot = bot
        self.cog = cog
        self.players: list[int] = []
        self._msg: discord.Message | None = None

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _player_names(self, guild) -> list[str]:
        return [resolve_name(guild, uid) for uid in self.players]

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="rushmore_join", row=0)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        def _add(p):
            if uid not in p.get("players", []):
                p.setdefault("players", []).append(uid)
        payload = await modify_payload(self.db, self.game_id, _add)
        self.players = payload.get("players", [])
        names = self._player_names(interaction.guild)
        embed = build_join_embed(self.host_name, names, self.topic)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ You've joined!", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="rushmore_leave", row=0)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        uid = interaction.user.id
        def _remove(p):
            players = p.get("players", [])
            if uid in players:
                players.remove(uid)
        payload = await modify_payload(self.db, self.game_id, _remove)
        self.players = payload.get("players", [])
        names = self._player_names(interaction.guild)
        embed = build_join_embed(self.host_name, names, self.topic)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("You've left.", ephemeral=True)

    @discord.ui.button(label="Start Draft", style=discord.ButtonStyle.primary, custom_id="rushmore_start", row=1)
    async def start_draft(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return
        if len(self.players) < 3:
            await interaction.response.send_message("Need at least 3 players to start a Mt. Rushmore draft.", ephemeral=True)
            return

        # Resolve topic if needed
        topic = self.topic
        if not topic:
            if self.source == "host":
                modal = HostTopicModal()
                await interaction.response.send_modal(modal)
                topic = await modal.wait_for_result(timeout=60.0)
                if not topic:
                    try:
                        await interaction.followup.send("Topic entry timed out. Try again.", ephemeral=True)
                    except discord.HTTPException:
                        pass
                    return
            elif self.source == "ai":
                await interaction.response.defer()
                topic = await generate_text(
                    RUSHMORE_SYSTEM_PROMPT, RUSHMORE_USER_PROMPT,
                    model="gpt-4o-mini", max_tokens=50,
                )
                if not topic:
                    _tags = (await get_game_payload(self.db, self.game_id)).get("settings", {}).get("tags") or None
                    topic = await get_rushmore_topic(
                        self.db, tags=_tags,
                        allow_nsfw=channel_allows_nsfw(interaction.channel),
                    )
                if not topic:
                    await interaction.followup.send("Couldn't generate a topic. Try setting one manually with `/rushmore topic:...`.", ephemeral=True)
                    return
            elif self.source == "bank":
                await interaction.response.defer()
                _tags = (await get_game_payload(self.db, self.game_id)).get("settings", {}).get("tags") or None
                topic = await get_rushmore_topic(
                    self.db, tags=_tags, allow_nsfw=channel_allows_nsfw(interaction.channel)
                )
                if not topic:
                    await interaction.followup.send("No topics in the question bank.", ephemeral=True)
                    return
            else:
                await interaction.response.defer()
        else:
            await interaction.response.defer()

        self.topic = topic
        # Disable join view
        for item in self.children:
            item.disabled = True
        try:
            await self._msg.edit(view=self)
        except discord.HTTPException:
            pass

        # Start the draft
        await self.cog._start_draft(
            game_id=self.game_id,
            host_id=self.host_id,
            host_name=self.host_name,
            topic=topic,
            players=list(self.players),
            channel=interaction.channel,
            guild=interaction.guild,
            msg=self._msg,
            settings=await self.cog._get_settings(self.game_id),
        )

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="rushmore_htp", row=2)
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        await interaction.response.send_message(HOW_TO_PLAY["rushmore"], ephemeral=True)


class RushmoreDraftView(discord.ui.View):
    """Persistent view during the snake draft."""

    def __init__(self, game_id: str, host_id: int, host_name: str, topic: str,
                 players: list[int], timer_secs: int, guild, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.host_name = host_name
        self.topic = topic
        self.players = players
        self.timer_secs = timer_secs
        self.guild = guild
        self.db = db
        self.bot = bot
        self.cog = cog

        # Draft state
        random.shuffle(self.players)
        self.draft_order = generate_snake_order(self.players)
        self.current_pick_index = 0
        self.boards: dict[str, list] = {str(uid): [None] * DRAFT_ROUNDS for uid in self.players}
        self.all_picks: list[str] = []
        self.pick_times: dict[str, float | None] = {}
        self.skipped: list[str] = []
        self._msg: discord.Message | None = None
        self._closed = False
        self._pick_event: asyncio.Event | None = None
        self._pick_start: float = 0.0
        self._draft_start: float = _time.time()
        self._active_player_id: int | None = None

    def _player_tuples(self) -> list[tuple[int, str]]:
        return [(uid, resolve_name(self.guild, uid)) for uid in self.players]

    def accept_pick(self, pick_text: str, user_id: int):
        """Called by PickModal when a valid pick is made."""
        if self._active_player_id != user_id:
            return
        rnd, pid = self.draft_order[self.current_pick_index]
        if pid != user_id:
            return

        self.boards[str(user_id)][rnd - 1] = pick_text
        self.all_picks.append(pick_text)
        elapsed = _time.time() - self._pick_start
        key = f"{user_id}_{rnd}"
        self.pick_times[key] = elapsed

        if self._pick_event:
            self._pick_event.set()

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    def _build_embed(self) -> discord.Embed:
        if self.current_pick_index < len(self.draft_order):
            rnd, pid = self.draft_order[self.current_pick_index]
            name = resolve_name(self.guild, pid)
        else:
            rnd, pid, name = DRAFT_ROUNDS, None, None
        return build_draft_embed(
            self.host_name, self.topic, self._player_tuples(),
            self.boards, pid, name, rnd, self.timer_secs,
        )

    @discord.ui.button(label="\U0001f5ff Make Your Pick", style=discord.ButtonStyle.success, custom_id="rushmore_pick", row=0)
    async def make_pick(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if self._closed:
            await interaction.response.send_message("The draft is over.", ephemeral=True)
            return
        if interaction.user.id != self._active_player_id:
            active_name = resolve_name(self.guild, self._active_player_id) if self._active_player_id else "someone"
            await interaction.response.send_message(
                f"It's not your turn! Waiting on **{discord.utils.escape_markdown(active_name)}**.",
                ephemeral=True,
            )
            return
        rnd = self.draft_order[self.current_pick_index][0]
        await interaction.response.send_modal(PickModal(rnd, self.topic, self))


class RushmoreVoteView(discord.ui.View):
    def __init__(self, game_id: str, eligible_players: list[int], guild, db, bot):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.eligible_players = eligible_players
        self.guild = guild
        self.db = db
        self.bot = bot
        self.votes: dict[int, int] = {}
        self._msg: discord.Message | None = None
        self._timer_obj = None
        self._done_event: asyncio.Event | None = None

        options = []
        for uid in eligible_players:
            name = resolve_name(guild, uid)
            options.append(discord.SelectOption(label=name, value=str(uid)))
        self.add_item(RushmoreVoteSelect(options))

    def all_voted(self) -> bool:
        for uid in self.eligible_players:
            if uid not in self.votes:
                return False
        return True

    def skip_timer(self):
        if self._done_event:
            self._done_event.set()


class RushmoreRecapView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, cog: "RushmoreCog", settings: dict):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.cog = cog
        self._settings = settings

    def is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="\U0001f501 Run Again", style=discord.ButtonStyle.primary, custom_id="rushmore_run_again")
    async def run_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can restart.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()
        await interaction.response.defer()
        await self.cog.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={
                "topic": "",
                "timer": self._settings.get("timer", 30),
                "source": self._settings.get("source", "host"),
                "vote_timer": self._settings.get("vote_timer", 30),
            },
        )

    @discord.ui.button(label="\U0001f504 Hand Off", style=discord.ButtonStyle.secondary, custom_id="rushmore_hand_off")
    async def hand_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self.is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can hand off.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Type the **/rushmore** command to start a new game as the new host!",
            ephemeral=True,
        )
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()


# ── Cog ──────────────────────────────────────────────────────────────────────

class RushmoreCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Recover after a restart.

        Rushmore is a single linear game (join -> draft -> vote -> recap), not a
        series of rounds. The join lobby re-registers cleanly. Once drafting or
        voting is underway there's no prior round to fall back to, and those run
        in blocking loops that can't be safely re-driven, so we end the game
        gracefully rather than leave dead buttons.
        """
        game_id = row["game_id"]
        host_id = int(row["host_id"])
        guild = getattr(channel, "guild", None)
        host_name = resolve_name(guild, host_id) if guild else "Host"

        if row["state"] == "joining":
            settings = payload.get("settings", {})
            view = RushmoreJoinView(
                game_id, host_id, host_name,
                payload.get("topic"), settings.get("source", "host"),
                self.db, self.bot, self,
            )
            view.players = list(payload.get("players", []))
            view._msg = message
            self.bot.active_views[game_id] = view
            self.bot.add_view(view, message_id=message.id)
            log.info("Recovered rushmore game %s (join phase) in #%s", game_id, getattr(channel, "name", channel.id))
            return True

        # Drafting/voting underway — end gracefully.
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass
        try:
            await channel.send(
                "🗿 This Rushmore game was interrupted by a bot restart and can't be "
                "resumed — start a new one with `/games play rushmore`."
            )
        except discord.HTTPException:
            pass
        await end_game(self.db, game_id)
        self.bot.active_views.pop(game_id, None)
        log.info("Rushmore game %s was mid-play at restart; ended gracefully.", game_id)
        return True

    async def _get_settings(self, game_id: str) -> dict:
        payload = await get_game_payload(self.db, game_id)
        return payload.get("settings", {})

    # ── Slash command ────────────────────────────────────────────────

    @app_commands.command(name="rushmore", description="Start a Mt. Rushmore Draft!")
    @app_commands.describe(
        topic="The topic (leave blank for AI/bank/manual entry)",
        source="Where topics come from",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="Host picks topic", value="host"),
            app_commands.Choice(name="AI generated", value="ai"),
            app_commands.Choice(name="Question bank", value="bank"),
        ],
    )
    async def rushmore_cmd(
        self,
        interaction: discord.Interaction,
        topic: str = "",
        source: str = "host",
    ):
        log.info(
            "%s used /games play rushmore in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "rushmore", interaction.guild_id or 0):
            await interaction.response.send_message("Mt. Rushmore Draft is currently disabled on this server.", ephemeral=True)
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"topic": topic, "source": source},
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
        topic = options.get("topic") or None
        source = options.get("source", "host")
        # Pacing knobs come from the per-server dashboard config; an explicit
        # *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "rushmore", guild_id)
        timer, vote_timer = clamp_settings(
            int(options.get("timer", game_opts.get("timer", 30))),
            int(options.get("vote_timer", game_opts.get("vote_timer", 30))),
        )
        settings = {"timer": timer, "source": source, "vote_timer": vote_timer, "tags": options.get("tags") or []}

        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "rushmore",
            state="joining",
            payload={"settings": settings, "players": [], "topic": topic},
        )
        log.info("Game %s (rushmore) created by host %s", game_id, host_id)

        join_view = RushmoreJoinView(
            game_id, host_id, host_name,
            topic, source, self.db, self.bot, self,
        )
        embed = build_join_embed(host_name, [], topic)
        try:
            msg = await channel.send(embed=embed, view=join_view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("rushmore launch lacked send perms in channel %s", channel.id)
            return None
        join_view._msg = msg
        self.bot.active_views[game_id] = join_view
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Draft execution ──────────────────────────────────────────────

    async def _start_draft(
        self,
        game_id: str,
        host_id: int,
        host_name: str,
        topic: str,
        players: list[int],
        channel,
        guild,
        msg: discord.Message,
        settings: dict,
    ):
        await update_game_state(self.db, game_id, "playing")
        payload = await get_game_payload(self.db, game_id)
        payload["topic"] = topic
        await update_game_payload(self.db, game_id, payload)

        draft_view = RushmoreDraftView(
            game_id, host_id, host_name, topic,
            players, settings.get("timer", 30), guild, self.db, self.bot, self,
        )
        self.bot.active_views[game_id] = draft_view

        # Show initial draft board
        embed = draft_view._build_embed()
        try:
            await msg.edit(embed=embed, view=draft_view)
            draft_view._msg = msg
        except Exception:
            new_msg = await channel.send(embed=embed, view=draft_view)
            draft_view._msg = new_msg
            msg = new_msg
            await update_game_message(self.db, game_id, msg.id)

        # Show draft order announcement
        order_names = [resolve_name(guild, uid) for uid in draft_view.players]
        rev_names = list(reversed(order_names))
        await channel.send(
            f"**Draft order:** {' → '.join(order_names)}\n"
            f"(Round 2 reverses: {' → '.join(rev_names)})"
        )

        # Save draft state
        payload = await get_game_payload(self.db, game_id)
        payload["draft_order"] = draft_view.draft_order
        payload["boards"] = draft_view.boards
        payload["all_picks"] = draft_view.all_picks
        await update_game_payload(self.db, game_id, payload)

        # Run each pick
        await self._run_draft_loop(draft_view, channel, guild, settings)

    async def _run_draft_loop(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        timer_secs = settings.get("timer", 30)

        while draft_view.current_pick_index < len(draft_view.draft_order):
            if draft_view._closed:
                return

            rnd, pid = draft_view.draft_order[draft_view.current_pick_index]
            draft_view._active_player_id = pid
            player_name = resolve_name(guild, pid)

            # Update draft board embed
            embed = draft_view._build_embed()
            try:
                await draft_view._msg.edit(embed=embed, view=draft_view)
            except discord.HTTPException:
                pass

            # Ping the player
            member = guild.get_member(pid) if guild else None
            ping_text = (
                f"{member.mention if member else player_name} It's your turn! "
                f"Pick for Round {rnd} of your Mt. Rushmore of **{discord.utils.escape_markdown(draft_view.topic)}**!"
            )
            try:
                await channel.send(ping_text, delete_after=timer_secs)
            except discord.HTTPException:
                pass

            # Wait for pick or timeout
            draft_view._pick_event = asyncio.Event()
            draft_view._pick_start = _time.time()

            # Schedule nudge
            nudge_task = None
            if timer_secs > 15:
                async def _nudge():
                    await asyncio.sleep(timer_secs - 10)
                    if not draft_view._pick_event.is_set() and not draft_view._closed:
                        try:
                            m = member.mention if member else player_name
                            await channel.send(f"{m} ⏰ 10 seconds left to pick!", delete_after=10)
                        except discord.HTTPException:
                            pass
                nudge_task = asyncio.create_task(_nudge())

            try:
                await asyncio.wait_for(draft_view._pick_event.wait(), timeout=timer_secs)
            except asyncio.TimeoutError:
                if not draft_view._closed:
                    # Skipped
                    key = f"{pid}_{rnd}"
                    draft_view.boards[str(pid)][rnd - 1] = SKIPPED_MARKER
                    draft_view.skipped.append(key)
                    draft_view.pick_times[key] = None
                    try:
                        m = member.mention if member else player_name
                        await channel.send(f"{m} ⏱️ Time's up! Your pick was skipped.", delete_after=10)
                    except discord.HTTPException:
                        pass

            if nudge_task and not nudge_task.done():
                nudge_task.cancel()

            if draft_view._closed:
                return

            # Advance to next pick
            draft_view.current_pick_index += 1

            # Save progress to DB
            payload = await get_game_payload(self.db, draft_view.game_id)
            payload["boards"] = draft_view.boards
            payload["all_picks"] = draft_view.all_picks
            payload["current_pick_index"] = draft_view.current_pick_index
            payload["pick_times"] = draft_view.pick_times
            payload["skipped"] = draft_view.skipped
            await update_game_payload(self.db, draft_view.game_id, payload)

            # Update board after pick
            if not draft_view._closed:
                embed = draft_view._build_embed()
                try:
                    await draft_view._msg.edit(embed=embed, view=draft_view)
                except discord.HTTPException:
                    pass

        # Draft complete — disable draft view
        draft_view._closed = True
        for item in draft_view.children:
            item.disabled = True
        try:
            await draft_view._msg.edit(view=draft_view)
        except discord.HTTPException:
            pass

        # Show final boards
        await self._show_final_boards(draft_view, channel, guild, settings)

    async def _show_final_boards(self, draft_view: RushmoreDraftView, channel, guild, settings: dict):
        game_id = draft_view.game_id
        player_tuples = draft_view._player_tuples()

        final_embed = build_final_boards_embed(
            draft_view.host_name, draft_view.topic, player_tuples, draft_view.boards,
        )
        try:
            await channel.send(embed=final_embed)
        except discord.HTTPException:
            pass

        # Determine eligible voters (players with at least 1 real pick)
        eligible = eligible_voters(draft_view.players, draft_view.boards)

        if len(eligible) <= 1:
            # Auto-win or no valid boards
            if eligible:
                winner_uid = eligible[0]
                winner_name = resolve_name(guild, winner_uid)
                await channel.send(f"\U0001f3c6 **{discord.utils.escape_markdown(winner_name)}** wins by default!")
            else:
                winner_uid = None
                await channel.send("No valid boards — nobody wins!")
            await self._show_recap(
                draft_view, channel, guild, settings,
                votes={}, winner_uids=[winner_uid] if winner_uid else [],
            )
            return

        await asyncio.sleep(5)

        # Voting phase
        vote_view = RushmoreVoteView(game_id, eligible, guild, self.db, self.bot)
        self.bot.active_views[game_id] = vote_view

        vote_timer = settings.get("vote_timer", 30)
        vote_embed = build_vote_embed(draft_view.host_name, draft_view.topic, vote_timer)

        try:
            vote_msg = await channel.send(embed=vote_embed, view=vote_view)
            vote_view._msg = vote_msg
        except Exception:
            await self._show_recap(draft_view, channel, guild, settings, votes={}, winner_uids=[])
            return

        # Timer
        vote_done = asyncio.Event()
        vote_view._done_event = vote_done

        async def _on_vote_timer():
            vote_done.set()

        from bot_modules.games.utils.timer import GameTimer
        timer = GameTimer(duration=vote_timer, message=vote_msg, callback=_on_vote_timer, timer_field_index=0)
        vote_view._timer_obj = timer
        await timer.start()
        await vote_done.wait()

        # Bail if the game was force-ended (e.g. /games end) while voting.
        if game_id not in self.bot.active_views:
            return

        # Disable vote view
        for item in vote_view.children:
            item.disabled = True
        try:
            await vote_msg.edit(view=vote_view)
        except discord.HTTPException:
            pass

        # Tally
        winner_uids, max_votes, results_by_uid = tally_votes(vote_view.votes, eligible)

        winner_names = [resolve_name(guild, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        # Resolve names for the sorted results
        all_results = [(resolve_name(guild, uid), v) for uid, v in results_by_uid]

        winner_embed = build_winner_embed(
            draft_view.host_name, draft_view.topic,
            winner_names, max_votes, winner_boards_list, all_results,
        )
        try:
            await vote_msg.edit(embed=winner_embed, view=None)
        except discord.HTTPException:
            pass

        await asyncio.sleep(5)
        await self._show_recap(draft_view, channel, guild, settings, vote_view.votes, winner_uids)

    async def _show_recap(
        self,
        draft_view: RushmoreDraftView,
        channel,
        guild,
        settings: dict,
        votes: dict[int, int],
        winner_uids: list[int],
    ):
        game_id = draft_view.game_id
        duration = _time.time() - draft_view._draft_start

        # Recompute max_votes from the (possibly empty) vote tally
        tally: dict[int, int] = {}
        for _voter, target in votes.items():
            tally[target] = tally.get(target, 0) + 1
        max_votes = max(tally.values()) if tally else 0

        winner_names = [resolve_name(guild, uid) for uid in winner_uids]
        winner_boards_list = [draft_view.boards.get(str(uid), []) for uid in winner_uids]

        stats = compute_recap_stats(
            draft_view.draft_order,
            draft_view.boards,
            draft_view.all_picks,
            draft_view.pick_times,
            draft_view.skipped,
            votes,
            name_resolver=lambda uid: resolve_name(guild, uid),
        )

        recap_embed = build_recap_embed(
            draft_view.host_name, draft_view.topic, len(draft_view.players),
            duration, winner_names, max_votes, winner_boards_list, stats,
        )
        recap_view = RushmoreRecapView(game_id, draft_view.host_id, self, settings)

        try:
            await channel.send(embed=recap_embed, view=recap_view)
        except discord.HTTPException:
            pass

        # End game
        payload = await get_game_payload(self.db, game_id)
        payload["votes"] = {str(k): str(v) for k, v in votes.items()}
        await end_game(
            self.db, game_id,
            player_count=len(draft_view.players),
            round_count=DRAFT_ROUNDS,
            payload=payload,
        )
        if game_id in self.bot.active_views:
            del self.bot.active_views[game_id]


async def setup(bot: "Bot"):
    cog = RushmoreCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("rushmore")
    play.add_command(cog.rushmore_cmd, override=True)
    bot.game_launchers["rushmore"] = cog.launch
    bot.game_recoverers["rushmore"] = cog.recover_game
