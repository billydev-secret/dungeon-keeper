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
import json
import logging
import sqlite3
import time

from collections.abc import Callable
from typing import NamedTuple

import discord

from discord.ext import commands, tasks

from bot_modules.cogs.casino import embeds as casino_embeds
from bot_modules.cogs.casino.pools_panel import PoolsMixin
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
    KenoNextView,
    KenoTicketModal,
    KenoTierButton,
    PlayAgainButton,
    PoolsPanelView,
    RoundResolveButton,
    RouletteBetButton,
    RouletteBetModal,
    RouletteNextView,
    WarActionButton,
    build_baccarat_view,
    build_blackjack_view,
    build_derby_view,
    build_dice_view,
    build_hub_view,
    build_keno_view,
    build_roulette_view,
    build_war_view,
    play_again_view,
    safe_ephemeral,
)
from bot_modules.core.app_context import Bot
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.branding_service import resolve_casino_name_conn
from bot_modules.services import casino_logic as logic
from bot_modules.services import casino_service as svc
from bot_modules.services.economy_service import (
    EconSettings,
    get_balance,
    load_econ_settings,
)
from bot_modules.services.name_resolver import NameFn, build_name_fn

log = logging.getLogger("dungeonkeeper.casino")

# The hub panel used to hold its restick while a communal round was taking
# bets in the channel, because a delete+repost would yank the entry point
# around under people mid-bet, and moving it the instant a round settled
# pulled the result out from under everyone reading it. Private rounds live
# in each player's own ephemeral message, which no amount of channel
# traffic can disturb, so there is nothing left to hold for and the panel
# resticks on the ordinary debounce.
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
    # The verb on this game's resolve button. A private round has no
    # countdown — the player decides when the wheel turns — so every game
    # needs its own word for "go" ("Spin" is wrong for a horse race).
    resolve_label: str
    resolve_emoji: str
    # Refusal when the player already has one of these open. Per-game
    # wording because "you already have a round open" reads wrong for a
    # horse race, and this is the one message a player meets by accident.
    already_open_note: str
    live_player_round: Callable[..., sqlite3.Row | None]
    get_round: Callable[..., sqlite3.Row | None]
    open_round: Callable[..., int | None]
    open_rounds: Callable[..., list[sqlite3.Row]]  # boot/backstop sweeps
    set_message: Callable[..., None]
    round_bets: Callable[..., list[sqlite3.Row]]
    settle: Callable[..., list[dict] | None]
    void: Callable[..., dict[int, int]]
    # A number (roulette/derby) or the dealt coup (baccarat) — opaque here,
    # only draw/settle/build_show agree on its shape.
    draw: Callable[[], object]
    describe_bet: Callable[..., str]
    round_embed: Callable[..., discord.Embed]
    build_view: Callable[[int], discord.ui.View]
    next_view: Callable[[], discord.ui.View]
    # (econ, accent, result, bets, pot_after, name_fn=) → (frames, result embed)
    build_show: Callable[..., tuple[list[discord.Embed], discord.Embed]]
    frame_sleep: float
    # Result-time ticket line for games whose *draw* annotates the bet
    # (keno catches). describe_bet can't do this: the bets board renders
    # before anything is drawn, so there is nothing to catch against.
    # None → the board-time description is the whole story. (bet row,
    # draw result) → line.
    annotate_bet: Callable[..., str] | None = None


def _roulette_show(
    econ: EconSettings,
    accent: discord.Color | None,
    result: int,
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
    *,
    name_fn: NameFn,
) -> tuple[list[discord.Embed], discord.Embed]:
    frames = [
        casino_embeds.build_roulette_bounce_embed(
            econ, ((result + 5) % 37, (result + 23) % 37), accent
        )
    ]
    return frames, casino_embeds.build_roulette_result_embed(
        econ, result, bets, pot_after=pot_after, name_fn=name_fn
    )


def _derby_show(
    econ: EconSettings,
    accent: discord.Color | None,
    winner: int,
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
    *,
    name_fn: NameFn,
) -> tuple[list[discord.Embed], discord.Embed]:
    race = logic.derby_frames(winner)
    frames = [
        casino_embeds.build_derby_race_embed(econ, frame, accent)
        for frame in race[:-1]
    ]
    return frames, casino_embeds.build_derby_result_embed(
        econ, winner, race[-1], bets, pot_after=pot_after, name_fn=name_fn
    )


_ROULETTE_UI = _WindowUI(
    key="roulette",
    enabled_attr="roulette_enabled",
    resolve_label="Spin",
    resolve_emoji="🎡",
    already_open_note="You already have a spin open — finish it first.",
    live_player_round=svc.live_roulette_player_round,
    get_round=svc.get_roulette_round,
    open_round=svc.open_roulette_round,
    open_rounds=svc.open_roulette_rounds,
    set_message=svc.set_roulette_message,
    round_bets=svc.roulette_bets,
    settle=svc.settle_roulette_round,
    void=svc.void_roulette_round,
    draw=logic.spin_roulette,
    describe_bet=lambda b: logic.describe_bet(
        str(b["bet_type"]), int(b["selection"])
    ),
    round_embed=casino_embeds.build_roulette_round_embed,
    build_view=build_roulette_view,
    next_view=RouletteNextView,
    build_show=_roulette_show,
    frame_sleep=1.5,
)

_DERBY_UI = _WindowUI(
    key="derby",
    enabled_attr="derby_enabled",
    resolve_label="Race",
    resolve_emoji="🏇",
    already_open_note="You already have a race running — finish it first.",
    live_player_round=svc.live_race_player_round,
    get_round=svc.get_race_round,
    open_round=svc.open_race_round,
    open_rounds=svc.open_race_rounds,
    set_message=svc.set_race_message,
    round_bets=svc.race_bets,
    settle=svc.settle_race_round,
    void=svc.void_race_round,
    draw=logic.run_derby,
    describe_bet=lambda b: logic.describe_runner(int(b["runner"])),
    round_embed=casino_embeds.build_derby_round_embed,
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
    *,
    name_fn: NameFn,
) -> tuple[list[discord.Embed], discord.Embed]:
    player, banker = coup
    frames = [
        casino_embeds.build_baccarat_deal_embed(econ, player, banker, accent)
    ]
    return frames, casino_embeds.build_baccarat_result_embed(
        econ, player, banker, bets, pot_after=pot_after, name_fn=name_fn
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
    resolve_label="Deal",
    resolve_emoji="🎴",
    already_open_note="You already have a coup open — finish it first.",
    live_player_round=svc.live_baccarat_player_round,
    get_round=svc.get_baccarat_round,
    open_round=svc.open_baccarat_round,
    open_rounds=svc.open_baccarat_rounds,
    set_message=svc.set_baccarat_message,
    round_bets=svc.baccarat_bets,
    settle=_settle_baccarat,
    void=svc.void_baccarat_round,
    draw=logic.deal_baccarat,
    describe_bet=lambda b: logic.describe_baccarat_side(str(b["side"])),
    round_embed=casino_embeds.build_baccarat_round_embed,
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
    *,
    name_fn: NameFn,
) -> tuple[list[discord.Embed], discord.Embed]:
    frames = [casino_embeds.build_dice_tumble_embed(econ, accent)]
    return frames, casino_embeds.build_dice_result_embed(
        econ, dice, bets, pot_after=pot_after, name_fn=name_fn
    )


_DICE_UI = _WindowUI(
    key="dice",
    enabled_attr="dice_enabled",
    resolve_label="Roll",
    resolve_emoji="🎲",
    already_open_note="You already have a roll open — finish it first.",
    live_player_round=svc.live_dice_player_round,
    get_round=svc.get_dice_round,
    open_round=svc.open_dice_round,
    open_rounds=svc.open_dice_rounds,
    set_message=svc.set_dice_message,
    round_bets=svc.dice_bets,
    settle=svc.settle_dice_round,
    void=svc.void_dice_round,
    draw=logic.roll_sicbo,
    describe_bet=lambda b: logic.describe_sicbo_bet(str(b["bet_type"])),
    round_embed=casino_embeds.build_dice_round_embed,
    build_view=build_dice_view,
    next_view=DiceNextView,
    build_show=_dice_show,
    frame_sleep=1.5,
)


def _keno_show(
    econ: EconSettings,
    accent: discord.Color | None,
    drawn: list[int],
    bets: list[tuple[int, str, int, int]],
    pot_after: int,
    *,
    name_fn: NameFn,
) -> tuple[list[discord.Embed], discord.Embed]:
    frames = [casino_embeds.build_keno_tumble_embed(econ, accent)]
    return frames, casino_embeds.build_keno_result_embed(
        econ, drawn, bets, pot_after=pot_after, name_fn=name_fn
    )


_KENO_UI = _WindowUI(
    key="keno",
    enabled_attr="keno_enabled",
    resolve_label="Draw",
    resolve_emoji="🔢",
    already_open_note="You already have a ticket open — finish it first.",
    live_player_round=svc.live_keno_player_round,
    get_round=svc.get_keno_round,
    open_round=svc.open_keno_round,
    open_rounds=svc.open_keno_rounds,
    set_message=svc.set_keno_message,
    round_bets=svc.keno_bets,
    settle=svc.settle_keno_round,
    void=svc.void_keno_round,
    draw=logic.draw_keno,
    describe_bet=lambda b: logic.describe_keno_ticket(
        json.loads(str(b["spots"]))
    ),
    annotate_bet=lambda b, drawn: logic.describe_keno_result(
        json.loads(str(b["spots"])), list(drawn), int(b["payout"])
    ),
    round_embed=casino_embeds.build_keno_round_embed,
    build_view=build_keno_view,
    next_view=KenoNextView,
    build_show=_keno_show,
    frame_sleep=1.5,
)

# Every windowed game. The boot and maintenance sweeps iterate THIS —
# results keyed by ui.key, never zipped positionally — so a game absent
# from a sweep (stuck stakes after a restart) or a cross-game id mixup
# (row ids collide across tables) cannot happen by omission or reorder.
_WINDOW_UIS = (_ROULETTE_UI, _DERBY_UI, _BACCARAT_UI, _DICE_UI, _KENO_UI)
# Resolve-button dispatch: the custom_id carries the game key, and the
# button's own template only matches these five, so a stale id for a game
# that no longer exists never reaches the lookup.
_WINDOW_UIS_BY_KEY = {ui.key: ui for ui in _WINDOW_UIS}


class CasinoCog(PoolsMixin, commands.Cog, name="CasinoCog"):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.ctx = bot.ctx
        self._bj_locks: dict[int, asyncio.Lock] = {}
        self._war_locks: dict[int, asyncio.Lock] = {}
        # Webhook handles for the private rounds' ephemeral messages, keyed
        # (game key, round id) since the five tables' row ids can collide.
        # The same shape as _bj_followups: an ephemeral is editable only
        # through its own interaction's webhook, and only until Discord
        # expires the token (~15 min), so these are best-effort render
        # handles — never anything money depends on.
        self._window_followups: dict[
            tuple[str, int], tuple[discord.Webhook, int, float]
        ] = {}
        # Pools repaints are coalesced per guild: a stake moves the
        # implied odds, and each repaint re-renders and re-uploads a PNG.
        self._pools_repaints: dict[int, asyncio.Task] = {}
        self._pools_last_paint: dict[int, float] = {}
        self.hub_panel = StickyPanel(
            "casino hub",
            bot,
            load_ids=self._panel_ids,
            save_ids=self._save_panel_ids,
            build=self._build_hub_panel,
            # The casino buries its own panel: round results, big-win
            # broadcasts and jackpot celebrations all land in the hub channel.
            # Without this a round could settle and leave the hub -- the only
            # entry point -- stranded above the result with nobody typing.
            restick_on_bot=True,
        )
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
        self.bot.add_view(KenoNextView())
        self.bot.add_view(PoolsPanelView())
        self.bot.add_dynamic_items(
            BlackjackActionButton, RouletteBetButton, DerbyBetButton,
            BaccaratBetButton, DiceBetButton, KenoTierButton,
            WarActionButton, PlayAgainButton, RoundResolveButton,
        )
        self._boot_task = asyncio.create_task(self._boot())
        self.maintenance.start()

    async def cog_unload(self) -> None:
        self.maintenance.cancel()
        if self._boot_task is not None:
            self._boot_task.cancel()
        self.hub_panel.cancel_all()
        for task_map in (self._hub_repaints, self._pools_repaints):
            for task in task_map.values():
                task.cancel()
            task_map.clear()
        self._window_followups.clear()

    async def _boot(self) -> None:
        """Post-restart recovery: refund orphaned hands and rounds, make
        sure every configured guild has its hub panel.

        Rounds are refunded rather than resolved for exactly the reason
        hands are: a private round lives in an ephemeral message, and a
        restart kills the webhook token that message is editable through,
        so nothing could ever show the player the result. Settling anyway
        would move money against an outcome only the ledger ever sees.
        """
        await self.bot.wait_until_ready()

        def _sweep() -> tuple[list[sqlite3.Row], dict[str, dict[int, int]]]:
            with self.ctx.open_db() as conn:
                return (
                    svc.refund_live_blackjack_hands(conn)
                    + svc.refund_live_war_hands(conn),
                    svc.refund_live_rounds(conn),
                )

        try:
            swept, rounds_by_game = await asyncio.to_thread(_sweep)
        except Exception:
            log.exception("casino boot sweep failed")
            return
        if swept:
            # The hands' ephemeral messages died with their webhook tokens;
            # the register feed's casino_refund entry is the player-facing
            # notice, and stale buttons answer "already finished".
            log.info("casino boot sweep refunded %d live hand(s)", len(swept))
        for game, totals in rounds_by_game.items():
            log.info(
                "casino boot sweep refunded %d %s round stake(s), %d coins",
                len(totals), game, sum(totals.values()),
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
            list[int], list[int], dict[str, list[int]], dict[int, int]
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
                # Private rounds have no timer of their own — this IS their
                # auto-resolve. A player who bet and wandered off has a
                # stake already debited, so the round resolves once it
                # passes its abandonment TTL (round_idle_seconds, stamped
                # into closes_at at open) rather than sitting forever.
                overdue_by_game = {
                    ui.key: [
                        int(r["id"])
                        for r in ui.open_rounds(conn)
                        if float(r["closes_at"]) <= now
                    ]
                    for ui in _WINDOW_UIS
                }
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
                return stale, stale_wars, overdue_by_game, pots

        try:
            stale, stale_wars, overdue_by_game, pots = (
                await asyncio.to_thread(_scan)
            )
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
        for ui in _WINDOW_UIS:
            for round_id in overdue_by_game[ui.key]:
                try:
                    # show=False: a pile of abandoned rounds must not
                    # serialize seconds of cosmetic frames per round inside
                    # the sweep — and nobody is watching an abandoned one
                    # anyway. The verdict still lands on the card if the
                    # player's webhook token is somehow still alive.
                    await self._resolve_window(ui, round_id, show=False)
                except Exception:
                    log.exception(
                        "casino overdue %s resolve failed for %s",
                        ui.key, round_id,
                    )
        # Pools rides this same minute tick rather than owning a timer: its
        # round is a day long, so a minute of lateness at the roll is
        # invisible, and the outcome is recomputed from the ledger anyway.
        await self.pools_tick()

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

    # ── hub panel stickiness (core.sticky) ─────────────────────────────

    def _panel_ids(self, guild_id: int) -> tuple[int, int]:
        with self.ctx.open_db() as conn:
            settings = svc.load_casino_settings(conn, guild_id)
        return settings.panel_channel_id, settings.panel_message_id

    def _save_panel_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        with self.ctx.open_db() as conn:
            svc.save_casino_settings(
                conn, guild_id,
                {"panel_message_id": message_id, "panel_channel_id": channel_id},
            )

    async def _build_hub_panel(self, guild: discord.Guild) -> PanelContent:
        econ, settings, pot, casino_name, ticker, standings = await asyncio.to_thread(
            self._read_hub, guild.id
        )
        if pot is not None:
            self._last_pot[guild.id] = pot
        # The ticker and standings name past betters, who are exactly the
        # players a reader's client is least likely to have cached.
        named = [uid for uid, *_ in ticker]
        named += [s[0] for s in standings if s is not None]
        return PanelContent(
            embed=casino_embeds.build_hub_embed(
                econ, settings, await self._accent(guild), jackpot=pot,
                ticker=ticker, standings=standings, casino_name=casino_name,
                name_fn=await self._names(guild, named),
            ),
            # Per-guild copy of the hub view: disabled tables' buttons drop off
            # (the full view stays registered for stale-panel routing).
            view=build_hub_view(settings),
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Keep the hub panel — the casino's only entry point — at the bottom
        of its channel. Armed by bot messages too: with the games private,
        what buries it now is the occasional big-win broadcast and the other
        panels sharing the channel, not a stream of round results."""
        await self.hub_panel.on_message(message)

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

    async def _names(
        self,
        guild: discord.Guild | None,
        user_ids: list[int],
        *,
        guild_id: int | None = None,
    ) -> NameFn:
        """The resolver every card renders players through (todo #90).

        A ``<@id>`` in an embed is resolved by the reading client from its own
        cache, so it degrades to a bare id for anyone who hasn't seen that
        player — the norm for the hub ticker and for result cards the rest of
        the channel reads. Present members come from the member cache at no
        I/O cost; only the misses (players who have left) cost one batched
        read.

        ``guild_id`` overrides the id taken from ``guild``, and callers that
        can be handed ``guild=None`` must pass it: the ``known_users`` fallback
        is keyed on ``(guild_id, user_id)``, so defaulting to 0 there would
        match no rows and hand back the very ``<@id>`` this exists to remove —
        exactly when an uncached guild makes the table the *only* source of a
        name (see ``_auto_resolve_hand``).
        """
        return await build_name_fn(
            guild=guild,
            db_path=self.ctx.db_path,
            guild_id=(
                guild_id if guild_id is not None
                else (guild.id if guild is not None else 0)
            ),
            user_ids=user_ids,
        )

    # ── hub panel upkeep ───────────────────────────────────────────────

    def _read_hub(self, guild_id: int) -> tuple[
        EconSettings, svc.CasinoSettings, int | None, str,
        list[tuple[int, str, int, int]],
        tuple[svc.DailyStanding | None, svc.DailyStanding | None],
    ]:
        """Everything the hub panel renders from, in one connection."""
        with self.ctx.open_db() as conn:
            settings = svc.load_casino_settings(conn, guild_id)
            pot: int | None = None
            ticker: list[tuple[int, str, int, int]] = []
            standings: tuple[
                svc.DailyStanding | None, svc.DailyStanding | None
            ] = (None, None)
            if settings.jackpot_enabled and settings.slots_enabled:
                pot = svc.get_jackpot(conn, guild_id, seed=settings.jackpot_seed)
            if settings.channel_id:
                ticker = [
                    (int(r["user_id"]), str(r["game"]), int(r["stake"]),
                     int(r["payout"]))
                    for r in svc.recent_ticker(conn, guild_id)
                ]
                standings = svc.daily_standings(conn, guild_id)
            return (
                load_econ_settings(conn, guild_id),
                settings,
                pot,
                resolve_casino_name_conn(conn, guild_id),
                ticker,
                standings,
            )

    async def ensure_panel(self, guild: discord.Guild) -> None:
        """Post/refresh the hub panel in the configured casino channel.

        Edits in place when the panel is already in the right channel, so a
        repaint after a spin never moves it; posts fresh when it has moved or
        was never posted. Also the teardown path: an unset channel (or a
        disabled economy) deletes any panel we previously posted. Called at
        boot and by config changes; the sticky repost goes through
        ``hub_panel`` directly.
        """

        try:
            econ, settings, pot, casino_name, ticker, standings = (
                await asyncio.to_thread(self._read_hub, guild.id)
            )
        except Exception:
            log.exception("casino panel read failed for guild %s", guild.id)
            return
        if pot is not None:
            self._last_pot[guild.id] = pot

        self._casino_channels[guild.id] = settings.channel_id
        # Publish the panel-guild set so the on_message gate stays a dict
        # lookup rather than a DB read, as it was before the extraction.
        self.hub_panel.set_known_guilds(
            {gid for gid, ch in self._casino_channels.items() if ch}
        )
        if not settings.channel_id or not econ.enabled:
            # Teardown: an unset channel (or a disabled economy) removes the
            # panel and clears the stored ids.
            if settings.panel_message_id:
                await self.hub_panel.unpost(guild)
            return

        channel = guild.get_channel(settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        await self.hub_panel.place_or_refresh(guild, channel)

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_hub_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the hub's stored ids if its channel was deleted."""
        await self.hub_panel.on_channel_delete(channel)
        self._casino_channels.pop(channel.guild.id, None)

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
        name_fn = await self._names(guild, [interaction.user.id])
        final = casino_embeds.build_coinflip_embed(
            econ, interaction.user.id, side, landed, amount, result.payout,
            streak=result.streak, pot_after=result.pot_after, name_fn=name_fn,
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
                        await self._accent(guild), name_fn=name_fn,
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
        name_fn = await self._names(guild, [interaction.user.id])
        final = casino_embeds.build_slots_embed(
            econ, interaction.user.id, reels, amount, result.payout,
            result.label,
            jackpot_won=result.jackpot_won, streak=result.streak,
            pot_after=result.pot_after, casino_name=casino_name,
            name_fn=name_fn,
        )
        try:
            if logic.is_big_bet(amount, settings.max_bet):
                # Reels stop one at a time; the outcome is already banked.
                accent = await self._accent(guild)
                await self._respond_private(
                    interaction,
                    casino_embeds.build_slots_spin_embed(
                        econ, interaction.user.id, amount, (None, None, None),
                        accent, casino_name=casino_name, name_fn=name_fn,
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
                            accent, casino_name=casino_name, name_fn=name_fn,
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
                        casino_name=casino_name, name_fn=name_fn,
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
            name_fn=await self._names(guild, [uid]),
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
        name_fn = await self._names(guild, [uid])
        embed = casino_embeds.build_blackjack_embed(
            econ, uid, step.player or [], step.dealer or [],
            step.stake, accent,
            doubled=step.doubled, outcome=step.outcome, payout=step.payout,
            streak=step.streak, pot_after=step.pot_after, name_fn=name_fn,
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
                        doubled=step.doubled, name_fn=name_fn,
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

    async def _auto_resolve_hand(
        self,
        hand_id: int,
        *,
        locks: dict[int, asyncio.Lock],
        followups: dict[int, tuple[discord.Webhook, int, float]],
        get_hand: Callable[..., sqlite3.Row | None],
        resolve_idle: Callable[..., object],
        render: Callable[..., tuple[discord.Embed, Callable[[], discord.ui.View]]],
        footer: str,
    ) -> None:
        """The idle sweep's resolve for a live-hand game (blackjack stand,
        war default-war) — the shared tail: claim/settle in the service,
        best-effort ephemeral repaint through the stored webhook handle,
        hub repaint, big-win broadcast.

        ``resolve_idle`` returns None when the hand settled concurrently
        (a button press holding the claim), so this can never render an
        outcome the settle didn't pay. ``render(row, step, econ, accent,
        name_fn)`` returns the result embed and a Play-Again view factory (a
        fresh view per message it lands on).
        """
        lock = locks.setdefault(hand_id, asyncio.Lock())
        async with lock:

            def _resolve():
                with self.ctx.open_db() as conn:
                    row = get_hand(conn, hand_id)
                    if row is None:
                        return None
                    step = resolve_idle(conn, hand_id)
                    if step is None:
                        return None
                    gid = int(row["guild_id"])
                    return (
                        row, step, load_econ_settings(conn, gid),
                        svc.load_casino_settings(conn, gid),
                    )

            result = await asyncio.to_thread(_resolve)
        locks.pop(hand_id, None)
        handle = followups.pop(hand_id, None)
        if result is None:
            return
        row, step, econ, settings = result
        guild = self.bot.get_guild(int(row["guild_id"]))
        embed, make_view = render(
            row, step, econ, await self._accent(guild),
            # guild_id off the row, not off `guild`: get_guild can miss during
            # a boot/outage window, and that is the case where known_users is
            # the only thing that can name this player.
            await self._names(
                guild, [int(row["user_id"])], guild_id=int(row["guild_id"])
            ),
        )
        embed.set_footer(text=footer)
        if handle is not None:
            # Best-effort: the hand's ephemeral message is reachable only
            # through its interaction webhook, and only while the token
            # lives. The settle above never depended on this edit.
            webhook, message_id, _ = handle
            try:
                await webhook.edit_message(
                    message_id, embed=embed, view=make_view()
                )
            except discord.HTTPException:
                pass
        self._schedule_hub_repaint(int(row["guild_id"]))
        payout = int(getattr(step, "payout", 0))
        if settings.broadcast_min_payout > 0 and (
            payout >= settings.broadcast_min_payout
        ):
            channel = self.bot.get_channel(int(row["channel_id"]))
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        embed=embed,
                        view=make_view(),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    log.warning(
                        "casino big-win broadcast failed in #%s", channel.id
                    )

    async def _auto_stand(self, hand_id: int) -> None:
        """The idle sweep's stand — same settle path as a button press."""

        def render(
            row: sqlite3.Row,
            step: svc.BlackjackStep,
            econ: EconSettings,
            accent: discord.Color | None,
            name_fn: NameFn,
        ) -> tuple[discord.Embed, Callable[[], discord.ui.View]]:
            embed = casino_embeds.build_blackjack_embed(
                econ, int(row["user_id"]), step.player or [],
                step.dealer or [], step.stake, accent,
                doubled=step.doubled, outcome=step.outcome,
                payout=step.payout, streak=step.streak,
                pot_after=step.pot_after, name_fn=name_fn,
            )
            base_stake = step.stake // 2 if step.doubled else step.stake
            return embed, lambda: play_again_view("blackjack", base_stake)

        await self._auto_resolve_hand(
            hand_id,
            locks=self._bj_locks,
            followups=self._bj_followups,
            get_hand=svc.get_blackjack_hand,
            resolve_idle=svc.stand_idle_blackjack_hand,
            render=render,
            footer="Stood automatically — the dealer waits for no one.",
        )

    # ── war (instant, with the tie's rare live decision) ───────────────

    def _war_embed(
        self,
        econ: EconSettings,
        user_id: int,
        step: svc.WarStep,
        accent: discord.Color | None,
        name_fn: NameFn,
    ) -> discord.Embed:
        return casino_embeds.build_war_embed(
            econ, user_id, step.player, step.dealer, step.stake, accent,
            war_player=step.war_player, war_dealer=step.war_dealer,
            outcome=step.outcome, payout=step.payout, streak=step.streak,
            pot_after=step.pot_after, name_fn=name_fn,
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
        embed = self._war_embed(
            econ, uid, step, await self._accent(guild),
            await self._names(guild, [uid]),
        )
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
        embed = self._war_embed(
            econ, uid, step, await self._accent(guild),
            await self._names(guild, [uid]),
        )
        try:
            await interaction.response.edit_message(
                embed=embed, view=play_again_view("war", step.original)
            )
        except discord.HTTPException:
            pass
        self._war_followups.pop(hand_id, None)
        await self._after_instant(
            interaction, payout=step.payout,
            threshold=settings.broadcast_min_payout, embed=embed,
            view=play_again_view("war", step.original),
        )
        return True

    async def _auto_war(self, hand_id: int) -> None:
        """The idle sweep's resolve — war when affordable, else retreat."""

        def render(
            row: sqlite3.Row,
            step: svc.WarStep,
            econ: EconSettings,
            accent: discord.Color | None,
            name_fn: NameFn,
        ) -> tuple[discord.Embed, Callable[[], discord.ui.View]]:
            embed = self._war_embed(
                econ, int(row["user_id"]), step, accent, name_fn
            )
            return embed, lambda: play_again_view("war", step.original)

        await self._auto_resolve_hand(
            hand_id,
            locks=self._war_locks,
            followups=self._war_followups,
            get_hand=svc.get_war_hand,
            resolve_idle=svc.resolve_idle_war_hand,
            render=render,
            footer="Resolved automatically — fortune favors the decisive.",
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

    async def open_keno(self, interaction: discord.Interaction) -> None:
        await self._open_window(interaction, _KENO_UI)

    async def _open_window(
        self, interaction: discord.Interaction, ui: _WindowUI
    ) -> None:
        """Open this player's own private round.

        Since migration 158 a round belongs to one player and renders in
        their ephemeral message: nobody else's channel scrolls and nobody
        else's buttons move. There is no countdown — they bet as many
        times as they like and press the game's resolve button when ready.
        The only clock is the abandonment TTL the maintenance sweep uses
        (``round_idle_seconds``), because the stake is debited at bet time.
        """
        guild = interaction.guild
        channel_id = interaction.channel_id
        if guild is None or channel_id is None:
            return
        uid = interaction.user.id

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
                if ui.live_player_round(conn, guild.id, uid) is not None:
                    return _RoundOpen(err=ui.already_open_note)
                round_id = ui.open_round(
                    conn, guild.id, channel_id, settings.round_idle_seconds,
                    user_id=uid,
                )
                if round_id is None:
                    return _RoundOpen(err=ui.already_open_note)
                rnd = ui.get_round(conn, round_id)
                assert rnd is not None
                return _RoundOpen(
                    econ=econ, round_id=round_id,
                    closes_at=float(rnd["closes_at"]),
                )

        try:
            result = await asyncio.to_thread(_open)
        except sqlite3.IntegrityError:
            # Two presses raced the pre-check and the partial unique index
            # caught the second — their first round is the live one.
            await safe_ephemeral(interaction, f"❌ {ui.already_open_note}")
            return
        if result.err is not None:
            await safe_ephemeral(interaction, f"❌ {result.err}")
            return
        if result.econ is None:
            return
        round_id = result.round_id
        embed = ui.round_embed(
            result.econ, result.closes_at, [], await self._accent(guild),
            name_fn=await self._names(guild, []),
        )
        try:
            await interaction.response.send_message(
                embed=embed,
                view=ui.build_view(round_id),
                allowed_mentions=discord.AllowedMentions.none(),
                ephemeral=True,
            )
            message = await interaction.original_response()
        except discord.HTTPException:
            # No message means the player can neither bet nor resolve, so
            # the round would sit until the idle sweep. Kill it now.
            await self._void_window(ui, round_id)
            await safe_ephemeral(
                interaction, "❌ Couldn't open the round — try again."
            )
            return

        # The only handle back into an ephemeral message is its
        # interaction's webhook, whose token Discord expires after ~15
        # minutes — which is why the idle TTL sits comfortably under it.
        self._window_followups[(ui.key, round_id)] = (
            interaction.followup, message.id, time.time()
        )

        def _bind() -> None:
            with self.ctx.open_db() as conn:
                ui.set_message(conn, round_id, message.id)

        await asyncio.to_thread(_bind)

    async def _finish_window_bet(
        self,
        interaction: discord.Interaction,
        ui: _WindowUI,
        guild: discord.Guild,
        round_id: int,
        amount: int,
        err: str | None,
        desc: str,
        *,
        verb: str = "Bet placed",
    ) -> None:
        """Shared tail of every private-round bet: error apology, last-bet
        memory, confirmation, board repaint.

        The repaint is immediate rather than debounced now. The 2s
        coalescing window existed because a communal round could take a
        burst of bets from several people at once; a private round has one
        bettor, so debouncing only ever made their own board lag behind
        their own click.
        """
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        self._last_bets[(guild.id, interaction.user.id, ui.key)] = amount
        await safe_ephemeral(interaction, f"✅ {verb}: {desc} for {amount:,}.")
        await self._repaint_window(ui, guild, round_id)

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

        await self._finish_window_bet(
            interaction, _ROULETTE_UI, guild, round_id, amount,
            await asyncio.to_thread(_bet),
            logic.describe_bet(bet_type, selection),
        )

    async def _repaint_window(
        self, ui: _WindowUI, guild: discord.Guild, round_id: int
    ) -> None:
        """Repaint the player's own board after a bet lands."""
        try:

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
                return  # already resolved — the result edit owns the message
            bets = result.bets or []
            embed = ui.round_embed(
                result.econ, float(rnd["closes_at"]), bets,
                await self._accent(guild),
                name_fn=await self._names(guild, [b[0] for b in bets]),
            )
            await self._edit_window_message(
                ui, round_id, embed=embed, view=ui.build_view(round_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s repaint failed for round %s", ui.key, round_id)

    async def _edit_window_message(
        self,
        ui: _WindowUI,
        round_id: int,
        *,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        """Best-effort edit of a round's ephemeral message.

        An ephemeral message is reachable only through the webhook of the
        interaction that created it, and only while that token lives — so
        this is deliberately best-effort. Nothing that moves money may
        depend on it: the settle has always already happened by the time
        the result gets rendered, and a player whose token died sees the
        payout in their balance and the ticker rather than on the card.
        """
        handle = self._window_followups.get((ui.key, round_id))
        if handle is None:
            return
        webhook, message_id, _ = handle
        try:
            await webhook.edit_message(message_id, embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def resolve_round(
        self, interaction: discord.Interaction, game: str, round_id: int
    ) -> None:
        """The resolve button's entry point — the player says go.

        Refuses a round with nothing staked (an empty resolve would burn
        the round and leave them re-opening it to bet), and refuses a
        round that is not theirs. The ephemeral message is private, so a
        foreign press means a stale custom_id rather than an attack — but
        the ownership check is what makes that a polite no instead of
        someone else's wheel spinning.
        """
        ui = _WINDOW_UIS_BY_KEY.get(game)
        if ui is None:
            return
        uid = interaction.user.id

        def _check() -> str | None:
            with self.ctx.open_db() as conn:
                rnd = ui.get_round(conn, round_id)
                if rnd is None or str(rnd["status"]) != "open":
                    return "That round is already finished."
                if int(rnd["user_id"]) != uid:
                    return "That isn't your round."
                if not ui.round_bets(conn, round_id):
                    return "Place a bet first."
                return None

        err = await asyncio.to_thread(_check)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        # Ack before the show: the frames run on the webhook, and Discord
        # kills the interaction if nothing responds within 3s.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass
        await self._resolve_window(ui, round_id)

    async def _resolve_window(
        self, ui: _WindowUI, round_id: int, *, show: bool = True
    ) -> None:
        """Settle one private round and render the result into its own
        ephemeral message. ``show=False`` (the idle sweep) skips the
        cosmetic frames and jumps straight to the verdict.

        No lock needed: the status='open' claim inside the settle is the
        mutual exclusion — the player's resolve press, the idle sweep and
        a void can all reach a round, and only the first claim pays.
        """

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
                cs = svc.load_casino_settings(conn, gid)
                pot_after = 0
                if any(int(b["payout"]) == 0 for b in bets) and cs.jackpot_enabled:
                    pot_after = svc.get_jackpot(conn, gid)
                return rnd, result, bets, econ, pot_after, cs

        settled = await asyncio.to_thread(_settle)
        if settled is None:
            return
        rnd, result, bet_rows, econ, pot_after, settings = settled
        annotate = ui.annotate_bet
        describe = (
            ui.describe_bet if annotate is None
            else (lambda b: annotate(b, result))
        )
        bets = [
            (int(b["user_id"]), describe(b), int(b["amount"]),
             int(b["payout"]))
            for b in bet_rows
        ]
        guild = self.bot.get_guild(int(rnd["guild_id"]))
        frames, result_embed = ui.build_show(
            econ, await self._accent(guild), result, bets, pot_after,
            name_fn=await self._names(guild, [b[0] for b in bets]),
        )
        # The show plays inside the player's own ephemeral message. The
        # money settled above, so a token that dies mid-frames leaves a
        # stale card, never a wrong balance.
        if show:
            for frame in frames:
                await self._edit_window_message(
                    ui, round_id, embed=frame, view=None
                )
                await asyncio.sleep(ui.frame_sleep)
        await self._edit_window_message(
            ui, round_id, embed=result_embed, view=ui.next_view()
        )
        self._window_followups.pop((ui.key, round_id), None)
        if guild is not None:
            self._schedule_hub_repaint(guild.id)
        await self._broadcast_window_win(
            ui, rnd, settings, bets, result_embed
        )

    async def _broadcast_window_win(
        self,
        ui: _WindowUI,
        rnd: sqlite3.Row,
        settings: svc.CasinoSettings,
        bets: list[tuple[int, str, int, int]],
        result_embed: discord.Embed,
    ) -> None:
        """Repost the result publicly when it clears the big-win bar.

        Private rounds get the same treatment the instant games have had
        since 2026-07-24 — nothing in the channel unless a payout is worth
        advertising. Every play still shows on the hub's floor ticker, so
        going quiet is not the same as going invisible.
        """
        threshold = settings.broadcast_min_payout
        if threshold <= 0:
            return
        best = max((b[3] for b in bets), default=0)
        if best < threshold:
            return
        channel = self.bot.get_channel(int(rnd["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(
                embed=result_embed,
                view=ui.next_view(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            log.warning(
                "%s big-win broadcast failed in #%s", ui.key, channel.id
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

        await self._finish_window_bet(
            interaction, _DERBY_UI, guild, round_id, amount,
            await asyncio.to_thread(_bet), logic.describe_runner(runner),
        )

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

        await self._finish_window_bet(
            interaction, _BACCARAT_UI, guild, round_id, amount,
            await asyncio.to_thread(_bet), logic.describe_baccarat_side(side),
        )

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

        await self._finish_window_bet(
            interaction, _DICE_UI, guild, round_id, amount,
            await asyncio.to_thread(_bet), logic.describe_sicbo_bet(bet_type),
        )

    # ── keno-only glue (everything else rides _WindowUI above) ─────────

    async def open_keno_ticket_modal(
        self, interaction: discord.Interaction, round_id: int, spots: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        label, last = await self._modal_context(
            guild.id, interaction.user.id, "keno"
        )
        await interaction.response.send_modal(
            KenoTicketModal(
                round_id, spots, limits_label=label, default_amount=last
            )
        )

    async def place_keno_ticket(
        self,
        interaction: discord.Interaction,
        round_id: int,
        spots: int,
        amount: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        def _bet() -> str | list[int]:
            with self.ctx.open_db() as conn:
                return svc.place_keno_ticket(conn, round_id, uid, spots, amount)

        result = await asyncio.to_thread(_bet)
        err = result if isinstance(result, str) else None
        desc = "" if isinstance(result, str) else logic.describe_keno_ticket(result)
        await self._finish_window_bet(
            interaction, _KENO_UI, guild, round_id, amount, err, desc,
            verb="Ticket punched",
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(CasinoCog(bot))
