"""Economy metrics — the weekly rollup DB layer (spec §9).

Computed at the guild-local ISO-week roll for the week that just closed, from
the ``econ_ledger`` / ``econ_rentals`` / ``econ_streaks`` state. The rollup is
idempotent via the ``econ_metrics_weekly`` (guild_id, iso_week) primary key:
:func:`compute_weekly_rollup` writes INSERT OR IGNORE and returns ``None`` on a
replay, so a loop crash before the trailing mark update recomputes nothing.

It rides the caller's connection/transaction (no internal commit) exactly like
the other stage services — the week-roll branch of the economy loop calls it in
the same transaction as the weekly rotation and community settlement, before the
day marks advance.

Money definitions (spec §9):

* **income** per member — sum of positive ledger credits in the week EXCLUDING
  ``transfer_in`` (a transfer moves currency, it does not mint). ``earners`` are
  members with income > 0; median / p90 are over those earners.
* **minted** — sum of positive amounts excluding ``transfer_in``; **burned** —
  ``|sum of negative amounts|`` excluding ``transfer_out``.
* **faucet_mix** — JSON share of minted per faucet group (see
  ``economy.metrics.FAUCET_GROUPS``); ``{}`` when nothing was minted.
* rental / streak columns are point-in-time-at-rollup counts, except
  ``rentals_ended`` / ``streaks_7plus`` / ``grace_used`` which are windowed to
  the closed week.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

from bot_modules.economy import metrics
from bot_modules.services.economy_quests_service import active_member_ids

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

_ROLLUP_COLS = (
    "guild_id, iso_week, median_income, p90_income, active_members, earners, "
    "minted, burned, faucet_mix, rental_holders, rentals_live, rentals_ended, "
    "streaks_7plus, grace_used, computed_at"
)


def compute_weekly_rollup(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    iso_week: str,
    *,
    offset_hours: float,
    now: float,
) -> sqlite3.Row | None:
    """Compute and persist the metrics rollup for a closed ISO week.

    Idempotent: returns ``None`` if a row for (guild, iso_week) already exists
    (INSERT OR IGNORE semantics), else computes every column from the ledger /
    rentals / streaks state, inserts, and returns the stored row. ``settings`` is
    unused by the current math but kept in the signature for parity with the
    other rollup consumers and future per-guild tuning.
    """
    del settings  # reserved for future per-guild metric tuning
    existing = conn.execute(
        "SELECT 1 FROM econ_metrics_weekly WHERE guild_id = ? AND iso_week = ?",
        (guild_id, iso_week),
    ).fetchone()
    if existing is not None:
        return None

    start, end = metrics.iso_week_bounds(iso_week, offset_hours)
    monday, sunday = metrics.iso_week_day_range(iso_week)

    # ── income per earner (positive credits, transfer_in excluded) ──
    income_rows = conn.execute(
        """
        SELECT user_id, SUM(amount) AS income
        FROM econ_ledger
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
          AND amount > 0 AND kind != 'transfer_in'
        GROUP BY user_id
        HAVING income > 0
        """,
        (guild_id, start, end),
    ).fetchall()
    incomes = [float(r["income"]) for r in income_rows]
    earners = len(incomes)
    median_income = metrics.median_income(incomes)
    p90_income = metrics.p90_income(incomes)

    # ── minted / burned totals ──
    minted = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM econ_ledger
            WHERE guild_id = ? AND created_at >= ? AND created_at < ?
              AND amount > 0 AND kind != 'transfer_in'
            """,
            (guild_id, start, end),
        ).fetchone()[0]
    )
    burned = int(
        conn.execute(
            """
            SELECT COALESCE(SUM(-amount), 0)
            FROM econ_ledger
            WHERE guild_id = ? AND created_at >= ? AND created_at < ?
              AND amount < 0 AND kind != 'transfer_out'
            """,
            (guild_id, start, end),
        ).fetchone()[0]
    )

    # ── faucet mix: minted share per group ──
    kind_rows = conn.execute(
        """
        SELECT kind, SUM(amount) AS total
        FROM econ_ledger
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
          AND amount > 0 AND kind != 'transfer_in'
        GROUP BY kind
        """,
        (guild_id, start, end),
    ).fetchall()
    minted_by_kind = {str(r["kind"]): float(r["total"]) for r in kind_rows}
    faucet_mix = metrics.faucet_shares(minted_by_kind, minted)

    # ── membership + rentals ──
    active_members = len(active_member_ids(conn, guild_id, days=30))

    rental_holders = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT beneficiary_id) FROM econ_rentals
            WHERE guild_id = ? AND state IN ('active', 'grace')
            """,
            (guild_id,),
        ).fetchone()[0]
    )
    rentals_live = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM econ_rentals
            WHERE guild_id = ? AND state IN ('active', 'grace')
            """,
            (guild_id,),
        ).fetchone()[0]
    )
    rentals_ended = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM econ_rentals
            WHERE guild_id = ? AND ended_at >= ? AND ended_at < ?
            """,
            (guild_id, start, end),
        ).fetchone()[0]
    )

    # ── streak health (day strings compare lexicographically as ISO dates) ──
    streaks_7plus = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM econ_streaks
            WHERE guild_id = ? AND current_streak >= 7
              AND last_login_day >= ? AND last_login_day <= ?
            """,
            (guild_id, monday, sunday),
        ).fetchone()[0]
    )
    grace_used = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM econ_streaks
            WHERE guild_id = ? AND last_grace_day IS NOT NULL
              AND last_grace_day >= ? AND last_grace_day <= ?
            """,
            (guild_id, monday, sunday),
        ).fetchone()[0]
    )

    conn.execute(
        f"""
        INSERT OR IGNORE INTO econ_metrics_weekly ({_ROLLUP_COLS})
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            iso_week,
            median_income,
            p90_income,
            active_members,
            earners,
            minted,
            burned,
            json.dumps(faucet_mix),
            rental_holders,
            rentals_live,
            rentals_ended,
            streaks_7plus,
            grace_used,
            now,
        ),
    )
    return conn.execute(
        f"SELECT {_ROLLUP_COLS} FROM econ_metrics_weekly "
        "WHERE guild_id = ? AND iso_week = ?",
        (guild_id, iso_week),
    ).fetchone()


def get_weekly_metrics(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 12
) -> list[sqlite3.Row]:
    """The guild's most recent weekly rollups, newest ISO week first.

    ``iso_week`` sorts lexicographically in chronological order (zero-padded
    week number), so a plain DESC ordering is chronological.
    """
    return conn.execute(
        f"""
        SELECT {_ROLLUP_COLS} FROM econ_metrics_weekly
        WHERE guild_id = ?
        ORDER BY iso_week DESC
        LIMIT ?
        """,
        (guild_id, limit),
    ).fetchall()


def latest_median_income(conn: sqlite3.Connection, guild_id: int) -> float:
    """Median weekly income from the latest rollup; 0.0 when none exist yet."""
    row = conn.execute(
        """
        SELECT median_income FROM econ_metrics_weekly
        WHERE guild_id = ?
        ORDER BY iso_week DESC
        LIMIT 1
        """,
        (guild_id,),
    ).fetchone()
    if row is None or row["median_income"] is None:
        return 0.0
    return float(row["median_income"])
