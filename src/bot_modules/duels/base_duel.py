"""BaseDuel — fixed 2-player special case of BaseGame.

Adds the single-opponent challenge/accept/decline flow and the pairwise winner
resolution (winner = the player who isn't the loser). All shared machinery —
lifecycle, the expiry/auto-revert sweep, the nickname-stake flow, rate limiting, and
the abstract hooks — lives in `BaseGame`.

Two stake modes are supported on the duel path:
  * **Nickname mode** (no custom stakes): the winner renames the loser for 24h.
  * **Custom stakes** (free-text stakes given): the loser owes the agreed-upon
    stakes; the bot enforces nothing and never renames anyone.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.embeds import COLOR_GOLD, COLOR_YELLOW

from . import db as duels_db
from .base_game import _RATE_LIMIT_MAX, BaseGame
from .filters import validate_stakes
from .views import ChallengeView, ResultView


class BaseDuel(BaseGame):
    """Abstract base for 2-player nickname-duel games.

    Subclasses must define GAME_KEY / GAME_DISPLAY_NAME and implement the abstract
    hooks declared on BaseGame.
    """

    # ── Shared challenge entrypoint ───────────────────────────────────────────

    async def _base_challenge(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        stakes_text: str | None,
        wager: int | None = None,
    ) -> None:
        """Run all pre-game checks and create a challenge embed. Called by subclass command.

        ``wager`` makes it a coin duel: the amount is *declared* now but no
        money moves until the target accepts, so a decline or a timeout costs
        nothing. Both antes are taken at accept, and the winner takes the pot.
        """
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

        # Nickname-mode preflight only applies when no custom stakes are set;
        # custom-stakes games never rename anyone, so they don't need the
        # Manage Nicknames permission or a clear nickname slate.
        if stakes_text is None:
            perm_error = await self._check_bot_can_nick(guild, [challenger, target])  # type: ignore[list-item]
            if perm_error:
                await interaction.response.send_message(perm_error, ephemeral=True)
                return

            nick_error = await self._check_no_active_nick(guild, [challenger, target])  # type: ignore[list-item]
            if nick_error:
                await interaction.response.send_message(nick_error, ephemeral=True)
                return

        existing = await self._db_get_active_game_for_pair(guild.id, challenger.id, target.id)
        if existing:
            await interaction.response.send_message(
                "You two already have a game in progress.", ephemeral=True
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

        if wager is not None:
            err = await self._wager_precheck(guild.id, challenger.id, wager)
            if err:
                await interaction.response.send_message(err, ephemeral=True)
                return

        game_id = await self._db_create_game(
            guild_id=guild.id,
            channel_id=interaction.channel_id,  # type: ignore[arg-type]
            challenger_id=challenger.id,
            target_id=target.id,
            stakes_text=stakes_text,
        )
        self._record_challenge(challenger.id)

        if wager is not None:
            await self._declare_wager(guild.id, game_id, challenger.id, wager)

        accent = await resolve_accent_color(self.bot.ctx.db_path, guild)
        embed = self._build_challenge_embed(
            challenger, target, stakes_text, accent, wager=wager,  # type: ignore[arg-type]
        )
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
        color: "discord.Color | None" = None,
        *,
        wager: int | None = None,
    ) -> discord.Embed:
        if color is None:
            color = discord.Color(COLOR_GOLD)
        stakes_text = stakes or "Loser surrenders their nickname for 24 hours."
        embed = discord.Embed(
            title=f"⚔️ {self.GAME_DISPLAY_NAME.upper()} CHALLENGE",
            color=color,
        )
        embed.add_field(
            name="Challenge",
            value=f"{challenger.mention} has challenged {target.mention}!",
            inline=False,
        )
        embed.add_field(name="📋 Stakes", value=stakes_text, inline=False)
        if wager:
            embed.add_field(
                name="💰 Wager",
                value=(
                    f"**{wager:,}** each — winner takes **{wager * 2:,}**.\n"
                    "_Nothing is charged unless the challenge is accepted._"
                ),
                inline=False,
            )
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

        ante = await self._game_ante(game_id)
        if ante > 0:
            # Both antes land at accept — no money moves while a challenge is
            # merely pending, so a decline or a timeout needs no refund. If
            # either side can't cover it now, the challenge is called off
            # rather than started half-funded.
            for uid, who in (
                (game.target_id, "you"),
                (game.challenger_id, "the challenger"),
            ):
                err = await self._take_stake(game.guild_id, game_id, uid, ante)
                if err is None:
                    continue
                await self._db_set_state(game_id, "DECLINED")  # refunds + drops
                note = err if who == "you" else (
                    f"The challenger can no longer cover the {ante:,} wager — "
                    "challenge called off."
                )
                await interaction.response.edit_message(
                    embed=discord.Embed(
                        title="❌ Challenge Called Off",
                        description=note,
                        color=COLOR_YELLOW,
                    ),
                    view=None,
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
        """Entry point for all in-game button presses. Subclass build_game_view passes this."""
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
        """Resolve from a button interaction — posts via the interaction followup."""
        await self._finalize_result(
            game, winner_id, loser_id, send=interaction.followup.send
        )
        await self.on_game_resolved(game.id)
        self._game_locks.pop(game.id, None)

    async def _finalize_result(
        self,
        game: Any,
        winner_id: int,
        loser_id: int,
        *,
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        """Render + post the result message and set the terminal state.

        ``send`` decouples the transport: an interaction followup when a player's
        click resolves the game, or ``channel.send`` when a timeout resolves it
        with no interaction in hand. Caller handles timer cancellation / lock
        cleanup (it differs between the interaction and timer paths).
        """
        guild = self.bot.get_guild(game.guild_id)

        # Two modes: nickname (no custom stakes → winner renames the loser) and
        # custom stakes (loser owes the agreed-upon stakes, no bot enforcement).
        nick_mode = game.stakes_text is None

        result_embed = self.render_result_state(game, guild)  # type: ignore[arg-type]

        winner_m = guild.get_member(winner_id) if guild else None
        loser_m = guild.get_member(loser_id) if guild else None
        ping_content = " ".join(m.mention for m in (winner_m, loser_m) if m)

        # winner/loser ride along with the terminal write: the economy hook
        # re-reads the row, and not every cog persists them before this point.
        if nick_mode:
            result_view = ResultView(game.id, winner_id, loser_id, self._handle_set_nick)
            result_msg = await send(
                content=ping_content, embed=result_embed, view=result_view
            )
            self.bot.add_view(result_view, message_id=result_msg.id)
            await self._db_set_state(
                game.id, "RESOLVED",
                result_message_id=result_msg.id,
                winner_id=winner_id,
                loser_id=loser_id,
            )
        else:
            # Custom stakes: announce only — no rename button, no expiry sweep.
            result_msg = await send(content=ping_content, embed=result_embed)
            await self._db_set_state(
                game.id, "RESOLVED_NO_NICK",
                result_message_id=result_msg.id,
                winner_id=winner_id,
                loser_id=loser_id,
            )
