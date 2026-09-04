"""BaseGame — shared lifecycle for all nickname-stake games (2..N players).

`BaseDuel` (the fixed 2-player special case) and the N-player group games both
subclass this. Everything here is roster-count-agnostic: lifecycle, the background
expiry/auto-revert sweep, the nickname-stake flow (one winner names one loser), rate
limiting, and the abstract DB/game hooks. Pairwise-specific behavior (the single
opponent accept/decline challenge) lives in `BaseDuel`; lobby/elimination behavior for
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
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands, tasks

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services import no_contact_service
from bot_modules.services.dm_branding import send_branded_dm
from bot_modules.economy.game_rewards import pay_game_rewards
from bot_modules.services import economy_wager_service as wager_svc
from bot_modules.services.economy_service import (
    EconSettings,
    get_balance,
    load_econ_settings,
)
from bot_modules.games.utils.game_history import history_insert
from bot_modules.games.utils.game_manager import check_game_enabled, sign_off_game_chore
from bot_modules.services.embeds import COLOR_GOLD, COLOR_YELLOW
from bot_modules.core.branding import apply_section_spacing

from . import db as duels_db
from .db import (
    LOBBY_IDLE_SECONDS,
    NAMING_WINDOW_SECONDS,
    REMATCH_WINDOW_SECONDS,
    active_idle_seconds,
)
from .filters import (
    custom_stakes_from,
    game_is_nick_stake,
    nick_stakes_line,
    resolve_nick_stake,
    resolve_stakes_text,
    validate_nickname,
    validate_stakes,
)
from .lobby import LobbyView
from .modals import NicknameModal
from .views import ResultView

log = logging.getLogger("dungeonkeeper.duels")


def _fmt_coins(settings: EconSettings, n: int) -> str:
    """A wager amount in the shared currency vocabulary: ``🪙 **500** coins``.

    Singular unit at 1. Used for wager fields so the duel pot reads like every
    other economy surface (see embed_style_guide.md → Currency vocabulary).
    """
    unit = settings.currency_name if abs(n) == 1 else settings.currency_plural
    return f"{settings.currency_emoji} **{n:,}** {unit}"

_RATE_LIMIT_WINDOW = 3600
_RATE_LIMIT_MAX = 3

# The states a game can end in. Every one of them must pass through
# _db_set_state so _on_terminal_state observes it — that guarantee is what
# the wager escrow (stage 4b) settles and refunds on. NICKED / NO_NICK_SET
# are post-terminal cosmetic follow-ups to RESOLVED, not game ends.
# DECLINED joined the set with wagers: no money moves before an accept, but
# the challenger's *declared* ante row has to be cleaned up.
_TERMINAL_STATES = frozenset({
    "RESOLVED", "RESOLVED_NO_NICK", "ABANDONED", "VOID",
    "EXPIRED_PENDING", "EXPIRED_LOBBY", "DECLINED",
})

# Terminal states that pay the winner. Everything else refunds every stake —
# the pot never evaporates and never pays a half-finished game.
_SETTLING_STATES = frozenset({"RESOLVED", "RESOLVED_NO_NICK"})


class BaseGame(commands.Cog):
    """Abstract base for all nickname-stake games (2..N players).

    Subclasses must define:
      GAME_KEY            str  e.g. 'pressure'
      GAME_DISPLAY_NAME   str  e.g. 'Pressure Cooker'

    And implement all hooks that raise NotImplementedError.
    """

    GAME_KEY: str = ""
    GAME_DISPLAY_NAME: str = ""

    #: One-paragraph rules blurb, rendered as a "How to play" field on the
    #: lobby embed. Optional — a game that leaves it None renders the lobby
    #: exactly as before. Exists because Musical Chairs players had no way to
    #: learn the rules before the first round started (game night 2026-08-21):
    #: three separate people sat during the music and were eliminated without
    #: ever having been told not to.
    HOW_TO_PLAY: str | None = None

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
                view = self._result_view(game)
                if view.children:  # NICKED past its rematch window has nothing left
                    self.bot.add_view(view, message_id=game.result_message_id)

        lobby = await self._db_fetch_lobby_games()
        for game in lobby:
            if game.message_id:
                self.bot.add_view(
                    self._build_lobby_view(game.id, game.host_id),
                    message_id=game.message_id,
                )

        pending = await self._reattach_pending_challenges()

        self._expire_loop.start()
        log.info(
            "%s loaded: %d active, %d resolved, %d lobby, %d pending",
            self.GAME_DISPLAY_NAME,
            len(active),
            len(resolved),
            len(lobby),
            pending,
        )

    async def _reattach_pending_challenges(self) -> int:
        """Give a challenge that was waiting through a restart its buttons back.

        The Accept / Decline view is not persistent — it times out with the
        challenge — so after a restart a card posted in the minutes before it
        answered every press with "interaction failed" until the sweep
        expired it (duels-party-126). Each still-live PENDING row gets a
        persistent view carrying the same deadline the card counts down to;
        a press after that deadline is refused with the timed-out copy, the
        same as a late press on the original. Returns how many were
        re-attached.
        """
        count = 0
        now = time.time()
        for game in await self._db_fetch_pending_games():
            if not game.message_id:
                continue
            deadline = float(game.created_at) + duels_db.CHALLENGE_RESPONSE_SECONDS
            if deadline <= now:
                continue  # the sweep will flip the card to Expired within a minute
            view = self._build_challenge_view(game, deadline=deadline)
            if view is None:
                continue
            self.bot.add_view(view, message_id=game.message_id)
            count += 1
        return count

    def _build_challenge_view(self, game: Any, *, deadline: float) -> discord.ui.View | None:
        """The Accept / Decline view for a pending challenge. Duels override;
        group games have lobbies, not challenges, and return None."""
        return None

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

            await self._warn_stale_lobbies(now)
            await self._remind_unnamed(now)
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

    def _dead_lobby_copy(self, roster_size: int, min_players: int) -> str:
        """Why a lobby closed, in the words of what actually happened. The old
        card blamed the players ("Not enough players started in time") even
        when a full lobby died because nobody pressed Start."""
        window = self._span(LOBBY_IDLE_SECONDS)
        if roster_size >= min_players:
            return (
                f"Nobody pressed **▶️ Start** in time — a lobby closes after "
                f"{window} without a join or a start."
            )
        return (
            f"Not enough people joined in time — this game needs {min_players}, "
            f"and a lobby closes after {window} without a join or a start."
        )

    async def _expire_lobby(self, game: Any) -> None:
        min_players, _max_players = await self.get_lobby_params(game.guild_id)
        copy = self._dead_lobby_copy(len(getattr(game, "roster", []) or []), min_players)
        await self._db_set_state(game.id, "EXPIRED_LOBBY")
        self._game_locks.pop(game.id, None)
        await self._edit_message_silent(
            game.channel_id,
            game.message_id,
            embed=discord.Embed(
                title="⏱️ Lobby Expired",
                description=copy,
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
                description=(
                    f"No activity in {self._span(active_idle_seconds(self.GAME_KEY))}. "
                    "Game over — no nickname consequences."
                ),
                color=COLOR_YELLOW,
            ),
            view=None,
        )

    async def _expire_resolved(self, game: Any) -> None:
        await self._conclude_unnamed(
            game,
            duels_db.NICK_REASON_WINNER_TIMEOUT,
            card=discord.Embed(
                title="⏰ Nickname Not Set",
                description=(
                    f"The winner didn't name the loser within "
                    f"{self._span(NAMING_WINDOW_SECONDS)}. No rename applied."
                ),
                color=COLOR_YELLOW,
            ),
        )

    async def _conclude_unnamed(
        self,
        game: Any,
        reason: str,
        *,
        card: discord.Embed | None = None,
    ) -> None:
        """End a RESOLVED game with no rename, saying why.

        ``NO_NICK_SET`` used to cover four different endings — the winner
        never pressed the button, the loser outranks the bot, the loser left
        the server, the loser was already serving — so the state lied about
        what happened (duels-party-118). Every path that concludes without a
        rename comes through here and writes ``nick_reason``. ``card``
        replaces the result message when the room should see the ending too;
        the interaction paths answer the winner ephemerally and leave the
        card as it is.
        """
        assert reason in duels_db.NICK_REASONS, reason
        await self._db_set_state(game.id, "NO_NICK_SET", nick_reason=reason)
        if card is not None and getattr(game, "result_message_id", None):
            await self._edit_message_silent(
                game.channel_id, game.result_message_id, embed=card, view=None,
            )

    async def _warn_stale_lobbies(self, now: float) -> None:
        """One ping to the host shortly before an idle lobby closes."""
        for game_id in await duels_db.fetch_lobby_warning_ids(self.db, self.GAME_KEY, now):
            game = await self._db_get_game(game_id)
            if game is None or game.state != "LOBBY":
                continue
            await duels_db.mark_lobby_warned(self.db, self.GAME_KEY, game_id)
            closes_at = int(float(game.last_action_at or now) + LOBBY_IDLE_SECONDS)
            await self._announce_to_channel(
                game_id,
                f"<@{game.host_id}> ⏱️ your {self.GAME_DISPLAY_NAME} lobby closes "
                f"<t:{closes_at}:R> — press **▶️ Start**, or wait for another join "
                "to reset the clock.",
            )

    async def _remind_unnamed(self, now: float) -> None:
        """One ping to a winner who hasn't pressed Name the Loser yet."""
        for game_id in await duels_db.fetch_naming_reminder_ids(self.db, self.GAME_KEY, now):
            game = await self._db_get_game(game_id)
            if game is None or game.state != "RESOLVED" or not game.winner_id:
                continue
            await duels_db.mark_naming_reminded(self.db, self.GAME_KEY, game_id)
            guild = self.bot.get_guild(game.guild_id)
            loser = self._member_label(guild, int(game.loser_id)) if game.loser_id else "the loser"
            closes_at = int(float(game.resolved_at or now) + NAMING_WINDOW_SECONDS)
            await self._announce_to_channel(
                game_id,
                f"<@{game.winner_id}> 📝 you haven't named {loser} yet — press "
                f"**Name the Loser** on the result card. It closes <t:{closes_at}:R>.",
            )

    # ── A member leaves the server ────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Take a leaver out of every game of this type they were in.

        The economy cog already refunds a leaver's escrow; what nobody did was
        drop them from the roster, so a lobby or a live round that included
        them stalled until the sweep abandoned it (duels-party-127). The manual
        promised the refund, and the refund arrived — the game just sat there.
        """
        try:
            await self._drop_leaver(member.guild.id, member.id)
        except Exception:
            log.exception(
                "%s: failed to drop leaver %d from guild %d",
                self.GAME_DISPLAY_NAME, member.id, member.guild.id,
            )

    async def _drop_leaver(self, guild_id: int, user_id: int) -> None:
        for game in await self._db_fetch_lobby_games():
            if game.guild_id == guild_id and user_id in game.roster:
                async with self._get_lock(game.id):
                    await self._drop_leaver_from_lobby(game, user_id)
        for game in await self._db_fetch_active_games():
            if game.guild_id != guild_id:
                continue
            roster = getattr(game, "roster", None)
            if roster is not None:
                if user_id in game.alive:
                    async with self._get_lock(game.id):
                        await self._drop_leaver_from_round(game, user_id)
            elif user_id in (game.challenger_id, game.target_id):
                async with self._get_lock(game.id):
                    await self._void_for_leaver(game, user_id)
        for game in await self._db_fetch_pending_games():
            if game.guild_id == guild_id and user_id in (game.challenger_id, game.target_id):
                await self._expire_pending(game)
        for game in await self._db_fetch_resolved_games():
            if game.guild_id != guild_id or game.state != "RESOLVED":
                continue
            if user_id == game.loser_id:
                reason = duels_db.NICK_REASON_LOSER_LEFT
            elif user_id == game.winner_id:
                reason = duels_db.NICK_REASON_WINNER_LEFT
            else:
                continue
            who = "loser" if reason == duels_db.NICK_REASON_LOSER_LEFT else "winner"
            await self._conclude_unnamed(
                game, reason,
                card=discord.Embed(
                    title="🚪 Nickname Not Set",
                    description=f"The {who} left the server. No rename applied.",
                    color=COLOR_YELLOW,
                ),
            )

    async def _drop_leaver_from_lobby(self, game: Any, user_id: int) -> None:
        game = await self._db_get_game(game.id)
        if not game or game.state != "LOBBY" or user_id not in game.roster:
            return
        if user_id == game.host_id:
            await self._db_set_state(game.id, "EXPIRED_LOBBY")  # refunds everyone
            self._game_locks.pop(game.id, None)
            await self._edit_message_silent(
                game.channel_id, game.message_id,
                embed=discord.Embed(
                    title="🚫 Lobby Closed",
                    description="The host left the server, so the lobby is closed.",
                    color=COLOR_YELLOW,
                ),
                view=None,
            )
            return
        await self._return_stake(game.id, user_id)
        new_roster = [u for u in game.roster if u != user_id]
        await self._db_set_state(
            game.id, "LOBBY", roster=json.dumps(new_roster), last_action_at=time.time(),
        )
        guild = self.bot.get_guild(game.guild_id)
        game = await self._db_get_game(game.id)
        if guild is None or game is None:
            return
        min_players, max_players = await self.get_lobby_params(game.guild_id)
        embed = await self._lobby_embed(
            game, guild, min_players, max_players, await self._game_ante(game.id)
        )
        await self._edit_message_silent(
            game.channel_id, game.message_id, embed,
            self._build_lobby_view(game.id, game.host_id),
        )

    async def _drop_leaver_from_round(self, game: Any, user_id: int) -> None:
        """A live group game loses a player: out of ``alive``, announced, and
        the game resolves if only one player is left standing. An empty
        ``alive`` (Chicken, where bailers already left it) is left to the
        game's own timer, which resolves it the same way it always would."""
        game = await self._db_get_game(game.id)
        if not game or game.state != "ACTIVE" or user_id not in game.alive:
            return
        await self._return_stake(game.id, user_id)
        new_alive = [u for u in game.alive if u != user_id]
        new_elim = list(game.elimination_order) + [user_id]
        game.alive = new_alive
        game.elimination_order = new_elim
        await self._db_set_state(
            game.id, "ACTIVE",
            alive=json.dumps(new_alive),
            elimination_order=json.dumps(new_elim),
            last_action_at=time.time(),
        )
        await self._announce_elimination(game, user_id, "left the server", len(new_alive))
        if len(new_alive) == 1:
            await self._post_group_result(game, new_alive[0], user_id)
            if game_is_nick_stake(game):
                # The winner can't name someone who is gone; say so on the card
                # instead of offering a button that fails.
                fresh = await self._db_get_game(game.id)
                if fresh is not None:
                    await self._conclude_unnamed(
                        fresh, duels_db.NICK_REASON_LOSER_LEFT,
                        card=discord.Embed(
                            title="🚪 Nickname Not Set",
                            description=(
                                "The loser left the server, so there's nobody to "
                                "rename. The win stands."
                            ),
                            color=COLOR_YELLOW,
                        ),
                    )
        else:
            await self.on_player_left(game, user_id)

    async def _void_for_leaver(self, game: Any, user_id: int) -> None:
        """A duelist left mid-game: called off, every stake refunded."""
        game = await self._db_get_game(game.id)
        if not game or game.state != "ACTIVE":
            return
        await self._db_set_state(game.id, "VOID")
        guild = self.bot.get_guild(game.guild_id)
        await self._edit_message_silent(
            game.channel_id, game.message_id,
            embed=discord.Embed(
                title="🏳️ Game Called Off",
                description=(
                    f"{self._member_label(guild, user_id)} left the server mid-game. "
                    "No result, no nickname — any stakes are refunded."
                ),
                color=COLOR_YELLOW,
            ),
            view=None,
        )
        await self.on_game_resolved(game.id)
        self._game_locks.pop(game.id, None)

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
            await send_branded_dm(
                member,
                db_path=self.db.db_path,
                guild=member.guild,
                embed=discord.Embed(
                    description=(
                        f"Your {self.GAME_DISPLAY_NAME} nickname sentence has "
                        f"expired. Your nickname has been restored to "
                        f"**{restored}**."
                    )
                ),
            )
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

    async def _check_bot_can_nick(self, guild: discord.Guild) -> str | None:
        """Hard gate: without **Manage Nicknames** the bot can rename no one, so
        a nickname-stake game is pointless — abort. Role hierarchy (a specific
        player outranking the bot) is *not* fatal; it's handled non-fatally by
        :meth:`_unrenameable_members` so a staff member in the roster only means
        *they* can't be renamed, not that the game can't run."""
        if not guild.me.guild_permissions.manage_nicknames:
            return "I need the **Manage Nicknames** permission to enforce this game."
        return None

    def _unrenameable_members(
        self,
        guild: discord.Guild,
        members: list[discord.Member],
    ) -> list[discord.Member]:
        """Members the bot can't rename because their top role sits at or above
        the bot's own. The game still runs — if one of them loses, the win
        stands and the rename is skipped. The guild owner is excluded: Discord
        blocks renaming them too, but that path has the owner self-apply the
        sentence (see the owner branch in the nick-submit handler)."""
        me = guild.me
        return [
            m
            for m in members
            if m.id != guild.owner_id and me.top_role <= m.top_role
        ]

    def _unrenameable_notice(self, members: list[discord.Member]) -> str | None:
        """A non-fatal heads-up naming the players the bot can't rename, or
        ``None`` when everyone is renameable. Shown as a warning so the game
        proceeds instead of stopping when a staff member outranks the bot."""
        if not members:
            return None
        names = ", ".join(f"**{m.display_name}**" for m in members)
        who = "this player" if len(members) == 1 else "these players"
        loses = "they lose" if len(members) == 1 else "one of them loses"
        return (
            f"⚠️ I can't rename {who} — their role is above mine: {names}. "
            f"The game runs anyway; if {loses}, the win stands but no nickname "
            f"is applied. Ask an admin to move my role higher to enable it."
        )

    def _rename_warning(
        self, guild: discord.Guild, members: list[discord.Member]
    ) -> str | None:
        """Everything worth warning about before a nickname game starts.

        Combines the role-hierarchy heads-up with an owner heads-up. The owner
        is deliberately outside :meth:`_unrenameable_members` (the rename flow
        has its own branch for them) but Discord blocks that rename just the
        same, so without this the room only found out at the end — the owner
        lost their name on 2026-08-21 and had to apply it by hand with no
        warning anywhere that it would work that way.
        """
        parts = [
            n
            for n in (
                self._unrenameable_notice(self._unrenameable_members(guild, members)),
                self._owner_notice(guild, members),
            )
            if n
        ]
        return "\n".join(parts) or None

    def _owner_notice(
        self, guild: discord.Guild, members: list[discord.Member]
    ) -> str | None:
        """Heads-up that the server owner is playing for their nickname.

        Discord lets nobody rename a guild owner, bot or not. The game runs and
        the sentence is still recorded — the owner just applies it themselves.
        """
        owner = next((m for m in members if m.id == guild.owner_id), None)
        if owner is None:
            return None
        return (
            f"👑 Heads up: Discord won't let *anyone* rename the server owner, so "
            f"if **{owner.display_name}** loses I'll post the nickname and they "
            f"apply it themselves. The win still counts."
        )

    async def _check_no_active_nick(
        self,
        guild: discord.Guild,
        members: list[discord.Member],
    ) -> str | None:
        """A refusal naming the first of ``members`` who is wearing a sentence.

        Only the *nickname* stake is blocked — the old line said they "can't
        play again until it expires", which was untrue: the loser of sentence
        23 played seven wagered Pressure Cooker games while wearing it.
        """
        for member in members:
            nick = await duels_db.get_active_nick_for_user(self.db, guild.id, member.id)
            if nick:
                return (
                    f"**{member.display_name}** is wearing a nickname sentence, so "
                    f"their nickname can't be staked again until it ends — play for "
                    f"a wager or custom stakes with `nickname: False` instead."
                )
        return None

    # ── Refusals ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _refuse(interaction: discord.Interaction, text: str) -> None:
        """Send an ephemeral refusal in the house shape: ``❌ `` first.

        One helper so no refusal on these six games can forget the prefix
        again (duels-party-128: four of the six and the shared base had
        none). Not in the style contract's ``_SEND_WRAPPERS`` on purpose —
        that sweep expects a wrapper to forward its literal verbatim, and this
        one adds the prefix itself; ``tests/test_duels_copy.py`` pins it.
        """
        body = text.strip()
        if not body.startswith("❌"):
            body = f"❌ {body}"
        if interaction.response.is_done():
            await interaction.followup.send(body, ephemeral=True)
        else:
            await interaction.response.send_message(body, ephemeral=True)

    # ── Availability ──────────────────────────────────────────────────────────

    async def _launch_refusal(
        self, guild_id: int, channel_id: int | None, cfg: dict
    ) -> str | None:
        """Why this game may not start here right now, or None.

        The two gates every door into one of these games shares — the
        command, and the Run It Back button on a result card. The "Available
        on This Server" toggle is the same ``games_game_config`` row every
        other game reads (no row means enabled); the channel rule is the
        game's own allowlist, empty meaning everywhere. The party games'
        global channel list and their one-game-per-channel rule are
        deliberately not applied — a duel has always run alongside a party
        game, and changing that is a decision, not a fix (spec §8).
        """
        if not await check_game_enabled(self.db, self.GAME_KEY, guild_id):
            return f"{self.GAME_DISPLAY_NAME} is switched off on this server."
        allowlist: list[int] = json.loads(cfg.get("channel_allowlist") or "[]")
        if allowlist and channel_id not in allowlist:
            return (
                f"{self.GAME_DISPLAY_NAME} isn't allowed in this channel — "
                "an admin picks where it can run on the dashboard."
            )
        return None

    # ── Copy that names a dial ───────────────────────────────────────────────
    #
    # "24 hours" and "5 minutes" were typed into every result card, DM and
    # slash description while sentence_hours is a dial and the sweeps have
    # their own constants (duels-party-125). Everything member-facing that
    # states a duration comes through here now.

    @staticmethod
    def _span(seconds: int | float) -> str:
        """``300`` → "5 minutes", ``90`` → "90 seconds", ``7200`` → "2 hours"."""
        seconds = int(seconds)
        if seconds % 3600 == 0 and seconds >= 3600:
            n, unit = seconds // 3600, "hour"
        elif seconds % 60 == 0 and seconds >= 60:
            n, unit = seconds // 60, "minute"
        else:
            n, unit = seconds, "second"
        return f"{n} {unit}" if n == 1 else f"{n} {unit}s"

    @classmethod
    def _hours_span(cls, hours: int | float | None) -> str:
        """"24 hours" — from the dial when the caller knows it, else the
        default a guild with no config row plays under."""
        if hours is None:
            hours = duels_db.default_sentence_hours()
        return cls._span(float(hours) * 3600)

    async def _sentence_hours(self, guild_id: int) -> int:
        cfg = await duels_db.get_config(self.db, guild_id, self.GAME_KEY)
        try:
            return max(1, int(cfg.get("sentence_hours") or duels_db.default_sentence_hours()))
        except (TypeError, ValueError):
            return duels_db.default_sentence_hours()

    def _nick_stakes_line(self, sentence_hours: int | None) -> str:
        """The "🏷️ Loser surrenders their nickname for N hours." stakes line."""
        return nick_stakes_line(self._hours_span(sentence_hours))

    def nick_forfeit_copy(self, sentence_hours: int | None = None) -> str:
        """Fallback stakes line for a plain nickname game (``stakes_text`` None)."""
        return f"Loser surrenders their nickname for {self._hours_span(sentence_hours)}."

    def nick_applied_copy(
        self, who: str, nick: str, sentence_hours: int | None = None
    ) -> str:
        return (
            f"**{who}** is now known as **{nick}** for "
            f"{self._hours_span(sentence_hours)}."
        )

    def nick_self_apply_copy(
        self, who: str, nick: str, sentence_hours: int | None = None
    ) -> str:
        # Discord blocks the bot from renaming the guild owner, so the sentence
        # is real but has to be applied by hand. Saying "is now known as" here
        # would be a plain lie about what happened.
        return (
            f"Discord won't let me rename the server owner, so **{who}** has to "
            f"set **{nick}** themselves. It stands for {self._hours_span(sentence_hours)}."
        )

    def awaiting_nick_copy(self, winner_name: str, sentence_hours: int | None = None) -> str:
        return (
            f"**{winner_name}**, press **Name the Loser** within "
            f"{self._span(NAMING_WINDOW_SECONDS)}. The nickname lasts "
            f"{self._hours_span(sentence_hours)}."
        )

    @staticmethod
    def _remaining(seconds: float) -> str:
        """``5400`` → "1h 30m"; under a minute rounds up to "1m"."""
        total = max(60, int(seconds + 59) // 60 * 60)
        hours, mins = total // 3600, (total % 3600) // 60
        return f"{hours}h {mins}m" if hours else f"{mins}m"

    def _cooldown_copy(self, remaining: float | None) -> str:
        """The lobby-join cooldown refusal. ``remaining`` None is the form
        the no-contact gate borrows, so it must stay a sentence a real
        cooldown could also produce."""
        if remaining is None:
            return "You're on cooldown for this game — try again later."
        return (
            f"You're on cooldown for this game — try again in "
            f"**{self._remaining(remaining)}**."
        )

    # ── Rate limit ────────────────────────────────────────────────────────────

    def _challenge_limit(self, cfg: dict) -> int:
        """This guild's per-hour challenge cap for this game (0 = no limit).

        Was a hardcoded 3, which is a spam brake set at the pace of an idle
        channel rather than a game night — the room's most engaged player hit
        it twice in one evening ("Wait I can't challenge anybody this hour
        anyway"). It is a dashboard dial now, defaulting high enough that only
        genuine spam reaches it.
        """
        try:
            return max(0, int(cfg.get("challenge_limit_per_hour", _RATE_LIMIT_MAX)))
        except (TypeError, ValueError):
            return _RATE_LIMIT_MAX

    def _check_rate_limit(self, user_id: int, limit: int = _RATE_LIMIT_MAX) -> bool:
        """True when the user has already used up ``limit`` challenges this
        hour. ``limit`` of 0 disables the cap entirely."""
        if limit <= 0:
            return False
        dq = self._challenge_rate[user_id]
        now = time.time()
        while dq and now - dq[0] > _RATE_LIMIT_WINDOW:
            dq.popleft()
        return len(dq) >= limit

    def _record_challenge(self, user_id: int) -> None:
        self._challenge_rate[user_id].append(time.time())

    # ── Nickname-stake flow (one winner names one loser) ──────────────────────

    # ── No-contact gate ───────────────────────────────────────────────────────
    #
    # Every surface that puts two members in contact consults the no-contact
    # list (CLAUDE.md, docs/no_contact_spec.md). A challenge publicly pings
    # its target and makes them answer in-channel, a lobby seats the joiner
    # next to everyone already in, and a win lets one member rename the
    # other for a day — so all three go through here, and every game that
    # subclasses BaseGame inherits the gate. The refusal at each surface is
    # an ordinary outcome that surface already produces, never a new
    # "blocked" line: the blocked party must not be able to tell.

    def _no_contact_db_path(self) -> Path:
        """The main DB, where ``no_contact_pairs`` lives. Same file as the
        games DB in prod; the fallback keeps a bot fake without ``ctx`` on
        the real table rather than silently on no table at all."""
        ctx = getattr(self.bot, "ctx", None)
        return ctx.db_path if ctx is not None else self.db.db_path

    async def _blocked_pair(
        self,
        guild_id: int,
        actor_id: int,
        target_id: int,
        *,
        record_surface: str | None = None,
    ) -> bool:
        """Whether these two hold a no-contact pair, in either direction.

        ``record_surface`` also logs an attempt event for staff — used only
        where the actor aimed something at the target on purpose (a
        challenge). A lobby join or a Name-the-Loser press is the game's own
        arithmetic putting two people together, not an attempt, and is
        gated without a log line (the Risky Rolls reasoning in the spec).
        """
        db_path = self._no_contact_db_path()
        if record_surface:
            return await asyncio.to_thread(
                no_contact_service.check_and_record,
                db_path, guild_id,
                actor_id=actor_id, target_id=target_id, surface=record_surface,
            )
        return await asyncio.to_thread(
            no_contact_service.is_no_contact, db_path, guild_id, actor_id, target_id
        )

    async def _blocked_with_any(
        self, guild_id: int, user_id: int, others: "list[int] | set[int]"
    ) -> bool:
        """Whether ``user_id`` holds a pair with anyone in ``others`` (one read)."""
        ids = {int(u) for u in others} - {int(user_id)}
        if not ids:
            return False
        partners = await asyncio.to_thread(
            no_contact_service.no_contact_partners,
            self._no_contact_db_path(), guild_id, user_id,
        )
        return bool(partners & ids)

    @staticmethod
    def _sentence_in_progress_copy(loser_name: str) -> str:
        """The winner's ephemeral when the loser can't be renamed because a
        nickname sentence is already running. Shared with the no-contact
        gate, which borrows it as its ordinary-looking refusal — so the two
        can never drift apart."""
        return (
            f"**{loser_name}** is already serving a nickname sentence from "
            "another game. Your win stands, but a new nickname can't be applied until "
            "that one expires."
        )

    async def _refuse_rename_across_pair(
        self, interaction: discord.Interaction, game: Any
    ) -> bool:
        """Conclude at NO_NICK_SET with the sentence-in-progress line when the
        winner and loser hold a pair. True when the caller must return."""
        if not await self._blocked_pair(game.guild_id, game.winner_id, game.loser_id):
            return False
        guild = interaction.guild
        loser = guild.get_member(game.loser_id) if guild else None
        name = loser.display_name if loser else "The loser"
        await self._refuse(interaction, self._sentence_in_progress_copy(name))
        await self._conclude_unnamed(game, duels_db.NICK_REASON_ALREADY_SERVING)
        return True

    async def _handle_set_nick(self, interaction: discord.Interaction, game_id: int) -> None:
        game = await self._db_get_game(game_id)
        if not game or game.state != "RESOLVED":
            await interaction.response.send_message(
                "A nickname has already been set for this game.", ephemeral=True
            )
            return
        # Before the modal, not after: the winner never gets to compose a
        # name for someone they are kept apart from.
        if await self._refuse_rename_across_pair(interaction, game):
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
                "❌ Only the winner can set the nickname.", ephemeral=True
            )
            return
        # Gated again under the lock: a modal opened before the pair existed
        # still applies no rename.
        if await self._refuse_rename_across_pair(interaction, game):
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
        sentence_hours = int(cfg["sentence_hours"])
        loser = guild.get_member(game.loser_id)  # type: ignore[arg-type]
        if not loser:
            await self._refuse(
                interaction, "The loser appears to have left the server. No rename applied."
            )
            await self._conclude_unnamed(game, duels_db.NICK_REASON_LOSER_LEFT)
            return

        perm_error = await self._check_bot_can_nick(guild)
        if perm_error:
            await interaction.response.send_message(perm_error, ephemeral=True)
            return

        # The loser outranks the bot (e.g. a staff member): Discord won't let me
        # rename them. The win still stands — conclude with no nickname rather
        # than erroring out, mirroring the owner / left-server / active-sentence
        # paths above and below.
        if self._unrenameable_members(guild, [loser]):
            # Same shape as the owner branch below: the rename can't happen, so
            # hand the name over publicly rather than swallowing it in an
            # ephemeral the loser and the room never see.
            await self._refuse(
                interaction,
                f"**{loser.display_name}**'s role is above mine, so I can't "
                f"rename them — but your win stands, and they've been told.",
            )
            await self._conclude_unnamed(game, duels_db.NICK_REASON_LOSER_OUTRANKS)
            await self._announce_to_channel(
                game_id,
                f"{loser.mention} 📋 {interaction.user.mention} won, and named you "
                f"**{cleaned_nick}** — but your role sits above mine so I can't "
                f"apply it. Honour system for the next "
                f"{self._hours_span(sentence_hours)}.",
            )
            return

        # Guard against overlapping sentences: if the loser is already serving a
        # nick sentence from a concurrent game, applying a second one here would
        # snapshot the already-imposed nick as the "original" and corrupt the
        # eventual revert. Refuse rather than stack sentences.
        existing_sentence = await self._check_no_active_nick(guild, [loser])
        if existing_sentence:
            await self._refuse(interaction, self._sentence_in_progress_copy(loser.display_name))
            await self._conclude_unnamed(game, duels_db.NICK_REASON_ALREADY_SERVING)
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
                game,
                guild,
                self_apply_nick=cleaned_nick,
                original_name=original_display_name,
                sentence_hours=sentence_hours,
            )
            await interaction.response.edit_message(
                embed=embed, view=self._result_view(game, disabled=True)
            )
            # Mention the loser: the embed edit is easy to scroll past, and the
            # rename genuinely will not happen unless they act on it.
            await interaction.followup.send(
                f"{loser.mention} 📋 Discord won't let me rename the server owner, "
                f"so this one is on the honour system — your sentence is "
                f"**{cleaned_nick}**, for the next {self._hours_span(sentence_hours)}. "
                f"{interaction.user.mention} won it fair and square.",
            )
            return

        try:
            await loser.edit(
                nick=cleaned_nick,
                reason=f"{self.GAME_DISPLAY_NAME}: lost to {interaction.user.display_name}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to rename that user.", ephemeral=True
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
            game, guild, imposed_nick=cleaned_nick, original_name=original_display_name,
            sentence_hours=sentence_hours,
        )
        await interaction.response.edit_message(
            embed=embed, view=self._result_view(game, disabled=True)
        )

    # ── The result card's buttons ─────────────────────────────────────────────

    @staticmethod
    def _rematch_deadline(game: Any) -> float | None:
        """When this result's Run It Back stops working, or None once it has.
        Measured from ``resolved_at`` when the game recorded one, else from
        now (the card is being posted this instant)."""
        base = getattr(game, "resolved_at", None) or time.time()
        deadline = float(base) + REMATCH_WINDOW_SECONDS
        return deadline if deadline > time.time() else None

    def _result_view(
        self,
        game: Any,
        *,
        winner_id: int | None = None,
        loser_id: int | None = None,
        disabled: bool = False,
    ) -> ResultView:
        """The one place a result card's buttons are built.

        ``📝 Name the Loser`` only on a nickname game still at RESOLVED;
        ``🔁 Run It Back`` on every settled game while its window is open.
        """
        state = getattr(game, "state", None)
        nick_live = game_is_nick_stake(game) and state in (None, "RESOLVED", "ACTIVE")
        view = ResultView(
            game.id,
            int(winner_id if winner_id is not None else game.winner_id),
            int(loser_id if loser_id is not None else game.loser_id),
            self._handle_set_nick if nick_live else None,
            on_rematch=self._handle_rematch,
            rematch_deadline=None if disabled else self._rematch_deadline(game),
        )
        if disabled:
            view.disable()
        return view

    async def _handle_rematch(self, interaction: discord.Interaction, game_id: int) -> None:
        """Run It Back: the same people, stakes and wager, one press.

        A duel re-posts the challenge card from the presser to the other
        duelist — Accept is still theirs to press, so nobody's coins or
        nickname go on the line without them saying so, and the wager is
        declared now and taken at accept exactly as a typed challenge is. A
        group game reopens a lobby with the host seated (and their ante
        taken) and pings the old roster to press Join; it starts itself the
        moment it is full. Both go through the same door as the command —
        the enabled switch, the channel rule, the no-contact list, the
        sentence and cooldown preflights — because they *are* the command.
        """
        game = await self._db_get_game(game_id)
        if game is None or game.state not in (
            "RESOLVED", "RESOLVED_NO_NICK", "NICKED", "NO_NICK_SET",
        ):
            await self._refuse(interaction, "That game hasn't finished yet.")
            return
        if self._rematch_deadline(game) is None:
            from .views import REMATCH_EXPIRED_TEXT

            await self._refuse(interaction, REMATCH_EXPIRED_TEXT)
            return
        guild = interaction.guild
        if guild is None:
            await self._refuse(interaction, "This only works in a server.")
            return
        presser = interaction.user.id
        ante = await self._game_ante(game_id)
        wager = ante if ante > 0 else None
        nick_stake = game_is_nick_stake(game)
        custom = custom_stakes_from(getattr(game, "stakes_text", None))

        roster = getattr(game, "roster", None)
        if roster is None:
            other = game.target_id if presser == game.challenger_id else game.challenger_id
            if presser not in (game.challenger_id, game.target_id):
                await self._refuse(interaction, "Only the two who played can run it back.")
                return
            target = guild.get_member(int(other))
            if target is None:
                await self._refuse(
                    interaction, "Your opponent has left the server — challenge someone else."
                )
                return
            await self._base_challenge(  # type: ignore[attr-defined]
                interaction, target, custom, wager, nickname=nick_stake,
                stakes_prevalidated=True,
            )
            return

        if presser != game.host_id:
            await self._refuse(interaction, "Only the host can run it back — ask them to press it.")
            return
        new_id = await self._base_lobby(
            interaction, custom, wager, nickname=nick_stake, stakes_prevalidated=True,
        )
        if new_id is None:
            return  # refused before a lobby was posted
        others = [int(u) for u in roster if int(u) != presser and guild.get_member(int(u))]
        if others:
            mentions = " ".join(f"<@{u}>" for u in others)
            try:
                await interaction.followup.send(
                    f"🔁 Run it back! {mentions} — press **✋ Join** on the new lobby."
                )
            except (discord.Forbidden, discord.HTTPException):
                pass

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
        self,
        game: Any,
        guild: discord.Guild,
        min_players: int,
        max_players: int,
        ante: int = 0,
        *,
        color: discord.Color | None = None,
        settings: EconSettings | None = None,
        closes_at: float | None = None,
        sentence_hours: int | None = None,
    ) -> discord.Embed:
        names = []
        for uid in game.roster:
            m = guild.get_member(uid)
            names.append(m.display_name if m else str(uid))
        host = guild.get_member(game.host_id)
        host_name = host.display_name if host else str(game.host_id)
        embed = discord.Embed(
            title=f"🎮 {self.GAME_DISPLAY_NAME} — Lobby",
            description=(
                "Press **✋ Join** to get in. Host presses **▶️ Start** when ready — "
                f"or it starts on its own once {max_players} are in."
            ),
            color=color or discord.Color(COLOR_GOLD),
        )
        if self.HOW_TO_PLAY:
            embed.add_field(name="📖 How to Play", value=self.HOW_TO_PLAY, inline=False)
        embed.add_field(
            name=f"👥 Players ({len(game.roster)}/{max_players})",
            value="\n".join(f"• {n}" for n in names) or "—",
            inline=False,
        )
        stakes = game.stakes_text or (
            "Last one standing wins; the final loser surrenders their nickname for "
            f"{self._hours_span(sentence_hours)}."
        )
        embed.add_field(name="📋 Stakes", value=stakes, inline=False)
        if ante > 0:
            pot = (
                _fmt_coins(settings, ante * len(game.roster))
                if settings
                else f"**{ante * len(game.roster):,}**"
            )
            # The ante itself is already a line in the stakes field (see
            # filters.resolve_stakes_text); this field only carries what that
            # string can't — the pot, which grows as people join.
            embed.add_field(
                name="💰 Pot",
                value=(
                    f"{pot} so far, and it grows with each player.\n"
                    "_Leaving the lobby refunds you._"
                ),
                inline=False,
            )
        if closes_at is not None:
            # A live countdown, not a number: the old card said nothing at all
            # about the clock, and a footer can't carry a timestamp.
            embed.add_field(
                name="⏱️ Closes",
                value=f"<t:{int(closes_at)}:R> — every join resets the clock.",
                inline=False,
            )
        embed.set_footer(text=f"Host: {host_name} • Need {min_players}+ players to start.")
        apply_section_spacing(embed)
        return embed

    async def _lobby_embed(
        self,
        game: Any,
        guild: discord.Guild,
        min_players: int,
        max_players: int,
        ante: int = 0,
    ) -> discord.Embed:
        """Build the lobby embed with the guild accent + currency vocabulary.

        The async companion to :meth:`_render_lobby` — resolves the accent and
        (for a wagered lobby) loads the econ settings so the wager field reads
        ``🪙 **500** coins``, then hands both to the sync builder.
        """
        accent = await safe_resolve_accent(self.bot, guild, log_label="base game")
        settings = await self._econ_settings(guild.id) if ante > 0 else None
        last = getattr(game, "last_action_at", None) or getattr(game, "created_at", None)
        closes_at = float(last) + LOBBY_IDLE_SECONDS if last else None
        sentence_hours = await self._sentence_hours(guild.id) if game_is_nick_stake(game) else None
        return self._render_lobby(
            game, guild, min_players, max_players, ante,
            color=accent, settings=settings, closes_at=closes_at,
            sentence_hours=sentence_hours,
        )

    async def _econ_settings(self, guild_id: int) -> EconSettings | None:
        """Load this guild's econ settings off-thread (for currency vocabulary),
        or ``None`` when there's no app context (economy unavailable)."""
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return None

        def _load() -> EconSettings:
            with ctx.open_db() as conn:
                return load_econ_settings(conn, guild_id)

        return await asyncio.to_thread(_load)

    async def _base_lobby(
        self,
        interaction: discord.Interaction,
        stakes_text: str | None,
        wager: int | None = None,
        nickname: bool | None = None,
        *,
        stakes_prevalidated: bool = False,
    ) -> int | None:
        """Open a join lobby for an N-player game. Called by a subclass /start command.

        Returns the new game's id, or None when the lobby was refused.

        ``wager`` opens a coin-wagered lobby: the host antes immediately (they
        are in the roster from creation, and their stake is what records the
        ante every joiner must match), each joiner pays on join, and the pot
        goes to the winner. Leaving refunds; so does a cancelled, expired or
        abandoned game. ``stakes_prevalidated`` is the Run It Back path
        handing back text that already went through ``validate_stakes`` (and
        its markdown escape) the first time.
        """
        if not interaction.guild:
            await self._refuse(interaction, "This command only works in a server.")
            return None

        host = interaction.user  # type: ignore[assignment]
        guild: discord.Guild = interaction.guild

        cfg = await duels_db.get_config(self.db, guild.id, self.GAME_KEY)
        refusal = await self._launch_refusal(guild.id, interaction.channel_id, cfg)
        if refusal:
            await self._refuse(interaction, refusal)
            return None

        limit = self._challenge_limit(cfg)
        if self._check_rate_limit(host.id, limit):
            await self._refuse(
                interaction,
                f"You've started too many games recently — the limit here is "
                f"{limit} an hour. Try again a little later.",
            )
            return None

        # Normalise the stakes text before deciding whether this is a nickname
        # game — see the same reordering in _base_challenge: whitespace-only
        # stakes clean to None, and reading the raw string here would skip the
        # preflights for a game that turns out to stake the nickname after all.
        if stakes_text and not stakes_prevalidated:
            stakes_result = validate_stakes(
                stakes_text,
                max_length=cfg["max_stakes_length"],
                denylist=json.loads(cfg.get("nick_denylist") or "[]"),
            )
            if not stakes_result.ok:
                await self._refuse(interaction, f"Stakes rejected: {stakes_result.reason}")
                return None
            stakes_text = stakes_result.value or None

        # Nickname-mode preflight only applies when the loser is going to be
        # renamed; every other stake leaves nicknames alone.
        nick_stake = resolve_nick_stake(stakes_text, wager, nickname)
        if not nick_stake and stakes_text is None and wager is None:
            # See _base_challenge: a game with nothing staked is not a game.
            await self._refuse(
                interaction,
                "Turning the nickname stake off means you need to stake "
                "something else — add `wager:` or `stakes:`.",
            )
            return None
        nick_notice: str | None = None
        if nick_stake:
            err = await self._check_bot_can_nick(guild)
            if err:
                await self._refuse(interaction, err)
                return None
            err = await self._check_no_active_nick(guild, [host])  # type: ignore[list-item]
            if err:
                await self._refuse(interaction, err)
                return None
            # The host outranking the bot doesn't block the lobby — warn later.
            nick_notice = self._rename_warning(guild, [host])  # type: ignore[list-item]
            cd = await duels_db.check_group_cooldown(
                self.db, guild.id, self.GAME_KEY, host.id, cfg["cooldown_hours"]
            )
            if cd is not None:
                await self._refuse(interaction, self._cooldown_copy(cd))
                return None

        if wager is not None:
            err = await self._wager_precheck(guild.id, host.id, wager)
            if err:
                await self._refuse(interaction, err)
                return None

        # Every live stake goes into the persisted text so the lobby, the
        # round embeds and the result all list the same set.
        settings = await self._econ_settings(guild.id) if wager is not None else None
        wager_line = None
        if wager is not None:
            each = _fmt_coins(settings, wager) if settings else f"**{wager:,}**"
            wager_line = f"💰 {each} to join — winner takes the pot."
        stakes_text = resolve_stakes_text(
            stakes_text, wager, nick_stake=nick_stake, wager_line=wager_line,
            nick_line=self._nick_stakes_line(int(cfg["sentence_hours"])),
        )

        min_players, max_players = await self.get_lobby_params(guild.id)
        game_id = await self._db_create_lobby(
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            host_id=host.id,
            stakes_text=stakes_text,
            nick_stake=nick_stake,
        )
        self._record_challenge(host.id)

        if wager is not None:
            # The host's own ante both funds the pot and records the amount
            # every joiner has to match (_game_ante reads it back).
            err = await self._take_stake(guild.id, game_id, host.id, wager)
            if err:
                await self._db_set_state(game_id, "EXPIRED_LOBBY")
                await self._refuse(interaction, err)
                return None

        game = await self._db_get_game(game_id)
        embed = await self._lobby_embed(
            game, guild, min_players, max_players, wager or 0
        )
        view = self._build_lobby_view(game_id, host.id)
        await interaction.response.send_message(embed=embed, view=view)
        msg = await interaction.original_response()
        self.bot.add_view(view, message_id=msg.id)
        await self._db_set_state(game_id, "LOBBY", message_id=msg.id, last_action_at=time.time())
        if nick_notice:
            await interaction.followup.send(nick_notice, ephemeral=True)
        return game_id

    async def _handle_lobby_join(self, interaction: discord.Interaction, game_id: int) -> None:
        #: Set when a join filled the lobby and started the game (see
        #: _handle_lobby_start for why the chore is signed off after the lock).
        started: tuple[int, int] | None = None
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await self._refuse(interaction, "This lobby is no longer open.")
                return
            uid = interaction.user.id
            if uid in game.roster:
                await self._refuse(interaction, "You're already in.")
                return
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            min_players, max_players = await self.get_lobby_params(game.guild_id)
            if len(game.roster) >= max_players:
                await self._refuse(interaction, f"The lobby is full ({max_players}).")
                return
            # A joiner kept apart from anyone already seated (host included)
            # gets the lobby's own cooldown line: a private condition nobody
            # else can check, unlike "full" or "no longer open" which the
            # card visibly contradicts. Before the nickname preflight, so a
            # refused joiner never reaches it.
            if await self._blocked_with_any(game.guild_id, uid, game.roster):
                await self._refuse(interaction, self._cooldown_copy(None))
                return

            member = guild.get_member(uid)
            nick_notice: str | None = None
            if game_is_nick_stake(game) and member is not None:
                err = await self._check_bot_can_nick(guild) or \
                    await self._check_no_active_nick(guild, [member])
                if err:
                    await self._refuse(interaction, err)
                    return
                # Joining while outranking the bot is allowed — warn, don't block.
                nick_notice = self._rename_warning(guild, [member])
                cfg = await duels_db.get_config(self.db, game.guild_id, self.GAME_KEY)
                cd = await duels_db.check_group_cooldown(
                    self.db, game.guild_id, self.GAME_KEY, uid, cfg["cooldown_hours"]
                )
                if cd is not None:
                    await self._refuse(interaction, self._cooldown_copy(cd))
                    return

            # Wagered lobby: the ante is taken on JOIN, so a player knows
            # immediately whether they're in and the host can never be
            # blocked at start by someone else's empty wallet. A failed
            # debit refuses the join outright.
            ante = await self._game_ante(game_id)
            if ante > 0:
                err = await self._take_stake(game.guild_id, game_id, uid, ante)
                if err:
                    await self._refuse(interaction, err)
                    return

            new_roster = list(game.roster) + [uid]
            await self._db_set_state(
                game_id, "LOBBY", roster=json.dumps(new_roster), last_action_at=time.time()
            )
            game = await self._db_get_game(game_id)
            if not game:
                return
            if len(new_roster) >= max_players:
                # A full lobby starts itself: the 10-player Musical Chairs
                # lobby on 2026-08-17 expired under its host while everyone
                # was reading the rules, waiting for a Start press.
                err = await self._start_lobby_locked(interaction, game, guild)
                if err is None:
                    started = (game.guild_id, game.host_id)
                else:
                    # Can't start (someone in the roster is wearing a
                    # sentence, say): the joiner is in, the card refreshes,
                    # and the joiner hears why Start will refuse too.
                    nick_notice = f"{nick_notice}\n{err}" if nick_notice else err
            if started is None:
                embed = await self._lobby_embed(game, guild, min_players, max_players, ante)
                await interaction.response.edit_message(
                    embed=embed, view=self._build_lobby_view(game_id, game.host_id)
                )
            if nick_notice:
                await interaction.followup.send(nick_notice, ephemeral=True)
        if started is not None:
            await sign_off_game_chore(self.bot, *started)

    async def _handle_lobby_leave(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await self._refuse(interaction, "This lobby is no longer open.")
                return
            uid = interaction.user.id
            if uid == game.host_id:
                await self._refuse(
                    interaction, "The host can't leave — use **🚫 Cancel** to close the lobby."
                )
                return
            if uid not in game.roster:
                await self._refuse(interaction, "You're not in this lobby.")
                return
            guild: discord.Guild = interaction.guild  # type: ignore[assignment]
            min_players, max_players = await self.get_lobby_params(game.guild_id)
            refunded = await self._return_stake(game_id, uid)
            new_roster = [u for u in game.roster if u != uid]
            await self._db_set_state(
                game_id, "LOBBY", roster=json.dumps(new_roster), last_action_at=time.time()
            )
            if refunded:
                log.info(
                    "%s: refunded %d to %d on lobby leave (game %d)",
                    self.GAME_KEY, refunded, uid, game_id,
                )
            game = await self._db_get_game(game_id)
            if not game:
                return
            ante = await self._game_ante(game_id)
            embed = await self._lobby_embed(game, guild, min_players, max_players, ante)
            await interaction.response.edit_message(
                embed=embed, view=self._build_lobby_view(game_id, game.host_id)
            )

    async def _handle_lobby_cancel(self, interaction: discord.Interaction, game_id: int) -> None:
        async with self._get_lock(game_id):
            game = await self._db_get_game(game_id)
            if not game or game.state != "LOBBY":
                await self._refuse(interaction, "This lobby is no longer open.")
                return
            if interaction.user.id != game.host_id:
                await self._refuse(interaction, "Only the host can cancel the lobby.")
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
        #: Set once the game really starts, so the chore sign-off can happen
        #: after the interaction is answered and outside the per-game lock.
        started: tuple[int, int] | None = None
        try:
            async with self._get_lock(game_id):
                game = await self._db_get_game(game_id)
                if not game or game.state != "LOBBY":
                    await self._refuse(interaction, "This lobby is no longer open.")
                    return
                if interaction.user.id != game.host_id:
                    await self._refuse(interaction, "Only the host can start the game.")
                    return
                min_players, _max_players = await self.get_lobby_params(game.guild_id)
                if len(game.roster) < min_players:
                    await self._refuse(
                        interaction,
                        f"You need at least **{min_players}** players to start "
                        f"(currently {len(game.roster)}).",
                    )
                    return

                guild: discord.Guild = interaction.guild  # type: ignore[assignment]
                err = await self._start_lobby_locked(interaction, game, guild)
                if err is not None:
                    await self._refuse(interaction, err)
                    return
                # A lobby game only counts as "run" once it actually starts — the
                # roster is real by here, where at lobby-open time it was one
                # person and an invitation. Credited to the host who opened it,
                # not whoever pressed Start.
                started = (game.guild_id, game.host_id)

        finally:
            # In a finally, and outside the lock: by the time `started` is
            # set the game is ACTIVE, so the chore is owed even if the row
            # vanishes before the view is built and that guard returns out.
            # Signing off can repaint the todo board, and a repaint is a
            # REST edit discord.py sleeps through under per-channel rate
            # limiting — long enough to burn the three-second interaction
            # window (the hazard todo_cog.add_todo documents) and, in here,
            # to hold the per-game lock while it does.
            if started is not None:
                await sign_off_game_chore(self.bot, *started)

    async def _start_lobby_locked(
        self, interaction: discord.Interaction, game: Any, guild: discord.Guild
    ) -> str | None:
        """Start a LOBBY game. Caller holds the lock and has checked who is
        pressing and that the floor is met. Returns a refusal (nothing sent)
        when the nickname preflight fails; otherwise the game is ACTIVE, the
        lobby message has become the game card, and the rename warning (if
        any) has been sent as a follow-up.

        Shared by the host's Start press and the auto-start a full lobby
        performs on the join that fills it.
        """
        nick_notice: str | None = None
        if game_is_nick_stake(game):
            members = [m for m in (guild.get_member(u) for u in game.roster) if m]
            err = await self._check_bot_can_nick(guild) or \
                await self._check_no_active_nick(guild, members)
            if err:
                return err
            # Players outranking the bot don't block the start — warn, and
            # skip their rename if one of them loses.
            nick_notice = self._rename_warning(guild, members)

        await self._db_set_state(
            game.id, "ACTIVE",
            alive=json.dumps(list(game.roster)),
            last_action_at=time.time(),
        )
        fresh = await self._db_get_game(game.id)
        if not fresh:
            return None
        await self.on_game_start(fresh)
        # on_game_start may have written more fields — read the row it left.
        fresh = await self._db_get_game(game.id)
        if not fresh:
            return None
        view = self.build_game_view(fresh.id)
        embed = self.render_game_state(fresh, guild)
        self.bot.add_view(view, message_id=fresh.message_id)
        await interaction.response.edit_message(embed=embed, view=view)
        if nick_notice:
            await interaction.followup.send(nick_notice, ephemeral=True)
        return None

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

        nick_mode = game_is_nick_stake(game)

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
            sentence_hours = await self._sentence_hours(game.guild_id) if nick_mode else None
            result_embed = self.render_result_state(
                game, guild, sentence_hours=sentence_hours
            )
            winner_m = guild.get_member(winner_id)
            loser_m = guild.get_member(loser_id)
            ping = " ".join(m.mention for m in (winner_m, loser_m) if m)
            game.resolved_at = now
            rv = self._result_view(game, winner_id=winner_id, loser_id=loser_id)
            try:
                msg = await channel.send(content=ping, embed=result_embed, view=rv)  # type: ignore[union-attr]
                self.bot.add_view(rv, message_id=msg.id)
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

    def _member_label(self, guild: discord.Guild | None, user_id: int) -> str:
        """Bold display name when the member is cached, a mention otherwise."""
        member = guild.get_member(user_id) if guild is not None else None
        return f"**{member.display_name}**" if member else f"<@{user_id}>"

    async def _announce_elimination(
        self, game: Any, player_id: int, reason: str, remaining: int
    ) -> None:
        """Say publicly that a player is out, and why.

        A player knocked out by their own press only ever saw an ephemeral, so
        the room watched the survivor count jump with a single announcement
        covering several exits and nobody knew who had gone or what they had
        done wrong (game night, 2026-08-21). ``reason`` completes the sentence
        "X <reason>" — e.g. "sat before the music stopped".
        """
        guild = self.bot.get_guild(game.guild_id)
        who = self._member_label(guild, player_id)
        left = "1 left!" if remaining == 1 else f"{remaining} left."
        await self._announce_to_channel(game.id, f"❌ {who} {reason} — {left}")

    async def _group_eliminate(
        self,
        game: Any,
        player_id: int,
        *,
        interaction: discord.Interaction | None = None,
        reason: str | None = None,
    ) -> None:
        """Remove player_id from `alive`, append to `elimination_order`, and resolve
        the game if only one player remains (loser = last eliminated). Caller holds
        the per-game lock.

        ``reason`` opts the caller into a public call-out (see
        :meth:`_announce_elimination`); it posts before any terminal resolution
        so the last exit is explained too, not swallowed by the result embed.
        """
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
        if reason:
            await self._announce_elimination(game, player_id, reason, len(new_alive))
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

    async def on_player_left(self, game: Any, user_id: int) -> None:
        """Called after a leaver has been dropped from a live round and the
        game continues with two or more players. A game whose round state
        points at the leaver (a potato holder, say) can re-aim here."""

    # ── Abstract DB hooks (subclass must implement) ───────────────────────────

    async def _db_create_game(
        self,
        guild_id: int,
        channel_id: int,
        challenger_id: int,
        target_id: int,
        stakes_text: str | None,
        nick_stake: bool = False,
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
        """Template method: persist the state, then let the economy observe
        game ends. Cogs implement the write in ``_db_write_state`` and must
        route every state change through here — writing through their db
        module directly would end a game without the economy seeing it."""
        await self._db_write_state(game_id, state, **kw)
        if state in _TERMINAL_STATES:
            await self._on_terminal_state(game_id, state)

    async def _db_write_state(self, game_id: int, state: str, **kw: Any) -> None:
        raise NotImplementedError

    async def _on_terminal_state(self, game_id: int, state: str) -> None:
        """Economy hook fired on every game end.

        Two independent jobs, in order:

        1. **Wager escrow.** A settling state pays the whole pot to the
           winner; every other terminal state refunds each stake. Both are
           exactly-once (``settled_at`` predicates), because this hook can
           fire more than once for one game — the 1-minute sweep, the resume
           path and a normal resolution all reach it. A winner of None (a
           Chicken wipeout, a degenerate Musical Chairs round) refunds rather
           than paying nobody.
        2. **The faucet.** RESOLVED / RESOLVED_NO_NICK pay
           participation/win rewards as before.

        Failures are swallowed — economy must never block game flow. The
        escrow *debit* deliberately does NOT inherit that rule: a failed
        debit raises and stops the game from starting (see
        ``economy_wager_service``).
        """
        try:
            game = await self._db_get_game(game_id)
            winner_id = getattr(game, "winner_id", None) if game else None
            guild_id = getattr(game, "guild_id", None) if game else None
            await self._resolve_wagers(game_id, state, winner_id, guild_id)
            if state not in _SETTLING_STATES:
                return
            if game is None:
                return
            await self._record_rematch_cooldown(game)
            await self._record_game_history(game_id, game, state)
            await pay_game_rewards(
                self.bot,
                game.guild_id,
                self._game_participants(game),
                [winner_id] if winner_id is not None else [],
                self.GAME_KEY,
                occurrence=str(game_id),
            )
        except Exception:
            log.exception(
                "%s terminal-state hook failed for game %s (%s)",
                self.GAME_DISPLAY_NAME, game_id, state,
            )

    async def _record_rematch_cooldown(self, game: Any) -> None:
        """Note that this game was played, for the cooldown dial. Duels record
        the pair (see ``BaseDuel``); group games write their per-player rows
        at resolution and leave this a no-op."""

    async def _record_game_history(self, game_id: int, game: Any, state: str) -> None:
        """Put a settled game on the games record (``games_game_history``).

        These games keep their own tables and never had a
        ``games_active_games`` row for ``end_game`` to archive, so 135 prod
        games were paid by the economy yet invisible to Play Statistics,
        ``/recap`` and the game-night session (duels-party-122). Host is the
        challenger for a duel and the lobby host for a group game;
        ``player_count`` is everyone who played, eliminated players included.
        The id is namespaced by game type because six tables share small
        integer ids. Idempotent — this hook can fire more than once per game.
        Never raises: the faucet behind it must still pay.
        """
        try:
            participants = self._game_participants(game)
            host_id = getattr(game, "host_id", None) or getattr(game, "challenger_id", None)
            sql, params = history_insert(
                game_id=f"{self.GAME_KEY}:{game_id}",
                game_type=self.GAME_KEY,
                channel_id=int(game.channel_id),
                host_id=int(host_id or 0),
                player_count=len(participants),
                round_count=1,
                payload={
                    "players": participants,
                    "winner_id": getattr(game, "winner_id", None),
                    "loser_id": getattr(game, "loser_id", None),
                    "state": state,
                },
                started_at=float(getattr(game, "created_at", None) or time.time()),
                guild_id=int(game.guild_id),
            )
            await self.db.execute(sql, params)
        except Exception:
            log.exception(
                "%s: failed to record game %s to history", self.GAME_DISPLAY_NAME, game_id
            )

    async def _resolve_wagers(
        self,
        game_id: int,
        state: str,
        winner_id: int | None,
        guild_id: int | None = None,
    ) -> None:
        """Settle or refund this game's escrow, then announce the outcome."""
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return

        def _work() -> tuple[int, int, dict[int, int], int]:
            with ctx.open_db() as conn:
                if wager_svc.pot_total(conn, self.GAME_KEY, game_id) == 0:
                    wager_svc.drop_pending(conn, self.GAME_KEY, game_id)
                    return 0, 0, {}, 0
                if state in _SETTLING_STATES and winner_id is not None:
                    paid, rake = wager_svc.settle(
                        conn, self.GAME_KEY, game_id, winner_id
                    )
                    return paid, rake, {}, winner_id or 0
                refunds = wager_svc.refund_game(conn, self.GAME_KEY, game_id)
                return 0, 0, refunds, 0

        paid, rake, refunds, paid_to = await asyncio.to_thread(_work)
        settings = await self._econ_settings(guild_id) if guild_id else None

        def _coins(n: int) -> str:
            return _fmt_coins(settings, n) if settings else f"**{n:,}**"

        if paid > 0:
            # The house cut is named right where the pot is paid — a raked
            # wager must never look like the full pot arrived.
            note = f" *(house kept {rake:,})*" if rake else ""
            await self._announce_wager_result(
                game_id, f"💰 <@{paid_to}> takes the pot — {_coins(paid)}!{note}"
            )
        elif refunds:
            await self._announce_wager_result(
                game_id,
                f"↩️ Stakes refunded to {len(refunds)} player(s) — "
                f"{_coins(sum(refunds.values()))} returned.",
            )

    async def _wager_precheck(
        self, guild_id: int, user_id: int, wager: int
    ) -> str | None:
        """Validate a requested wager before a game row exists.

        Returns member-facing text on refusal. Amounts are deliberately
        uncapped (2026-07-20 decision) — the only gates are a positive
        whole number, the economy being on, and the member actually
        holding it.
        """
        if wager < 1:
            return "A wager has to be at least 1."
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return "Wagers aren't available right now."

        def _check() -> str | None:
            with ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    return "The economy isn't enabled here, so wagers can't run."
                have = get_balance(conn, guild_id, user_id)
                if have < wager:
                    return (
                        f"You need {_fmt_coins(settings, wager)} to "
                        f"stake that — you have {_fmt_coins(settings, have)}."
                    )
            return None

        return await asyncio.to_thread(_check)

    async def _declare_wager(
        self, guild_id: int, game_id: int, user_id: int, wager: int
    ) -> None:
        """Record a challenger's intended ante — no money moves yet."""
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return

        def _work() -> None:
            with ctx.open_db() as conn:
                wager_svc.declare_stake(
                    conn, guild_id, self.GAME_KEY, game_id, user_id, wager
                )

        await asyncio.to_thread(_work)

    async def _game_ante(self, game_id: int) -> int:
        """This game's per-player ante (0 = not a wagered game)."""
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return 0

        def _read() -> int:
            with ctx.open_db() as conn:
                return wager_svc.game_ante(conn, self.GAME_KEY, game_id)

        return await asyncio.to_thread(_read)

    async def _take_stake(
        self, guild_id: int, game_id: int, user_id: int, amount: int
    ) -> str | None:
        """Escrow one player's ante. Returns a member-facing error, or None.

        The caller MUST refuse the join/start when this returns a message —
        the whole point of the escrow debit is that it blocks game entry
        (unlike the faucet, which never blocks game flow).
        """
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None or amount < 1:
            return None

        def _work() -> str | None:
            with ctx.open_db() as conn:
                settings = load_econ_settings(conn, guild_id)
                if not settings.enabled:
                    return "The economy isn't enabled here, so wagers can't run."
                try:
                    wager_svc.hold_stake(
                        conn, guild_id, self.GAME_KEY, game_id, user_id, amount,
                        currency_plural=settings.currency_plural,
                    )
                except ValueError as exc:
                    return str(exc)
            return None

        return await asyncio.to_thread(_work)

    async def _return_stake(self, game_id: int, user_id: int) -> int:
        """Refund one player's escrow (they left a lobby, or left the guild)."""
        ctx = getattr(self.bot, "ctx", None)
        if ctx is None:
            return 0

        def _work() -> int:
            with ctx.open_db() as conn:
                return wager_svc.refund_player(
                    conn, self.GAME_KEY, game_id, user_id
                )

        return await asyncio.to_thread(_work)

    async def _announce_to_channel(self, game_id: int, text: str) -> None:
        """Post a line to the game's own channel (best-effort, never raises).

        Shared by the pot outcome and the per-elimination call-outs: both are
        commentary the whole room needs to see, and neither is worth failing a
        game over if the bot has lost Send Messages in the meantime.
        """
        try:
            game = await self._db_get_game(game_id)
            if game is None or not getattr(game, "channel_id", None):
                return
            channel = self.bot.get_channel(int(game.channel_id))
            if not isinstance(channel, discord.abc.Messageable):
                return
            await channel.send(text)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    async def _announce_wager_result(self, game_id: int, text: str) -> None:
        """Post the pot outcome to the game's channel (best-effort)."""
        await self._announce_to_channel(game_id, text)

    def _game_participants(self, game: Any) -> list[int]:
        """Everyone who played: the roster for group games (bailed/eliminated
        players included), the challenger/target pair for duels."""
        roster = getattr(game, "roster", None)
        if roster:
            return [int(u) for u in roster]
        return [
            int(uid)
            for uid in (
                getattr(game, "challenger_id", None),
                getattr(game, "target_id", None),
            )
            if uid is not None
        ]

    async def _db_fetch_active_games(self) -> list:
        raise NotImplementedError

    async def _db_fetch_resolved_games(self) -> list:
        raise NotImplementedError

    async def _db_fetch_sweepable(self, now: float) -> list:
        raise NotImplementedError

    # ── Lobby hooks (N-player games implement; duels leave as defaults) ───────

    async def _db_create_lobby(
        self, guild_id: int, channel_id: int, host_id: int, stakes_text: str | None,
        nick_stake: bool = False,
    ) -> int:
        """Create a LOBBY-state game with roster=[host_id]. Returns its id."""
        raise NotImplementedError

    async def _db_fetch_lobby_games(self) -> list:
        """Return open LOBBY games to re-attach views on cog_load. Duels: none."""
        return []

    async def _db_fetch_pending_games(self) -> list:
        """Return PENDING challenges to re-attach views on cog_load. Group games: none."""
        return []

    async def get_lobby_params(self, guild_id: int) -> tuple[int, int]:
        """Return (min_players, max_players) for this guild."""
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
