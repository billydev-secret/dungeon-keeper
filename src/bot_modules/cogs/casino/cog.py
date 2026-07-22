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
        self._accents: dict[int, discord.Color] = {}
        self._bj_locks: dict[int, asyncio.Lock] = {}
        self._rl_locks: dict[int, asyncio.Lock] = {}
        self._roulette_timers: dict[int, asyncio.Task] = {}
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
        for task in self._roulette_timers.values():
            task.cancel()
        self._roulette_timers.clear()

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
        """Auto-stand blackjack hands whose player wandered off."""

        def _idle() -> list[int]:
            with self.ctx.open_db() as conn:
                out: list[int] = []
                thresholds: dict[int, int] = {}
                now = time.time()
                for row in svc.idle_live_blackjack_hands(conn, now):
                    gid = int(row["guild_id"])
                    if gid not in thresholds:
                        thresholds[gid] = svc.load_casino_settings(
                            conn, gid
                        ).blackjack_idle_seconds
                    if now - float(row["last_action_at"]) >= thresholds[gid]:
                        out.append(int(row["id"]))
                return out

        try:
            stale = await asyncio.to_thread(_idle)
        except Exception:
            log.exception("casino idle sweep failed")
            return
        for hand_id in stale:
            try:
                await self._auto_stand(hand_id)
            except Exception:
                log.exception("casino auto-stand failed for hand %s", hand_id)

    @maintenance.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_casino_config_change(self, guild_id: int) -> None:
        """The dashboard's casino PUT dispatches this after a save so the
        hub panel appears/moves/updates without waiting for a restart."""
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self.ensure_panel(guild)

    # ── shared helpers ─────────────────────────────────────────────────

    async def _accent(self, guild: discord.Guild | None) -> discord.Color | None:
        """Resolve + cache the guild accent; never let it break a game."""
        if guild is None:
            return None
        cached = self._accents.get(guild.id)
        if cached is not None:
            return cached
        try:
            accent = await resolve_accent_color(self.ctx.db_path, guild)
        except Exception:
            log.debug("casino accent resolve failed", exc_info=True)
            return None
        self._accents[guild.id] = accent
        return accent

    # ── hub panel upkeep ───────────────────────────────────────────────

    async def ensure_panel(self, guild: discord.Guild) -> None:
        """Post/refresh the hub panel in the configured casino channel.

        Also the teardown path: an unset channel (or a disabled economy)
        deletes any panel we previously posted. Called at boot and by the
        dashboard's casino PUT route after a config change.
        """

        def _read() -> tuple[EconSettings, svc.CasinoSettings]:
            with self.ctx.open_db() as conn:
                return (
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                )

        try:
            econ, settings = await asyncio.to_thread(_read)
        except Exception:
            log.exception("casino panel read failed for guild %s", guild.id)
            return

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

        if not settings.channel_id or not econ.enabled:
            if settings.panel_message_id:
                await _delete_stale()
                await asyncio.to_thread(_save_ids, 0, 0)
            return

        channel = guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if settings.panel_message_id and settings.panel_channel_id != channel.id:
            await _delete_stale()  # the casino moved; drop the old panel
            settings = replace(settings, panel_message_id=0)
        embed = casino_embeds.build_hub_embed(
            econ, settings, await self._accent(guild)
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
            message = await channel.send(embed=embed, view=CasinoHubView())
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

        def _play() -> tuple[str | None, EconSettings | None, str, int]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "coinflip"
                )
                if err is not None:
                    return err, None, "", 0
                landed = logic.flip_coin()
                payout = logic.coinflip_payout(amount) if landed == side else 0
                svc.pay_out(
                    conn, guild.id, interaction.user.id, payout, "coinflip",
                    meta={"call": side, "landed": landed},
                )
                return None, load_econ_settings(conn, guild.id), landed, payout

        err, econ, landed, payout = await asyncio.to_thread(_play)
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        await interaction.response.send_message(
            embed=casino_embeds.build_coinflip_embed(
                econ, interaction.user.id, side, landed, amount, payout
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
            str | None, EconSettings | None, tuple[str, str, str], int, str | None
        ]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "slots"
                )
                if err is not None:
                    return err, None, ("", "", ""), 0, None
                reels = logic.spin_slots()
                payout, label = logic.slots_payout(reels, amount)
                svc.pay_out(
                    conn, guild.id, interaction.user.id, payout, "slots",
                    meta={"reels": "".join(reels)},
                )
                return None, load_econ_settings(conn, guild.id), reels, payout, label

        err, econ, reels, payout, label = await asyncio.to_thread(_play)
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        await interaction.response.send_message(
            embed=casino_embeds.build_slots_embed(
                econ, interaction.user.id, reels, amount, payout, label
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
                err = svc.take_stake(conn, guild.id, uid, amount, "blackjack")
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
            outcome = await self._blackjack_step(interaction, hand_id, action)
        if outcome:
            self._bj_locks.pop(hand_id, None)

    async def _blackjack_step(
        self, interaction: discord.Interaction, hand_id: int, action: str
    ) -> bool:
        """One button press; True once the hand is finished."""
        guild = interaction.guild
        assert guild is not None
        uid = interaction.user.id

        def _step() -> _HandOutcome:
            with self.ctx.open_db() as conn:
                row = svc.get_blackjack_hand(conn, hand_id)
                if row is None or row["settled_at"] is not None:
                    return _HandOutcome(err="That hand is already finished.")
                if int(row["user_id"]) != uid:
                    return _HandOutcome(err="That's not your hand — deal your own!")
                deck, player, dealer = svc.deserialize_blackjack(
                    str(row["state_json"])
                )
                stake = int(row["stake"])
                doubled = bool(row["doubled"])
                econ = load_econ_settings(conn, guild.id)

                def _finish(payout: int, outcome: str) -> _HandOutcome:
                    svc.settle_blackjack_hand(conn, hand_id, payout, outcome)
                    return _HandOutcome(
                        econ=econ, player=player, dealer=dealer, stake=stake,
                        doubled=doubled, outcome=outcome, payout=payout,
                    )

                if action == "double":
                    if len(player) != 2:
                        return _HandOutcome(
                            err="You can only double on your first two cards."
                        )
                    err = svc.double_blackjack_stake(
                        conn, guild.id, hand_id, uid, stake
                    )
                    if err is not None:
                        return _HandOutcome(err=err)
                    stake *= 2
                    doubled = True
                    player.append(deck.pop())
                    if logic.hand_value(player) > 21:
                        return _finish(0, "bust")
                    logic.dealer_play(deck, dealer)
                    return _finish(*logic.blackjack_settle(player, dealer, stake))

                if action == "hit":
                    player.append(deck.pop())
                    value = logic.hand_value(player)
                    if value > 21:
                        return _finish(0, "bust")
                    if value == 21:
                        logic.dealer_play(deck, dealer)
                        return _finish(
                            *logic.blackjack_settle(player, dealer, stake)
                        )
                    svc.update_blackjack_state(
                        conn, hand_id, svc.serialize_blackjack(deck, player, dealer)
                    )
                    return _HandOutcome(
                        econ=econ, player=player, dealer=dealer, stake=stake,
                        doubled=doubled,
                    )

                # stand
                logic.dealer_play(deck, dealer)
                return _finish(*logic.blackjack_settle(player, dealer, stake))

        result = await asyncio.to_thread(_step)
        if result.err is not None or result.econ is None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return False
        embed = casino_embeds.build_blackjack_embed(
            result.econ, uid, result.player or [], result.dealer or [],
            result.stake, await self._accent(guild),
            doubled=result.doubled, outcome=result.outcome, payout=result.payout,
        )
        view = (
            None if result.outcome is not None
            else build_blackjack_view(hand_id, can_double=False)
        )
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException:
            pass
        return result.outcome is not None

    async def _auto_stand(self, hand_id: int) -> None:
        """The idle sweep's stand — same settle path, message edit best-effort."""
        lock = self._bj_locks.setdefault(hand_id, asyncio.Lock())
        async with lock:

            def _stand():
                with self.ctx.open_db() as conn:
                    row = svc.get_blackjack_hand(conn, hand_id)
                    if row is None or row["settled_at"] is not None:
                        return None
                    deck, player, dealer = svc.deserialize_blackjack(
                        str(row["state_json"])
                    )
                    logic.dealer_play(deck, dealer)
                    payout, outcome = logic.blackjack_settle(
                        player, dealer, int(row["stake"])
                    )
                    svc.settle_blackjack_hand(conn, hand_id, payout, outcome)
                    econ = load_econ_settings(conn, int(row["guild_id"]))
                    return row, deck, player, dealer, payout, outcome, econ

            result = await asyncio.to_thread(_stand)
        self._bj_locks.pop(hand_id, None)
        if result is None:
            return
        row, _, player, dealer, payout, outcome, econ = result
        channel = self.bot.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel) or not int(row["message_id"]):
            return
        embed = casino_embeds.build_blackjack_embed(
            econ, int(row["user_id"]), player, dealer, int(row["stake"]),
            self._accents.get(int(row["guild_id"])),
            doubled=bool(row["doubled"]), outcome=outcome, payout=payout,
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

        result = await asyncio.to_thread(_open)
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
        embed = casino_embeds.build_roulette_round_embed(
            result.econ, result.closes_at, [], await self._accent(guild)
        )
        await interaction.response.send_message(
            embed=embed, view=build_roulette_view(round_id)
        )
        message = await interaction.original_response()

        def _bind() -> None:
            with self.ctx.open_db() as conn:
                svc.set_roulette_message(conn, round_id, message.id)

        await asyncio.to_thread(_bind)
        self._arm_roulette_timer(round_id, result.closes_at)

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

        def _bet() -> _RoundBet:
            with self.ctx.open_db() as conn:
                err = svc.place_roulette_bet(
                    conn, round_id, uid, bet_type, selection, amount
                )
                if err is not None:
                    return _RoundBet(err=err)
                rnd = svc.get_roulette_round(conn, round_id)
                assert rnd is not None
                bets = [
                    (
                        int(b["user_id"]),
                        logic.describe_bet(str(b["bet_type"]), int(b["selection"])),
                        int(b["amount"]),
                    )
                    for b in svc.roulette_bets(conn, round_id)
                ]
                return _RoundBet(
                    econ=load_econ_settings(conn, guild.id), rnd=rnd, bets=bets
                )

        result = await asyncio.to_thread(_bet)
        if result.err is not None or result.econ is None or result.rnd is None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return
        desc = logic.describe_bet(bet_type, selection)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        rnd = result.rnd
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
        lock = self._rl_locks.setdefault(round_id, asyncio.Lock())
        async with lock:

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
        self._rl_locks.pop(round_id, None)
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
