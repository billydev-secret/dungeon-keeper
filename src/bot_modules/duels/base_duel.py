"""BaseDuel — shared lifecycle for all nickname-duel games."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Any

import discord
from discord.ext import commands, tasks

from bot_modules.services.embeds import COLOR_GOLD, COLOR_YELLOW

from . import db as duels_db
from .filters import validate_nickname, validate_stakes
from .modals import NicknameModal
from .views import ChallengeView, ResultView

log = logging.getLogger("dungeonkeeper.duels")

_RATE_LIMIT_WINDOW = 3600
_RATE_LIMIT_MAX = 3


class BaseDuel(commands.Cog):
    """Abstract base for all nickname-duel games.

    Subclasses must define:
      GAME_KEY            str  e.g. 'pressure'
      GAME_DISPLAY_NAME   str  e.g. 'Pressure Cooker'

    And implement all hooks that raise NotImplementedError.
    """

    GAME_KEY: str = ""
    GAME_DISPLAY_NAME: str = ""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._game_locks: dict[int, asyncio.Lock] = {}
        self._challenge_rate: dict[int, collections.deque] = collections.defaultdict(
            lambda: collections.deque()
        )

    @property
    def db(self):
        return self.bot.games_db  # type: ignore[attr-defined]

    def _get_lock(self, game_id: int) -> asyncio.Lock:
        if game_id not in self._game_locks:
            self._game_locks[game_id] = asyncio.Lock()
        return self._game_locks[game_id]

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        active = await self._db_fetch_active_games()
        for game in active:
            if game.message_id:
                view = self.build_game_view(game.id)
                self.bot.add_view(view, message_id=game.message_id)
                await self.on_game_resume(game)

        resolved = await self._db_fetch_resolved_games()
        for game in resolved:
            if game.result_message_id and game.winner_id and game.loser_id:
                self.bot.add_view(
                    ResultView(game.id, game.winner_id, game.loser_id, self._handle_set_nick),
                    message_id=game.result_message_id,
                )

        self._expire_loop.start()
        log.info(
            "%s loaded: %d active, %d resolved",
            self.GAME_DISPLAY_NAME,
            len(active),
            len(resolved),
        )

    async def cog_unload(self) -> None:
        self._expire_loop.cancel()

    # ── Background sweep ──────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _expire_loop(self) -> None:
        now = time.time()
        try:
            games = await self._db_fetch_sweepable(now)
            for game in games:
                if game.state == "PENDING":
                    await self._expire_pending(game)
                elif game.state == "ACTIVE":
                    await self._expire_active(game)
                elif game.state == "RESOLVED":
                    await self._expire_resolved(game)

            nicks = await duels_db.fetch_expired_nicks(self.db, now)
            for nick_row in nicks:
                await self._revert_nick(nick_row)
        except Exception:
            log.exception("%s expire loop error", self.GAME_DISPLAY_NAME)

    @_expire_loop.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    async def _expire_pending(self, game: Any) -> None:
        await self._db_set_state(game.id, "EXPIRED_PENDING")
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="⏱️ Challenge Expired",
                description="No response in time.",
                color=COLOR_YELLOW,
            ),
            view=None,
        )

    async def _expire_active(self, game: Any) -> None:
        await self._db_set_state(game.id, "ABANDONED")
        self._game_locks.pop(game.id, None)
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="🏳️ Game Abandoned",
                description="No activity in 5 minutes. Game over — no nickname consequences.",
                color=COLOR_YELLOW,
            ),
            view=None,
        )

    async def _expire_resolved(self, game: Any) -> None:
        await self._db_set_state(game.id, "NO_NICK_SET")
        if game.result_message_id:
            await self._edit_message_silent(
                game.channel_id,
                game.result_message_id,
                embed=discord.Embed(
                    title="⏰ Nickname Not Set",
                    description="Winner didn't set a nickname in time. No rename applied.",
                    color=COLOR_YELLOW,
                ),
                view=None,
            )

    async def _revert_nick(self, nick_row: dict) -> None:
        guild = self.bot.get_guild(nick_row["guild_id"])
        if not guild:
            await duels_db.mark_nick_reverted(self.db, nick_row["id"], "guild_gone")
            return
        member = guild.get_member(nick_row["loser_id"])
        if not member:
            await duels_db.mark_nick_reverted(self.db, nick_row["id"], "member_gone")
            return
        try:
            original = nick_row["original_nick"]
            await member.edit(nick=original, reason=f"{self.GAME_DISPLAY_NAME} sentence expired")
            await duels_db.mark_nick_reverted(self.db, nick_row["id"], "expired")
            restored = original or member.name
            try:
                await member.send(
                    f"Your {self.GAME_DISPLAY_NAME} nickname sentence has expired. "
                    f"Your nickname has been restored to **{restored}**."
                )
            except discord.Forbidden:
                pass
            log.info(
                "Reverted nick for user %d in guild %d (restored: %r)",
                nick_row["loser_id"],
                nick_row["guild_id"],
                original,
            )
        except discord.Forbidden:
            await duels_db.mark_nick_reverted(self.db, nick_row["id"], "forbidden")
            log.warning(
                "Forbidden reverting nick for %d in guild %d",
                nick_row["loser_id"],
                nick_row["guild_id"],
            )
        except discord.HTTPException as e:
            log.exception("HTTP error reverting nick: %s", e)

    async def _edit_message_silent(
        self,
        channel_id: int,
        message_id: int | None,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        if not message_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)  # type: ignore[union-attr]
            await msg.edit(embed=embed, view=view)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ── Permission preflight ──────────────────────────────────────────────────

    async def _check_bot_can_nick(
        self,
        guild: discord.Guild,
        challenger: discord.Member,
        target: discord.Member,
    ) -> str | None:
        me = guild.me
        if not me.guild_permissions.manage_nicknames:
            return "I need the **Manage Nicknames** permission to enforce this game."
        for member in (challenger, target):
            if member.id != guild.owner_id and me.top_role <= member.top_role:
                return (
                    "My highest role must be above both players' roles to rename the loser. "
                    "Ask an admin to fix my role position."
                )
        return None

    async def _check_no_active_nick(
        self,
        guild: discord.Guild,
        challenger: discord.Member,
        target: discord.Member,
    ) -> str | None:
        for member in (challenger, target):
            nick = await duels_db.get_active_nick_for_user(self.db, guild.id, member.id)
            if nick:
                return (
                    f"**{member.display_name}** is serving a nickname sentence "
                    f"and can't play again until it expires."
                )
        return None

    # ── Rate limit ────────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: int) -> bool:
        dq = self._challenge_rate[user_id]
        now = time.time()
        while dq and now - dq[0] > _RATE_LIMIT_WINDOW:
            dq.popleft()
        return len(dq) >= _RATE_LIMIT_MAX

    def _record_challenge(self, user_id: int) -> None:
        self._challenge_rate[user_id].append(time.time())

    # ── Shared challenge entrypoint ───────────────────────────────────────────

    async def _base_challenge(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        stakes_text: str | None,
    ) -> None:
        """Run all pre-game checks and create a challenge embed. Called by subclass command."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        challenger = interaction.user  # type: ignore[assignment]
        guild: discord.Guild = interaction.guild

        if target.id == challenger.id:
            await interaction.response.send_message(
                "You can't challenge yourself.", ephemeral=True
            )
            return
        if target.bot:
            await interaction.response.send_message(
                "You can't challenge a bot.", ephemeral=True
            )
            return

        cfg = await duels_db.get_config(self.db, guild.id, self.GAME_KEY)
        allowlist: list[int] = json.loads(cfg.get("channel_allowlist") or "[]")
        if allowlist and interaction.channel_id not in allowlist:
            await interaction.response.send_message(
                f"{self.GAME_DISPLAY_NAME} isn't allowed in this channel.", ephemeral=True
            )
            return

        if self._check_rate_limit(challenger.id):
            await interaction.response.send_message(
                f"You've issued too many challenges recently. "
                f"Maximum {_RATE_LIMIT_MAX} per hour.",
                ephemeral=True,
            )
            return

        perm_error = await self._check_bot_can_nick(guild, challenger, target)  # type: ignore[arg-type]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        nick_error = await self._check_no_active_nick(guild, challenger, target)  # type: ignore[arg-type]
        if nick_error:
            await interaction.response.send_message(nick_error, ephemeral=True)
            return

        existing = await self._db_get_active_game_for_pair(guild.id, challenger.id, target.id)
        if existing:
            await interaction.response.send_message(
                "You two already have a game in progress.", ephemeral=True
            )
            return

        cooldown = await duels_db.check_cooldown(
            self.db, guild.id, self.GAME_KEY, challenger.id, target.id,
            cfg["cooldown_hours"],
        )
        if cooldown is not None:
            hours = int(cooldown // 3600)
            mins = int((cooldown % 3600) // 60)
            await interaction.response.send_message(
                f"You two need to wait **{hours}h {mins}m** before playing again.",
                ephemeral=True,
            )
            return

        if stakes_text:
            stakes_result = validate_stakes(
                stakes_text,
                max_length=cfg["max_stakes_length"],
                denylist=json.loads(cfg.get("nick_denylist") or "[]"),
            )
            if not stakes_result.ok:
                await interaction.response.send_message(
                    f"Stakes rejected: {stakes_result.reason}", ephemeral=True
                )
                return
            stakes_text = stakes_result.value or None

        game_id = await self._db_create_game(
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            challenger_id=challenger.id,
            target_id=target.id,
            stakes_text=stakes_text,
        )
        self._record_challenge(challenger.id)

        embed = self._build_challenge_embed(challenger, target, stakes_text)  # type: ignore[arg-type]
        view = ChallengeView(
            game_id=game_id,
            target_id=target.id,
            on_accept=self._handle_accept,
            on_decline=self._handle_decline,
        )
        await interaction.response.send_message(
            content=target.mention, embed=embed, view=view
        )
        msg = await interaction.original_response()
        await self._db_set_state(game_id, "PENDING", message_id=msg.id)

    def _build_challenge_embed(
        self,
        challenger: discord.Member,
        target: discord.Member,
        stakes: str | None,
    ) -> discord.Embed:
        stakes_text = stakes or "Loser surrenders their nickname for 24 hours."
        embed = discord.Embed(
            title=f"⚔️ {self.GAME_DISPLAY_NAME.upper()} CHALLENGE",
            color=COLOR_GOLD,
        )
        embed.add_field(
            name="Challenge",
            value=f"{challenger.mention} has challenged {target.mention}!",
            inline=False,
        )
        embed.add_field(name="📋 Stakes", value=stakes_text, inline=False)
        embed.set_footer(text="⏱️ 60 seconds to respond.")
        return embed

    # ── View callbacks ────────────────────────────────────────────────────────

    async def _handle_accept(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "PENDING":
            await interaction.response.send_message(
                "This challenge is no longer active.", ephemeral=True
            )
            return

        await self._db_set_state(game_id, "ACTIVE")
        await self.on_game_start(game)

        # Re-fetch after on_game_start (subclass may have set additional fields)
        game = await self._db_get_game(game_id)
        if not game:
            return

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        view = self.build_game_view(game.id)
        embed = self.render_game_state(game, guild)
        self.bot.add_view(view, message_id=game.message_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _handle_decline(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "PENDING":
            await interaction.response.send_message(
                "This challenge is no longer active.", ephemeral=True
            )
            return
        await self._db_set_state(game_id, "DECLINED")
        embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"{interaction.user.mention} declined the challenge.",
            color=COLOR_YELLOW,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def _handle_game_button(self, interaction: discord.Interaction, game_id: int) -> None:
        """Entry point for all in-game button presses. Subclass builds_game_view passes this."""
        await interaction.response.defer()
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game:
                await interaction.followup.send("Game not found.", ephemeral=True)
                return
            if game.state != "ACTIVE":
                await interaction.followup.send("This game is no longer active.", ephemeral=True)
                return

            status, loser_id = await self.handle_interaction(interaction, game)

            if status == "rejected":
                return

            if status == "done":
                assert loser_id is not None
                winner_id = (
                    game.challenger_id if loser_id != game.challenger_id else game.target_id
                )
                await self._post_result(interaction, game, winner_id, loser_id)
            else:  # "continue"
                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                embed = self.render_game_state(game, guild)
                await interaction.edit_original_response(embed=embed)

    async def _post_result(
        self,
        interaction: discord.Interaction,
        game: Any,
        winner_id: int,
        loser_id: int,
    ) -> None:
        guild: discord.Guild = interaction.guild  # type: ignore[assignment]

        await duels_db.set_cooldown(
            self.db, game.guild_id, self.GAME_KEY, game.challenger_id, game.target_id
        )

        result_embed = self.render_result_state(game, guild)
        result_view = ResultView(game.id, winner_id, loser_id, self._handle_set_nick)

        winner_m = guild.get_member(winner_id)
        loser_m = guild.get_member(loser_id)
        ping_content = " ".join(m.mention for m in (winner_m, loser_m) if m)

        result_msg = await interaction.followup.send(
            content=ping_content, embed=result_embed, view=result_view
        )
        self.bot.add_view(result_view, message_id=result_msg.id)  # type: ignore[union-attr]
        await self._db_set_state(game.id, "RESOLVED", result_message_id=result_msg.id)  # type: ignore[union-attr]
        await self.on_game_resolved(game.id)
        self._game_locks.pop(game.id, None)

    async def _handle_set_nick(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "RESOLVED":
            await interaction.response.send_message(
                "A nickname has already been set for this game.", ephemeral=True
            )
            return
        await interaction.response.send_modal(NicknameModal(game_id, self._handle_nick_submit))

    async def _handle_nick_submit(
        self, interaction: discord.Interaction, game_id: int, raw_nick: str
    ) -> None:
        async with self._get_lock(game_id):
            await self._handle_nick_submit_locked(interaction, game_id, raw_nick)

    async def _handle_nick_submit_locked(
        self, interaction: discord.Interaction, game_id: int, raw_nick: str
    ) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "RESOLVED":
            await interaction.response.send_message(
                "A nickname has already been set for this game.", ephemeral=True
            )
            return
        if interaction.user.id != game.winner_id:
            await interaction.response.send_message(
                "Only the winner can set the nickname.", ephemeral=True
            )
            return

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        cfg = await duels_db.get_config(self.db, guild.id, self.GAME_KEY)
        denylist: list[str] = json.loads(cfg.get("nick_denylist") or "[]")

        admin_names = [
            m.display_name
            for m in guild.members
            if m.guild_permissions.administrator or m.guild_permissions.manage_guild
        ]
        all_names = [m.display_name for m in guild.members]

        nick_result = validate_nickname(
            raw_nick,
            max_length=cfg["max_nick_length"],
            denylist=denylist,
            admin_display_names=admin_names,
            all_member_display_names=all_names,
        )
        if not nick_result.ok:
            await interaction.response.send_message(
                f"Nickname rejected: {nick_result.reason}", ephemeral=True
            )
            return

        cleaned_nick = nick_result.value
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        if not loser:
            await interaction.response.send_message(
                "The loser appears to have left the server. No rename applied.", ephemeral=True
            )
            await self._db_set_state(game_id, "NO_NICK_SET")
            return

        challenger_member = guild.get_member(game.challenger_id)  # type: ignore[arg-type]
        perm_error = await self._check_bot_can_nick(guild, challenger_member or loser, loser)  # type: ignore[arg-type]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        original_nick = loser.nick

        if loser.id == guild.owner_id:
            await duels_db.apply_nick(
                self.db,
                game_id=game.id,
                game_type=self.GAME_KEY,
                guild_id=guild.id,
                loser_id=game.loser_id,  # type: ignore[arg-type]
                winner_id=game.winner_id,  # type: ignore[arg-type]
                original_nick=original_nick,
                imposed_nick=cleaned_nick,
                sentence_hours=cfg["sentence_hours"],
            )
            await self._db_set_state(game_id, "NICKED")
            embed = self.render_result_state(game, guild, imposed_nick=cleaned_nick)
            await interaction.response.edit_message(
                embed=embed, view=self._disabled_result_view(game)
            )
            await interaction.followup.send(
                f"📋 Discord won't let me rename the server owner. "
                f"**{loser.display_name}**, your sentence is: **{cleaned_nick}** — please apply it yourself.",
            )
            return

        try:
            await loser.edit(
                nick=cleaned_nick,
                reason=f"{self.GAME_DISPLAY_NAME}: lost to {interaction.user.display_name}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to rename that user.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Failed to rename: {e}", ephemeral=True
            )
            return

        await duels_db.apply_nick(
            self.db,
            game_id=game.id,
            game_type=self.GAME_KEY,
            guild_id=guild.id,
            loser_id=game.loser_id,  # type: ignore[arg-type]
            winner_id=game.winner_id,  # type: ignore[arg-type]
            original_nick=original_nick,
            imposed_nick=cleaned_nick,
            sentence_hours=cfg["sentence_hours"],
        )
        await self._db_set_state(game_id, "NICKED")

        embed = self.render_result_state(game, guild, imposed_nick=cleaned_nick)
        await interaction.response.edit_message(
            embed=embed, view=self._disabled_result_view(game)
        )

    def _disabled_result_view(self, game: Any) -> ResultView:
        view = ResultView(
            game.id,
            game.winner_id,  # type: ignore[arg-type]
            game.loser_id,  # type: ignore[arg-type]
            self._handle_set_nick,
        )
        view.disable()
        return view

    # ── Timer hooks (no-op stubs — override in timer-based games) ─────────────

    async def on_game_start(self, game: Any) -> None:
        """Called when a challenge is accepted, before the game embed is posted."""

    async def on_game_resume(self, game: Any) -> None:
        """Called on cog_load for each ACTIVE game — restart timer if needed."""

    async def on_game_resolved(self, game_id: int) -> None:
        """Called after result message is posted — cancel any running timers."""

    # ── Abstract DB hooks (subclass must implement) ───────────────────────────

    async def _db_create_game(
        self,
        guild_id: int,
        channel_id: int,
        challenger_id: int,
        target_id: int,
        stakes_text: str | None,
    ) -> int:
        raise NotImplementedError

    async def _db_get_game(self, game_id: int) -> Any | None:
        raise NotImplementedError

    async def _db_get_active_game_for_pair(
        self, guild_id: int, user_a: int, user_b: int
    ) -> Any | None:
        raise NotImplementedError

    async def _db_get_pending_for_challenger(
        self, guild_id: int, channel_id: int, user_id: int
    ) -> Any | None:
        raise NotImplementedError

    async def _db_set_state(self, game_id: int, state: str, **kw: Any) -> None:
        raise NotImplementedError

    async def _db_fetch_active_games(self) -> list:
        raise NotImplementedError

    async def _db_fetch_resolved_games(self) -> list:
        raise NotImplementedError

    async def _db_fetch_sweepable(self, now: float) -> list:
        raise NotImplementedError

    # ── Abstract game hooks (subclass must implement) ─────────────────────────

    def render_game_state(self, game: Any, guild: discord.Guild) -> discord.Embed:
        """Return the current game embed (live game state)."""
        raise NotImplementedError

    def render_result_state(
        self, game: Any, guild: discord.Guild, **kwargs: Any
    ) -> discord.Embed:
        """Return the result embed (post-game outcome). Accepts imposed_nick kwarg."""
        raise NotImplementedError

    def build_game_view(self, game_id: int) -> discord.ui.View:
        """Return a fresh View whose buttons call self._handle_game_button."""
        raise NotImplementedError

    async def handle_interaction(
        self, interaction: discord.Interaction, game: Any
    ) -> tuple[str, int | None]:
        """Process a button press. Return one of:
          ("rejected", None)  — invalid press, already sent ephemeral feedback
          ("continue", None)  — valid press, game continues; BaseDuel re-renders
          ("done", loser_id)  — game over; handle_interaction updated game embed
        """
        raise NotImplementedError
