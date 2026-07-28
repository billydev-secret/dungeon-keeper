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

import itertools
import json
import operator
import sqlite3
from typing import NamedTuple

from bot_modules.economy.logic import local_day_bounds, local_day_for
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


DayMetric = pools_logic.DayMetric


def _local_day(ts: float, tz_offset_hours: float) -> str:
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


def _row_meta(
    meta_raw: str | None, sessions: dict[tuple[str, int], str]
) -> tuple[str | None, str | None]:
    """``(session day, game)`` for a casino ledger row, or ``(None, None)``.

    Only ever called for casino kinds. Everything else — the ~half of the
    ledger that is faucets and shop spend — carries no session to attribute
    to and no game to exclude on, so parsing its JSON would be pure waste on
    a scan measured in thousands of rows.
    """
    if not meta_raw:
        return None, None
    try:
        meta = json.loads(meta_raw)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(meta, dict):
        return None, None
    game = meta.get("game")
    for key, tag in (("round_id", "round"), ("hand_id", "hand")):
        raw = meta.get(key)
        if raw is None:
            continue
        try:
            day = sessions.get((tag, int(raw)))
        except (ValueError, TypeError):
            continue
        if day is not None:
            return day, (str(game) if game is not None else None)
    return None, (str(game) if game is not None else None)


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

    This is a full scan, so it is deliberately NOT on any hot path:
    ``plan_tick`` answers the common minute-by-minute case from two indexed
    lookups and only reaches here when a round actually has to open or
    settle.
    """
    sessions = _session_days(conn, guild_id, tz_offset_hours)
    rows: list[tuple[str, float, int, int, str]] = []
    for row_id, amount, kind, meta, created_at in conn.execute(
        "SELECT id, amount, kind, meta, created_at FROM econ_ledger "
        "WHERE guild_id = ?",
        (guild_id,),
    ):
        kind = str(kind)
        day = game = None
        if kind in CASINO_KINDS:
            day, game = _row_meta(meta, sessions)
            # Pools' own money is invisible to the metric it settles against.
            if game == POOLS_GAME:
                continue
        rows.append((
            day or _local_day(float(created_at), tz_offset_hours),
            float(created_at), int(row_id), int(amount), kind,
        ))

    # Accumulate in ATTRIBUTED-day order, not timestamp order. A payout
    # pulled back across midnight has to land inside its own day's run of
    # rows, or that day's close and the next day's open stop agreeing and
    # the series develops a gap the ledger does not actually have.
    rows.sort()
    out: list[DayMetric] = []
    level = 0
    for day, group in itertools.groupby(rows, key=operator.itemgetter(0)):
        opened = high = low = level
        mint = burn = hold = volume = 0
        for _, _, _, amount, kind in group:
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
        out.append(DayMetric(
            day=day, mint=mint, burn=burn, hold=hold, net=level - opened,
            open=opened, high=high, low=low, close=level, volume=volume,
        ))
    if limit_days is not None:
        out = out[-limit_days:]
    return out


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

    ``series`` is the day series the decision was made from, or None when
    the tick short-circuited without needing it. Settlement wants the same
    rows for its chart, and recomputing them would be a second full scan of
    the ledger for an answer already in hand.
    """

    settle: list[SettleJob]
    open: OpenJob | None
    series: list[DayMetric] | None = None

    @property
    def idle(self) -> bool:
        return not self.settle and self.open is None


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

    **The two lookups come first, deliberately.** This runs from the cog's
    minute maintenance loop, and on all but one tick a day the answer is
    "nothing to do". Both queries below hit ``idx_casino_pools_guild_day``
    on a table that grows by one row a day, so the idle case costs
    microseconds; only a tick with actual work pays for ``daily_series``,
    which is a full scan of the ledger and grows with it forever. Computing
    the series first made every idle minute pay that price.
    """
    today = _local_day(now, tz_offset_hours)
    overdue = conn.execute(
        "SELECT id, local_day, line FROM casino_pools_rounds "
        "WHERE guild_id = ? AND status = 'open' AND local_day < ?",
        (guild_id, today),
    ).fetchall()
    has_today = conn.execute(
        "SELECT 1 FROM casino_pools_rounds WHERE guild_id = ? AND local_day = ?",
        (guild_id, today),
    ).fetchone() is not None
    closes_at = local_day_bounds(today, tz_offset_hours)[0] + close_hour * 3600
    # Nothing to settle, today already has a market (or is past its close
    # and never will) — the overwhelmingly common case.
    if not overdue and (has_today or now >= closes_at):
        return Tick([], None)

    series = daily_series(conn, guild_id, tz_offset_hours=tz_offset_hours)
    by_day = {m.day: m for m in series}

    settle = [
        SettleJob(
            int(r["id"]), str(r["local_day"]),
            # A day with no ledger rows at all had a net change of zero.
            by_day[str(r["local_day"])].net
            if str(r["local_day"]) in by_day else 0,
            float(r["line"]),
        )
        for r in overdue
    ]

    opening: OpenJob | None = None
    if not has_today:
        line = pools_logic.derive_line(
            [m.net for m in series if m.day < today]
        )
        if line is not None:
            opening = OpenJob(today, line, closes_at)

    return Tick(settle, opening, series)
