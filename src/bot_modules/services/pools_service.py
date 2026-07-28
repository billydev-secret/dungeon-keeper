"""Pools — the economy metric, and the round lifecycle.

See docs/plans/casino-classics-and-prediction-market.md Stage 2. Money and
persistence live here; the maths lives in ``pools_logic``.

**The metric.** A day's "net change in the economy" is the change in total
Petals in circulation over that guild-local day. Circulation is exactly the
running sum of ``econ_ledger.amount`` — verified against prod, where the
ledger's cumulative total reconciles to ``SUM(econ_wallets.balance)`` with
no gap — so the day's net change is simply the sum of the amounts booked to
it. ``mint``/``burn``/``hold`` are the decomposition of that one number, not
three independent measurements, which is why the settlement value and the
chart's candle body cannot drift apart: they are the same sum.

**Session-day attribution.** A stake and its payout are written at different
moments. A blackjack hand dealt at 23:59 and stood at 00:00:30 would, under
naive timestamp bucketing, push its whole stake into one day's burn and its
payout into the next — moving the metric by the full stake at a cost of
roughly nothing. So both halves of a casino session are attributed to the
day the round or hand was *created*. The linkage comes from the ledger meta
(``round_id`` / ``hand_id``), which ``take_stake`` and ``pay_out`` both
write.

Rows with no linkage — anything before that meta existed, and non-casino
kinds — fall back to their own timestamp. PvP wager escrow
(``wager_stake``/``wager_payout``) can straddle midnight the same way and is
NOT yet attributed; it nets to zero across the pair, so it shifts value
between adjacent days rather than creating it, and it is a rounding error
next to casino flow. Worth revisiting if duels ever carry real volume.

**Pools' own rows are excluded** from the metric. Market stakes and payouts
route through ``take_stake``/``pay_out`` like every other game, so without
the exclusion a bigger pool would mechanically drag the number it is
betting on — bet under, inflate the pool, and the burned takeout alone
moves the metric your way.
"""

from __future__ import annotations

import json
import sqlite3
from typing import NamedTuple

from bot_modules.services import pools_logic
from bot_modules.services.casino_service import (
    ALL_ROUND_TABLES,
    BLACKJACK_HANDS,
    POOLS_TABLES,
    WAR_HANDS,
)

# Ledger kinds that move currency sideways rather than minting it. Casino
# payouts belong here: a returned bet is the member's own stake coming
# back. Canonical — scripts/economy_tuning_report.py imports these so the
# offline report and the live line cannot diverge.
NON_FAUCET_KINDS = (
    "transfer_in", "wager_payout", "wager_refund", "casino_payout",
    "casino_refund",
)
# Kinds that don't actually destroy currency (transfers/wagers move it
# sideways; most of a casino stake is handed straight back, so the real
# casino burn is the hold, booked separately).
BURN_KINDS_EXCLUDED = ("transfer_out", "wager_stake", "casino_stake")

CASINO_KINDS = ("casino_stake", "casino_payout", "casino_refund")
POOLS_GAME = POOLS_TABLES.game


class DayMetric(NamedTuple):
    """One guild-local day of the economy, with its candle.

    ``net`` is the settlement value AND ``close - open`` — the same sum
    twice, so the market and the chart cannot disagree.
    """

    day: str
    mint: int
    burn: int
    hold: int
    net: int
    open: int
    high: int
    low: int
    close: int
    volume: int


def _local_day(ts: float, tz_offset_hours: float) -> str:
    from bot_modules.economy.logic import local_day_for  # noqa: PLC0415

    return local_day_for(ts, tz_offset_hours)


def _session_days(
    conn: sqlite3.Connection, guild_id: int, tz_offset_hours: float
) -> dict[tuple[str, int], str]:
    """``(kind, id) -> guild-local day`` for every round and live hand.

    ``kind`` is "round" or "hand", matching the ledger meta key. Built in
    one pass over the module's own table descriptors rather than a hand
    written list, so a game added to ALL_ROUND_TABLES is covered
    automatically.

    Indexed positionally rather than by column name: this also runs from
    scripts/economy_tuning_report.py, whose connection has no Row factory.
    """
    out: dict[tuple[str, int], str] = {}
    sources = [("round", t.rounds, "opened_at") for t in ALL_ROUND_TABLES]
    sources += [("hand", h.table, "created_at") for h in (BLACKJACK_HANDS, WAR_HANDS)]
    for tag, table, ts_col in sources:
        try:
            rows = conn.execute(
                f"SELECT id, {ts_col} FROM {table} WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        except sqlite3.OperationalError:
            # A database that predates this game's migration simply has no
            # sessions to attribute — the rows fall back to their own day
            # rather than the whole series failing to compute.
            continue
        for row in rows:
            out[(tag, int(row[0]))] = _local_day(float(row[1]), tz_offset_hours)
    return out


def _row_day(
    meta_raw: str | None,
    created_at: float,
    sessions: dict[tuple[str, int], str],
    tz_offset_hours: float,
) -> tuple[str, str | None]:
    """The day a ledger row counts against, plus its game (if any).

    Falls back to the row's own timestamp whenever there is no session to
    attribute it to — an unlinked historical row, or any non-casino kind.
    """
    fallback = _local_day(created_at, tz_offset_hours)
    if not meta_raw:
        return fallback, None
    try:
        meta = json.loads(meta_raw)
    except (ValueError, TypeError):
        return fallback, None
    if not isinstance(meta, dict):
        return fallback, None
    game = meta.get("game")
    game = str(game) if game is not None else None
    for key, tag in (("round_id", "round"), ("hand_id", "hand")):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            day = sessions.get((tag, int(raw)))
        except (ValueError, TypeError):
            continue
        if day is not None:
            return day, game
    return fallback, game


def daily_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    limit_days: int | None = None,
) -> list[DayMetric]:
    """Every guild-local day of the economy, oldest first.

    One pass over the guild's ledger. That is affordable because the series
    is wanted whole — the line needs a trailing window, the chart needs a
    running circulation level that only exists cumulatively, and settlement
    needs one day — and computing them separately is how the three drift
    apart. ``limit_days`` trims the *returned* tail; the level is always
    accumulated from the beginning, because a partial sum is not a level.
    """
    sessions = _session_days(conn, guild_id, tz_offset_hours)
    rows: list[tuple[str, float, int, int, str]] = []
    for row_id, amount, kind, meta, created_at in conn.execute(
        "SELECT id, amount, kind, meta, created_at FROM econ_ledger "
        "WHERE guild_id = ?",
        (guild_id,),
    ):
        kind = str(kind)
        day, game = _row_day(meta, float(created_at), sessions, tz_offset_hours)
        # Pools' own money is invisible to the metric it settles against.
        if game == POOLS_GAME and kind in CASINO_KINDS:
            continue
        rows.append((day, float(created_at), int(row_id), int(amount), kind))

    # Accumulate in ATTRIBUTED-day order, not timestamp order. A payout
    # pulled back across midnight has to land inside its own day's run of
    # rows, or that day's close and the next day's open stop agreeing and
    # the series develops a gap the ledger does not actually have.
    rows.sort()
    out: list[DayMetric] = []
    level = 0
    i = 0
    while i < len(rows):
        day = rows[i][0]
        opened = high = low = level
        mint = burn = hold = volume = 0
        while i < len(rows) and rows[i][0] == day:
            _, _, _, amount, kind = rows[i]
            level += amount
            volume += 1
            high = max(high, level)
            low = min(low, level)
            if amount > 0 and kind not in NON_FAUCET_KINDS:
                mint += amount
            elif amount < 0 and kind not in BURN_KINDS_EXCLUDED:
                burn += -amount
            if kind == "casino_stake":
                hold += -amount
            elif kind == "casino_payout":
                hold -= amount
            i += 1
        out.append(DayMetric(
            day=day, mint=mint, burn=burn, hold=hold, net=level - opened,
            open=opened, high=high, low=low, close=level, volume=volume,
        ))
    if limit_days is not None:
        out = out[-limit_days:]
    return out


def net_change_for(
    conn: sqlite3.Connection, guild_id: int, day: str, *, tz_offset_hours: float
) -> int | None:
    """The settled metric for one day. None = that day has no rows at all."""
    for m in daily_series(conn, guild_id, tz_offset_hours=tz_offset_hours):
        if m.day == day:
            return m.net
    return None


def line_for(
    conn: sqlite3.Connection, guild_id: int, day: str, *, tz_offset_hours: float
) -> float | None:
    """The line for ``day``: median of the trailing completed days, +0.5.

    Only days strictly before ``day`` count — opening a round on a line
    that included its own partial day would let the first hours of trading
    set the target they are trading against.
    """
    history = [
        m.net
        for m in daily_series(conn, guild_id, tz_offset_hours=tz_offset_hours)
        if m.day < day
    ]
    return pools_logic.derive_line(history)


class SettleJob(NamedTuple):
    round_id: int
    day: str
    result: int
    line: float


class OpenJob(NamedTuple):
    day: str
    line: float
    closes_at: float


class Tick(NamedTuple):
    """What the day-roll sweep should do, decided without touching Discord.

    Ordering is not incidental: **settle yesterday before opening today**.
    Today's line is a median of the completed days before it, so opening
    first would compute it against a day that has not finished — and in the
    worst case against its own partial self.
    """

    settle: list[SettleJob]
    open: OpenJob | None


def plan_tick(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    close_hour: int,
    now: float,
) -> Tick:
    """Decide the day roll's work.

    Settles every open round whose measured day is over — recomputed from
    the ledger, so a round missed by hours or a whole restart lands on the
    same answer it would have at midnight. Opens today's round if there is
    not one, there is enough history for a line, and betting has not
    already passed its close hour.
    """
    from bot_modules.economy.logic import local_day_bounds  # noqa: PLC0415

    today = _local_day(now, tz_offset_hours)
    series = {
        m.day: m
        for m in daily_series(conn, guild_id, tz_offset_hours=tz_offset_hours)
    }

    settle: list[SettleJob] = []
    for rnd in conn.execute(
        "SELECT id, local_day, line FROM casino_pools_rounds "
        "WHERE guild_id = ? AND status = 'open'",
        (guild_id,),
    ):
        day = str(rnd["local_day"])
        if day >= today:
            continue  # still being measured
        metric = series.get(day)
        # A day with no ledger rows at all had a net change of exactly zero.
        settle.append(SettleJob(
            int(rnd["id"]), day, metric.net if metric else 0, float(rnd["line"])
        ))

    opening: OpenJob | None = None
    if conn.execute(
        "SELECT 1 FROM casino_pools_rounds WHERE guild_id = ? AND local_day = ?",
        (guild_id, today),
    ).fetchone() is None:
        line = pools_logic.derive_line(
            [m.net for day, m in sorted(series.items()) if day < today]
        )
        closes_at = local_day_bounds(today, tz_offset_hours)[0] + close_hour * 3600
        if line is not None and now < closes_at:
            opening = OpenJob(today, line, closes_at)

    return Tick(settle, opening)


def candles(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    days: int = 14,
) -> list[DayMetric]:
    """The last ``days`` of the series, for the instrument chart.

    Same rows the settlement reads, so a candle body is the metric by
    construction rather than by coincidence.
    """
    return daily_series(
        conn, guild_id, tz_offset_hours=tz_offset_hours, limit_days=days
    )
