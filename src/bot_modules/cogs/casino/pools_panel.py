"""Pools — the daily market's Discord surface (plan doc Stage 2).

Mixed into ``CasinoCog`` rather than added to cog.py, which is large
enough. Everything here is glue: the decisions live in ``pools_service``
(what to settle, what to open, the line) and ``pools_logic`` (the pool
split), and are tested there.

Two things are specific to a round lasting a day rather than 45 seconds:

* **The panel repaint is coalesced.** Every stake changes the implied odds,
  and each repaint re-renders a PNG and re-uploads it. At 13-18 bettors a
  day the cost is nothing, but the throttle is what stops a busy day from
  turning into a render queue.
* **Settlement is driven by the day rolling over, not by betting closing.**
  The market shuts in the early evening and settles after midnight, so the
  round sits open with betting closed for hours in between.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time

import discord

from bot_modules.core.app_context import AppContext, Bot
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.services import casino_service as svc
from bot_modules.services import pools_charts, pools_logic, pools_service
from bot_modules.services.economy_service import load_econ_settings

from . import embeds as E
from .views import PoolsBetModal, PoolsPanelView, safe_ephemeral

log = logging.getLogger(__name__)

# Minimum seconds between panel repaints. A stake lands, the odds move, and
# the next repaint folds in every stake that arrived meanwhile.
_REPAINT_EVERY = 20.0
# Days of history on the instrument chart.
_CHART_DAYS = 14


class PoolsMixin:
    """Pools panel, buttons and day-roll sweep. Mixed into CasinoCog.

    The attributes below are provided by the cog this is mixed into;
    declaring them keeps the type checker honest about the contract rather
    than letting the mixin quietly depend on whatever happens to exist.
    """

    bot: Bot
    ctx: AppContext
    # Populated by CasinoCog.__init__.
    _pools_repaints: dict[int, asyncio.Task]
    _pools_last_paint: dict[int, float]

    async def _accent(
        self, guild: discord.Guild | None
    ) -> discord.Color | None:
        """Provided by CasinoCog; declared so this file type-checks alone."""
        raise NotImplementedError

    # ── member-facing ──────────────────────────────────────────────────

    async def open_pools_bet(
        self, interaction: discord.Interaction, side: str
    ) -> None:
        """Resolve the live market and open the amount modal."""
        guild = interaction.guild
        if guild is None:
            return
        state = await asyncio.to_thread(self._read_pools_round, guild.id)
        if state is None:
            await safe_ephemeral(
                interaction, "❌ There's no market open right now."
            )
            return
        rnd, econ, settings = state
        if float(rnd["closes_at"]) <= time.time():
            await safe_ephemeral(
                interaction,
                "❌ Betting on today's market has closed — it settles when "
                "the day rolls over.",
            )
            return
        limits = f"Your bet ({settings.min_bet}–{settings.max_bet})"
        await interaction.response.send_modal(
            PoolsBetModal(side, limits_label=limits)
        )

    async def place_pools_bet(
        self, interaction: discord.Interaction, side: str, amount: int
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        user_id = interaction.user.id

        def _place() -> tuple[str | None, int | None]:
            with self.ctx.open_db() as conn:
                rnd = svc.live_pools_round(
                    conn, svc.pools_channel(svc.load_casino_settings(conn, guild.id))
                )
                if rnd is None:
                    return "There's no market open right now.", None
                rid = int(rnd["id"])
                return svc.place_pools_bet(conn, rid, user_id, side, amount), rid

        err, round_id = await asyncio.to_thread(_place)
        if err is not None:
            await safe_ephemeral(interaction, f"❌ {err}")
            return
        await safe_ephemeral(
            interaction,
            f"✅ **{amount:,}** on **{pools_logic.describe_side(side)}**. "
            "Your return depends on how the pool ends up split — the panel "
            "shows the live odds.",
        )
        if round_id is not None:
            self._schedule_pools_repaint(guild)

    # ── panel ──────────────────────────────────────────────────────────

    def _read_pools_round(self, guild_id: int):
        with self.ctx.open_db() as conn:
            settings = svc.load_casino_settings(conn, guild_id)
            if not settings.pools_enabled:
                return None
            rnd = svc.live_pools_round(conn, svc.pools_channel(settings))
            if rnd is None:
                return None
            return rnd, load_econ_settings(conn, guild_id), settings

    def _schedule_pools_repaint(self, guild: discord.Guild) -> None:
        """Coalescing debounce — a pending repaint absorbs later stakes."""
        if guild.id in self._pools_repaints:
            return
        delay = max(
            0.0,
            _REPAINT_EVERY - (time.time() - self._pools_last_paint.get(guild.id, 0.0)),
        )

        async def _later() -> None:
            try:
                await asyncio.sleep(delay)
                await self.render_pools_panel(guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pools repaint failed for guild %s", guild.id)
            finally:
                self._pools_repaints.pop(guild.id, None)

        self._pools_repaints[guild.id] = asyncio.create_task(_later())

    async def render_pools_panel(self, guild: discord.Guild) -> None:
        """Post or edit the standing market panel."""
        state = await asyncio.to_thread(self._read_pools_panel, guild.id)
        if state is None:
            return
        rnd, econ, split, points = state
        channel = guild.get_channel(int(rnd["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        accent = await self._accent(guild)
        line = float(rnd["line"])
        png = await asyncio.to_thread(
            pools_charts.render_market_chart, points, line,
            pool_total=split.total,
            accent=f"#{accent.value:06x}" if accent else pools_charts._DEFAULT_ACCENT,
        )
        embed = E.build_pools_panel_embed(
            econ, line, split, float(rnd["closes_at"]), str(rnd["local_day"]),
            accent, closed=float(rnd["closes_at"]) <= time.time(),
        )
        self._pools_last_paint[guild.id] = time.time()
        file = discord.File(io.BytesIO(png), filename=pools_charts.MARKET_FILENAME)
        message_id = int(rnd["message_id"])
        if message_id:
            with contextlib.suppress(discord.HTTPException):
                msg = await channel.fetch_message(message_id)
                await msg.edit(embed=embed, attachments=[file], view=PoolsPanelView())
                return
            file = discord.File(
                io.BytesIO(png), filename=pools_charts.MARKET_FILENAME
            )
        try:
            sent = await channel.send(
                embed=embed, file=file, view=PoolsPanelView()
            )
        except discord.HTTPException:
            log.exception("pools panel post failed for guild %s", guild.id)
            return
        await asyncio.to_thread(self._save_pools_message, int(rnd["id"]), sent.id)

    def _read_pools_panel(self, guild_id: int):
        with self.ctx.open_db() as conn:
            settings = svc.load_casino_settings(conn, guild_id)
            if not settings.pools_enabled:
                return None
            rnd = svc.live_pools_round(conn, svc.pools_channel(settings))
            if rnd is None:
                return None
            bets = [dict(b) for b in svc.pools_bets(conn, int(rnd["id"]))]
            return (
                rnd,
                load_econ_settings(conn, guild_id),
                pools_logic.pool_split(bets),
                pools_logic.probability_series(
                    bets, float(rnd["opened_at"]), float(rnd["closes_at"])
                ),
            )

    def _save_pools_message(self, round_id: int, message_id: int) -> None:
        with self.ctx.open_db() as conn:
            svc.set_pools_message(conn, round_id, message_id)

    # ── the day roll ───────────────────────────────────────────────────

    async def pools_tick(self) -> None:
        """Settle finished days, then open today's. Called from maintenance.

        Settling first is not incidental: today's line is a median of the
        completed days before it, so opening first would compute it against
        a day that has not finished.
        """
        for guild in self.bot.guilds:
            try:
                await self._pools_tick_guild(guild)
            except Exception:
                log.exception("pools tick failed for guild %s", guild.id)

    async def _pools_tick_guild(self, guild: discord.Guild) -> None:
        plan = await asyncio.to_thread(self._plan_pools, guild.id)
        if plan is None:
            return
        tick, channel_id = plan
        for job in tick.settle:
            await self._settle_pools(guild, channel_id, job)
        if tick.open is not None:
            opened = await asyncio.to_thread(
                self._open_pools, guild.id, channel_id, tick.open
            )
            if opened:
                await self.render_pools_panel(guild)

    def _plan_pools(self, guild_id: int):
        with self.ctx.open_db() as conn:
            settings = svc.load_casino_settings(conn, guild_id)
            if not settings.pools_enabled or not svc.pools_channel(settings):
                return None
            tz = get_tz_offset_hours(conn, guild_id)
            return (
                pools_service.plan_tick(
                    conn, guild_id, tz_offset_hours=tz,
                    close_hour=settings.pools_close_hour, now=time.time(),
                ),
                svc.pools_channel(settings),
            )

    def _open_pools(self, guild_id: int, channel_id: int, job) -> bool:
        with self.ctx.open_db() as conn:
            return svc.open_pools_round(
                conn, guild_id, channel_id, job.day, job.line, job.closes_at
            ) is not None

    async def _settle_pools(
        self, guild: discord.Guild, channel_id: int, job
    ) -> None:
        def _run():
            with self.ctx.open_db() as conn:
                res = svc.settle_pools_round(conn, job.round_id, job.result)
                econ = load_econ_settings(conn, guild.id)
                tz = get_tz_offset_hours(conn, guild.id)
                days = pools_service.candles(
                    conn, guild.id, tz_offset_hours=tz, days=_CHART_DAYS
                )
                return res, econ, days

        res, econ, days = await asyncio.to_thread(_run)
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        accent = await self._accent(guild)
        await self._retire_pools_panel(guild, channel, job.round_id)

        if res.voided:
            if res.refunds:
                await channel.send(
                    embed=E.build_pools_void_embed(
                        job.day, sum(res.refunds.values()), accent
                    )
                )
            return
        if res.bets is None:
            return  # someone else claimed it

        payouts = sorted(
            (
                (int(b["user_id"]), int(b["amount"]), int(b["payout"]))
                for b in res.bets
            ),
            key=lambda r: -r[2],
        )
        winners = [uid for uid, _, payout in payouts if payout]
        png = await asyncio.to_thread(
            pools_charts.render_instrument_chart, days, float(job.line),
            accent=f"#{accent.value:06x}" if accent else pools_charts._DEFAULT_ACCENT,
            currency=econ.currency_plural,
        )
        embed = E.build_pools_result_embed(
            econ, job.day, job.result, float(job.line),
            pools_logic.winning_side(job.result, float(job.line)),
            payouts, res.takeout, accent,
        )
        with contextlib.suppress(discord.HTTPException):
            await channel.send(
                embed=embed,
                file=discord.File(
                    io.BytesIO(png), filename=pools_charts.INSTRUMENT_FILENAME
                ),
                # Winners are pinged; nobody else is. Allow-listed to the
                # exact ids so an @everyone can never ride in on a payout
                # table built from user input.
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False,
                    users=[discord.Object(id=u) for u in winners],
                ),
            )

    async def _retire_pools_panel(
        self, guild: discord.Guild, channel: discord.TextChannel, round_id: int
    ) -> None:
        """Drop the buttons off yesterday's panel so it can't be clicked."""
        message_id = await asyncio.to_thread(self._pools_message_id, round_id)
        if not message_id:
            return
        with contextlib.suppress(discord.HTTPException):
            msg = await channel.fetch_message(message_id)
            await msg.edit(view=None)

    def _pools_message_id(self, round_id: int) -> int:
        with self.ctx.open_db() as conn:
            rnd = svc.get_pools_round(conn, round_id)
            return int(rnd["message_id"]) if rnd else 0

