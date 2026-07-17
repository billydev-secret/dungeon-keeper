"""BaseGame — shared lifecycle for all nickname-stake games (2..N players).

`BaseDuel` (the fixed 2-player special case) and the N-player group games both
subclass this. Everything here is roster-count-agnostic: lifecycle, the background
expiry/auto-revert sweep, the nickname-stake flow (one winner names one loser), rate
limiting, and the abstract DB/game hooks. Pairwise-specific behaviour (the single
opponent accept/decline challenge) lives in `BaseDuel`; lobby/elimination behaviour for
N>2 is added by `lobby.py` helpers and the group cogs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

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
from .lobby import LobbyView
from .modals import NicknameModal
from .views import ResultView

log = logging.getLogger("dungeonkeeper.duels")

_RATE_LIMIT_WINDOW = 3600
_RATE_LIMIT_MAX = 3


class BaseGame(commands.Cog):
    """Abstract base for all nickname-stake games (2..N players).

    Subclasses must define:
      GAME_KEY            str  e.g. 'pressure'
      GAME_DISPLAY_NAME   str  e.g. 'Pressure Cooker'

    And implement all hooks that raise NotImplementedError.
    """

    GAME_KEY: str = ""
    GAME_DISPLAY_NAME: str = ""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._game_locks: dict[int, asyncio.Lock] = {}
        self._challenge_rate: dict[int, collections.deque] = collections.defaultdict(
            lambda: collections.deque()
        )

    @property
    def db(self):
        return self.bot.games_db

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

        lobby = await self._db_fetch_lobby_games()
        for game in lobby:
            if game.message_id:
                self.bot.add_view(
                    self._build_lobby_view(game.id, game.host_id),
                    message_id=game.message_id,
                )

        self._expire_loop.start()
        log.info(
            "%s loaded: %d active, %d resolved, %d lobby",
            self.GAME_DISPLAY_NAME,
            len(active),
            len(resolved),
            len(lobby),
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
                elif game.state == "LOBBY":
                    await self._expire_lobby(game)
                elif game.state == "ACTIVE":
                    await self._expire_active(game)
                elif game.state == "RESOLVED":
                    await self._expire_resolved(game)

            nicks = await duels_db.fetch_expired_nicks(self.db, now, self.GAME_KEY)
            for nick_row in nicks:
                await self._revert_nick(nick_row)
        except Exception:
            log.exception("%s expire loop error", self.GAME_DISPLAY_NAME)

    @_expire_loop.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Re-apply an unexpired nickname sentence when a sentenced member
        rejoins, so leaving and coming back can't be used to dodge it."""
        row = await duels_db.get_active_nick_for_user(self.db, member.guild.id, member.id)
        # get_active_nick_for_user returns any game's active sentence; only the
        # owning cog handles it, so all duel cogs don't redundantly re-apply.
        if not row or row.get("game_type") != self.GAME_KEY:
            return
        if float(row.get("expires_at") or 0) <= time.time():
            return  # already lapsed — the expire loop will revert it
        imposed = row.get("imposed_nick")
        if not imposed:
            return
        try:
            await member.edit(
                nick=imposed,
                reason=f"{self.GAME_DISPLAY_NAME} sentence still active (rejoined)",
            )
            log.info(
                "Re-applied active nick for rejoining user %d in guild %d",
                member.id, member.guild.id,
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

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

    async def _expire_lobby(self, game: Any) -> None:
        await self._db_set_state(game.id, "EXPIRED_LOBBY")
        self._game_locks.pop(game.id, None)
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="⏱️ Lobby Expired",
                description="Not enough players started in time.",
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

    async def _edit_embed_silent(
        self,
        channel_id: int,
        message_id: int | None,
        embed: discord.Embed,
    ) -> None:
        """Edit only the embed, leaving the message's components untouched.

        Used by high-frequency updates (e.g. Chicken's meter ticker). Re-sending the
        view on every edit re-renders the action row, which can make an in-flight button
        click fail ("interaction failed") before it reaches the bot — so we never touch
        components here.
        """
        if not message_id:
            return
        channel = self.bot.get_channel(channel_id)
        if not channel:
            return
        try:
            msg = await channel.fetch_message(message_id)  # type: ignore[union-attr]
            await msg.edit(embed=embed)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    # ── Permission preflight ──────────────────────────────────────────────────

    async def _check_bot_can_nick(
        self,
        guild: discord.Guild,
        members: list[discord.Member],
    ) -> str | None:
        me = guild.me
        if not me.guild_permissions.manage_nicknames:
            return "I need the **Manage Nicknames** permission to enforce this game."
        for member in members:
            if member.id != guild.owner_id and me.top_role <= member.top_role:
                return (
                    "My highest role must be above all players' roles to rename the loser. "
                    "Ask an admin to fix my role position."
                )
        return None

    async def _check_no_active_nick(
        self,
        guild: discord.Guild,
        members: list[discord.Member],
    ) -> str | None:
        for member in members:
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

    # ── Nickname-stake flow (one winner names one loser) ──────────────────────

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
        perm_error = await self._check_bot_can_nick(guild, [challenger_member or loser, loser])  # type: ignore[list-item]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        # Guard against overlapping sentences: if the loser is already serving a
        # nick sentence from a concurrent game, applying a second one here would
        # snapshot the already-imposed nick as the "original" and corrupt the
        # eventual revert. Refuse rather than stack sentences.
        existing_sentence = await self._check_no_active_nick(guild, [loser])
        if existing_sentence:
            await interaction.response.send_message(
                f"**{loser.display_name}** is already serving a nickname sentence from "
                "another game. Your win stands, but a new nickname can't be applied until "
                "that one expires.",
                ephemeral=True,
            )
            await self._db_set_state(game_id, "NO_NICK_SET")
            return

        original_nick = loser.nick
        # The displayed name *before* the rename, for the result embed's
        # "is now known as" line. Captured here because the render runs after
        # loser.edit() below, by which point loser.display_name is the new nick
        # (rendering "NewNick is now known as NewNick"). Distinct from
        # original_nick, which is None when the loser had no prior nickname.
        original_display_name = loser.display_name

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
            embed = self.render_result_state(
                game, guild, imposed_nick=cleaned_nick, original_name=original_display_name
            )
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

        embed = self.render_result_state(
                game, guild, imposed_nick=cleaned_nick, original_name=original_display_name
            )
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

    # ── Lobby flow (N-player games) ───────────────────────────────────────────

    def _build_lobby_view(self, game_id: int, host_id: int) -> LobbyView:
        return LobbyView(
            game_id,
            host_id,
            on_join=self._handle_lobby_join,
            on_leave=self._handle_lobby_leave,
            on_start=self._handle_lobby_start,
            on_cancel=self._handle_lobby_cancel,
        )

    def _render_lobby(
        self, game: Any, guild: discord.Guild, min_players: int, max_players: int
    ) -> discord.Embed:
        names = []
        for uid in game.roster:
            m = guild.get_member(uid)
            names.append(m.display_name if m else str(uid))
        host = guild.get_member(game.host_id)
        host_name = host.display_name if host else str(game.host_id)
        embed = discord.Embed(
            title=f"🎮 {self.GAME_DISPLAY_NAME.upper()} — LOBBY",
            description="Press **✋ Join** to get in. Host presses **▶️ Start** when ready.",
            color=COLOR_GOLD,
        )
        embed.add_field(
            name=f"👥 Players ({len(game.roster)}/{max_players})",
            value="\n".join(f"• {n}" for n in names) or "—",
            inline=False,
        )
        stakes = game.stakes_text or "Last one standing wins; the final loser surrenders their nickname for 24h."
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        embed.set_footer(text=f"Host: {host_name} • Need {min_players}+ players to start.")
        return embed

    async def _base_lobby(
        self, interaction: discord.Interaction, stakes_text: str | None
    ) -> None:
        """Open a join lobby for an N-player game. Called by a subclass /start command."""
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        host = interaction.user  # type: ignore[assignment]
        guild: discord.Guild = interaction.guild

        cfg = await duels_db.get_config(self.db, guild.id, self.GAME_KEY)
        allowlist: list[int] = json.loads(cfg.get("channel_allowlist") or "[]")
        if allowlist and interaction.channel_id not in allowlist:
            await interaction.response.send_message(
                f"{self.GAME_DISPLAY_NAME} isn't allowed in this channel.", ephemeral=True
            )
            return

        if self._check_rate_limit(host.id):
            await interaction.response.send_message(
                f"You've started too many games recently. Maximum {_RATE_LIMIT_MAX} per hour.",
                ephemeral=True,
            )
            return

        # Nickname-mode preflight only applies when no custom stakes are set.
        if stakes_text is None:
            err = await self._check_bot_can_nick(guild, [host])  # type: ignore[list-item]
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            err = await self._check_no_active_nick(guild, [host])  # type: ignore[list-item]
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return
            cd = await duels_db.check_group_cooldown(
                self.db, guild.id, self.GAME_KEY, host.id, cfg["cooldown_hours"]
            )
            if cd is not None:
                hours, mins = int(cd // 3600), int((cd % 3600) // 60)
                await interaction.response.send_message(
                    f"You need to wait **{hours}h {mins}m** before playing again.",
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

        min_players, max_players, _timeout = await self.get_lobby_params(guild.id)
        game_id = await self._db_create_lobby(
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            host_id=host.id,
            stakes_text=stakes_text,
        )
        self._record_challenge(host.id)

        game = await self._db_get_game(game_id)
        embed = self._render_lobby(game, guild, min_players, max_players)
        view = self._build_lobby_view(game_id, host.id)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        self.bot.add_view(view, message_id=msg.id)
        await self._db_set_state(game_id, "LOBBY", message_id=msg.id, last_action_at=time.time())

    async def _handle_lobby_join(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await interaction.response.send_message(
                    "This lobby is no longer open.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid in game.roster:
                await interaction.response.send_message("You're already in.", ephemeral=True)
                return
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            min_players, max_players, _ = await self.get_lobby_params(game.guild_id)
            if len(game.roster) >= max_players:
                await interaction.response.send_message(
                    f"The lobby is full ({max_players}).", ephemeral=True
                )
                return

            member = guild.get_member(uid)
            if game.stakes_text is None and member is not None:
                err = await self._check_bot_can_nick(guild, [member]) or \
                    await self._check_no_active_nick(guild, [member])
                if err:
                    await interaction.response.send_message(err, ephemeral=True)
                    return
                cfg = await duels_db.get_config(self.db, game.guild_id, self.GAME_KEY)
                cd = await duels_db.check_group_cooldown(
                    self.db, game.guild_id, self.GAME_KEY, uid, cfg["cooldown_hours"]
                )
                if cd is not None:
                    await interaction.response.send_message(
                        "You're on cooldown for this game.", ephemeral=True
                    )
                    return

            new_roster = list(game.roster) + [uid]
            await self._db_set_state(
                game_id, "LOBBY", roster=json.dumps(new_roster), last_action_at=time.time()
            )
            game.roster = new_roster
            embed = self._render_lobby(game, guild, min_players, max_players)
            await interaction.response.edit_message(
                embed=embed, view=self._build_lobby_view(game_id, game.host_id)
            )

    async def _handle_lobby_leave(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await interaction.response.send_message(
                    "This lobby is no longer open.", ephemeral=True
                )
                return
            uid = interaction.user.id
            if uid == game.host_id:
                await interaction.response.send_message(
                    "The host can't leave — use **🚫 Cancel** to close the lobby.",
                    ephemeral=True,
                )
                return
            if uid not in game.roster:
                await interaction.response.send_message("You're not in this lobby.", ephemeral=True)
                return
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            min_players, max_players, _ = await self.get_lobby_params(game.guild_id)
            new_roster = [u for u in game.roster if u != uid]
            await self._db_set_state(
                game_id, "LOBBY", roster=json.dumps(new_roster), last_action_at=time.time()
            )
            game.roster = new_roster
            embed = self._render_lobby(game, guild, min_players, max_players)
            await interaction.response.edit_message(
                embed=embed, view=self._build_lobby_view(game_id, game.host_id)
            )

    async def _handle_lobby_cancel(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await interaction.response.send_message(
                    "This lobby is no longer open.", ephemeral=True
                )
                return
            if interaction.user.id != game.host_id:
                await interaction.response.send_message(
                    "Only the host can cancel the lobby.", ephemeral=True
                )
                return
            await self._db_set_state(game_id, "EXPIRED_LOBBY")
            self._game_locks.pop(game_id, None)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title="🚫 Lobby Cancelled",
                    description=f"{interaction.user.mention} closed the lobby.",
                    color=COLOR_YELLOW,
                ),
                view=None,
            )

    async def _handle_lobby_start(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await interaction.response.send_message(
                    "This lobby is no longer open.", ephemeral=True
                )
                return
            if interaction.user.id != game.host_id:
                await interaction.response.send_message(
                    "Only the host can start the game.", ephemeral=True
                )
                return
            min_players, _max_players, _ = await self.get_lobby_params(game.guild_id)
            if len(game.roster) < min_players:
                await interaction.response.send_message(
                    f"You need at least **{min_players}** players to start "
                    f"(currently {len(game.roster)}).",
                    ephemeral=True,
                )
                return

            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            if game.stakes_text is None:
                members = [m for m in (guild.get_member(u) for u in game.roster) if m]
                err = await self._check_bot_can_nick(guild, members) or \
                    await self._check_no_active_nick(guild, members)
                if err:
                    await interaction.response.send_message(err, ephemeral=True)
                    return

            await self._db_set_state(
                game_id, "ACTIVE",
                alive=json.dumps(list(game.roster)),
                last_action_at=time.time(),
            )
            game = await self._db_get_game(game_id)
            if not game:
                return
            await self.on_game_start(game)
            game = await self._db_get_game(game_id)
            if not game:
                return
            view = self.build_game_view(game.id)
            embed = self.render_game_state(game, guild)
            self.bot.add_view(view, message_id=game.message_id)
            await interaction.response.edit_message(embed=embed, view=view)

    # ── Group resolution (timer-driven, posts to channel like duel _explode) ──

    async def _post_group_result(self, game: Any, winner_id: int, loser_id: int) -> None:
        """Finalize an N-player game. Caller holds the per-game lock.

        Honors the same two stake modes as duels: nickname (winner names the final
        loser) when no custom stakes were set, otherwise an announce-only result.
        """
        now = time.time()
        game.winner_id = winner_id
        game.loser_id = loser_id
        guild = self.bot.get_guild(game.guild_id)

        for uid in game.roster:
            await duels_db.set_group_cooldown(self.db, game.guild_id, self.GAME_KEY, uid)

        nick_mode = game.stakes_text is None

        if guild and game.message_id:
            disabled = self.build_game_view(game.id)
            disable = getattr(disabled, "disable", None)
            if callable(disable):
                disable()
            await self._edit_message_silent(
                game.channel_id, game.message_id, self.render_game_state(game, guild), disabled
            )

        result_message_id = None
        channel = self.bot.get_channel(game.channel_id)
        if channel and guild:
            result_embed = self.render_result_state(game, guild)
            winner_m = guild.get_member(winner_id)
            loser_m = guild.get_member(loser_id)
            ping = " ".join(m.mention for m in (winner_m, loser_m) if m)
            try:
                if nick_mode:
                    rv = ResultView(game.id, winner_id, loser_id, self._handle_set_nick)
                    msg = await channel.send(content=ping, embed=result_embed, view=rv)  # type: ignore[union-attr]
                    self.bot.add_view(rv, message_id=msg.id)
                else:
                    msg = await channel.send(content=ping, embed=result_embed)  # type: ignore[union-attr]
                result_message_id = msg.id
            except (discord.Forbidden, discord.HTTPException):
                pass

        state = "RESOLVED" if nick_mode else "RESOLVED_NO_NICK"
        await self._db_set_state(
            game.id, state,
            winner_id=winner_id,
            loser_id=loser_id,
            result_message_id=result_message_id,
            resolved_at=now,
            last_action_at=now,
        )
        await self.on_game_resolved(game.id)

    # ── Group button entrypoint + elimination ────────────────────────────────

    async def _handle_group_button(
        self, interaction: discord.Interaction, game_id: int
    ) -> None:
        """Entry point for in-game button presses on N-player games.

        handle_interaction returns:
          ("rejected", None)        — invalid press, ephemeral already sent
          ("continue", None)        — re-render the live embed
          ("eliminate", player_id)  — player is out; base removes them & checks terminal
          ("done", winner_id)       — terminal; base resolves with that winner
        """
        await interaction.response.defer()
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game:
                await interaction.followup.send("Game not found.", ephemeral=True)
                return
            if game.state != "ACTIVE":
                await interaction.followup.send(
                    "This game is no longer active.", ephemeral=True
                )
                return

            status, pid = await self.handle_interaction(interaction, game)

            if status == "rejected":
                return
            if status == "continue":
                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                await interaction.edit_original_response(embed=self.render_game_state(game, guild))
                return
            if status == "eliminate":
                assert pid is not None
                await self._group_eliminate(game, pid, interaction=interaction)
                return
            if status == "done":
                assert pid is not None  # winner_id
                loser = game.elimination_order[-1] if game.elimination_order else pid
                await self._post_group_result(game, pid, loser)

    async def _group_eliminate(
        self,
        game: Any,
        player_id: int,
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        """Remove player_id from `alive`, append to `elimination_order`, and resolve
        the game if only one player remains (loser = last eliminated). Caller holds
        the per-game lock."""
        now = time.time()
        new_alive = [u for u in game.alive if u != player_id]
        new_elim = list(game.elimination_order) + [player_id]
        game.alive = new_alive
        game.elimination_order = new_elim
        await self._db_set_state(
            game.id, "ACTIVE",
            alive=json.dumps(new_alive),
            elimination_order=json.dumps(new_elim),
            last_action_at=now,
        )
        if len(new_alive) <= 1:
            winner = new_alive[0] if new_alive else player_id
            await self._post_group_result(game, winner, player_id)
        elif interaction is not None:
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            try:
                await interaction.edit_original_response(embed=self.render_game_state(game, guild))
            except discord.HTTPException:
                pass

    # ── Timer hooks (no-op stubs — override in timer-based games) ─────────────

    async def on_game_start(self, game: Any) -> None:
        """Called when a challenge is accepted / lobby starts, before the game embed posts."""

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

    # ── Lobby hooks (N-player games implement; duels leave as defaults) ───────

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None
    ) -> int:
        """Create a LOBBY-state game with roster=[host_id]. Returns its id."""
        raise NotImplementedError

    async def _db_fetch_lobby_games(self) -> list:
        """Return open LOBBY games to re-attach views on cog_load. Duels: none."""
        return []

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int, float]:
        """Return (min_players, max_players, lobby_timeout) for this guild."""
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
        """Return a fresh View whose buttons call the game's button handler."""
        raise NotImplementedError

    async def handle_interaction(
        self, interaction: discord.Interaction, game: Any
    ) -> tuple[str, int | None]:
        """Process a button press. Return one of:
          ("rejected", None)        — invalid press, already sent ephemeral feedback
          ("continue", None)        — valid press, game continues; base re-renders
          ("eliminate", player_id)  — (group games) player_id is out this round
          ("done", id)              — game over; for duels id is loser_id (pairwise
                                      winner mapping in BaseDuel), for group games id
                                      is winner_id
        """
        raise NotImplementedError
