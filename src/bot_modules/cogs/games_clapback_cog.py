import asyncio
import logging
import re
import time

import discord
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import (
    GAME_ICONS,
    HOW_TO_PLAY,
)
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    get_game_options,
    create_game,
    update_game_message,
    update_game_payload,
    get_game_payload,
    modify_payload,
    end_game,
    update_session,
    ConfirmCloseView,
    resolve_name,
)
from bot_modules.games.utils.question_source import get_clapback_prompt, has_clapback_prompts
from bot_modules.games.utils.ai_client import generate_text
from bot_modules.games.command_groups import play
from bot_modules.games_clapback.logic import (
    AI_SYSTEM_PROMPT,
    AI_USER_PROMPT,
    MAX_PLAYERS,
    MIN_PLAYERS,
    calculate_matchup_score,
    clamp_config_values,
    create_matchups,
    shuffled_replay_config,
)
from bot_modules.games_clapback.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
    build_scoreboard_embed,
    build_submit_embed,
    build_vote_embed,
)

log = logging.getLogger(__name__)

ICON = GAME_ICONS["clapback"]


# ── Prompt fetching ──────────────────────────────────────────────────────────


async def fetch_prompt(db, config: dict, used: list[str]) -> str | None:
    """Get a prompt from bank and/or AI depending on *source*."""
    source = config.get("source", "both")

    if source == "bank":
        return await get_clapback_prompt(db, exclude=used)

    if source == "ai":
        return await _ai_prompt(used)

    # source == "both": try bank first, fall back to AI
    prompt = await get_clapback_prompt(db, exclude=used)
    if prompt:
        return prompt
    return await _ai_prompt(used)


async def _ai_prompt(used: list[str]) -> str | None:
    for _ in range(2):  # retry once on failure
        result = await generate_text(AI_SYSTEM_PROMPT, AI_USER_PROMPT, max_tokens=100)
        if not result:
            continue
        # Find the first non-header, non-empty line
        prompt = None
        for line in result.strip().splitlines():
            line = line.strip().lstrip("-•*0123456789). ").strip('"').strip()
            if line and not line.startswith("#"):
                prompt = line
                break
        if prompt and prompt not in used:
            return prompt
    return None


# ── Modal ────────────────────────────────────────────────────────────────────


class ClapbackAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer_input = discord.ui.TextInput(
        label="Your funniest answer",
        placeholder="Type your answer here...",
        style=discord.TextStyle.short,
        required=True,
        max_length=200,
    )

    def __init__(self, game_id: str, round_num: int, db, cog):
        super().__init__()
        self.title = f"Round {round_num} — Your Answer"
        self.game_id = game_id
        self.db = db
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        log.info(
            "%s submitted answer in game %s",
            interaction.user.display_name, self.game_id,
        )
        answer = re.sub(r'@(everyone|here)', '@​\\1', self.answer_input.value.strip())
        if not answer:
            await interaction.response.send_message(
                "Nice try, but you need to actually write something. 😄",
                ephemeral=True,
            )
            return

        uid = interaction.user.id

        def _store(payload):
            answers = payload.setdefault("answers", {})
            answers[str(uid)] = answer

        await modify_payload(self.db, self.game_id, _store)
        await interaction.response.send_message(
            "Answer submitted! You can click Submit again to change it before time runs out.",
            ephemeral=True,
        )

        # Signal the cog that a new answer arrived
        cog = self.cog
        if self.game_id in cog._submit_events:
            cog._submit_events[self.game_id].set()


# ── Views ────────────────────────────────────────────────────────────────────


class ClapbackJoinView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog, config: dict):
        super().__init__(timeout=300)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.config = config

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def on_timeout(self):
        await self.cog._cancel_game(self.game_id, reason="Lobby timed out")

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="ql_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id

        def _add(payload):
            players = payload.setdefault("players", [])
            if uid not in players:
                players.append(uid)

        payload = await modify_payload(self.db, self.game_id, _add)
        log.info("%s joined game %s", interaction.user.display_name, self.game_id)
        await self._update_embed(interaction, payload)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="ql_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        uid = interaction.user.id

        if uid == self.host_id:
            await interaction.response.send_message(
                "You're the host! If you leave, the game will be cancelled. "
                "Use **Cancel** instead.",
                ephemeral=True,
            )
            return

        def _remove(payload):
            players = payload.setdefault("players", [])
            if uid in players:
                players.remove(uid)

        payload = await modify_payload(self.db, self.game_id, _remove)
        log.info("%s left game %s", interaction.user.display_name, self.game_id)
        await self._update_embed(interaction, payload)

    @discord.ui.button(label="▶️ Start Game", style=discord.ButtonStyle.primary, custom_id="ql_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        min_p = self.config.get("min_players", MIN_PLAYERS)

        if len(players) < min_p:
            await interaction.response.send_message(
                f"Need at least {min_p} players to start Clapback. Currently: {len(players)}.",
                ephemeral=True,
            )
            return
        if len(players) > MAX_PLAYERS:
            await interaction.response.send_message(
                f"Clapback supports up to {MAX_PLAYERS} players. {len(players)} are joined — ask some to sit this one out.",
                ephemeral=True,
            )
            return

        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        # Ping players
        if interaction.guild:
            mentions = [
                interaction.guild.get_member(uid).mention
                for uid in players
                if interaction.guild.get_member(uid)
            ]
            if mentions:
                try:
                    await interaction.followup.send(
                        f"{ICON} **Clapback is starting!** {' '.join(mentions)}",
                    )
                except discord.Forbidden:
                    log.warning("Clapback %s: missing perms to send start ping in #%s", self.game_id, getattr(interaction.channel, "name", "?"))

        # Initialize scores
        payload["scores"] = {str(p): 0 for p in players}
        payload["clapbacks"] = {str(p): 0 for p in players}
        payload["current_round"] = 0
        payload["round_history"] = []
        payload["used_prompts"] = []
        payload["phase"] = "playing"
        payload["last_bye"] = None
        await update_game_payload(self.db, self.game_id, payload)

        try:
            await self.cog._run_game(self.game_id, interaction.channel, payload)
        except Exception as e:
            log.error("Clapback game %s crashed: %s", self.game_id, e, exc_info=True)
            await interaction.channel.send("❌ Something went wrong. Game ended.")
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

    @discord.ui.button(label="🛑 Cancel", style=discord.ButtonStyle.danger, custom_id="ql_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can cancel.", ephemeral=True)
            return
        game_msg = interaction.message

        async def _confirmed(confirm_interaction):
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await game_msg.edit(content="Game cancelled.", view=self)
            except Exception:
                pass
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to cancel this game?", view=view, ephemeral=True,
        )

    @discord.ui.button(label="❓ How to Play", style=discord.ButtonStyle.secondary, custom_id="ql_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        cfg = self.config
        text = HOW_TO_PLAY["clapback"] + (
            f"\n\n⏱️ **{cfg['timer']}s** to write each answer\n"
            f"🗳️ **{cfg['vote_timer']}s** to vote each matchup\n"
            f"🏆 **{cfg['rounds']}** rounds — highest score wins"
        )
        await interaction.response.send_message(text, ephemeral=True)

    async def _update_embed(self, interaction: discord.Interaction, payload: dict):
        players = payload.get("players", [])
        guild = interaction.guild

        host_member = guild.get_member(self.host_id) if guild else None
        host_name = host_member.display_name if host_member else "Host"

        embed = build_lobby_embed(
            host_name=host_name,
            config=self.config,
            players=players,
            name_resolver=lambda uid: resolve_name(guild, uid),
            start_at=self.config.get("start_epoch"),
        )
        await interaction.response.edit_message(embed=embed, view=self)


class ClapbackSubmitView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, round_num: int, db, bot, cog):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.round_num = round_num
        self.db = db
        self.bot = bot
        self.cog = cog

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="✏️ Submit Answer", style=discord.ButtonStyle.primary, custom_id="ql_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if interaction.user.id not in players:
            await interaction.response.send_message(
                "You're not in this game. Join next round!", ephemeral=True,
            )
            return
        modal = ClapbackAnswerModal(self.game_id, self.round_num, self.db, self.cog)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🛑 End Game", style=discord.ButtonStyle.danger, custom_id="ql_submit_end", row=1)
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can end the game.", ephemeral=True)
            return
        await self.cog._confirm_end(interaction, self.game_id)


class ClapbackVoteView(discord.ui.View):
    def __init__(
        self, game_id: str, host_id: int, matchup_index: int,
        player_a: int, player_b: int, players: list[int],
        db, bot, cog,
    ):
        super().__init__(timeout=None)
        self.game_id = game_id
        self.host_id = host_id
        self.matchup_index = matchup_index
        self.player_a = player_a
        self.player_b = player_b
        self.players = players
        self.db = db
        self.bot = bot
        self.cog = cog
        self._closed = False

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="🅰️", style=discord.ButtonStyle.primary, custom_id="ql_vote_a", row=0)
    async def vote_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, self.player_a, "🅰️")

    @discord.ui.button(label="🅱️", style=discord.ButtonStyle.primary, custom_id="ql_vote_b", row=0)
    async def vote_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_vote(interaction, self.player_b, "🅱️")

    async def _do_vote(self, interaction: discord.Interaction, voted_for: int, label: str):
        log.info("%s voted in game %s", interaction.user.display_name, self.game_id)
        if self._closed:
            await interaction.response.send_message("Voting is closed.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid in (self.player_a, self.player_b):
            await interaction.response.send_message(
                "You can't vote on your own matchup! 😎",
                ephemeral=True,
            )
            return
        if uid not in self.players:
            await interaction.response.send_message(
                "Only players in this game can vote.",
                ephemeral=True,
            )
            return

        idx = self.matchup_index

        def _store_vote(payload):
            matchups = payload.get("matchups", [])
            if idx < len(matchups):
                matchups[idx]["votes"][str(uid)] = voted_for

        payload = await modify_payload(self.db, self.game_id, _store_vote)
        matchups = payload.get("matchups", [])
        vote_count = len(matchups[idx]["votes"]) if idx < len(matchups) else 0

        await interaction.response.send_message(
            f"Voted for {label}!", ephemeral=True, delete_after=3,
        )

        # Matchup participants can't vote — everyone else does.
        eligible = len(self.players) - 2
        if vote_count >= eligible and eligible > 0:
            if self.game_id in self.cog._vote_events:
                self.cog._vote_events[self.game_id].set()

    @discord.ui.button(label="🛑 End Game", style=discord.ButtonStyle.danger, custom_id="ql_vote_end", row=1)
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can end the game.", ephemeral=True)
            return
        await self.cog._confirm_end(interaction, self.game_id)


class ClapbackRoundSummaryView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, db, bot, cog):
        super().__init__(timeout=15)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self._advanced = asyncio.Event()

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def on_timeout(self):
        self._advanced.set()

    @discord.ui.button(label="▶️ Next Round", style=discord.ButtonStyle.primary, custom_id="ql_next")
    async def next_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self._advanced.set()

    @discord.ui.button(label="🛑 End Game", style=discord.ButtonStyle.danger, custom_id="ql_round_end")
    async def end_game_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can end the game.", ephemeral=True)
            return
        await self.cog._confirm_end(interaction, self.game_id)


class ClapbackRecapView(discord.ui.View):
    def __init__(self, game_id: str, host_id: int, config: dict, db, bot, cog):
        super().__init__(timeout=120)
        self.game_id = game_id
        self.host_id = host_id
        self.config = config
        self.db = db
        self.bot = bot
        self.cog = cog

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild:
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="🔄 Play Again", style=discord.ButtonStyle.primary, custom_id="ql_replay")
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host can start a rematch.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        await self.cog._start_new_game(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild=interaction.guild,
            config=self.config,
        )

    @discord.ui.button(label="🔀 Play Again (Shuffled)", style=discord.ButtonStyle.secondary, custom_id="ql_replay_shuffle")
    async def play_again_shuffled(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, interaction.channel.name if interaction.channel else "unknown")
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host can start a rematch.", ephemeral=True)
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        shuffled = shuffled_replay_config(self.config)
        await interaction.channel.send(
            f"🔀 **Shuffled settings:** {shuffled['rounds']} rounds, "
            f"{shuffled['timer']}s submit, {shuffled['vote_timer']}s vote"
        )
        await self.cog._start_new_game(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild=interaction.guild,
            config=shuffled,
        )


# ── Cog ──────────────────────────────────────────────────────────────────────


class ClapbackCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Events used to signal early completion of submit / vote phases
        self._submit_events: dict[str, asyncio.Event] = {}
        self._vote_events: dict[str, asyncio.Event] = {}
        self._game_cancelled: set[str] = set()

    @property
    def db(self):
        return self.bot.games_db

    # ── Slash command ────────────────────────────────────────────────────

    @app_commands.command(name="clapback", description="Start a Clapback game — comedy head-to-head!")
    @app_commands.describe(
        rounds="Number of prompt rounds (1-15, default 5)",
        timer="Seconds for answer submission (15-180, default 120)",
        vote_timer="Seconds per matchup vote (10-60, default 40)",
        source="Where prompts come from",
        anonymous="Hide author names until final recap",
        start_in="Show a lobby countdown — game starts in this many minutes (host still clicks Start)",
    )
    @app_commands.choices(
        source=[
            app_commands.Choice(name="AI Generated", value="ai"),
            app_commands.Choice(name="Question Bank", value="bank"),
            app_commands.Choice(name="Both", value="both"),
        ],
    )
    async def clapback(
        self,
        interaction: discord.Interaction,
        rounds: int = 5,
        timer: int = 120,
        vote_timer: int = 40,
        source: str = "both",
        anonymous: bool = False,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play clapback in #%s",
            interaction.user.display_name,
            interaction.channel.name if interaction.channel else "unknown",
        )

        # Pre-flight checks
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return
        if not await check_game_enabled(self.db, "clapback", interaction.guild_id or 0):
            await interaction.response.send_message("Clapback is currently disabled on this server.", ephemeral=True)
            return

        # Hard bank-error pre-check (nice ephemeral message; launch falls back silently).
        if source == "bank" and not await has_clapback_prompts(self.db):
            await interaction.response.send_message(
                "No prompts in the bank for Clapback. "
                "Add some with `/bank add clapback` or use `source:ai`.",
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
                "rounds": rounds, "timer": timer, "vote_timer": vote_timer,
                "source": source, "anonymous": anonymous,
                "start_in": start_in,
            },
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
        rounds, timer, vote_timer = clamp_config_values(
            int(options.get("rounds", 5)),
            int(options.get("timer", 120)),
            int(options.get("vote_timer", 40)),
        )
        source = options.get("source", "both")
        # Bank fallback: if the bank is empty, fall back to AI (no user to prompt headless).
        if source in ("bank", "both") and not await has_clapback_prompts(self.db):
            source = "ai"
        start_in_min = options.get("start_in")
        start_epoch = int(time.time()) + int(start_in_min) * 60 if start_in_min else None
        game_opts = await get_game_options(self.db, "clapback", guild_id)
        min_players = int(game_opts.get("min_players", MIN_PLAYERS))
        config = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "source": source,
            "anonymous": bool(options.get("anonymous", False)),
            "start_epoch": start_epoch,
            "min_players": min_players,
        }
        return await self._start_new_game(
            channel=channel, host_id=host_id, host_name=host_name,
            guild=getattr(channel, "guild", None), config=config,
        )

    async def _start_new_game(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild,
        config: dict,
    ) -> str | None:
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "clapback",
            state="joining",
            payload={"config": config, "players": []},
        )
        log.info("Game %s (clapback) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        embed = build_lobby_embed(
            host_name=host_name,
            config=config,
            players=[],
            name_resolver=lambda uid: resolve_name(guild, uid),
            start_at=config.get("start_epoch"),
        )

        view = ClapbackJoinView(game_id, host_id, self.db, self.bot, self, config)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("clapback launch lacked send perms in channel %s", channel.id)
            return None

        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Game loop ────────────────────────────────────────────────────────

    async def _run_game(self, game_id: str, channel, payload: dict):
        config = payload["config"]
        total_rounds = config["rounds"]
        host_id = payload.get("host_id") or payload["players"][0]

        for round_num in range(1, total_rounds + 1):
            if self._is_cancelled(game_id):
                return

            payload = await get_game_payload(self.db, game_id)
            payload["current_round"] = round_num
            payload["phase"] = "submitting"
            payload["answers"] = {}
            await update_game_payload(self.db, game_id, payload)

            # Get prompt
            prompt = await fetch_prompt(self.db, config, payload.get("used_prompts", []))
            if not prompt:
                await channel.send(
                    f"Couldn't generate a prompt for round {round_num} — skipping.",
                )
                continue

            payload["prompt"] = prompt
            payload["used_prompts"] = payload.get("used_prompts", []) + [prompt]
            await update_game_payload(self.db, game_id, payload)

            # Submit phase
            answers = await self._submit_phase(
                game_id, channel, payload, prompt, round_num, config, host_id,
            )
            if self._is_cancelled(game_id):
                return

            if len(answers) < 2:
                await channel.send("Not enough answers this round — moving on!")
                continue

            # Create matchups
            last_bye = payload.get("last_bye")
            matchups, bye_player = create_matchups(answers, last_bye)
            payload = await get_game_payload(self.db, game_id)
            payload["matchups"] = matchups
            payload["phase"] = "voting"
            if bye_player is not None:
                payload["last_bye"] = bye_player
                # Award bye points
                payload["scores"][str(bye_player)] = payload["scores"].get(str(bye_player), 0) + 50
            await update_game_payload(self.db, game_id, payload)

            # Vote phase — process each matchup sequentially
            round_matchup_results = []
            for mi, matchup in enumerate(matchups):
                if self._is_cancelled(game_id):
                    return
                result = await self._vote_matchup(
                    game_id, channel, payload, mi, matchup,
                    answers, config, host_id, round_num, len(matchups),
                )
                if result is None:
                    return  # game cancelled
                round_matchup_results.append(result)
                await asyncio.sleep(1)

            if self._is_cancelled(game_id):
                return

            # Record round history
            payload = await get_game_payload(self.db, game_id)
            round_record = {
                "round": round_num,
                "prompt": prompt,
                "matchups": round_matchup_results,
            }
            if bye_player is not None:
                round_record["bye_player"] = bye_player
            payload.setdefault("round_history", []).append(round_record)
            payload["phase"] = "revealing"
            await update_game_payload(self.db, game_id, payload)

            # Round summary
            is_last = round_num == total_rounds
            if not is_last:
                should_continue = await self._round_summary(
                    game_id, channel, payload, round_num, total_rounds, host_id, bye_player,
                )
                if not should_continue or self._is_cancelled(game_id):
                    return
                await asyncio.sleep(2)  # between-round breather
            else:
                # Show final summary scoreboard briefly before recap
                await self._post_scoreboard(channel, payload, round_num, total_rounds, bye_player, final=True)

        if self._is_cancelled(game_id):
            return

        # Final recap
        payload = await get_game_payload(self.db, game_id)
        await self._post_recap(game_id, channel, payload, config)

    # ── Submit phase ─────────────────────────────────────────────────────

    async def _submit_phase(
        self, game_id, channel, payload, prompt, round_num, config, host_id,
    ):
        from bot_modules.games.utils.timer import format_deadline, now_plus
        players = payload["players"]
        timer_secs = config["timer"]
        deadline = now_plus(timer_secs)

        embed = build_submit_embed(
            prompt=prompt,
            round_num=round_num,
            total_rounds=config["rounds"],
            deadline_str=format_deadline(deadline),
            answers_in=0,
            total_players=len(players),
        )

        view = ClapbackSubmitView(game_id, host_id, round_num, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        msg = await channel.send(embed=embed, view=view)
        await update_game_message(self.db, game_id, msg.id)

        submit_event = asyncio.Event()
        self._submit_events[game_id] = submit_event

        elapsed = 0
        last_count = 0
        last_edit_at = -5  # triggers first timer update at elapsed=1
        while elapsed < timer_secs:
            if self._is_cancelled(game_id):
                break
            submit_event.clear()
            try:
                await asyncio.wait_for(submit_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            elapsed += 1

            p = await get_game_payload(self.db, game_id)
            count = len(p.get("answers", {}))
            count_changed = count != last_count
            timer_due = (elapsed - last_edit_at) >= 5

            if count_changed or timer_due:
                if count_changed:
                    last_count = count
                last_edit_at = elapsed
                remaining = max(0, timer_secs - elapsed)
                mins, secs = divmod(remaining, 60)
                timer_val = f"⏰ {mins}:{secs:02d}" if mins else f"⏰ {secs}s"
                embed.set_field_at(0, name="Timer", value=timer_val, inline=True)
                embed.set_field_at(1, name="Answers In", value=f"{count}/{len(players)}", inline=True)
                try:
                    await msg.edit(embed=embed)
                except Exception:
                    pass

            if count >= len(players):
                break

        self._submit_events.pop(game_id, None)

        # Disable submit view
        view.stop()
        for item in view.children:
            item.disabled = True
        try:
            p = await get_game_payload(self.db, game_id)
            count = len(p.get("answers", {}))
            embed.set_field_at(0, name="Timer", value="⏱️ Closed", inline=True)
            embed.set_field_at(1, name="Answers In", value=f"{count}/{len(players)}", inline=True)
            await msg.edit(embed=embed, view=view)
        except Exception:
            pass

        payload = await get_game_payload(self.db, game_id)
        return payload.get("answers", {})

    # ── Vote matchup ─────────────────────────────────────────────────────

    async def _vote_matchup(
        self, game_id, channel, payload, matchup_index, matchup,
        answers, config, host_id, round_num, total_matchups,
    ):
        from bot_modules.games.utils.timer import format_deadline, now_plus
        player_a, player_b = int(matchup["pair"][0]), int(matchup["pair"][1])
        players = payload["players"]
        answer_a = answers.get(str(player_a), "???")
        answer_b = answers.get(str(player_b), "???")
        anonymous = config.get("anonymous", False)
        vote_timer = config["vote_timer"]
        deadline = now_plus(vote_timer)

        embed = build_vote_embed(
            answer_a=answer_a,
            answer_b=answer_b,
            round_num=round_num,
            matchup_index=matchup_index,
            total_matchups=total_matchups,
            deadline_str=format_deadline(deadline),
            vote_count=0,
        )

        view = ClapbackVoteView(
            game_id, host_id, matchup_index,
            player_a, player_b, players,
            self.db, self.bot, self,
        )
        self.bot.active_views[game_id] = view

        msg = await channel.send(embed=embed, view=view)

        vote_event = asyncio.Event()
        self._vote_events[game_id] = vote_event

        elapsed = 0
        last_vcount = 0
        last_edit_at = -5  # triggers first timer update at elapsed=1
        while elapsed < vote_timer:
            if self._is_cancelled(game_id):
                return None
            vote_event.clear()
            try:
                await asyncio.wait_for(vote_event.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            elapsed += 1

            p = await get_game_payload(self.db, game_id)
            m = p.get("matchups", [])
            if matchup_index < len(m):
                vcount = len(m[matchup_index].get("votes", {}))
                vcount_changed = vcount != last_vcount
                timer_due = (elapsed - last_edit_at) >= 5

                if vcount_changed or timer_due:
                    if vcount_changed:
                        last_vcount = vcount
                    last_edit_at = elapsed
                    remaining = max(0, vote_timer - elapsed)
                    mins, secs = divmod(remaining, 60)
                    timer_val = f"⏰ {mins}:{secs:02d}" if mins else f"⏰ {secs}s"
                    embed.set_field_at(0, name="Timer", value=timer_val, inline=True)
                    embed.set_field_at(1, name="Votes", value=str(vcount), inline=True)
                    try:
                        await msg.edit(embed=embed)
                    except Exception:
                        pass

                eligible = len(players)
                if vcount >= eligible and eligible > 0:
                    break

        self._vote_events.pop(game_id, None)
        view._closed = True
        view.stop()

        # Calculate result
        payload = await get_game_payload(self.db, game_id)
        matchup_data = payload["matchups"][matchup_index]
        result = calculate_matchup_score(matchup_data["votes"], player_a, player_b)

        # Update scores in payload
        for pid, pts in result["scores"].items():
            payload["scores"][str(pid)] = payload["scores"].get(str(pid), 0) + pts
        if result["clapback"]:
            winner_id = result["winner"]
            if winner_id:
                payload["clapbacks"][str(winner_id)] = payload["clapbacks"].get(str(winner_id), 0) + 1
        payload["matchups"][matchup_index]["winner"] = result["winner"]
        await update_game_payload(self.db, game_id, payload)

        # Build reveal embed
        guild = channel.guild if hasattr(channel, "guild") else None
        reveal = build_reveal_embed(
            result=result,
            answers=answers,
            player_a=player_a,
            player_b=player_b,
            anonymous=anonymous,
            name_resolver=lambda uid: resolve_name(guild, uid),
        )

        # Edit message with reveal (buttons disabled)
        for item in view.children:
            item.disabled = True
        try:
            await msg.edit(embed=reveal, view=view)
        except Exception:
            pass

        await asyncio.sleep(4)  # Let players read the reveal

        # Build result record for round history
        vc = result["vote_counts"]
        return {
            "player_a": player_a,
            "answer_a": answer_a,
            "votes_a": vc[player_a],
            "player_b": player_b,
            "answer_b": answer_b,
            "votes_b": vc[player_b],
            "clapback": result["clapback"],
        }

    # ── Round summary ────────────────────────────────────────────────────

    async def _round_summary(
        self, game_id, channel, payload, round_num, total_rounds, host_id, bye_player,
    ):
        embed = build_scoreboard_embed(payload, round_num, total_rounds, bye_player, final=False)
        view = ClapbackRoundSummaryView(game_id, host_id, self.db, self.bot, self)
        self.bot.active_views[game_id] = view
        msg = await channel.send(embed=embed, view=view)

        # Auto-advance after 10s or host click
        try:
            await asyncio.wait_for(view._advanced.wait(), timeout=10)
        except asyncio.TimeoutError:
            view._advanced.set()

        if self._is_cancelled(game_id):
            return False

        view.stop()
        for item in view.children:
            item.disabled = True
        try:
            await msg.edit(view=view)
        except Exception:
            pass
        return True

    async def _post_scoreboard(self, channel, payload, round_num, total_rounds, bye_player, final=False):
        embed = build_scoreboard_embed(payload, round_num, total_rounds, bye_player, final=final)
        await channel.send(embed=embed)

    # ── Final recap ──────────────────────────────────────────────────────

    async def _post_recap(self, game_id, channel, payload, config):
        players = payload.get("players", [])
        guild = channel.guild if hasattr(channel, "guild") else None

        embed = build_recap_embed(
            payload=payload,
            config=config,
            name_resolver=lambda uid: resolve_name(guild, uid),
        )

        rounds_played = len(payload.get("round_history", []))
        host_id = payload.get("host_id") or (players[0] if players else 0)
        view = ClapbackRecapView(game_id, host_id, config, self.db, self.bot, self)
        await channel.send(embed=embed, view=view)

        # End game
        log.info("Game %s ended — %d players, %d rounds", game_id, len(players), rounds_played)
        await end_game(
            self.db, game_id,
            player_count=len(players),
            round_count=rounds_played,
            payload=payload,
        )
        self.bot.active_views.pop(game_id, None)
        self._cleanup(game_id)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _confirm_end(self, interaction: discord.Interaction, game_id: str):
        """Show end-game confirmation. Used by multiple views."""
        channel = interaction.channel

        async def _confirmed(confirm_interaction):
            self._game_cancelled.add(game_id)
            # Unblock any waiting events
            ev = self._submit_events.pop(game_id, None)
            if ev:
                ev.set()
            ev = self._vote_events.pop(game_id, None)
            if ev:
                ev.set()

            # Post partial recap if scores exist
            payload = await get_game_payload(self.db, game_id)
            if payload.get("scores"):
                await self._post_recap(game_id, channel, payload, payload.get("config", {}))
            else:
                await end_game(self.db, game_id)
                self.bot.active_views.pop(game_id, None)
                self._cleanup(game_id)
                await channel.send(f"{ICON} Game ended by host.")

        view = ConfirmCloseView(_confirmed)
        await interaction.response.send_message(
            "⚠️ Are you sure you want to end this game?", view=view, ephemeral=True,
        )

    async def _cancel_game(self, game_id: str, reason: str = ""):
        """Silently cancel a game (e.g. lobby timeout)."""
        self._game_cancelled.add(game_id)
        log.info("Game %s cancelled: %s", game_id, reason)
        await end_game(self.db, game_id)
        self.bot.active_views.pop(game_id, None)
        self._cleanup(game_id)

    def _is_cancelled(self, game_id: str) -> bool:
        return game_id in self._game_cancelled or game_id not in self.bot.active_views

    def _cleanup(self, game_id: str):
        self._submit_events.pop(game_id, None)
        self._vote_events.pop(game_id, None)
        self._game_cancelled.discard(game_id)


async def setup(bot: commands.Bot):
    cog = ClapbackCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("clapback")
    play.add_command(cog.clapback)
    bot.game_launchers["clapback"] = cog.launch
