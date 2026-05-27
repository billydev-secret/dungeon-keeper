"""Pressure Cooker cog — slash commands, callbacks, expire loop."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import random
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.services.embeds import COLOR_GOLD, COLOR_GREEN, COLOR_RED, COLOR_YELLOW

from . import db as pdb
from .filters import validate_nickname, validate_stakes
from .game import PressureGame, apply_pump
from .modals import NicknameModal
from .views import GameView, ChallengeView, ResultView, gauge_bar

if TYPE_CHECKING:
    pass

log = logging.getLogger("dungeonkeeper.pressure")

_PENDING_TTL = 60        # seconds before PENDING times out
_IDLE_TTL = 300          # seconds before ACTIVE idles out
_RESULT_TTL = 300        # seconds before RESOLVED with no nick → NO_NICK_SET
_RATE_LIMIT_WINDOW = 3600
_RATE_LIMIT_MAX = 3


class PressureCookerCog(commands.Cog, name="PressureCookerCog"):

    pressure = app_commands.Group(
        name="pressure",
        description="Pressure Cooker — a high-stakes nickname duel",
    )

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
        active = await pdb.fetch_active_games(self.db)
        for game in active:
            if game.message_id:
                self.bot.add_view(
                    GameView(game.id, self._handle_pump),
                    message_id=game.message_id,
                )

        resolved = await pdb.fetch_resolved_games(self.db)
        for game in resolved:
            if game.result_message_id and game.winner_id and game.loser_id:
                self.bot.add_view(
                    ResultView(
                        game.id,
                        game.winner_id,
                        game.loser_id,
                        self._handle_set_nick,
                        self._handle_honor,
                        self._handle_rematch,
                    ),
                    message_id=game.result_message_id,
                )

        self._expire_loop.start()
        log.info(
            "PressureCookerCog loaded: %d active, %d resolved",
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
            games = await pdb.fetch_sweepable_games(self.db, now)
            for game in games:
                if game.state == "PENDING":
                    await self._expire_pending(game)
                elif game.state == "ACTIVE":
                    await self._expire_active(game)
                elif game.state == "RESOLVED":
                    await self._expire_resolved(game)

            nicks = await pdb.fetch_expired_nicks(self.db, now)
            for nick_row in nicks:
                await self._revert_nick(nick_row)
        except Exception:
            log.exception("Pressure expire loop error")

    @_expire_loop.before_loop
    async def _before_expire(self) -> None:
        await self.bot.wait_until_ready()

    async def _expire_pending(self, game: PressureGame) -> None:
        await pdb.set_game_state(self.db, game.id, "EXPIRED_PENDING")
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

    async def _expire_active(self, game: PressureGame) -> None:
        await pdb.set_game_state(self.db, game.id, "ABANDONED")
        self._game_locks.pop(game.id, None)
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="🏳️ Game Abandoned",
                description="No pump in 5 minutes. Game over — no nickname consequences.",
                color=COLOR_YELLOW,
            ),
            view=None,
        )

    async def _expire_resolved(self, game: PressureGame) -> None:
        await pdb.set_game_state(self.db, game.id, "NO_NICK_SET")
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
            await pdb.mark_nick_reverted(self.db, nick_row["id"], "guild_gone")
            return
        member = guild.get_member(nick_row["loser_id"])
        if not member:
            await pdb.mark_nick_reverted(self.db, nick_row["id"], "member_gone")
            return
        try:
            original = nick_row["original_nick"]
            await member.edit(nick=original, reason="Pressure Cooker sentence expired")
            await pdb.mark_nick_reverted(self.db, nick_row["id"], "expired")
            restored = original or member.name
            try:
                await member.send(
                    f"Your Pressure Cooker nickname sentence has expired. "
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
            await pdb.mark_nick_reverted(self.db, nick_row["id"], "forbidden")
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
        # Server owner can't be renamed by bots — they self-apply if they lose.
        # Skip the role-hierarchy check for them; check everyone else.
        for member in (challenger, target):
            if member.id != guild.owner_id and me.top_role <= member.top_role:
                return (
                    "My highest role must be above both players' roles to rename the loser. "
                    "Ask an admin to fix my role position."
                )
        return None

    # ── Rate limit ────────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: int) -> bool:
        """Return True if this user is rate-limited (≥3 challenges in the last hour)."""
        dq = self._challenge_rate[user_id]
        now = time.time()
        while dq and now - dq[0] > _RATE_LIMIT_WINDOW:
            dq.popleft()
        return len(dq) >= _RATE_LIMIT_MAX

    def _record_challenge(self, user_id: int) -> None:
        self._challenge_rate[user_id].append(time.time())

    # ── Slash commands ────────────────────────────────────────────────────────

    @pressure.command(name="challenge", description="Challenge someone to Pressure Cooker")
    @app_commands.describe(
        user="The player you're challenging",
        stakes="Optional custom stakes text (max 200 chars)",
    )
    async def pressure_challenge(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        stakes: str | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        challenger = interaction.user  # type: ignore[assignment]
        guild: discord.Guild = interaction.guild

        if user.id == challenger.id:
            await interaction.response.send_message(
                "You can't challenge yourself.", ephemeral=True
            )
            return
        if user.bot:
            await interaction.response.send_message(
                "You can't challenge a bot.", ephemeral=True
            )
            return

        cfg = await pdb.get_config(self.db, guild.id)
        allowlist: list[int] = json.loads(cfg.get("channel_allowlist") or "[]")
        if allowlist and interaction.channel_id not in allowlist:
            await interaction.response.send_message(
                "Pressure Cooker isn't allowed in this channel.", ephemeral=True
            )
            return

        if self._check_rate_limit(challenger.id):
            await interaction.response.send_message(
                f"You've issued too many challenges recently. "
                f"Maximum {_RATE_LIMIT_MAX} per hour.",
                ephemeral=True,
            )
            return

        perm_error = await self._check_bot_can_nick(guild, challenger, user)  # type: ignore[arg-type]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        existing = await pdb.get_active_game_for_pair(self.db, guild.id, challenger.id, user.id)
        if existing:
            await interaction.response.send_message(
                "You two already have a game in progress.", ephemeral=True
            )
            return

        cooldown = await pdb.check_cooldown(
            self.db, guild.id, challenger.id, user.id, cfg["cooldown_hours"]
        )
        if cooldown is not None:
            hours = int(cooldown // 3600)
            mins = int((cooldown % 3600) // 60)
            await interaction.response.send_message(
                f"You two need to wait **{hours}h {mins}m** before playing again.",
                ephemeral=True,
            )
            return

        if stakes:
            stakes_result = validate_stakes(
                stakes,
                max_length=cfg["max_stakes_length"],
                denylist=json.loads(cfg.get("nick_denylist") or "[]"),
            )
            if not stakes_result.ok:
                await interaction.response.send_message(
                    f"Stakes rejected: {stakes_result.reason}", ephemeral=True
                )
                return
            stakes = stakes_result.value or None

        game_id = await pdb.create_game(
            self.db,
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            challenger_id=challenger.id,
            target_id=user.id,
            stakes_text=stakes,
        )
        self._record_challenge(challenger.id)

        embed = self._build_challenge_embed(challenger, user, stakes)  # type: ignore[arg-type]
        view = ChallengeView(
            game_id=game_id,
            target_id=user.id,
            on_accept=self._handle_accept,
            on_decline=self._handle_decline,
        )
        await interaction.response.send_message(
            content=user.mention, embed=embed, view=view
        )
        msg = await interaction.original_response()
        await pdb.set_game_state(self.db, game_id, "PENDING", message_id=msg.id)

    @pressure.command(name="cancel", description="Cancel your pending challenge in this channel")
    async def pressure_cancel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        game = await pdb.get_pending_game_for_challenger(
            self.db,
            interaction.guild.id,
            interaction.channel_id,  # type: ignore[arg-type]
            interaction.user.id,
        )
        if not game:
            await interaction.response.send_message(
                "You don't have a pending challenge in this channel.", ephemeral=True
            )
            return
        await pdb.set_game_state(self.db, game.id, "EXPIRED_PENDING")
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="🚫 Challenge Cancelled",
                description=f"{interaction.user.mention} cancelled the challenge.",
                color=COLOR_YELLOW,
            ),
            view=None,
        )
        await interaction.response.send_message("Challenge cancelled.", ephemeral=True)

    @pressure.command(name="stats", description="View Pressure Cooker stats")
    @app_commands.describe(user="User to look up (defaults to yourself)")
    async def pressure_stats(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        target = user or interaction.user
        stats = await pdb.get_stats(self.db, interaction.guild.id, target.id)
        embed = discord.Embed(
            title=f"🔥 Pressure Cooker — {target.display_name}",
            color=COLOR_GOLD,
        )
        embed.add_field(name="Wins", value=str(stats["wins"]), inline=True)
        embed.add_field(name="Losses", value=str(stats["losses"]), inline=True)
        embed.add_field(name="Total Games", value=str(stats["total_games"]), inline=True)
        if stats["highest_gauge_win"] is not None:
            embed.add_field(
                name="Highest Gauge (Win)", value=f"{stats['highest_gauge_win']}/100", inline=True
            )
        await interaction.response.send_message(embed=embed)

    @pressure.command(name="revert", description="Request early revert of your Pressure Cooker nickname")
    async def pressure_revert(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        cfg = await pdb.get_config(self.db, interaction.guild.id)
        if not cfg.get("allow_early_revert"):
            await interaction.response.send_message(
                "Early revert isn't enabled on this server. Ask a mod.", ephemeral=True
            )
            return
        nick = await pdb.get_active_nick_for_user(
            self.db, interaction.guild.id, interaction.user.id
        )
        if not nick:
            await interaction.response.send_message(
                "You don't have an active nickname sentence.", ephemeral=True
            )
            return
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            try:
                await member.edit(nick=nick["original_nick"], reason="Early revert requested by user")
            except discord.Forbidden:
                await interaction.response.send_message(
                    "I couldn't revert your nickname — I may not have permission.", ephemeral=True
                )
                return
        await pdb.mark_nick_reverted(self.db, nick["id"], "early_revert")
        await interaction.response.send_message(
            "Your nickname has been restored early.", ephemeral=True
        )

    @pressure.command(name="config", description="Configure Pressure Cooker (mods only)")
    @app_commands.describe(
        cooldown_hours="Hours before the same pair can play again (default 48)",
        sentence_hours="Hours the imposed nickname lasts (default 24)",
        allow_early_revert="Allow losers to request early nick revert: 0=no, 1=yes",
        channel_allowlist="JSON array of allowed channel IDs, or '[]' for all channels",
        max_nick_length="Maximum nickname character count (default 32)",
        max_stakes_length="Maximum stakes text character count (default 200)",
    )
    async def pressure_config(
        self,
        interaction: discord.Interaction,
        cooldown_hours: int | None = None,
        sentence_hours: int | None = None,
        allow_early_revert: int | None = None,
        channel_allowlist: str | None = None,
        max_nick_length: int | None = None,
        max_stakes_length: int | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message(
                "You need the Manage Server permission to configure Pressure Cooker.",
                ephemeral=True,
            )
            return

        updates: dict = {}
        if cooldown_hours is not None:
            updates["cooldown_hours"] = max(0, cooldown_hours)
        if sentence_hours is not None:
            updates["sentence_hours"] = max(1, sentence_hours)
        if allow_early_revert is not None:
            updates["allow_early_revert"] = 1 if allow_early_revert else 0
        if channel_allowlist is not None:
            try:
                json.loads(channel_allowlist)
                updates["channel_allowlist"] = channel_allowlist
            except json.JSONDecodeError:
                await interaction.response.send_message(
                    "channel_allowlist must be a valid JSON array, e.g. `[123456789, 987654321]`",
                    ephemeral=True,
                )
                return
        if max_nick_length is not None:
            updates["max_nick_length"] = max(1, min(32, max_nick_length))
        if max_stakes_length is not None:
            updates["max_stakes_length"] = max(1, min(2000, max_stakes_length))

        if not updates:
            cfg = await pdb.get_config(self.db, interaction.guild.id)
            embed = discord.Embed(title="🔧 Pressure Cooker Config", color=COLOR_GOLD)
            for k, v in cfg.items():
                if k != "guild_id":
                    embed.add_field(name=k, value=str(v), inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await pdb.upsert_config(self.db, interaction.guild.id, **updates)
        lines = [f"**{k}** → `{v}`" for k, v in updates.items()]
        await interaction.response.send_message(
            "Config updated:\n" + "\n".join(lines), ephemeral=True
        )

    # ── View callbacks ────────────────────────────────────────────────────────

    async def _handle_accept(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await pdb.get_game(self.db, game_id)
        if not game or game.state != "PENDING":
            await interaction.response.send_message(
                "This challenge is no longer active.", ephemeral=True
            )
            return

        # Pick starting player randomly
        first_player = random.choice([game.challenger_id, game.target_id])
        await pdb.set_game_state(self.db, game_id, "ACTIVE", active_player=first_player)
        game.state = "ACTIVE"
        game.active_player = first_player

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        view = GameView(game_id, self._handle_pump)
        embed = self._build_game_embed(game, guild)
        self.bot.add_view(view, message_id=game.message_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def _handle_decline(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await pdb.get_game(self.db, game_id)
        if not game or game.state != "PENDING":
            await interaction.response.send_message(
                "This challenge is no longer active.", ephemeral=True
            )
            return
        await pdb.set_game_state(self.db, game_id, "DECLINED")
        embed = discord.Embed(
            title="❌ Challenge Declined",
            description=f"{interaction.user.mention} declined the challenge.",
            color=COLOR_YELLOW,
        )
        await interaction.response.edit_message(embed=embed, view=None)

    async def _handle_pump(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.defer()
        async with self._get_lock(game_id):
            game = await pdb.get_game(self.db, game_id)
            if not game:
                await interaction.followup.send("Game not found.", ephemeral=True)
                return
            if game.state != "ACTIVE":
                await interaction.followup.send("This game is no longer active.", ephemeral=True)
                return
            if interaction.user.id != game.active_player:
                await interaction.followup.send("It's not your turn.", ephemeral=True)
                return

            result = apply_pump(game, interaction.user.id)
            await pdb.save_pump(self.db, game)

            guild: discord.Guild = interaction.guild  # type: ignore[assignment]

            if result.busted:
                # Disable game view
                game_view = GameView(game_id, self._handle_pump)
                game_view.disable()
                bust_embed = self._build_game_embed(game, guild, busted=True)
                await interaction.edit_original_response(embed=bust_embed, view=game_view)

                await pdb.set_cooldown(self.db, guild.id, game.challenger_id, game.target_id)

                result_embed = self._build_result_embed(game, guild)
                result_view = ResultView(
                    game_id,
                    game.winner_id,  # type: ignore[arg-type]
                    game.loser_id,  # type: ignore[arg-type]
                    self._handle_set_nick,
                    self._handle_honor,
                    self._handle_rematch,
                )
                result_msg = await interaction.followup.send(
                    content=f"{guild.get_member(game.winner_id).mention} {guild.get_member(game.loser_id).mention}",  # type: ignore[union-attr]
                    embed=result_embed,
                    view=result_view,
                )
                self.bot.add_view(result_view, message_id=result_msg.id)  # type: ignore[union-attr]
                await pdb.set_game_state(
                    self.db, game_id, "RESOLVED", result_message_id=result_msg.id  # type: ignore[union-attr]
                )
                self._game_locks.pop(game_id, None)
            else:
                embed = self._build_game_embed(game, guild)
                await interaction.edit_original_response(embed=embed)

    async def _handle_set_nick(self, interaction: discord.Interaction, game_id: int) -> None:
        await interaction.response.send_modal(NicknameModal(game_id, self._handle_nick_submit))

    async def _handle_nick_submit(
        self, interaction: discord.Interaction, game_id: int, raw_nick: str
    ) -> None:
        game = await pdb.get_game(self.db, game_id)
        if not game or game.state not in ("RESOLVED", "NICKED"):
            await interaction.response.send_message(
                "This game is no longer waiting for a nickname.", ephemeral=True
            )
            return
        if interaction.user.id != game.winner_id:
            await interaction.response.send_message(
                "Only the winner can set the nickname.", ephemeral=True
            )
            return

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]
        cfg = await pdb.get_config(self.db, guild.id)
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
            await pdb.set_game_state(self.db, game_id, "NO_NICK_SET")
            return

        challenger_member = guild.get_member(game.challenger_id)  # type: ignore[arg-type]
        perm_error = await self._check_bot_can_nick(guild, challenger_member, loser)  # type: ignore[arg-type]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        original_nick = loser.nick

        if loser.id == guild.owner_id:
            # Discord doesn't allow bots to rename the server owner.
            # Record the sentence so stats work; owner applies it themselves.
            await pdb.apply_nick(
                self.db,
                game_id=game.id,
                guild_id=guild.id,
                loser_id=game.loser_id,  # type: ignore[arg-type]
                winner_id=game.winner_id,  # type: ignore[arg-type]
                original_nick=original_nick,
                imposed_nick=cleaned_nick,
                sentence_hours=cfg["sentence_hours"],
            )
            await pdb.set_game_state(self.db, game_id, "NICKED")
            embed = self._build_result_embed(game, guild, imposed_nick=cleaned_nick)
            await interaction.response.edit_message(embed=embed)
            await interaction.followup.send(
                f"📋 Discord won't let me rename the server owner. "
                f"**{loser.display_name}**, your sentence is: **{cleaned_nick}** — please apply it yourself.",
            )
            return

        try:
            await loser.edit(nick=cleaned_nick, reason=f"Pressure Cooker: lost to {interaction.user.display_name}")
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

        await pdb.apply_nick(
            self.db,
            game_id=game.id,
            guild_id=guild.id,
            loser_id=game.loser_id,  # type: ignore[arg-type]
            winner_id=game.winner_id,  # type: ignore[arg-type]
            original_nick=original_nick,
            imposed_nick=cleaned_nick,
            sentence_hours=cfg["sentence_hours"],
        )
        await pdb.set_game_state(self.db, game_id, "NICKED")

        embed = self._build_result_embed(game, guild, imposed_nick=cleaned_nick)
        await interaction.response.edit_message(embed=embed)

    async def _handle_honor(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await pdb.get_game(self.db, game_id)
        if not game:
            await interaction.response.send_message("Game not found.", ephemeral=True)
            return
        await pdb.set_game_state(self.db, game_id, game.state, stakes_honored=1)
        await interaction.response.send_message(
            "✅ Acknowledged — you've accepted the stakes.", ephemeral=True
        )

    async def _handle_rematch(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await pdb.get_game(self.db, game_id)
        if not game:
            await interaction.response.send_message("Game not found.", ephemeral=True)
            return
        if not interaction.guild:
            return

        guild: discord.Guild = interaction.guild  # type: ignore[assignment]

        if game.winner_id is None or game.loser_id is None:
            await interaction.response.send_message(
                "This game has no result yet.", ephemeral=True
            )
            return

        # Swap roles for rematch
        new_challenger_id = interaction.user.id
        other_id: int = game.loser_id if interaction.user.id == game.winner_id else game.winner_id
        new_target_id: int = other_id
        new_challenger = guild.get_member(new_challenger_id)
        new_target = guild.get_member(new_target_id)

        if not new_target:
            await interaction.response.send_message(
                "The other player has left the server.", ephemeral=True
            )
            return

        perm_error = await self._check_bot_can_nick(guild, new_challenger, new_target)  # type: ignore[arg-type]
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        new_game_id = await pdb.create_game(
            self.db,
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            challenger_id=new_challenger_id,
            target_id=new_target_id,
            stakes_text=game.stakes_text,
        )
        self._record_challenge(new_challenger_id)

        embed = self._build_challenge_embed(new_challenger, new_target, game.stakes_text)  # type: ignore[arg-type]
        view = ChallengeView(
            game_id=new_game_id,
            target_id=new_target_id,
            on_accept=self._handle_accept,
            on_decline=self._handle_decline,
        )
        await interaction.response.send_message(
            content=new_target.mention, embed=embed, view=view
        )
        msg = await interaction.original_response()
        await pdb.set_game_state(self.db, new_game_id, "PENDING", message_id=msg.id)

    # ── Embed builders ────────────────────────────────────────────────────────

    @staticmethod
    def _build_challenge_embed(
        challenger: discord.Member,
        target: discord.Member,
        stakes: str | None,
    ) -> discord.Embed:
        stakes_text = stakes or "Loser surrenders their nickname for 24 hours."
        embed = discord.Embed(
            title="🔥 PRESSURE COOKER CHALLENGE",
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

    @staticmethod
    def _build_game_embed(
        game: PressureGame,
        guild: discord.Guild,
        *,
        busted: bool = False,
        restored: bool = False,
    ) -> discord.Embed:
        if game.gauge >= 75:
            color = COLOR_RED
        elif game.gauge >= 50:
            color = COLOR_YELLOW
        else:
            color = COLOR_GREEN

        embed = discord.Embed(title="🔥 PRESSURE COOKER", color=color)

        p1 = guild.get_member(game.challenger_id)
        p2 = guild.get_member(game.target_id)
        p1_name = p1.display_name if p1 else str(game.challenger_id)
        p2_name = p2.display_name if p2 else str(game.target_id)
        embed.description = f"**{p1_name}** vs **{p2_name}**"

        embed.add_field(name="Gauge", value=gauge_bar(game.gauge), inline=False)

        if game.pumps:
            last_pumps = game.pumps[-5:]
            lines = []
            for entry in last_pumps:
                m = guild.get_member(entry.player_id)
                name = m.display_name if m else str(entry.player_id)
                gauge_after = entry.gauge_before + entry.roll
                bust_marker = " 💥" if gauge_after >= 100 else ""
                lines.append(f"**{name}**: +{entry.roll} → {gauge_after}/100{bust_marker}")
            embed.add_field(name="Recent Pumps", value="\n".join(lines), inline=False)

        if not busted and game.active_player:
            active = guild.get_member(game.active_player)
            turn = active.mention if active else str(game.active_player)
            embed.add_field(name="▶️ Turn", value=turn, inline=False)

        if restored:
            embed.set_footer(text="⚠️ Game restored after restart.")

        return embed

    @staticmethod
    def _build_result_embed(
        game: PressureGame,
        guild: discord.Guild,
        *,
        imposed_nick: str | None = None,
    ) -> discord.Embed:
        winner = guild.get_member(game.winner_id)  # type: ignore[arg-type]
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        winner_name = winner.display_name if winner else str(game.winner_id)
        loser_name = loser.display_name if loser else str(game.loser_id)

        embed = discord.Embed(title="💥 BOOM.", color=COLOR_RED)
        embed.description = (
            f"**{loser_name}** pushed the gauge to **{game.gauge}/100** and lost."
        )
        embed.add_field(name="🏆 Winner", value=winner_name, inline=True)
        embed.add_field(name="💀 Loser", value=loser_name, inline=True)

        stakes_text = game.stakes_text or "24-hour nickname surrender."
        embed.add_field(name="📋 Stakes", value=stakes_text, inline=False)

        if imposed_nick:
            embed.add_field(
                name="🏷️ Nickname Applied",
                value=f"**{loser_name}** is now known as **{imposed_nick}**",
                inline=False,
            )
        else:
            embed.add_field(
                name="⏳ Awaiting Nickname",
                value=f"{winner_name} has 5 minutes to set the nickname.",
                inline=False,
            )

        # Full pump log (last 10)
        if game.pumps:
            last = game.pumps[-10:]
            lines = []
            for entry in last:
                m = guild.get_member(entry.player_id)
                name = m.display_name if m else str(entry.player_id)
                gauge_after = entry.gauge_before + entry.roll
                bust = " 💥" if gauge_after >= 100 else ""
                lines.append(f"{name}: +{entry.roll} → {gauge_after}{bust}")
            embed.add_field(name="Pump Log", value="\n".join(lines), inline=False)

        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PressureCookerCog(bot))
