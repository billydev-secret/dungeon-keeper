"""Weekly hoard tax (demurrage) — idle wealth above a threshold evaporates.

The only sink that needs no buyer: every other stage sells something, so a
member who wants nothing keeps accumulating forever. At the guild's ISO-week
roll, every wallet above ``demurrage_threshold`` is taxed
``demurrage_rate_pct``% of the **excess** only — the threshold itself is a
protected floor, so modest wallets never feel it and a taxed member can never
be pushed below it (100% is therefore a hard wealth cap, not confiscation).
Rate 0 (the default) is the dark launch; setting a rate on the Sinks page is
the launch switch — announce first, the voice-lease pattern.

Exactly-once via the ``econ_demurrage_sweeps`` (guild, week) primary key —
the claim row lands before any debit (the raffle-draw pattern, migration
100), so a crash-and-replay of the week roll collects nothing twice. Ledger
kind ``demurrage``; each taxed member's row carries the closed week and their
pre-tax balance, and the register narrates it like any other movement.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import apply_debit

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

TAX_KIND = "demurrage"


@dataclass(frozen=True)
class SweepResult:
    iso_week: str
    taxed_members: int
    total: int


def demurrage_enabled(settings: EconSettings) -> bool:
    return int(settings.demurrage_rate_pct) > 0


def run_sweep(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    iso_week: str,
    *,
    now: float | None = None,
) -> SweepResult | None:
    """Tax every over-threshold wallet for a closed week, exactly once.

    Returns None when this week was already swept (the INSERT claim lost) —
    the caller treats that as a no-op re-run. The floor-division tax means a
    small excess can round to 0 and go untaxed; that's the intended grace,
    not a bug. Debits ride :func:`apply_debit`, so the wallet UPDATE and the
    ledger row land atomically per member inside the caller's transaction.
    """
    now = time.time() if now is None else now
    try:
        conn.execute(
            "INSERT INTO econ_demurrage_sweeps "
            "(guild_id, iso_week, created_at) VALUES (?, ?, ?)",
            (guild_id, iso_week, now),
        )
    except sqlite3.IntegrityError:
        return None  # already swept — a week-roll re-run

    rate = int(settings.demurrage_rate_pct)
    threshold = max(0, int(settings.demurrage_threshold))
    rows = conn.execute(
        "SELECT user_id, balance FROM econ_wallets "
        "WHERE guild_id = ? AND balance > ?",
        (guild_id, threshold),
    ).fetchall()

    taxed = 0
    total = 0
    for r in rows:
        balance = int(r["balance"])
        tax = (balance - threshold) * rate // 100
        if tax < 1:
            continue
        if not apply_debit(
            conn, guild_id, int(r["user_id"]), tax, TAX_KIND,
            meta={"iso_week": iso_week, "balance": balance},
        ):
            continue  # balance moved under us — skip, never go negative
        taxed += 1
        total += tax

    conn.execute(
        "UPDATE econ_demurrage_sweeps SET taxed_members = ?, total = ? "
        "WHERE guild_id = ? AND iso_week = ?",
        (taxed, total, guild_id, iso_week),
    )
    return SweepResult(iso_week=iso_week, taxed_members=taxed, total=total)


def get_sweep(
    conn: sqlite3.Connection, guild_id: int, iso_week: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_demurrage_sweeps "
        "WHERE guild_id = ? AND iso_week = ?",
        (guild_id, iso_week),
    ).fetchone()
