"""The casino cog — Discord glue around casino_service.

Thin by design: money and exactly-once settlement live in
``services/casino_service``, paytables in ``services/casino_logic``, embeds
in ``embeds.py``. This file owns the hub-panel upkeep, the per-hand and
per-round asyncio locks, the roulette close timers (re-armed on boot, the
Risky Rolls pattern) and the boot sweep that refunds blackjack hands a
restart orphaned.

No slash commands at all — the whole casino is the persistent hub panel the
bot maintains in the configured channel, plus the buttons each game posts.

Instant games (coinflip/slots/blackjack) render **ephemerally** — each
player gets a private machine that edits itself in place, so play never
scrolls the channel or moves anyone else's buttons. The channel carries
only the shared surfaces: the hub panel (whose floor ticker shows recent
action), communal roulette/derby rounds, and broadcast moments (jackpot
celebrations + wins at or over the configured ``broadcast_min_payout``).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from collections.abc import Callable
from dataclasses import replace
from typing import NamedTuple

import discord

from discord.ext import commands, tasks

from bot_modules.cogs.casino import embeds as casino_embeds
from bot_modules.cogs.casino.views import (
    BaccaratBetButton,
    BaccaratBetModal,
    BaccaratNextView,
    BetModal,
    BlackjackActionButton,
    CasinoHubView,
    DerbyBetButton,
    DerbyBetModal,
    DerbyNextView,
    DiceBetButton,
    DiceBetModal,
    DiceNextView,
    PlayAgainButton,
    RouletteBetButton,
    RouletteBetModal,
    RouletteNextView,
    WarActionButton,
    build_baccarat_view,
    build_blackjack_view,
    build_derby_view,
    build_dice_view,
    build_hub_view,
    build_roulette_view,
    build_war_view,
    play_again_view,
    safe_ephemeral,
)
from bot_modules.core.app_context import Bot
from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.branding_service import resolve_casino_name_conn
from bot_modules.services import casino_logic as logic
from bot_modules.services import casino_service as svc
from bot_modules.services.economy_service import (
    EconSettings,
    get_balance,
    load_econ_settings,
)

log = logging.getLogger("dungeonkeeper.casino")

# Restick pacing: chatter reveals a buried panel after the quick delay, but
# while a communal round is open in the channel the panel stays put (a
# delete+repost would yank the entry point around under people mid-bet) —
# up to the hold cap, in case rounds run back to back.
RESTICK_QUICK_SECONDS = 60
RESTICK_ROUND_HOLD_SECONDS = 300
# Floor-ticker repaints coalesce a burst of plays into one hub-panel edit.
HUB_REPAINT_SECONDS = 8.0
# Big-bet slots show: pause between each reel stopping (and before the
# verdict). Sits between coinflip's hang (1.2s) and roulette frames (1.5s).
SLOTS_REEL_STOP_SECONDS = 1.4
# Ephemeral messages are editable only through their interaction's webhook,
# and Discord expires those tokens after 15 minutes — followup handles for
# the blackjack auto-stand are useless past this age.
_BJ_FOLLOWUP_TTL = 870.0


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
    streak: int = 0
    can_double: bool = True  # clicker can actually afford the second stake
    pot_after: int = 0
    broadcast_min: int = 0  # the guild's big-win broadcast bar (0 = off)


class _RoundOpen(NamedTuple):
    err: str | None = None
    running_at: float | None = None  # a round is already open, closing then
    running_url: str | None = None  # jump link to its message, when known
    econ: EconSettings | None = None
    round_id: int = 0
    closes_at: float = 0.0


class _RoundBet(NamedTuple):
    err: str | None = None
    econ: EconSettings | None = None
    rnd: sqlite3.Row | None = None
    bets: list[tuple[int, str, int]] | None = None


class _WindowUI(NamedTuple):
    """Cog-side descriptor for one windowed communal game — the service's
    ``RoundTables`` sibling. Roulette and derby run the exact same
    open/bet/repaint/timer/resolve flow; everything that differs lives
    here, so the flow itself exists once."""

    key: str  # settings prefix, stake key, log label
    enabled_attr: str
    window_attr: str
    live_round: Callable[..., sqlite3.Row | None]
    get_round: Callable[..., sqlite3.Row | None]
    open_round: Callable[..., int | None]
    set_message: Callable[..., None]
    round_bets: Callable[..., list[sqlite3.Row]]
    settle: Callable[..., list[dict] | None]
    void: Callable[..., dict[int, int]]
    # A number (roulette/derby) or the dealt coup (baccarat) — opaque here,
    # only draw/settle/build_show agree on its shape.
    draw: Callable[[], object]
    describe_bet: Callable[..., str]
    round_embed: Callable[..., discord.Embed]
    running_note: Callable[..., str]
    build_view: Callable[[int], discord.ui.View]
    next_view: Callable[[], discord.ui.View]
    # (econ, accent, result, bets, pot_after) → (frames, result embed)
    build_show: Callable[..., tuple[list[discord.Embed], discord.Embed]]
    frame_sleep: float


def _roulette_show(
    econ: EconSettings,
    accent: discord.Color | None,
    result: int,
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
) -> tuple[list[discord.Embed], discord.Embed]:
    frames = [
        casino_embeds.build_roulette_bounce_embed(
            econ, ((result + 5) % 37, (result + 23) % 37), accent
        )
    ]
    return frames, casino_embeds.build_roulette_result_embed(
        econ, result, bets, pot_after=pot_after
    )


def _derby_show(
    econ: EconSettings,
    accent: discord.Color | None,
    winner: int,
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
) -> tuple[list[discord.Embed], discord.Embed]:
    race = logic.derby_frames(winner)
    frames = [
        casino_embeds.build_derby_race_embed(econ, frame, accent)
        for frame in race[:-1]
    ]
    return frames, casino_embeds.build_derby_result_embed(
        econ, winner, race[-1], bets, pot_after=pot_after
    )


_ROULETTE_UI = _WindowUI(
    key="roulette",
    enabled_attr="roulette_enabled",
    window_attr="roulette_window_seconds",
    live_round=svc.live_roulette_round,
    get_round=svc.get_roulette_round,
    open_round=svc.open_roulette_round,
    set_message=svc.set_roulette_message,
    round_bets=svc.roulette_bets,
    settle=svc.settle_roulette_round,
    void=svc.void_roulette_round,
    draw=logic.spin_roulette,
    describe_bet=lambda b: logic.describe_bet(
        str(b["bet_type"]), int(b["selection"])
    ),
    round_embed=casino_embeds.build_roulette_round_embed,
    running_note=casino_embeds.build_round_running_note,
    build_view=build_roulette_view,
    next_view=RouletteNextView,
    build_show=_roulette_show,
    frame_sleep=1.5,
)

_DERBY_UI = _WindowUI(
    key="derby",
    enabled_attr="derby_enabled",
    window_attr="derby_window_seconds",
    live_round=svc.live_race_round,
    get_round=svc.get_race_round,
    open_round=svc.open_race_round,
    set_message=svc.set_race_message,
    round_bets=svc.race_bets,
    settle=svc.settle_race_round,
    void=svc.void_race_round,
    draw=logic.run_derby,
    describe_bet=lambda b: logic.describe_runner(int(b["runner"])),
    round_embed=casino_embeds.build_derby_round_embed,
    running_note=casino_embeds.build_race_running_note,
    build_view=build_derby_view,
    next_view=DerbyNextView,
    build_show=_derby_show,
    # A touch slower than roulette's bounce: the derby's multi-frame show
    # must stay clear of the per-channel edit rate-limit bucket.
    frame_sleep=1.6,
)


def _baccarat_show(
    econ: EconSettings,
    accent: discord.Color | None,
    coup: tuple[list[str], list[str]],
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
) -> tuple[list[discord.Embed], discord.Embed]:
    player, banker = coup
    frames = [
        casino_embeds.build_baccarat_deal_embed(econ, player, banker, accent)
    ]
    return frames, casino_embeds.build_baccarat_result_embed(
        econ, player, banker, bets, pot_after=pot_after
    )


def _settle_baccarat(
    conn: sqlite3.Connection,
    round_id: int,
    coup: tuple[list[str], list[str]],
    *,
    now: float | None = None,
) -> list[dict] | None:
    """_WindowUI.settle adapter — the draw's coup unpacks into the service's
    explicit (player, banker) signature."""
    player, banker = coup
    return svc.settle_baccarat_round(conn, round_id, player, banker, now=now)


_BACCARAT_UI = _WindowUI(
    key="baccarat",
    enabled_attr="baccarat_enabled",
    window_attr="baccarat_window_seconds",
    live_round=svc.live_baccarat_round,
    get_round=svc.get_baccarat_round,
    open_round=svc.open_baccarat_round,
    set_message=svc.set_baccarat_message,
    round_bets=svc.baccarat_bets,
    settle=_settle_baccarat,
    void=svc.void_baccarat_round,
    draw=logic.deal_baccarat,
    describe_bet=lambda b: logic.describe_baccarat_side(str(b["side"])),
    round_embed=casino_embeds.build_baccarat_round_embed,
    running_note=casino_embeds.build_coup_running_note,
    build_view=build_baccarat_view,
    next_view=BaccaratNextView,
    build_show=_baccarat_show,
    frame_sleep=1.5,
)


def _dice_show(
    econ: EconSettings,
    accent: discord.Color | None,
    dice: tuple[int, int, int],
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
) -> tuple[list[discord.Embed], discord.Embed]:
    frames = [casino_embeds.build_dice_tumble_embed(econ, accent)]
    return frames, casino_embeds.build_dice_result_embed(
        econ, dice, bets, pot_after=pot_after
    )


def _settle_dice(
    conn: sqlite3.Connection,
    round_id: int,
    dice: tuple[int, int, int],
    *,
    now: float | None = None,
) -> list[dict] | None:
    """_WindowUI.settle adapter for the three-dice draw."""
    return svc.settle_dice_round(conn, round_id, dice, now=now)


_DICE_UI = _WindowUI(
    key="dice",
    enabled_attr="dice_enabled",
    window_attr="dice_window_seconds",
    live_round=svc.live_dice_round,
    get_round=svc.get_dice_round,
    open_round=svc.open_dice_round,
    set_message=svc.set_dice_message,
    round_bets=svc.dice_bets,
    settle=_settle_dice,
    void=svc.void_dice_round,
    draw=logic.roll_sicbo,
    describe_bet=lambda b: logic.describe_sicbo_bet(str(b["bet_type"])),
    round_embed=casino_embeds.build_dice_round_embed,
    running_note=casino_embeds.build_roll_running_note,
    build_view=build_dice_view,
    next_view=DiceNextView,
    build_show=_dice_show,
    frame_sleep=1.5,
)


class CasinoCog(commands.Cog, name="CasinoCog"):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx
        self._bj_locks: dict[int, asyncio.Lock] = {}
        self._war_locks: dict[int, asyncio.Lock] = {}
        # Windowed-game close timers and debounced round-embed repaints,
        # keyed (game key, round id) since the two tables' row ids can
        # collide; plus panel resticks (one per guild). The repaint/restick
        # maps are burst coalescers, not state.
        self._window_timers: dict[tuple[str, int], asyncio.Task] = {}
        self._window_repaints: dict[tuple[str, int], asyncio.Task] = {}
        self._restick_tasks: dict[int, asyncio.Task] = {}
        # guild_id → debounced hub-panel repaint (the floor ticker).
        self._hub_repaints: dict[int, asyncio.Task] = {}
        # hand_id → (followup webhook, message id, stored at): the only way
        # to edit an ephemeral blackjack hand later (auto-stand). Entries
        # die with the process or their 15-minute webhook token, whichever
        # comes first; the settle itself never depends on them.
        self._bj_followups: dict[int, tuple[discord.Webhook, int, float]] = {}
        # Same handle map for war's tie standoffs (their own id space).
        self._war_followups: dict[int, tuple[discord.Webhook, int, float]] = {}
        # guild_id → configured casino channel, kept warm by ensure_panel so
        # the on_message restick gate never touches the DB.
        self._casino_channels: dict[int, int] = {}
        # guild_id → last jackpot value rendered on the hub panel; the
        # maintenance loop repaints when the real pot drifts from this.
        self._last_pot: dict[int, int] = {}
        # (guild, user, game) → last successful stake, pre-filled into the
        # bet modal. In-memory on purpose — resets with the process.
        self._last_bets: dict[tuple[int, int, str], int] = {}
        self._boot_task: asyncio.Task | None = None

    # ── lifecycle ──────────────────────────────────────────────────────

    async def cog_load(self) -> None:
        self.bot.add_view(CasinoHubView())
        self.bot.add_view(RouletteNextView())
        self.bot.add_view(DerbyNextView())
        self.bot.add_view(BaccaratNextView())
        self.bot.add_view(DiceNextView())
        self.bot.add_dynamic_items(
            BlackjackActionButton, RouletteBetButton, DerbyBetButton,
            BaccaratBetButton, DiceBetButton, WarActionButton,
            PlayAgainButton,
        )
        self._boot_task = asyncio.create_task(self._boot())
        self.maintenance.start()

    async def cog_unload(self) -> None:
        self.maintenance.cancel()
        if self._boot_task is not None:
            self._boot_task.cancel()
        for task_map in (
            self._window_timers, self._window_repaints, self._restick_tasks,
            self._hub_repaints,
        ):
            for task in task_map.values():
                task.cancel()
            task_map.clear()

    async def _boot(self) -> None:
        """Post-restart recovery: refund orphaned hands, re-arm round timers,
        make sure every configured guild has its hub panel."""
        await self.bot.wait_until_ready()

        def _sweep() -> tuple[
            list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row],
            list[sqlite3.Row], list[sqlite3.Row],
        ]:
            with self.ctx.open_db() as conn:
                return (
                    svc.refund_live_blackjack_hands(conn)
                    + svc.refund_live_war_hands(conn),
                    svc.open_roulette_rounds(conn),
                    svc.open_race_rounds(conn),
                    svc.open_baccarat_rounds(conn),
                    svc.open_dice_rounds(conn),
                )

        try:
            swept, rounds, races, coups, rolls = await asyncio.to_thread(_sweep)
        except Exception:
            log.exception("casino boot sweep failed")
            return
        if swept:
            # The hands' ephemeral messages died with their webhook tokens;
            # the register feed's casino_refund entry is the player-facing
            # notice, and stale buttons answer "already finished".
            log.info("casino boot sweep refunded %d live hand(s)", len(swept))
        for ui, open_rows in (
            (_ROULETTE_UI, rounds), (_DERBY_UI, races), (_BACCARAT_UI, coups),
            (_DICE_UI, rolls),
        ):
            for rnd in open_rows:
                if self.bot.get_guild(int(rnd["guild_id"])) is None:
                    await self._void_window(ui, int(rnd["id"]))
                else:
                    self._arm_window_timer(
                        ui, int(rnd["id"]), float(rnd["closes_at"])
                    )
        for guild in self.bot.guilds:
            await self.ensure_panel(guild)

    @tasks.loop(seconds=60)
    async def maintenance(self) -> None:
        """Auto-stand idle blackjack hands AND resolve overdue roulette
        rounds and derby races.

        The round/race legs are the self-healing backstop the timer tasks
        need: a resolution that failed transiently, or a round whose send
        crashed before its timer armed, would otherwise hold members'
        stakes and block the channel until the next restart. The
        exactly-once ``status='open'`` claim makes replaying resolution
        here free.
        """
        cutoff = time.time() - _BJ_FOLLOWUP_TTL
        for followups in (self._bj_followups, self._war_followups):
            for hand_id, (_, _, stored_at) in list(followups.items()):
                if stored_at < cutoff:  # webhook token expired — handle is dead
                    followups.pop(hand_id, None)

        def _scan() -> tuple[
            list[int], list[int], list[int], list[int], list[int], list[int],
            dict[int, int],
        ]:
            with self.ctx.open_db() as conn:
                stale: list[int] = []
                stale_wars: list[int] = []
                thresholds: dict[int, int] = {}
                now = time.time()

                def _idle_after(gid: int) -> int:
                    if gid not in thresholds:
                        thresholds[gid] = svc.load_casino_settings(
                            conn, gid
                        ).blackjack_idle_seconds
                    return thresholds[gid]

                for row in svc.idle_live_blackjack_hands(conn, now):
                    if now - float(row["last_action_at"]) >= _idle_after(
                        int(row["guild_id"])
                    ):
                        stale.append(int(row["id"]))
                # War standoffs idle out on the same clock as blackjack
                # hands — one table-idle knob, not two.
                for row in svc.idle_live_war_hands(conn, now):
                    if now - float(row["last_action_at"]) >= _idle_after(
                        int(row["guild_id"])
                    ):
                        stale_wars.append(int(row["id"]))
                overdue = [
                    int(r["id"])
                    for r in svc.open_roulette_rounds(conn)
                    if float(r["closes_at"]) <= now - 5  # grace for a live timer
                ]
                overdue_races = [
                    int(r["id"])
                    for r in svc.open_race_rounds(conn)
                    if float(r["closes_at"]) <= now - 5
                ]
                overdue_coups = [
                    int(r["id"])
                    for r in svc.open_baccarat_rounds(conn)
                    if float(r["closes_at"]) <= now - 5
                ]
                overdue_rolls = [
                    int(r["id"])
                    for r in svc.open_dice_rounds(conn)
                    if float(r["closes_at"]) <= now - 5
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
                return (
                    stale, stale_wars, overdue, overdue_races, overdue_coups,
                    overdue_rolls, pots,
                )

        try:
            (
                stale, stale_wars, overdue, overdue_races, overdue_coups,
                overdue_rolls, pots,
            ) = await asyncio.to_thread(_scan)
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
        for hand_id in stale_wars:
            try:
                await self._auto_war(hand_id)
            except Exception:
                log.exception("casino auto-war failed for hand %s", hand_id)
        for ui, overdue_ids in (
            (_ROULETTE_UI, overdue), (_DERBY_UI, overdue_races),
            (_BACCARAT_UI, overdue_coups), (_DICE_UI, overdue_rolls),
        ):
            for round_id in overdue_ids:
                if (ui.key, round_id) in self._window_timers:
                    continue  # a healthy timer owns it
                try:
                    # show=False: a pile of overdue rounds (the crashed-timer
                    # case this backstop exists for) must not serialize
                    # seconds of cosmetic frames per round inside the sweep.
                    await self._resolve_window(ui, round_id, show=False)
                except Exception:
                    log.exception(
                        "casino overdue %s resolve failed for %s",
                        ui.key, round_id,
                    )

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
        traffic in the casino channel debounces a restick check. The
        restick holds off while a communal round is open (see
        ``_restick_later``) so the panel never jumps mid-bet."""
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
        """Restick once the coast is clear: quick when the burying traffic
        is chatter, held (up to RESTICK_ROUND_HOLD_SECONDS) while a
        roulette/derby round is open in the channel — a delete+repost
        would move the panel around under members who are mid-bet."""
        try:
            deadline = time.monotonic() + RESTICK_ROUND_HOLD_SECONDS
            while True:
                await asyncio.sleep(RESTICK_QUICK_SECONDS)
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    return

                def _read() -> tuple[svc.CasinoSettings, bool]:
                    with self.ctx.open_db() as conn:
                        settings = svc.load_casino_settings(conn, guild_id)
                        round_open = bool(settings.channel_id) and (
                            svc.live_roulette_round(conn, settings.channel_id)
                            is not None
                            or svc.live_race_round(conn, settings.channel_id)
                            is not None
                        )
                        return settings, round_open

                settings, round_open = await asyncio.to_thread(_read)
                channel = guild.get_channel(settings.channel_id)
                if not isinstance(channel, discord.TextChannel):
                    return
                if channel.last_message_id == settings.panel_message_id:
                    return  # nothing has buried it
                if not round_open or time.monotonic() >= deadline:
                    break
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

        def _read() -> tuple[
            EconSettings, svc.CasinoSettings, int | None, str,
            list[tuple[int, str, int, int]],
            tuple[svc.DailyStanding | None, svc.DailyStanding | None],
        ]:
            with self.ctx.open_db() as conn:
                settings = svc.load_casino_settings(conn, guild.id)
                pot: int | None = None
                ticker: list[tuple[int, str, int, int]] = []
                standings: tuple[
                    svc.DailyStanding | None, svc.DailyStanding | None
                ] = (None, None)
                if settings.jackpot_enabled and settings.slots_enabled:
                    pot = svc.get_jackpot(
                        conn, guild.id, seed=settings.jackpot_seed
                    )
                if settings.channel_id:
                    ticker = [
                        (int(r["user_id"]), str(r["game"]), int(r["stake"]),
                         int(r["payout"]))
                        for r in svc.recent_ticker(conn, guild.id)
                    ]
                    standings = svc.daily_standings(conn, guild.id)
                return (
                    load_econ_settings(conn, guild.id),
                    settings,
                    pot,
                    resolve_casino_name_conn(conn, guild.id),
                    ticker,
                    standings,
                )

        try:
            econ, settings, pot, casino_name, ticker, standings = (
                await asyncio.to_thread(_read)
            )
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
            econ, settings, await self._accent(guild), jackpot=pot,
            ticker=ticker, standings=standings, casino_name=casino_name,
        )
        # Per-guild copy of the hub view: disabled tables' buttons drop
        # off (the full view stays registered for stale-panel routing).
        hub_view = build_hub_view(settings)
        if settings.panel_message_id:
            try:
                await channel.get_partial_message(settings.panel_message_id).edit(
                    embed=embed, view=hub_view
                )
                return
            except discord.NotFound:
                pass  # deleted by hand — repost below
            except discord.HTTPException:
                return
        try:
            message = await channel.send(
                embed=embed,
                view=hub_view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("casino panel post failed in #%s", channel.id)
            return
        await asyncio.to_thread(_save_ids, message.id, channel.id)

    def _schedule_hub_repaint(self, guild_id: int) -> None:
        """Debounced hub-panel repaint: a burst of instant plays becomes
        one in-place edit refreshing the floor ticker (and the pot with
        it) — an edit never moves the panel, unlike the restick."""
        if guild_id in self._hub_repaints:
            return
        self._hub_repaints[guild_id] = asyncio.create_task(
            self._repaint_hub(guild_id)
        )

    async def _repaint_hub(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(HUB_REPAINT_SECONDS)
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self.ensure_panel(guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("casino hub repaint failed for guild %s", guild_id)
        finally:
            self._hub_repaints.pop(guild_id, None)

    # ── bet modals (pre-filled, limits in the label) ───────────────────

    _MODAL_TITLES = {
        "coinflip": "Coinflip",
        "slots": "Slots",
        "blackjack": "Blackjack",
        "war": "Casino War",
    }

    def _limits_label(
        self, settings: svc.CasinoSettings, used: int, cap: int
    ) -> str:
        span = (
            f"{settings.min_bet}–{settings.max_bet}"
            if settings.max_bet
            else f"{settings.min_bet}+"
        )
        if cap > 0:
            return f"Your bet ({span} · {max(0, cap - used)} left today)"
        return f"Your bet ({span})"

    async def _modal_context(
        self, guild_id: int, user_id: int, game: str
    ) -> tuple[str, int | None]:
        """(limits label, last-bet prefill) for a bet modal."""

        def _read() -> tuple[svc.CasinoSettings, int, int]:
            with self.ctx.open_db() as conn:
                used, cap, _ = svc.daily_cap_status(conn, guild_id, user_id)
                return svc.load_casino_settings(conn, guild_id), used, cap

        settings, used, cap = await asyncio.to_thread(_read)
        return (
            self._limits_label(settings, used, cap),
            self._last_bets.get((guild_id, user_id, game)),
        )

    async def open_bet_modal(
        self, interaction: discord.Interaction, game: str,
        side: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, game
        )
        title = self._MODAL_TITLES.get(game, "Casino")
        if side is not None:
            title = f"Coinflip — {side}"
        await interaction.response.send_modal(
            BetModal(
                title=title, game=game, side=side,
                limits_label=label, default_amount=last,
            )
        )

    async def open_roulette_bet_modal(
        self, interaction: discord.Interaction, round_id: int, kind: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, "roulette"
        )
        await interaction.response.send_modal(
            RouletteBetModal(
                round_id, kind, limits_label=label, default_amount=last
            )
        )

    async def send_my_stats(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _read():
            with self.ctx.open_db() as conn:
                used, cap, reset_ts = svc.daily_cap_status(conn, guild.id, uid)
                return (
                    load_econ_settings(conn, guild.id),
                    svc.member_casino_stats(conn, guild.id, uid),
                    used, cap, reset_ts,
                )

        econ, stats, used, cap, reset_ts = await asyncio.to_thread(_read)
        await interaction.response.send_message(
            embed=casino_embeds.build_my_stats_embed(
                econ, stats, used, cap, reset_ts, await self._accent(guild)
            ),
            ephemeral=True,
        )

    # ── instant games (each player's private, in-place machine) ────────

    async def _respond_private(
        self,
        interaction: discord.Interaction,
        embed: discord.Embed,
        view: discord.ui.View | None = None,
    ) -> None:
        """First render of an instant play: edit the clicker's own
        ephemeral machine in place when the press came from one (Play
        Again, the coinflip picker), else open a fresh ephemeral message.
        Later animation frames go through ``edit_original_response`` —
        the interaction's webhook, clear of the channel edit bucket."""
        message = interaction.message
        if message is not None and message.flags.ephemeral:
            await interaction.response.edit_message(embed=embed, view=view)
        elif view is not None:
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                embed=embed, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _after_instant(
        self,
        interaction: discord.Interaction,
        *,
        payout: int,
        threshold: int,
        embed: discord.Embed,
        view: discord.ui.View,
        skip_broadcast: bool = False,
    ) -> None:
        """Post-settle chores every instant play shares: the debounced
        floor-ticker repaint, and the public big-win broadcast (result
        embed + Play Again, the "me too" invitation) once the payout
        clears the configured bar."""
        guild = interaction.guild
        if guild is not None:
            self._schedule_hub_repaint(guild.id)
        if skip_broadcast or threshold <= 0 or payout < threshold:
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(
                embed=embed, view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning("casino big-win broadcast failed in #%s", channel.id)

    async def play_coinflip(
        self, interaction: discord.Interaction, side: str, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _play() -> tuple[
            str | None, EconSettings | None, str, svc.InstantResult,
            svc.CasinoSettings,
        ]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "coinflip",
                    channel_id=interaction.channel_id,
                )
                if err is not None:
                    return (
                        err, None, "", svc.InstantResult(0),
                        svc.DEFAULT_CASINO_SETTINGS,
                    )
                landed = logic.flip_coin()
                result = svc.settle_coinflip(
                    conn, guild.id, interaction.user.id, amount, side, landed
                )
                settings = svc.load_casino_settings(conn, guild.id)
                return None, load_econ_settings(conn, guild.id), landed, result, settings

        err, econ, landed, result, settings = await asyncio.to_thread(_play)
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, interaction.user.id, "coinflip")] = amount
        final = casino_embeds.build_coinflip_embed(
            econ, interaction.user.id, side, landed, amount, result.payout,
            streak=result.streak, pot_after=result.pot_after,
        )
        try:
            if not logic.is_big_bet(amount, settings.max_bet):
                await self._respond_private(
                    interaction, final, play_again_view("coinflip", amount, side)
                )
            else:
                # The show: money settled above — a crash mid-animation
                # leaves a stale message, never a wrong balance.
                await self._respond_private(
                    interaction,
                    casino_embeds.build_coinflip_spin_embed(
                        econ, interaction.user.id, side, amount,
                        await self._accent(guild),
                    ),
                )
                await asyncio.sleep(1.2)
                await interaction.edit_original_response(
                    embed=final, view=play_again_view("coinflip", amount, side)
                )
        except discord.HTTPException:
            pass
        await self._after_instant(
            interaction, payout=result.payout,
            threshold=settings.broadcast_min_payout, embed=final,
            view=play_again_view("coinflip", amount, side),
        )

    async def play_slots(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _play() -> tuple[
            str | None, EconSettings | None, tuple[str, str, str],
            svc.InstantResult, svc.CasinoSettings, str,
        ]:
            with self.ctx.open_db() as conn:
                err = svc.take_stake(
                    conn, guild.id, interaction.user.id, amount, "slots",
                    channel_id=interaction.channel_id,
                )
                if err is not None:
                    return (
                        err, None, ("", "", ""), svc.InstantResult(0),
                        svc.DEFAULT_CASINO_SETTINGS, "",
                    )
                reels = logic.spin_slots()
                result = svc.settle_slots(
                    conn, guild.id, interaction.user.id, amount, reels
                )
                return (
                    None, load_econ_settings(conn, guild.id), reels, result,
                    svc.load_casino_settings(conn, guild.id),
                    resolve_casino_name_conn(conn, guild.id),
                )

        err, econ, reels, result, settings, casino_name = (
            await asyncio.to_thread(_play)
        )
        if err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, interaction.user.id, "slots")] = amount
        final = casino_embeds.build_slots_embed(
            econ, interaction.user.id, reels, amount, result.payout,
            result.label,
            jackpot_won=result.jackpot_won, streak=result.streak,
            pot_after=result.pot_after, casino_name=casino_name,
        )
        try:
            if logic.is_big_bet(amount, settings.max_bet):
                # Reels stop one at a time; the outcome is already banked.
                accent = await self._accent(guild)
                await self._respond_private(
                    interaction,
                    casino_embeds.build_slots_spin_embed(
                        econ, interaction.user.id, amount, (None, None, None),
                        accent, casino_name=casino_name,
                    ),
                )
                for stop in (1, 2):
                    await asyncio.sleep(SLOTS_REEL_STOP_SECONDS)
                    revealed = (
                        reels[0] if stop >= 1 else None,
                        reels[1] if stop >= 2 else None,
                        None,
                    )
                    await interaction.edit_original_response(
                        embed=casino_embeds.build_slots_spin_embed(
                            econ, interaction.user.id, amount, revealed,
                            accent, casino_name=casino_name,
                        )
                    )
                await asyncio.sleep(SLOTS_REEL_STOP_SECONDS)
                await interaction.edit_original_response(
                    embed=final, view=play_again_view("slots", amount)
                )
            else:
                await self._respond_private(
                    interaction, final, play_again_view("slots", amount)
                )
        except discord.HTTPException:
            pass
        if result.jackpot_won and isinstance(
            interaction.channel, discord.TextChannel
        ):
            try:
                await interaction.channel.send(
                    embed=casino_embeds.build_jackpot_celebration(
                        econ, interaction.user.id, result.jackpot_won,
                        casino_name=casino_name,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
        # The jackpot celebration above already is the broadcast.
        await self._after_instant(
            interaction, payout=result.payout,
            threshold=settings.broadcast_min_payout, embed=final,
            view=play_again_view("slots", amount),
            skip_broadcast=bool(result.jackpot_won),
        )

    async def send_help(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return

        def _read() -> tuple[EconSettings, svc.CasinoSettings, str]:
            with self.ctx.open_db() as conn:
                return (
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                    resolve_casino_name_conn(conn, guild.id),
                )

        econ, settings, casino_name = await asyncio.to_thread(_read)
        await interaction.response.send_message(
            embed=casino_embeds.build_help_embed(
                econ, settings, await self._accent(guild),
                casino_name=casino_name,
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
                streak = 0
                pot_after = 0
                if logic.is_natural(player) or logic.is_natural(dealer):
                    payout, outcome = logic.blackjack_settle(player, dealer, amount)
                    svc.settle_blackjack_hand(conn, hand_id, payout, outcome)
                    stats = svc.member_casino_stats(conn, guild.id, uid)
                    streak = int(stats["streak"]) if stats is not None else 0
                    if payout == 0:  # dealer natural fed the pot
                        cs = svc.load_casino_settings(conn, guild.id)
                        if cs.jackpot_enabled:
                            pot_after = svc.get_jackpot(conn, guild.id)
                return _HandOutcome(
                    econ=econ, hand_id=hand_id, player=player, dealer=dealer,
                    stake=amount, outcome=outcome, payout=payout, streak=streak,
                    can_double=get_balance(conn, guild.id, uid) >= amount,
                    pot_after=pot_after,
                    broadcast_min=svc.load_casino_settings(
                        conn, guild.id
                    ).broadcast_min_payout,
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
        self._last_bets[(guild.id, uid, "blackjack")] = amount
        embed = casino_embeds.build_blackjack_embed(
            result.econ, uid, result.player or [], result.dealer or [], amount,
            await self._accent(guild), outcome=result.outcome,
            payout=result.payout, streak=result.streak,
            pot_after=result.pot_after,
        )
        view = (
            play_again_view("blackjack", amount)
            if result.outcome is not None
            else build_blackjack_view(
                result.hand_id, can_double=result.can_double
            )
        )
        await self._respond_private(interaction, embed, view)
        if result.outcome is None:
            message = await interaction.original_response()
            hand_id = result.hand_id
            # The auto-stand's only way back into an ephemeral message —
            # the interaction's own webhook, good for ~15 minutes.
            self._bj_followups[hand_id] = (
                interaction.followup, message.id, time.time()
            )

            def _bind() -> None:
                with self.ctx.open_db() as conn:
                    svc.set_blackjack_message(conn, hand_id, message.id)

            await asyncio.to_thread(_bind)
        else:
            await self._after_instant(
                interaction, payout=result.payout,
                threshold=result.broadcast_min, embed=embed,
                view=play_again_view("blackjack", amount),
            )

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

        def _step() -> tuple[
            svc.BlackjackStep, EconSettings | None, svc.CasinoSettings
        ]:
            with self.ctx.open_db() as conn:
                step = svc.resolve_blackjack_action(
                    conn, guild.id, hand_id, uid, action
                )
                if step.err is not None:
                    return step, None, svc.DEFAULT_CASINO_SETTINGS
                return (
                    step,
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                )

        step, econ, settings = await asyncio.to_thread(_step)
        if step.err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {step.err}")
            return step.err == "That hand is already finished."
        accent = await self._accent(guild)
        embed = casino_embeds.build_blackjack_embed(
            econ, uid, step.player or [], step.dealer or [],
            step.stake, accent,
            doubled=step.doubled, outcome=step.outcome, payout=step.payout,
            streak=step.streak, pot_after=step.pot_after,
        )
        base_stake = step.stake // 2 if step.doubled else step.stake
        view = (
            play_again_view("blackjack", base_stake)
            if step.outcome is not None
            else build_blackjack_view(hand_id, can_double=False)
        )
        try:
            if step.outcome is not None and logic.is_big_bet(
                step.stake, settings.max_bet
            ):
                # Pause on the hole-card flip before the verdict lands.
                await interaction.response.edit_message(
                    embed=casino_embeds.build_blackjack_reveal_embed(
                        econ, uid, step.player or [],
                        (step.dealer or [])[:2], step.stake, accent,
                        doubled=step.doubled,
                    ),
                    view=None,
                )
                await asyncio.sleep(1.4)
                # The hand message is ephemeral — only the interaction's
                # webhook can edit it, never the channel route.
                await interaction.edit_original_response(embed=embed, view=view)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
        except discord.HTTPException:
            pass
        if step.outcome is not None:
            self._bj_followups.pop(hand_id, None)
            await self._after_instant(
                interaction, payout=step.payout,
                threshold=settings.broadcast_min_payout, embed=embed,
                view=play_again_view("blackjack", base_stake),
            )
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
                    gid = int(row["guild_id"])
                    return (
                        row, step, load_econ_settings(conn, gid),
                        svc.load_casino_settings(conn, gid),
                    )

            result = await asyncio.to_thread(_stand)
        self._bj_locks.pop(hand_id, None)
        handle = self._bj_followups.pop(hand_id, None)
        if result is None:
            return
        row, step, econ, settings = result
        guild = self.bot.get_guild(int(row["guild_id"]))
        embed = casino_embeds.build_blackjack_embed(
            econ, int(row["user_id"]), step.player or [], step.dealer or [],
            step.stake, await self._accent(guild),
            doubled=step.doubled, outcome=step.outcome, payout=step.payout,
            streak=step.streak, pot_after=step.pot_after,
        )
        embed.set_footer(text="Stood automatically — the dealer waits for no one.")
        base_stake = step.stake // 2 if step.doubled else step.stake
        if handle is not None:
            # Best-effort: the hand's ephemeral message is reachable only
            # through its interaction webhook, and only while the token
            # lives. The settle above never depended on this edit.
            webhook, message_id, _ = handle
            try:
                await webhook.edit_message(
                    message_id, embed=embed,
                    view=play_again_view("blackjack", base_stake),
                )
            except discord.HTTPException:
                pass
        self._schedule_hub_repaint(int(row["guild_id"]))
        if settings.broadcast_min_payout > 0 and (
            step.payout >= settings.broadcast_min_payout
        ):
            channel = self.bot.get_channel(int(row["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        embed=embed,
                        view=play_again_view("blackjack", base_stake),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning(
                        "casino big-win broadcast failed in #%s", channel.id
                    )

    # ── war (instant, with the tie's rare live decision) ───────────────

    def _war_embed(
        self,
        econ: EconSettings,
        user_id: int,
        step: svc.WarStep,
        accent: discord.Color | None,
    ) -> discord.Embed:
        return casino_embeds.build_war_embed(
            econ, user_id, step.player, step.dealer, step.stake, accent,
            war_player=step.war_player, war_dealer=step.war_dealer,
            outcome=step.outcome, payout=step.payout, streak=step.streak,
            pot_after=step.pot_after,
        )

    async def play_war(
        self, interaction: discord.Interaction, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _play() -> tuple[
            svc.WarStep, EconSettings | None, svc.CasinoSettings
        ]:
            with self.ctx.open_db() as conn:
                step = svc.play_war(
                    conn, guild.id, interaction.channel_id, uid, amount
                )
                if step.err is not None:
                    return step, None, svc.DEFAULT_CASINO_SETTINGS
                return (
                    step,
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                )

        try:
            step, econ, settings = await asyncio.to_thread(_play)
        except sqlite3.IntegrityError:
            await safe_ephemeral(
                interaction,
                "❌ You already have a war decision pending — finish it first.",
            )
            return
        if step.err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {step.err}")
            return
        self._last_bets[(guild.id, uid, "war")] = amount
        embed = self._war_embed(econ, uid, step, await self._accent(guild))
        if step.outcome is None:  # the tie standoff — buttons, not a verdict
            await self._respond_private(
                interaction, embed, build_war_view(step.hand_id)
            )
            message = await interaction.original_response()
            hand_id = step.hand_id
            # The auto-resolve's only way back into the ephemeral message.
            self._war_followups[hand_id] = (
                interaction.followup, message.id, time.time()
            )

            def _bind() -> None:
                with self.ctx.open_db() as conn:
                    svc.set_war_message(conn, hand_id, message.id)

            await asyncio.to_thread(_bind)
            return
        try:
            await self._respond_private(
                interaction, embed, play_again_view("war", amount)
            )
        except discord.HTTPException:
            pass
        await self._after_instant(
            interaction, payout=step.payout,
            threshold=settings.broadcast_min_payout, embed=embed,
            view=play_again_view("war", amount),
        )

    async def war_action(
        self, interaction: discord.Interaction, hand_id: int, action: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        lock = self._war_locks.setdefault(hand_id, asyncio.Lock())
        async with lock:
            done = await self._war_step(interaction, hand_id, action)
        if done:
            self._war_locks.pop(hand_id, None)

    async def _war_step(
        self, interaction: discord.Interaction, hand_id: int, action: str
    ) -> bool:
        """One standoff button press — rules in the service; True once the
        hand is terminally gone (the blackjack lock-drop contract)."""
        guild = interaction.guild
        assert guild is not None
        uid = interaction.user.id

        def _step() -> tuple[
            svc.WarStep, EconSettings | None, svc.CasinoSettings
        ]:
            with self.ctx.open_db() as conn:
                step = svc.resolve_war_action(
                    conn, guild.id, hand_id, uid, action
                )
                if step.err is not None:
                    return step, None, svc.DEFAULT_CASINO_SETTINGS
                return (
                    step,
                    load_econ_settings(conn, guild.id),
                    svc.load_casino_settings(conn, guild.id),
                )

        step, econ, settings = await asyncio.to_thread(_step)
        if step.err is not None or econ is None:
            await safe_ephemeral(interaction, f"❌ {step.err}")
            return step.err == "That hand is already finished."
        embed = self._war_embed(econ, uid, step, await self._accent(guild))
        original = step.stake // 2 if step.outcome in ("war_win", "war_lose") else step.stake
        try:
            await interaction.response.edit_message(
                embed=embed, view=play_again_view("war", original)
            )
        except discord.HTTPException:
            pass
        self._war_followups.pop(hand_id, None)
        await self._after_instant(
            interaction, payout=step.payout,
            threshold=settings.broadcast_min_payout, embed=embed,
            view=play_again_view("war", original),
        )
        return True

    async def _auto_war(self, hand_id: int) -> None:
        """The idle sweep's resolve — war when affordable, else retreat.

        ``resolve_idle_war_hand`` returns None when the standoff settled
        concurrently, so this can never render an outcome the settle
        didn't pay.
        """
        lock = self._war_locks.setdefault(hand_id, asyncio.Lock())
        async with lock:

            def _resolve():
                with self.ctx.open_db() as conn:
                    row = svc.get_war_hand(conn, hand_id)
                    if row is None:
                        return None
                    step = svc.resolve_idle_war_hand(conn, hand_id)
                    if step is None:
                        return None
                    gid = int(row["guild_id"])
                    return (
                        row, step, load_econ_settings(conn, gid),
                        svc.load_casino_settings(conn, gid),
                    )

            result = await asyncio.to_thread(_resolve)
        self._war_locks.pop(hand_id, None)
        handle = self._war_followups.pop(hand_id, None)
        if result is None:
            return
        row, step, econ, settings = result
        guild = self.bot.get_guild(int(row["guild_id"]))
        uid = int(row["user_id"])
        embed = self._war_embed(econ, uid, step, await self._accent(guild))
        embed.set_footer(
            text="Resolved automatically — fortune favors the decisive."
        )
        original = step.stake // 2 if step.outcome in ("war_win", "war_lose") else step.stake
        if handle is not None:
            webhook, message_id, _ = handle
            try:
                await webhook.edit_message(
                    message_id, embed=embed,
                    view=play_again_view("war", original),
                )
            except discord.HTTPException:
                pass
        self._schedule_hub_repaint(int(row["guild_id"]))
        if settings.broadcast_min_payout > 0 and (
            step.payout >= settings.broadcast_min_payout
        ):
            channel = self.bot.get_channel(int(row["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        embed=embed,
                        view=play_again_view("war", original),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning(
                        "casino big-win broadcast failed in #%s", channel.id
                    )

    # ── windowed communal games (roulette + derby: ONE flow) ───────────

    async def open_roulette(self, interaction: discord.Interaction) -> None:
        await self._open_window(interaction, _ROULETTE_UI)

    async def open_derby(self, interaction: discord.Interaction) -> None:
        await self._open_window(interaction, _DERBY_UI)

    async def open_baccarat(self, interaction: discord.Interaction) -> None:
        await self._open_window(interaction, _BACCARAT_UI)

    async def open_dice(self, interaction: discord.Interaction) -> None:
        await self._open_window(interaction, _DICE_UI)

    async def _open_window(
        self, interaction: discord.Interaction, ui: _WindowUI
    ) -> None:
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
                if not getattr(settings, ui.enabled_attr):
                    return _RoundOpen(err="That table is closed right now.")
                existing = ui.live_round(conn, channel_id)
                if existing is not None:
                    mid = int(existing["message_id"])
                    return _RoundOpen(
                        running_at=float(existing["closes_at"]),
                        running_url=(
                            f"https://discord.com/channels/{guild.id}"
                            f"/{channel_id}/{mid}"
                            if mid
                            else None
                        ),
                    )
                round_id = ui.open_round(
                    conn, guild.id, channel_id, getattr(settings, ui.window_attr)
                )
                if round_id is None:
                    return _RoundOpen(running_at=time.time())
                rnd = ui.get_round(conn, round_id)
                assert rnd is not None
                return _RoundOpen(
                    econ=econ, round_id=round_id,
                    closes_at=float(rnd["closes_at"]),
                )

        try:
            result = await asyncio.to_thread(_open)
        except sqlite3.IntegrityError:
            # Two simultaneous presses both passed the pre-check; the
            # partial unique index caught the second — point them at the
            # REAL round (its closes_at + jump link), not a fabricated
            # "starts now".
            await safe_ephemeral(
                interaction, await self._running_note(ui, guild.id, channel_id)
            )
            return
        if result.err is not None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return
        if result.running_at is not None or result.econ is None:
            await safe_ephemeral(
                interaction,
                ui.running_note(result.running_at or 0.0, result.running_url),
            )
            return
        round_id = result.round_id
        # Arm BEFORE the send: if the send fails the timer still resolves
        # (refunds) the round instead of stranding it headless until boot.
        self._arm_window_timer(ui, round_id, result.closes_at)
        embed = ui.round_embed(
            result.econ, result.closes_at, [], await self._accent(guild)
        )
        try:
            await interaction.response.send_message(
                embed=embed,
                view=ui.build_view(round_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            message = await interaction.original_response()
        except discord.HTTPException:
            # No message means nobody can bet — kill the round now rather
            # than leave a headless betting window blocking the channel.
            timer = self._window_timers.pop((ui.key, round_id), None)
            if timer is not None:
                timer.cancel()
            await self._void_window(ui, round_id)
            await safe_ephemeral(
                interaction, "❌ Couldn't open the round — try again."
            )
            return

        def _bind() -> None:
            with self.ctx.open_db() as conn:
                ui.set_message(conn, round_id, message.id)

        await asyncio.to_thread(_bind)

    async def _running_note(
        self, ui: _WindowUI, guild_id: int, channel_id: int
    ) -> str:
        """The already-running pointer, from the live round's actual state."""

        def _read() -> sqlite3.Row | None:
            with self.ctx.open_db() as conn:
                return ui.live_round(conn, channel_id)

        try:
            existing = await asyncio.to_thread(_read)
        except Exception:
            existing = None
        if existing is None:  # resolved in the meantime — generic note
            return ui.running_note(time.time(), None)
        mid = int(existing["message_id"])
        url = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{mid}"
            if mid
            else None
        )
        return ui.running_note(float(existing["closes_at"]), url)

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
        self._last_bets[(guild.id, uid, "roulette")] = amount
        desc = logic.describe_bet(bet_type, selection)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        self._schedule_window_repaint(_ROULETTE_UI, guild, round_id)

    def _schedule_window_repaint(
        self, ui: _WindowUI, guild: discord.Guild, round_id: int
    ) -> None:
        # Repaint is debounced per round — a burst of bets coalesces into
        # one message edit (the live_signal idea) instead of one Discord
        # edit per bettor.
        key = (ui.key, round_id)
        if key in self._window_repaints:
            return
        self._window_repaints[key] = asyncio.create_task(
            self._repaint_window(ui, guild, round_id)
        )

    async def _repaint_window(
        self, ui: _WindowUI, guild: discord.Guild, round_id: int
    ) -> None:
        try:
            await asyncio.sleep(2.0)

            def _read() -> _RoundBet:
                with self.ctx.open_db() as conn:
                    rnd = ui.get_round(conn, round_id)
                    if rnd is None or str(rnd["status"]) != "open":
                        return _RoundBet()
                    bets = [
                        (int(b["user_id"]), ui.describe_bet(b), int(b["amount"]))
                        for b in ui.round_bets(conn, round_id)
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
                embed = ui.round_embed(
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
            log.exception("%s repaint failed for round %s", ui.key, round_id)
        finally:
            self._window_repaints.pop((ui.key, round_id), None)

    def _arm_window_timer(
        self, ui: _WindowUI, round_id: int, closes_at: float
    ) -> None:
        key = (ui.key, round_id)
        if key in self._window_timers:
            return
        delay = max(0.0, closes_at - time.time())
        self._window_timers[key] = asyncio.create_task(
            self._window_timer(ui, round_id, delay)
        )

    async def _window_timer(
        self, ui: _WindowUI, round_id: int, delay: float
    ) -> None:
        try:
            await asyncio.sleep(delay)
            await self._resolve_window(ui, round_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s resolution failed for round %s", ui.key, round_id)
        finally:
            self._window_timers.pop((ui.key, round_id), None)

    async def _resolve_window(
        self, ui: _WindowUI, round_id: int, *, show: bool = True
    ) -> None:
        """Settle and render one window. ``show=False`` (the maintenance
        backstop) skips the cosmetic frames and jumps to the verdict.

        No lock needed: the status='open' claim inside the settle is the
        mutual exclusion (timer, maintenance sweep and void can all reach
        a round; only the first claim pays).
        """
        repaint = self._window_repaints.pop((ui.key, round_id), None)
        if repaint is not None:
            repaint.cancel()  # a stale "bets open" edit must not land post-show

        def _settle():
            with self.ctx.open_db() as conn:
                rnd = ui.get_round(conn, round_id)
                if rnd is None or str(rnd["status"]) != "open":
                    return None
                result = ui.draw()
                bets = ui.settle(conn, round_id, result)
                if bets is None:
                    return None
                gid = int(rnd["guild_id"])
                econ = load_econ_settings(conn, gid)
                pot_after = 0
                if any(int(b["payout"]) == 0 for b in bets):
                    cs = svc.load_casino_settings(conn, gid)
                    if cs.jackpot_enabled:
                        pot_after = svc.get_jackpot(conn, gid)
                return rnd, result, bets, econ, pot_after

        settled = await asyncio.to_thread(_settle)
        if settled is None:
            return
        rnd, result, bet_rows, econ, pot_after = settled
        bets = [
            (int(b["user_id"]), ui.describe_bet(b), int(b["amount"]),
             int(b["payout"]))
            for b in bet_rows
        ]
        channel = self.bot.get_channel(int(rnd["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        frames, result_embed = ui.build_show(
            econ, await self._accent(channel.guild), result, bets, pot_after
        )
        if int(rnd["message_id"]):
            try:
                # One resolution per window, so the show is worth it — and
                # the money settled above, so a crash mid-frames leaves a
                # stale message, never a wrong balance.
                msg = channel.get_partial_message(int(rnd["message_id"]))
                if show:
                    for frame in frames:
                        await msg.edit(embed=frame, view=None)
                        await asyncio.sleep(ui.frame_sleep)
                await msg.edit(embed=result_embed, view=None)
            except discord.HTTPException:
                log.warning(
                    "%s result edit failed for round %s (%d winners) — "
                    "panel may be stuck mid-show",
                    ui.key, rnd["id"], sum(1 for b in bets if b[3] > 0),
                )
        if bets:
            try:
                await channel.send(
                    embed=result_embed,
                    view=ui.next_view(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                log.warning(
                    "%s result send failed for round %s (%d winners)",
                    ui.key, rnd["id"], sum(1 for b in bets if b[3] > 0),
                )

    async def _void_window(self, ui: _WindowUI, round_id: int) -> None:
        def _void() -> None:
            with self.ctx.open_db() as conn:
                ui.void(conn, round_id)

        try:
            await asyncio.to_thread(_void)
        except Exception:
            log.exception("%s void failed for round %s", ui.key, round_id)

    # ── derby-only glue (everything else rides _WindowUI above) ────────

    async def open_derby_bet_modal(
        self, interaction: discord.Interaction, round_id: int, runner: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, "derby"
        )
        await interaction.response.send_modal(
            DerbyBetModal(
                round_id, runner, logic.describe_runner(runner),
                limits_label=label, default_amount=last,
            )
        )

    async def place_derby_bet(
        self,
        interaction: discord.Interaction,
        round_id: int,
        runner: int,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _bet() -> str | None:
            with self.ctx.open_db() as conn:
                return svc.place_race_bet(conn, round_id, uid, runner, amount)

        err = await asyncio.to_thread(_bet)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, uid, "derby")] = amount
        desc = logic.describe_runner(runner)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        self._schedule_window_repaint(_DERBY_UI, guild, round_id)

    # ── baccarat-only glue (everything else rides _WindowUI above) ─────

    async def open_baccarat_bet_modal(
        self, interaction: discord.Interaction, round_id: int, side: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, "baccarat"
        )
        await interaction.response.send_modal(
            BaccaratBetModal(
                round_id, side, logic.describe_baccarat_side(side),
                limits_label=label, default_amount=last,
            )
        )

    async def place_baccarat_bet(
        self,
        interaction: discord.Interaction,
        round_id: int,
        side: str,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _bet() -> str | None:
            with self.ctx.open_db() as conn:
                return svc.place_baccarat_bet(conn, round_id, uid, side, amount)

        err = await asyncio.to_thread(_bet)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, uid, "baccarat")] = amount
        desc = logic.describe_baccarat_side(side)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        self._schedule_window_repaint(_BACCARAT_UI, guild, round_id)

    # ── dice-only glue (everything else rides _WindowUI above) ─────────

    async def open_dice_bet_modal(
        self, interaction: discord.Interaction, round_id: int, bet_type: str
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, "dice"
        )
        await interaction.response.send_modal(
            DiceBetModal(
                round_id, bet_type, logic.describe_sicbo_bet(bet_type),
                limits_label=label, default_amount=last,
            )
        )

    async def place_dice_bet(
        self,
        interaction: discord.Interaction,
        round_id: int,
        bet_type: str,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _bet() -> str | None:
            with self.ctx.open_db() as conn:
                return svc.place_dice_bet(conn, round_id, uid, bet_type, amount)

        err = await asyncio.to_thread(_bet)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, uid, "dice")] = amount
        desc = logic.describe_sicbo_bet(bet_type)
        await safe_ephemeral(interaction, f"✅ Bet placed: {desc} for {amount:,}.")
        self._schedule_window_repaint(_DICE_UI, guild, round_id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
