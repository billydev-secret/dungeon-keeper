#!/usr/bin/env python3
"""Economy tuning report — the knob-tuning numbers, from the live ledger.

Read-only. Prints the distribution/flow stats used to tune the Sinks-page
dials (raffle, voice lease, hoard tax, wager rake) and to judge whether the
quest faucet needs shaving: balance percentiles, last-full-week income
percentiles, faucet/sink mix, spender count, and the demurrage what-if grid.

Compare runs over time:

    python scripts/economy_tuning_report.py                 # human report
    python scripts/economy_tuning_report.py --save-baseline docs/reviews/economy-baseline-YYYY-MM-DD.json
    python scripts/economy_tuning_report.py --baseline docs/reviews/economy-baseline-2026-07-20.json

With --baseline, each headline number is printed alongside the baseline and
its delta — the "did the dials move anything" view. The DB is opened with
mode=ro so this can never touch production state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "dungeonkeeper.db"
MAIN_GUILD = 1469491362444480666
# Guild-local day offset (hours from UTC). The main guild has no tz row and
# inherits the global -7 — keep in sync with the tz_offset config.
TZ_OFFSET_HOURS = -7.0

# Ledger kinds that are member↔member movement, not real income.
NON_FAUCET_KINDS = ("transfer_in", "wager_payout", "wager_refund")
# Kinds that actually destroy currency (transfers/wagers move it sideways).
BURN_KINDS_EXCLUDED = ("transfer_out", "wager_stake")

DEMURRAGE_FLOORS = (300, 500, 750, 1000)
DEMURRAGE_RATES = (2, 5, 10)


def _percentiles(values: list[int], points: dict[str, float]) -> dict[str, int]:
    """Nearest-rank percentiles ({} when there are no values)."""
    if not values:
        return {k: 0 for k in points}
    ordered = sorted(values)
    n = len(ordered)
    return {
        key: ordered[min(n - 1, max(0, int(n * frac) - (0 if frac < 1 else 1)))]
        for key, frac in points.items()
    }


def _last_full_week(today: date) -> tuple[str, str]:
    """(monday, sunday) ISO dates of the most recent fully-elapsed ISO week."""
    monday_this = today - timedelta(days=today.weekday())
    return str(monday_this - timedelta(days=7)), str(monday_this - timedelta(days=1))


def collect(conn: sqlite3.Connection, guild_id: int, today: date) -> dict:
    day_expr = f"date(created_at - {-TZ_OFFSET_HOURS}*3600, 'unixepoch')"
    week_start, week_end = _last_full_week(today)

    balances = [
        int(r[0])
        for r in conn.execute(
            "SELECT balance FROM econ_wallets WHERE guild_id = ? AND balance > 0",
            (guild_id,),
        )
    ]
    top = [
        {"user_id": str(r[0]), "balance": int(r[1])}
        for r in conn.execute(
            "SELECT user_id, balance FROM econ_wallets WHERE guild_id = ? "
            "ORDER BY balance DESC LIMIT 10",
            (guild_id,),
        )
    ]

    marks = ",".join("?" * len(NON_FAUCET_KINDS))
    weekly_income = [
        int(r[0])
        for r in conn.execute(
            f"SELECT SUM(amount) FROM econ_ledger "
            f"WHERE guild_id = ? AND amount > 0 AND kind NOT IN ({marks}) "
            f"AND {day_expr} BETWEEN ? AND ? GROUP BY user_id",
            (guild_id, *NON_FAUCET_KINDS, week_start, week_end),
        )
    ]

    minted_week = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) FROM econ_ledger "
        f"WHERE guild_id = ? AND amount > 0 AND kind NOT IN ({marks}) "
        f"AND {day_expr} BETWEEN ? AND ?",
        (guild_id, *NON_FAUCET_KINDS, week_start, week_end),
    ).fetchone()[0]
    burns = ",".join("?" * len(BURN_KINDS_EXCLUDED))
    burned_week = conn.execute(
        f"SELECT COALESCE(SUM(-amount), 0) FROM econ_ledger "
        f"WHERE guild_id = ? AND amount < 0 AND kind NOT IN ({burns}) "
        f"AND {day_expr} BETWEEN ? AND ?",
        (guild_id, *BURN_KINDS_EXCLUDED, week_start, week_end),
    ).fetchone()[0]

    faucet_mix = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            f"SELECT kind, SUM(amount) FROM econ_ledger "
            f"WHERE guild_id = ? AND amount > 0 AND kind NOT IN ({marks}) "
            f"AND {day_expr} BETWEEN ? AND ? "
            f"GROUP BY kind ORDER BY 2 DESC",
            (guild_id, *NON_FAUCET_KINDS, week_start, week_end),
        )
    }
    sink_mix = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            f"SELECT kind, SUM(-amount) FROM econ_ledger "
            f"WHERE guild_id = ? AND amount < 0 AND kind NOT IN ({burns}) "
            f"AND {day_expr} BETWEEN ? AND ? "
            f"GROUP BY kind ORDER BY 2 DESC",
            (guild_id, *BURN_KINDS_EXCLUDED, week_start, week_end),
        )
    }
    spenders_week = conn.execute(
        f"SELECT COUNT(DISTINCT user_id) FROM econ_ledger "
        f"WHERE guild_id = ? AND amount < 0 AND kind NOT IN ({burns}) "
        f"AND {day_expr} BETWEEN ? AND ?",
        (guild_id, *BURN_KINDS_EXCLUDED, week_start, week_end),
    ).fetchone()[0]

    demurrage_grid = []
    for floor in DEMURRAGE_FLOORS:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(balance - ?), 0) FROM econ_wallets "
            "WHERE guild_id = ? AND balance > ?",
            (floor, guild_id, floor),
        ).fetchone()
        excess = int(row[1])
        demurrage_grid.append(
            {
                "floor": floor,
                "wallets_hit": int(row[0]),
                "excess": excess,
                **{f"burn_at_{r}pct": excess * r // 100 for r in DEMURRAGE_RATES},
            }
        )

    pcts = {"p50": 0.50, "p75": 0.75, "p90": 0.90, "p95": 0.95}
    income_pct = _percentiles(weekly_income, pcts)
    return {
        "generated": str(today),
        "guild_id": str(guild_id),
        "week": f"{week_start}..{week_end}",
        "wallets": len(balances),
        "float_total": sum(balances),
        "balance": _percentiles(balances, pcts),
        "top_wallets": top,
        "weekly_earners": len(weekly_income),
        "weekly_income": income_pct,
        "minted_week": int(minted_week),
        "burned_week": int(burned_week),
        "burn_ratio_pct": round(100 * burned_week / minted_week, 1) if minted_week else 0.0,
        "spenders_week": int(spenders_week),
        "faucet_mix": faucet_mix,
        "sink_mix": sink_mix,
        "demurrage_grid": demurrage_grid,
        "hoard_weeks": (
            round(_percentiles(balances, {"p50": 0.5})["p50"] / income_pct["p50"], 1)
            if income_pct["p50"]
            else None
        ),
    }


def _fmt_delta(cur: float, base: float | None) -> str:
    if base is None:
        return f"{cur:,}"
    diff = cur - base
    return f"{cur:,}  (was {base:,}, {'+' if diff >= 0 else ''}{diff:,})"


def print_report(stats: dict, baseline: dict | None) -> None:
    b = baseline or {}

    def line(label: str, key: str, sub: str | None = None) -> None:
        cur = stats[key] if sub is None else stats[key][sub]
        prev = b.get(key) if sub is None else (b.get(key) or {}).get(sub)
        print(f"  {label:<28} {_fmt_delta(cur, prev)}")

    print(f"Economy tuning report — guild {stats['guild_id']}, week {stats['week']}")
    if baseline:
        print(f"(deltas vs baseline {baseline.get('generated', '?')})")
    print("\nBalances")
    line("wallets (>0)", "wallets")
    line("total float", "float_total")
    for p in ("p50", "p75", "p90", "p95"):
        line(f"balance {p}", "balance", p)
    print("  top wallets              " + ", ".join(
        f"{w['balance']:,}" for w in stats["top_wallets"][:5]
    ))
    print("\nLast full week")
    line("earners", "weekly_earners")
    for p in ("p50", "p90"):
        line(f"weekly income {p}", "weekly_income", p)
    line("minted", "minted_week")
    line("burned (real sinks)", "burned_week")
    line("burn ratio %", "burn_ratio_pct")
    line("spenders", "spenders_week")
    if stats["hoard_weeks"] is not None:
        line("hoard-weeks (p50/p50)", "hoard_weeks")
    print("\nFaucet mix (week): " + ", ".join(
        f"{k}={v:,}" for k, v in stats["faucet_mix"].items()
    ))
    print("Sink mix (week):   " + (", ".join(
        f"{k}={v:,}" for k, v in stats["sink_mix"].items()
    ) or "(nothing burned)"))
    print("\nDemurrage what-if (weekly burn at rate % of excess over floor)")
    print(f"  {'floor':>6} {'hit':>4} {'excess':>8} " + " ".join(
        f"@{r}%".rjust(7) for r in DEMURRAGE_RATES
    ))
    for row in stats["demurrage_grid"]:
        cells = " ".join(
            f"{row[f'burn_at_{r}pct']:>7,}" for r in DEMURRAGE_RATES
        )
        print(f"  {row['floor']:>6} {row['wallets_hit']:>4} {row['excess']:>8,} {cells}")


def main() -> None:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--guild", type=int, default=MAIN_GUILD)
    ap.add_argument("--baseline", type=Path, help="baseline JSON to diff against")
    ap.add_argument("--save-baseline", type=Path, help="write this run as baseline JSON")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(hours=TZ_OFFSET_HOURS))
    ).date()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        stats = collect(conn, args.guild, today)
    finally:
        conn.close()

    if args.save_baseline:
        args.save_baseline.write_text(json.dumps(stats, indent=2) + "\n")
        print(f"baseline written: {args.save_baseline}")
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print_report(stats, baseline)


if __name__ == "__main__":
    main()
