import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord

from bot_modules.core.utils import disable_all_items
from discord.ext import commands
from discord import app_commands

from bot_modules.games.constants import (
    GAME_ICONS,
    HOW_TO_PLAY,
)
from bot_modules.games.utils.game_manager import (
    finish_launch_response,
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
    resolve_name,
    channel_name,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.games.utils.recovery import start_redrive
from bot_modules.games.utils.question_source import (
    get_clapback_prompt,
    has_clapback_prompts,
    channel_allows_nsfw,
)
from bot_modules.games.command_groups import play
from bot_modules.games_clapback.logic import (
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
    """Get a prompt from the question bank (Clapback is bank-only)."""
    tags = config.get("tags") or None
    allow_nsfw = bool(config.get("allow_nsfw", False))
    return await get_clapback_prompt(db, exclude=used, tags=tags, allow_nsfw=allow_nsfw)


# ── Modal ────────────────────────────────────────────────────────────────────


class ClapbackAnswerModal(discord.ui.Modal, title="Your Answer"):
    answer_input = discord.ui.TextInput(
        label="Your funniest answer",
        placeholder="Type your answer here…",
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
    # Inactivity window; every button press resets it. A scheduled start
    # (start_in) extends the first window past the advertised start time.
    LOBBY_TIMEOUT = 600

    def __init__(
        self, game_id: str, host_id: int, db, bot, cog, config: dict,
        accent: "discord.Color | None" = None,
    ):
        timeout = float(self.LOBBY_TIMEOUT)
        start_epoch = config.get("start_epoch")
        if start_epoch:
            timeout = max(timeout, start_epoch - time.time() + 120)
        super().__init__(timeout=timeout)
        self.game_id = game_id
        self.host_id = host_id
        self.db = db
        self.bot = bot
        self.cog = cog
        self.config = config
        # Guild accent resolved once at game start; reused for every live
        # lobby edit (join/leave) instead of re-resolving per button press.
        self.accent = accent
        self.message: discord.Message | None = None

    def _is_host_or_mod(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.host_id:
            return True
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def on_timeout(self):
        await self.cog._cancel_game(self.game_id, reason="Lobby timed out")
        # Retire the lobby message — a live-looking Join/Start row on a dead
        # view swallows clicks as "This interaction failed".
        if self.message is not None:
            disable_all_items(self)
            try:
                await self.message.edit(
                    content="⌛ **Lobby timed out** — the game wasn't started in time. Run `/games play clapback` to open a new one.",
                    view=self,
                )
            except discord.HTTPException:
                pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, custom_id="ql_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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

    @discord.ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="ql_start")
    async def start_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can start.", ephemeral=True)
            return

        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        min_p = MIN_PLAYERS

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

        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))

        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        # Players are pinged per-round when each round's prompt is posted
        # (see _submit_phase), so there's no separate start ping here.

        # Initialize scores
        payload["scores"] = {str(p): 0 for p in players}
        # Snapshot of scores as of the last fully-completed round. Restored on
        # crash-resume so a round interrupted mid-scoring can't double-count.
        payload["scores_checkpoint"] = {str(p): 0 for p in players}
        payload["clapbacks"] = {str(p): 0 for p in players}
        payload["current_round"] = 0
        payload["round_history"] = []
        payload["used_prompts"] = []
        payload["phase"] = "playing"
        payload["last_bye"] = None
        await update_game_payload(self.db, self.game_id, payload)

        try:
            await self.cog._run_game(self.game_id, channel, payload)
        except Exception as e:
            log.error("Clapback game %s crashed: %s", self.game_id, e, exc_info=True)
            await channel.send("❌ Something went wrong. Game ended.")
            await end_game(self.db, self.game_id)
            self.bot.active_views.pop(self.game_id, None)

    @discord.ui.button(label="❓ Help", style=discord.ButtonStyle.secondary, custom_id="ql_htp")
    async def how_to_play(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
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
            color=self.accent,
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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="✏️ Submit", style=discord.ButtonStyle.primary, custom_id="ql_submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        payload = await get_game_payload(self.db, self.game_id)
        players = payload.get("players", [])
        if interaction.user.id not in players:
            await interaction.response.send_message(
                "You're not in this game. Join next round!", ephemeral=True,
            )
            return
        modal = ClapbackAnswerModal(self.game_id, self.round_num, self.db, self.cog)
        await interaction.response.send_modal(modal)


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
        if interaction.guild and isinstance(interaction.user, discord.Member):
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

        # Anyone (players and spectators alike) can vote — the only people
        # blocked are the two contestants in this matchup, above.

        idx = self.matchup_index

        def _store_vote(payload):
            matchups = payload.get("matchups", [])
            if idx < len(matchups):
                matchups[idx]["votes"][str(uid)] = voted_for

        await modify_payload(self.db, self.game_id, _store_vote)

        await interaction.response.send_message(
            f"Voted for {label}!", ephemeral=True, delete_after=3,
        )


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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    async def on_timeout(self):
        self._advanced.set()

    @discord.ui.button(label="▶️ Next Round", style=discord.ButtonStyle.primary, custom_id="ql_next")
    async def next_round(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host or a mod can advance.", ephemeral=True)
            return
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)
        self._advanced.set()


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
        if interaction.guild and isinstance(interaction.user, discord.Member):
            perms = interaction.user.guild_permissions
            return perms.administrator or perms.manage_guild
        return False

    @discord.ui.button(label="🔁 Play Again", style=discord.ButtonStyle.primary, custom_id="ql_replay")
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host can start a rematch.", ephemeral=True)
            return
        self.stop()
        disable_all_items(self)
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
        log.info("%s pressed '%s' in #%s", interaction.user.display_name, button.label, channel_name(interaction.channel))
        if not self._is_host_or_mod(interaction):
            await interaction.response.send_message("Only the host can start a rematch.", ephemeral=True)
            return
        channel = interaction.channel
        assert channel is not None and not isinstance(channel, (discord.ForumChannel, discord.CategoryChannel))
        self.stop()
        disable_all_items(self)
        await interaction.response.edit_message(view=self)

        shuffled = shuffled_replay_config(self.config)
        await channel.send(
            f"🔀 **Shuffled settings:** {shuffled['rounds']} rounds, "
            f"{shuffled['timer']}s submit, {shuffled['vote_timer']}s vote"
        )
        await self.cog._start_new_game(
            channel=channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild=interaction.guild,
            config=shuffled,
        )


# ── Cog ──────────────────────────────────────────────────────────────────────


class ClapbackCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot
        # Events used to signal early completion of submit / vote phases
        self._submit_events: dict[str, asyncio.Event] = {}
        self._vote_events: dict[str, asyncio.Event] = {}
        self._game_cancelled: set[str] = set()
        # Guild accent resolved ONCE per game at start (or on recovery) and
        # reused by every phase's embed — never re-resolved per vote / update.
        self._accents: dict[str, "discord.Color | None"] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def _resolve_accent(self, guild) -> "discord.Color | None":
        """Resolve the guild accent once, swallowing any failure to None.

        Kept guild-tolerant: with no guild (headless / DM) or a resolver
        error we return None and the builders fall back to their neutral
        default rather than crashing the game loop.
        """
        if guild is None:
            return None
        try:
            return await resolve_accent_color(self.bot.ctx.db_path, guild)
        except Exception:
            log.warning("clapback accent resolve failed for guild %s", getattr(guild, "id", "?"))
            return None

    async def recover_game(self, row, payload, channel, message) -> bool:
        """Re-drive the game from the next un-played round after a restart.

        Completed rounds live in payload["round_history"]; _run_game resumes at
        len(round_history)+1, re-running the interrupted round after rolling
        scores back to the last-completed-round checkpoint so its partial
        mid-scoring mutations can't double-count. The stale phase message is
        retired and the game loop is re-spawned in the background.
        """
        if not payload.get("config"):
            return False
        game_id = row["game_id"]
        self._game_cancelled.discard(game_id)
        # Accent cache is lost across a restart — re-resolve it once here so the
        # resumed phases stay on-theme without re-resolving per update.
        self._accents[game_id] = await self._resolve_accent(getattr(channel, "guild", None))
        resume_round = len(payload.get("round_history", [])) + 1
        await start_redrive(
            self.bot, game_id, message,
            self._run_game(game_id, channel, payload),
            channel=channel, log_label=f"clapback game {game_id} (resuming at round {resume_round})",
        )
        return True

    # ── Slash command ────────────────────────────────────────────────────

    @app_commands.command(name="clapback", description="Start a Clapback game — comedy head-to-head!")
    @app_commands.describe(
        start_in="Show a lobby countdown — game starts in this many minutes (host still clicks Start)",
    )
    async def clapback(
        self,
        interaction: discord.Interaction,
        start_in: app_commands.Range[int, 1, 60] | None = None,
    ):
        log.info(
            "%s used /games play clapback in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
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

        # Clapback is bank-only, so an empty bank means there's nothing to play.
        if not await has_clapback_prompts(self.db):
            await interaction.response.send_message(
                "No prompts in the bank for Clapback. "
                "Add some from the Games question bank on the web dashboard.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"start_in": start_in},
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
        # Clapback is bank-only; nothing to play if the bank is empty. Covers
        # headless (scheduled) launches, which skip the slash pre-check.
        if not await has_clapback_prompts(self.db):
            log.warning("clapback launch skipped in channel %s — question bank is empty", getattr(channel, "id", "?"))
            return None
        # Pacing/content knobs live in the per-server dashboard config (game_opts);
        # an explicit *options* value (e.g. from a saved schedule) still wins.
        game_opts = await get_game_options(self.db, "clapback", guild_id)
        rounds, timer, vote_timer = clamp_config_values(
            int(options.get("rounds", game_opts.get("rounds", 5))),
            int(options.get("timer", game_opts.get("timer", 120))),
            int(options.get("vote_timer", game_opts.get("vote_timer", 40))),
        )
        start_in_min = options.get("start_in")
        start_epoch = int(time.time()) + int(start_in_min) * 60 if start_in_min else None
        # Normalize tags to a list — the dashboard/scheduler store a
        # comma-separated string, an explicit options value may be a list.
        tags_cfg = options.get("tags", game_opts.get("tags", ""))
        if isinstance(tags_cfg, str):
            tags = [t.strip() for t in tags_cfg.split(",") if t.strip()]
        else:
            tags = [str(t).strip() for t in (tags_cfg or []) if str(t).strip()]
        config = {
            "rounds": rounds,
            "timer": timer,
            "vote_timer": vote_timer,
            "anonymous": bool(options.get("anonymous", game_opts.get("anonymous", False))),
            "start_epoch": start_epoch,
            "tags": tags,
            "allow_nsfw": channel_allows_nsfw(channel),
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
            payload={"config": config, "players": [], "host_id": host_id},
        )
        log.info("Game %s (clapback) created by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        # Resolve the guild accent ONCE for the whole game and cache it; every
        # phase (lobby / submit / vote / reveal / scoreboard / recap) reuses it.
        accent = await self._resolve_accent(guild)
        self._accents[game_id] = accent
        embed = build_lobby_embed(
            host_name=host_name,
            config=config,
            players=[],
            name_resolver=lambda uid: resolve_name(guild, uid),
            start_at=config.get("start_epoch"),
            color=accent,
        )

        view = ClapbackJoinView(game_id, host_id, self.db, self.bot, self, config, accent=accent)
        self.bot.active_views[game_id] = view

        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await end_game(self.db, game_id)
            self.bot.active_views.pop(game_id, None)
            log.warning("clapback launch lacked send perms in channel %s", channel.id)
            return None

        view.message = msg
        await update_game_message(self.db, game_id, msg.id)
        await update_session(self.db, channel.id, game_id, [host_id])
        return game_id

    # ── Game loop ────────────────────────────────────────────────────────

    async def _run_game(self, game_id: str, channel, payload: dict):
        config = payload["config"]
        total_rounds = config["rounds"]
        host_id = payload.get("host_id") or payload["players"][0]

        # Resume-aware: a fresh game has an empty round_history (starts at round
        # 1); after a crash we continue from the round after the last completed
        # one. That interrupted round is re-run from scratch, so we roll scores
        # back to the last-completed-round checkpoint first — otherwise the
        # round's partial mid-scoring mutations would be counted twice.
        payload = await get_game_payload(self.db, game_id)
        start_round = len(payload.get("round_history", [])) + 1
        if "scores_checkpoint" in payload:
            payload["scores"] = dict(payload["scores_checkpoint"])
            await update_game_payload(self.db, game_id, payload)

        for round_num in range(start_round, total_rounds + 1):
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
                    answers, config, host_id, round_num, len(matchups), prompt,
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
            # Round fully scored — checkpoint so a later crash resumes from here.
            payload["scores_checkpoint"] = dict(payload.get("scores", {}))
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
                await self._post_scoreboard(game_id, channel, payload, round_num, total_rounds, bye_player, final=True)

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
            color=self._accents.get(game_id),
        )

        view = ClapbackSubmitView(game_id, host_id, round_num, self.db, self.bot, self)
        self.bot.active_views[game_id] = view

        # Ping the active players so nobody misses a new round starting. Only
        # user mentions go in the content, so no @everyone/@role pings.
        guild = getattr(channel, "guild", None)
        content = None
        if guild:
            mentions = " ".join(
                member.mention
                for uid in players
                if (member := guild.get_member(uid))
            )
            if mentions:
                content = f"{ICON} **Round {round_num} starting!** {mentions}"

        msg = await channel.send(content=content, embed=embed, view=view)
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
                except discord.HTTPException:
                    pass

            if count >= len(players):
                break

        self._submit_events.pop(game_id, None)

        # Disable submit view
        view.stop()
        disable_all_items(view)
        try:
            p = await get_game_payload(self.db, game_id)
            count = len(p.get("answers", {}))
            embed.set_field_at(0, name="Timer", value="⏱️ Closed", inline=True)
            embed.set_field_at(1, name="Answers In", value=f"{count}/{len(players)}", inline=True)
            await msg.edit(embed=embed, view=view)
        except discord.HTTPException:
            pass

        payload = await get_game_payload(self.db, game_id)
        return payload.get("answers", {})

    # ── Vote matchup ─────────────────────────────────────────────────────

    async def _vote_matchup(
        self, game_id, channel, payload, matchup_index, matchup,
        answers, config, host_id, round_num, total_matchups, prompt,
    ):
        from bot_modules.games.utils.timer import format_deadline, now_plus
        player_a, player_b = int(matchup["pair"][0]), int(matchup["pair"][1])
        players = payload["players"]
        answer_a = answers.get(str(player_a), "???")
        answer_b = answers.get(str(player_b), "???")
        anonymous = config.get("anonymous", False)
        vote_timer = config["vote_timer"]
        deadline = now_plus(vote_timer)

        accent = self._accents.get(game_id)
        embed = build_vote_embed(
            answer_a=answer_a,
            answer_b=answer_b,
            round_num=round_num,
            matchup_index=matchup_index,
            total_matchups=total_matchups,
            deadline_str=format_deadline(deadline),
            vote_count=0,
            prompt=prompt,
            color=accent,
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
                    except discord.HTTPException:
                        pass

        # Voting is open to everyone, so "all eligible voters have voted" is
        # no longer determinable — the matchup always runs the full vote timer
        # (a host /games end pops the game from active_views, which the next
        # _is_cancelled check below catches within a second).

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
            prompt=prompt,
            color=accent,
        )

        # Edit message with reveal (buttons disabled)
        disable_all_items(view)
        try:
            await msg.edit(embed=reveal, view=view)
        except discord.HTTPException:
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
        embed = build_scoreboard_embed(
            payload, round_num, total_rounds, bye_player,
            final=False, color=self._accents.get(game_id),
        )
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
        disable_all_items(view)
        try:
            await msg.edit(view=view)
        except discord.HTTPException:
            pass
        return True

    async def _post_scoreboard(self, game_id, channel, payload, round_num, total_rounds, bye_player, final=False):
        embed = build_scoreboard_embed(
            payload, round_num, total_rounds, bye_player,
            final=final, color=self._accents.get(game_id),
        )
        await channel.send(embed=embed)

    # ── Final recap ──────────────────────────────────────────────────────

    async def _post_recap(self, game_id, channel, payload, config):
        players = payload.get("players", [])
        guild = channel.guild if hasattr(channel, "guild") else None

        embed = build_recap_embed(
            payload=payload,
            config=config,
            name_resolver=lambda uid: resolve_name(guild, uid),
            color=self._accents.get(game_id),
        )
        if guild:
            from bot_modules.economy.game_rewards import append_payout_footer
            await append_payout_footer(self.bot, embed, guild.id, "clapback")

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
            bot=self.bot, player_ids=list(players),
        )
        self.bot.active_views.pop(game_id, None)
        self._cleanup(game_id)

    # ── Helpers ──────────────────────────────────────────────────────────

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
        self._accents.pop(game_id, None)
        self._game_cancelled.discard(game_id)

    # ── Mid-game join / leave (dispatched from /games join, /games leave) ──

    async def mid_game_join(self, channel, game_id: str, member):
        """Add *member* to a running game. The submit phase re-reads the
        roster each round, so they play from the next round on."""
        uid = member.id
        state: dict = {}

        def _add(payload):
            players = payload.setdefault("players", [])
            if uid in players:
                state["already"] = True
                return
            players.append(uid)
            payload.setdefault("scores", {}).setdefault(str(uid), 0)
            payload.setdefault("clapbacks", {}).setdefault(str(uid), 0)

        await modify_payload(self.db, game_id, _add)
        if state.get("already"):
            return False, f"**{member.display_name}** is already in this game."
        return True, f"{ICON} **{member.display_name}** joined Clapback — they'll play from the next round!"

    async def mid_game_leave(self, channel, game_id: str, member):
        """Remove *member* from a running game. Their score stays on the board."""
        uid = member.id
        state: dict = {}

        def _remove(payload):
            players = payload.setdefault("players", [])
            if uid not in players:
                state["missing"] = True
                return
            players.remove(uid)

        await modify_payload(self.db, game_id, _remove)
        if state.get("missing"):
            return False, f"**{member.display_name}** isn't in this game."
        return True, f"{ICON} **{member.display_name}** left Clapback — their score stays on the board."


async def setup(bot: "Bot"):
    cog = ClapbackCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("clapback")
    play.add_command(cog.clapback, override=True)
    bot.game_launchers["clapback"] = cog.launch
    bot.game_recoverers["clapback"] = cog.recover_game
    bot.game_joiners["clapback"] = cog.mid_game_join
    bot.game_leavers["clapback"] = cog.mid_game_leave
