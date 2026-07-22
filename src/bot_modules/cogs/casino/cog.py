"""The Golden Meadow casino cog — Discord glue around casino_service.

Thin by design: money and exactly-once settlement live in
``services/casino_service``, paytables in ``services/casino_logic``, embeds
in ``embeds.py``. This file owns the hub-panel upkeep, the per-hand and
per-round asyncio locks, the roulette close timers (re-armed on boot, the
Risky Rolls pattern) and the boot sweep that refunds blackjack hands a
restart orphaned.

No slash commands at all — the whole casino is the persistent hub panel the
bot maintains in the configured channel, plus the buttons each game posts.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from dataclasses import replace
from typing import NamedTuple

import discord

from discord.ext import commands, tasks

from bot_modules.cogs.casino import embeds as casino_embeds
from bot_modules.cogs.casino.views import (
    BlackjackActionButton,
    CasinoHubView,
    RouletteBetButton,
    build_blackjack_view,
    build_roulette_view,
    safe_ephemeral,
)
from bot_modules.core.app_context import Bot
from bot_modules.core.branding import resolve_accent_color
from bot_modules.services import casino_logic as logic
from bot_modules.services import casino_service as svc
from bot_modules.services.economy_service import EconSettings, load_econ_settings

log = logging.getLogger("dungeonkeeper.casino")


class _HandOutcome(NamedTuple):
    """A deal or button press's result — err set means nothing happened."""

    err: str | None = None
    econ: EconSettings | None = None
    hand_id: int = 0
    player: list[str] | None = None
    dealer: list[str] | None = None
    stake: int = 0
    doubled: bool = False
    outcome: str | None = None
    payout: int = 0


class _RoundOpen(NamedTuple):
    err: str | None = None
    running_at: float | None = None  # a round is already open, closing then
    econ: EconSettings | None = None
    round_id: int = 0
    closes_at: float = 0.0


class _RoundBet(NamedTuple):
    err: str | None = None
    econ: EconSettings | None = None
    rnd: sqlite3.Row | None = None
    bets: list[tuple[int, str, int]] | None = None


class CasinoCog(commands.Cog, name="CasinoCog"):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx
        self._bj_locks: dict[int, asyncio.Lock] = {}
        self._roulette_timers: dict[int, asyncio.Task] = {}
        # Debounced round-embed repaints (one per open round) and panel
        # resticks (one per guild) — burst coalescers, not state.
        self._repaint_tasks: dict[int, asyncio.Task] = {}
        self._restick_tasks: dict[int, asyncio.Task] = {}
        # guild_id → configured casino channel, kept warm by ensure_panel so
        # the on_message restick gate never touches the DB.
        self._casino_channels: dict[int, int] = {}
        # guild_id → last jackpot value rendered on the hub panel; the
        # maintenance loop repaints when the real pot drifts from this.
        self._last_pot: dict[int, int] = {}
        self._boot_task: asyncio.Task | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self.bot.add_view(CasinoHubView())
        self.bot.add_dynamic_items(BlackjackActionButton, RouletteBetButton)
        self._boot_task = asyncio.create_task(self._boot())
        self.maintenance.start()

    async def cog_unload(self) -> None:
        self.maintenance.cancel()
        if self._boot_task is not None:
            self._boot_task.cancel()
        for task_map in (
            self._roulette_timers, self._repaint_tasks, self._restick_tasks
        ):
            for task in task_map.values():
                task.cancel()
            task_map.clear()

    async def _boot(self) -> None:
        """Post-restart recovery: refund orphaned hands, re-arm round timers,
        make sure every configured guild has its hub panel."""
        await self.bot.wait_until_ready()

        def _sweep() -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
            with self.ctx.open_db() as conn:
                return (
                    svc.refund_live_blackjack_hands(conn),
                    svc.open_roulette_rounds(conn),
                )

        try:
            swept, rounds = await asyncio.to_thread(_sweep)
        except Exception:
            log.exception("casino boot sweep failed")
            return
        for row in swept:
            await self._note_refunded_hand(row)
        for rnd in rounds:
            if self.bot.get_guild(int(rnd["guild_id"])) is None:
                await self._void_round(int(rnd["id"]))
            else:
                self._arm_roulette_timer(int(rnd["id"]), float(rnd["closes_at"]))
        for guild in self.bot.guilds:
            await self.ensure_panel(guild)

    async def _note_refunded_hand(self, row: sqlite3.Row) -> None:
        channel = self.bot.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or not int(row["message_id"]):
            return
        try:
            await channel.get_partial_message(int(row["message_id"])).edit(
                content=(
                    "↩️ The casino restarted mid-hand — this bet went back "
                    "to its owner."
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

    @tasks.loop(seconds=60)
    async def maintenance(self) -> None:
        """Auto-stand idle blackjack hands AND resolve overdue roulette rounds.

        The roulette leg is the self-healing backstop the timer tasks need:
        a resolution that failed transiently, or a round whose send crashed
        before its timer armed, would otherwise hold members' stakes and
        block the channel until the next restart. The exactly-once
        ``status='open'`` claim makes replaying resolution here free.
        """

        def _scan() -> tuple[list[int], list[int], dict[int, int]]:
            with self.ctx.open_db() as conn:
                stale: list[int] = []
                thresholds: dict[int, int] = {}
                now = time.time()
                for row in svc.idle_live_blackjack_hands(conn, now):
                    gid = int(row["guild_id"])
                    if gid not in thresholds:
                        thresholds[gid] = svc.load_casino_settings(
                            conn, gid
                        ).blackjack_idle_seconds
                    if now - float(row["last_action_at"]) >= thresholds[gid]:
                        stale.append(int(row["id"]))
                overdue = [
                    int(r["id"])
                    for r in svc.open_roulette_rounds(conn)
                    if float(r["closes_at"]) <= now - 5  # grace for a live timer
                ]
                # Pots for guilds whose panel we're maintaining — repainted
                # below when the value drifted from the rendered one. Same
                # seed semantics as ensure_panel's read, or an unfed pot
                # (no row yet) would look like perpetual drift.
                pots: dict[int, int] = {}
                for gid, channel_id in self._casino_channels.items():
                    if not channel_id:
                        continue
                    cs = svc.load_casino_settings(conn, gid)
                    if cs.jackpot_enabled and cs.slots_enabled:
                        pots[gid] = svc.get_jackpot(conn, gid, seed=cs.jackpot_seed)
                return stale, overdue, pots

        try:
            stale, overdue, pots = await asyncio.to_thread(_scan)
        except Exception:
            log.exception("casino maintenance sweep failed")
            return
        for gid, pot in pots.items():
            if self._last_pot.get(gid) == pot:
                continue
            guild = self.bot.get_guild(gid)
            if guild is not None:
                await self.ensure_panel(guild)  # re-reads + records the pot
        for hand_id in stale:
            try:
                await self._auto_stand(hand_id)
            except Exception:
                log.exception("casino auto-stand failed for hand %s", hand_id)
        for round_id in overdue:
            if round_id in self._roulette_timers:
                continue  # a healthy timer owns it
            try:
                await self._resolve_roulette(round_id)
            except Exception:
                log.exception("casino overdue-round resolve failed for %s", round_id)

    @maintenance.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_casino_config_change(self, guild_id: int) -> None:
        """Dashboard saves (casino page AND the economy page's enable
        toggle) dispatch this so the hub panel appears/moves/updates/tears
        down without waiting for a restart."""
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self.ensure_panel(guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Refund a leaver's live casino stakes — the wager-escrow seam's
        rule extended to the casino, so nothing settles into a ghost
        wallet after the member is gone."""

        def _refund() -> dict[str, int]:
            with self.ctx.open_db() as conn:
                return svc.refund_member_live_stakes(
                    conn, member.guild.id, member.id
                )

        try:
            await asyncio.to_thread(_refund)
        except Exception:
            log.exception("casino leaver refund failed for %s", member.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Keep the hub panel — the casino's only entry point — at the
        bottom of its channel (the economy sticky-panel pattern): any
        traffic in the casino channel debounces a restick check."""
        guild = message.guild
        if guild is None:
            return
        if message.channel.id != self._casino_channels.get(guild.id):
            return
        if guild.id in self._restick_tasks:
            return
        self._restick_tasks[guild.id] = asyncio.create_task(
            self._restick_later(guild.id)
        )

    async def _restick_later(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(20)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return

            def _read() -> svc.CasinoSettings:
                with self.ctx.open_db() as conn:
                    return svc.load_casino_settings(conn, guild_id)

            settings = await asyncio.to_thread(_read)
            channel = guild.get_channel(settings.channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            if channel.last_message_id == settings.panel_message_id:
                return  # nothing has buried it
            await self.ensure_panel(guild, force_repost=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("casino panel restick failed for guild %s", guild_id)
        finally:
            self._restick_tasks.pop(guild_id, None)

    # ── shared helpers ─────────────────────────────────────────────────

    async def _accent(self, guild: discord.Guild | None) -> discord.Color | None:
        """Resolve the guild accent per render; never let it break a game.

        No cog-side cache: branding.py already caches by avatar hash and is
        invalidated on dashboard branding saves, so this stays cheap AND
        picks up accent changes without a restart.
        """
        if guild is None:
            return None
        try:
            return await resolve_accent_color(self.ctx.db_path, guild)
        except Exception:
            log.debug("casino accent resolve failed", exc_info=True)
            return None

    # ── hub panel upkeep ───────────────────────────────────────────────

    async def ensure_panel(
        self, guild: discord.Guild, *, force_repost: bool = False
    ) -> None:
        """Post/refresh the hub panel in the configured casino channel.

        Also the teardown path: an unset channel (or a disabled economy)
        deletes any panel we previously posted. Called at boot, by config
        changes (dashboard dispatch), and by the restick debounce with
        ``force_repost=True`` — delete-and-repost so the panel returns to
        the bottom of the channel instead of being edited in place.
        """

        def _read() -> tuple[EconSettings, svc.CasinoSettings, int | None]:
            with self.ctx.open_db() as conn:
                settings = svc.load_casino_settings(conn, guild.id)
                pot: int | None = None
                if settings.jackpot_enabled and settings.slots_enabled:
                    pot = svc.get_jackpot(
                        conn, guild.id, seed=settings.jackpot_seed
                    )
                return load_econ_settings(conn, guild.id), settings, pot

        try:
            econ, settings, pot = await asyncio.to_thread(_read)
        except Exception:
            log.exception("casino panel read failed for guild %s", guild.id)
            return
        if pot is not None:
            self._last_pot[guild.id] = pot

        async def _delete_stale() -> None:
            channel = self.bot.get_channel(settings.panel_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.get_partial_message(
                        settings.panel_message_id
                    ).delete()
                except discord.HTTPException:
                    pass

        def _save_ids(message_id: int, channel_id: int) -> None:
            with self.ctx.open_db() as conn:
                svc.save_casino_settings(
                    conn, guild.id,
                    {"panel_message_id": message_id, "panel_channel_id": channel_id},
                )

        self._casino_channels[guild.id] = settings.channel_id
        if not settings.channel_id or not econ.enabled:
            if settings.panel_message_id:
                await _delete_stale()
                await asyncio.to_thread(_save_ids, 0, 0)
            return

        channel = guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        moved = settings.panel_channel_id != channel.id
        if settings.panel_message_id and (moved or force_repost):
            await _delete_stale()  # moved or buried; drop the old panel
            settings = replace(settings, panel_message_id=0)
        embed = casino_embeds.build_hub_embed(
            econ, settings, await self._accent(guild), jackpot=pot
        )
        if settings.panel_message_id:
            try:
                await channel.get_partial_message(settings.panel_message_id).edit(
                    embed=embed, view=CasinoHubView()
                )
                return
            except discord.NotFound:
                pass  # deleted by hand — repost below
            except discord.HTTPException:
                return
        try:
            message = await channel.send(
                embed=embed,
                view=CasinoHubView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("casino panel post failed in #%s", channel.id)
            return
        await asyncio.to_thread(_save_ids, message.id, channel.id)

    # ── instant games ──────────────────────────────────────────────────

    async def play_coinflip(
        self, interaction: discord.Interaction, side: str, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _play() -> tuple[
            str | None, EconSettings | None, str, svc.InstantResult
        ]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "coinflip",
                    channel_id=interaction.channel_id,
                )
                if err is not None:
                    return err, None, "", svc.InstantResult(0)
                landed = logic.flip_coin()
                result = svc.settle_coinflip(
                    conn, guild.id, interaction.user.id, amount, side, landed
                )
                return None, load_econ_settings(conn, guild.id), landed, result

        err, econ, landed, result = await asyncio.to_thread(_play)
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        await interaction.response.send_message(
            embed=casino_embeds.build_coinflip_embed(
                econ, interaction.user.id, side, landed, amount, result.payout
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def play_slots(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _play() -> tuple[
            str | None, EconSettings | None, tuple[str, str, str],
            svc.InstantResult,
        ]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "slots",
                    channel_id=interaction.channel_id,
                )
                if err is not None:
                    return err, None, ("", "", ""), svc.InstantResult(0)
                reels = logic.spin_slots()
                result = svc.settle_slots(
                    conn, guild.id, interaction.user.id, amount, reels
                )
                return None, load_econ_settings(conn, guild.id), reels, result

        err, econ, reels, result = await asyncio.to_thread(_play)
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        await interaction.response.send_message(
            embed=casino_embeds.build_slots_embed(
                econ, interaction.user.id, reels, amount, result.payout,
                result.label,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_help(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _read() -> tuple[EconSettings, svc.CasinoSettings]:
            with self.ctx.open_db() as conn:
                return (
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                )

        econ, settings = await asyncio.to_thread(_read)
        await interaction.response.send_message(
            embed=casino_embeds.build_help_embed(
                econ, settings, await self._accent(guild)
            ),
            ephemeral=True,
        )

    # ── blackjack ──────────────────────────────────────────────────────

    async def deal_blackjack(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _deal() -> _HandOutcome:
            with self.ctx.open_db() as conn:
                if svc.live_blackjack_hand(conn, guild.id, uid) is not None:
                    return _HandOutcome(
                        err="You already have a hand at the table — "
                        "finish it first."
                    )
                err = svc.take_stake(
                    conn, guild.id, uid, amount, "blackjack",
                    channel_id=interaction.channel_id,
                )
                if err is not None:
                    return _HandOutcome(err=err)
                deck = logic.new_deck()
                player = [deck.pop(), deck.pop()]
                dealer = [deck.pop(), deck.pop()]
                channel_id = interaction.channel_id or 0
                hand_id = svc.create_blackjack_hand(
                    conn, guild.id, channel_id, uid, amount,
                    svc.serialize_blackjack(deck, player, dealer),
                )
                econ = load_econ_settings(conn, guild.id)
                outcome: str | None = None
                payout = 0
                if logic.is_natural(player) or logic.is_natural(dealer):
                    payout, outcome = logic.blackjack_settle(player, dealer, amount)
                    svc.settle_blackjack_hand(conn, hand_id, payout, outcome)
                return _HandOutcome(
                    econ=econ, hand_id=hand_id, player=player, dealer=dealer,
                    stake=amount, outcome=outcome, payout=payout,
                )

        try:
            result = await asyncio.to_thread(_deal)
        except sqlite3.IntegrityError:
            await safe_ephemeral(
                interaction,
                "❌ You already have a hand at the table — finish it first.",
            )
            return
        if result.err is not None or result.econ is None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return
        embed = casino_embeds.build_blackjack_embed(
            result.econ, uid, result.player or [], result.dealer or [], amount,
            await self._accent(guild), outcome=result.outcome,
            payout=result.payout,
        )
        view = (
            discord.utils.MISSING
            if result.outcome is not None
            else build_blackjack_view(result.hand_id, can_double=True)
        )
        await interaction.response.send_message(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if result.outcome is None:
            message = await interaction.original_response()
            hand_id = result.hand_id

            def _bind() -> None:
                with self.ctx.open_db() as conn:
                    svc.set_blackjack_message(conn, hand_id, message.id)

            await asyncio.to_thread(_bind)

    async def blackjack_action(
        self, interaction: discord.Interaction, hand_id: int, action: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        lock = self._bj_locks.setdefault(hand_id, asyncio.Lock())
        async with lock:
            done = await self._blackjack_step(interaction, hand_id, action)
        if done:
            self._bj_locks.pop(hand_id, None)

    async def _blackjack_step(
        self, interaction: discord.Interaction, hand_id: int, action: str
    ) -> bool:
        """One button press, rules in the service; True once the hand is
        terminally gone (settled now, or found already settled) so the
        caller drops its lock — clicks on stale buttons must not grow the
        lock dict forever."""
        guild = interaction.guild
        assert guild is not None
        uid = interaction.user.id

        def _step() -> tuple[svc.BlackjackStep, EconSettings | None]:
            with self.ctx.open_db() as conn:
                step = svc.resolve_blackjack_action(
                    conn, guild.id, hand_id, uid, action
                )
                econ = (
                    load_econ_settings(conn, guild.id)
                    if step.err is None
                    else None
                )
                return step, econ

        step, econ = await asyncio.to_thread(_step)
        if step.err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {step.err}")
            return step.err == "That hand is already finished."
        embed = casino_embeds.build_blackjack_embed(
            econ, uid, step.player or [], step.dealer or [],
            step.stake, await self._accent(guild),
            doubled=step.doubled, outcome=step.outcome, payout=step.payout,
        )
        view = (
            None if step.outcome is not None
            else build_blackjack_view(hand_id, can_double=False)
        )
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException:
            pass
        return step.outcome is not None

    async def _auto_stand(self, hand_id: int) -> None:
        """The idle sweep's stand — same settle path, message edit best-effort.

        ``stand_idle_blackjack_hand`` returns None when the hand was
        settled concurrently (a button press holding the claim), so this
        can never render an outcome the settle didn't pay.
        """
        lock = self._bj_locks.setdefault(hand_id, asyncio.Lock())
        async with lock:

            def _stand():
                with self.ctx.open_db() as conn:
                    row = svc.get_blackjack_hand(conn, hand_id)
                    if row is None:
                        return None
                    step = svc.stand_idle_blackjack_hand(conn, hand_id)
                    if step is None:
                        return None
                    econ = load_econ_settings(conn, int(row["guild_id"]))
                    return row, step, econ

            result = await asyncio.to_thread(_stand)
        self._bj_locks.pop(hand_id, None)
        if result is None:
            return
        row, step, econ = result
        channel = self.bot.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or not int(row["message_id"]):
            return
        embed = casino_embeds.build_blackjack_embed(
            econ, int(row["user_id"]), step.player or [], step.dealer or [],
            step.stake, await self._accent(channel.guild),
            doubled=step.doubled, outcome=step.outcome, payout=step.payout,
        )
        embed.set_footer(text="Stood automatically — the dealer waits for no one.")
        try:
            await channel.get_partial_message(int(row["message_id"])).edit(
                embed=embed, view=None
            )
        except discord.HTTPException:
            pass

    # ── roulette ───────────────────────────────────────────────────────

    async def open_roulette(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        channel_id = interaction.channel_id
        if guild is None or channel_id is None:
            return

        def _open() -> _RoundOpen:
            with self.ctx.open_db() as conn:
                econ = load_econ_settings(conn, guild.id)
                settings = svc.load_casino_settings(conn, guild.id)
                if not econ.enabled or not settings.channel_id:
                    return _RoundOpen(err="The casino is closed.")
                if channel_id != settings.channel_id:
                    return _RoundOpen(
                        err="The casino has moved — find it in "
                        f"<#{settings.channel_id}>."
                    )
                if not settings.roulette_enabled:
                    return _RoundOpen(err="That table is closed right now.")
                existing = svc.live_roulette_round(conn, channel_id)
                if existing is not None:
                    return _RoundOpen(running_at=float(existing["closes_at"]))
                round_id = svc.open_roulette_round(
                    conn, guild.id, channel_id, settings.roulette_window_seconds
                )
                if round_id is None:
                    return _RoundOpen(running_at=time.time())
                rnd = svc.get_roulette_round(conn, round_id)
                assert rnd is not None
                return _RoundOpen(
                    econ=econ, round_id=round_id,
                    closes_at=float(rnd["closes_at"]),
                )

        try:
            result = await asyncio.to_thread(_open)
        except sqlite3.IntegrityError:
            # Two simultaneous presses both passed the pre-check; the
            # partial unique index caught the second — same polite note as
            # the ordinary already-running path.
            await safe_ephemeral(
                interaction, casino_embeds.build_round_running_note(time.time())
            )
            return
        if result.err is not None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return
        if result.running_at is not None or result.econ is None:
            await safe_ephemeral(
                interaction,
                casino_embeds.build_round_running_note(result.running_at or 0.0),
            )
            return
        round_id = result.round_id
        # Arm BEFORE the send: if the send fails the timer still resolves
        # (refunds) the round instead of stranding it headless until boot.
        self._arm_roulette_timer(round_id, result.closes_at)
        embed = casino_embeds.build_roulette_round_embed(
            result.econ, result.closes_at, [], await self._accent(guild)
        )
        try:
            await interaction.response.send_message(
                embed=embed,
                view=build_roulette_view(round_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            message = await interaction.original_response()
        except discord.HTTPException:
            # No message means nobody can bet — kill the round now rather
            # than leave a headless betting window blocking the channel.
            timer = self._roulette_timers.pop(round_id, None)
            if timer is not None:
                timer.cancel()
            await self._void_round(round_id)
            await safe_ephemeral(
                interaction, "❌ Couldn't open the round — try again."
            )
            return

        def _bind() -> None:
            with self.ctx.open_db() as conn:
                svc.set_roulette_message(conn, round_id, message.id)

        await asyncio.to_thread(_bind)

    async def place_roulette_bet(
        self,
        interaction: discord.Interaction,
        round_id: int,
        bet_type: str,
        selection: int,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _bet() -> str | None:
            with self.ctx.open_db() as conn:
                return svc.place_roulette_bet(
                    conn, round_id, uid, bet_type, selection, amount
                )

        err = await asyncio.to_thread(_bet)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        desc = logic.describe_bet(bet_type, selection)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        # Repaint is debounced per round — a burst of bets coalesces into
        # one message edit (the live_signal idea) instead of one Discord
        # edit per bettor.
        self._schedule_round_repaint(guild, round_id)

    def _schedule_round_repaint(self, guild: discord.Guild, round_id: int) -> None:
        if round_id in self._repaint_tasks:
            return
        self._repaint_tasks[round_id] = asyncio.create_task(
            self._repaint_round(guild, round_id)
        )

    async def _repaint_round(self, guild: discord.Guild, round_id: int) -> None:
        try:
            await asyncio.sleep(2.0)

            def _read() -> _RoundBet:
                with self.ctx.open_db() as conn:
                    rnd = svc.get_roulette_round(conn, round_id)
                    if rnd is None or str(rnd["status"]) != "open":
                        return _RoundBet()
                    bets = [
                        (
                            int(b["user_id"]),
                            logic.describe_bet(
                                str(b["bet_type"]), int(b["selection"])
                            ),
                            int(b["amount"]),
                        )
                        for b in svc.roulette_bets(conn, round_id)
                    ]
                    return _RoundBet(
                        econ=load_econ_settings(conn, guild.id), rnd=rnd, bets=bets
                    )

            result = await asyncio.to_thread(_read)
            rnd = result.rnd
            if result.econ is None or rnd is None:
                return  # settled while we slept — the result edit owns it now
            channel = self.bot.get_channel(int(rnd["channel_id"]))
            if isinstance(channel, discord.TextChannel) and int(rnd["message_id"]):
                embed = casino_embeds.build_roulette_round_embed(
                    result.econ, float(rnd["closes_at"]), result.bets or [],
                    await self._accent(guild),
                )
                try:
                    await channel.get_partial_message(int(rnd["message_id"])).edit(
                        embed=embed
                    )
                except discord.HTTPException:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("roulette repaint failed for round %s", round_id)
        finally:
            self._repaint_tasks.pop(round_id, None)

    def _arm_roulette_timer(self, round_id: int, closes_at: float) -> None:
        if round_id in self._roulette_timers:
            return
        delay = max(0.0, closes_at - time.time())
        self._roulette_timers[round_id] = asyncio.create_task(
            self._roulette_timer(round_id, delay)
        )

    async def _roulette_timer(self, round_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._resolve_roulette(round_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("roulette resolution failed for round %s", round_id)
        finally:
            self._roulette_timers.pop(round_id, None)

    async def _resolve_roulette(self, round_id: int) -> None:
        # No lock needed: the status='open' claim inside settle_roulette_round
        # is the mutual exclusion (timer, maintenance sweep and void can all
        # reach a round; only the first claim pays).
        repaint = self._repaint_tasks.pop(round_id, None)
        if repaint is not None:
            repaint.cancel()  # a stale "bets open" edit must not land post-spin

        def _settle():
            with self.ctx.open_db() as conn:
                rnd = svc.get_roulette_round(conn, round_id)
                if rnd is None or str(rnd["status"]) != "open":
                    return None
                result = logic.spin_roulette()
                bets = svc.settle_roulette_round(conn, round_id, result)
                if bets is None:
                    return None
                econ = load_econ_settings(conn, int(rnd["guild_id"]))
                return rnd, result, bets, econ

        settled = await asyncio.to_thread(_settle)
        if settled is None:
            return
        rnd, result, bet_rows, econ = settled
        bets = [
            (
                int(b["user_id"]),
                logic.describe_bet(str(b["bet_type"]), int(b["selection"])),
                int(b["amount"]),
                int(b["payout"]),
            )
            for b in bet_rows
        ]
        channel = self.bot.get_channel(int(rnd["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        result_embed = casino_embeds.build_roulette_result_embed(econ, result, bets)
        if int(rnd["message_id"]):
            try:
                await channel.get_partial_message(int(rnd["message_id"])).edit(
                    embed=result_embed, view=None
                )
            except discord.HTTPException:
                pass
        if bets:
            try:
                await channel.send(
                    embed=result_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def _void_round(self, round_id: int) -> None:
        def _void() -> None:
            with self.ctx.open_db() as conn:
                svc.void_roulette_round(conn, round_id)

        try:
            await asyncio.to_thread(_void)
        except Exception:
            log.exception("roulette void failed for round %s", round_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
