"""Coin Drops service — DB layer for the random channel-drop faucet.

The bot drops a pouch of coins into ``drops_channel_id`` at random intervals
(``drops_per_day`` sets the average cadence); the first member to press the
drop message's **Claim** button collects it, and an unclaimed drop expires
after ``drops_expire_minutes``. Pure sync SQLite here — the async posting
loop and the persistent claim button live in ``economy_drops_loop.py``. See
docs/economy_spec.md §3.5.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import EconSettings, apply_credit

if TYPE_CHECKING:
    import random

DROP_KIND = "drop"

# The next drop lands a uniform-random 0.5–1.5× of the average period away —
# random enough that members can't clock it, bounded enough that a configured
# cadence still feels like that cadence.
_JITTER_LO = 0.5
_JITTER_HI = 1.5


def drops_configured(settings: EconSettings) -> bool:
    """Is the faucet live for this guild? The channel picker is the toggle."""
    return (
        settings.enabled
        and settings.drops_channel_id != 0
        and settings.drops_per_day > 0
        and max(settings.drops_min_coins, settings.drops_max_coins) > 0
    )


def roll_amount(settings: EconSettings, rng: random.Random) -> int:
    """Random drop size in [min, max] coins; tolerates swapped/zero bounds."""
    lo = min(settings.drops_min_coins, settings.drops_max_coins)
    hi = max(settings.drops_min_coins, settings.drops_max_coins)
    lo = max(1, lo)
    hi = max(lo, hi)
    return rng.randint(lo, hi)


def next_drop_delay(settings: EconSettings, rng: random.Random) -> float:
    """Seconds until the next drop for this guild (jittered average cadence)."""
    period = 86400.0 / max(1, settings.drops_per_day)
    return rng.uniform(_JITTER_LO, _JITTER_HI) * period


def create_drop(
    conn: sqlite3.Connection,
    guild_id: int,
    channel_id: int,
    amount: int,
    *,
    now_ts: float,
    expire_minutes: int,
) -> int:
    """Record a drop about to be posted; returns its row id.

    The row exists *before* the Discord message does — the Claim button's
    ``custom_id`` needs the id — so ``message_id`` starts 0 and is backfilled
    by ``set_drop_message`` (or the row removed by ``discard_drop`` when the
    send fails).
    """
    cur = conn.execute(
        """
        INSERT INTO econ_drops
            (guild_id, channel_id, message_id, amount, status,
             created_at, expires_at)
        VALUES (?, ?, 0, ?, 'open', ?, ?)
        """,
        (
            guild_id,
            channel_id,
            amount,
            now_ts,
            now_ts + max(1, expire_minutes) * 60.0,
        ),
    )
    return int(cur.lastrowid or 0)


def set_drop_message(
    conn: sqlite3.Connection, drop_id: int, message_id: int
) -> None:
    """Backfill the posted message id (the expiry sweep edits it later)."""
    conn.execute(
        "UPDATE econ_drops SET message_id = ? WHERE id = ?",
        (message_id, drop_id),
    )


def discard_drop(conn: sqlite3.Connection, drop_id: int) -> None:
    """Remove a drop whose message never made it to Discord.

    Only a still-open row is deleted — a claimed/expired row is settled
    history and stays for the audit trail.
    """
    conn.execute(
        "DELETE FROM econ_drops WHERE id = ? AND status = 'open'",
        (drop_id,),
    )


def has_open_drop(conn: sqlite3.Connection, guild_id: int) -> bool:
    """Does this guild already have an unclaimed pouch out? (Never stack.)"""
    row = conn.execute(
        "SELECT 1 FROM econ_drops WHERE guild_id = ? AND status = 'open' LIMIT 1",
        (guild_id,),
    ).fetchone()
    return row is not None


def try_claim_drop(
    conn: sqlite3.Connection,
    settings: EconSettings,
    drop_id: int,
    guild_id: int,
    user_id: int,
    *,
    now_ts: float,
    booster: bool,
) -> int | None:
    """First-wins claim: pay the drop to this member, or None if beaten to it.

    The conditional UPDATE is the race arbiter (never check-then-write): only
    one caller flips ``status`` off 'open', and an expired-but-unswept drop
    can't be claimed. Returns the credited amount (booster multiplier applied)
    on the winning path.
    """
    cur = conn.execute(
        """
        UPDATE econ_drops
        SET status = 'claimed', claimed_by = ?, claimed_at = ?
        WHERE id = ? AND status = 'open' AND expires_at > ?
        """,
        (user_id, now_ts, drop_id, now_ts),
    )
    if (cur.rowcount or 0) == 0:
        return None
    row = conn.execute(
        "SELECT amount FROM econ_drops WHERE id = ?", (drop_id,)
    ).fetchone()
    return apply_credit(
        conn,
        guild_id,
        user_id,
        int(row["amount"]),
        DROP_KIND,
        meta={"drop_id": drop_id},
        booster=booster,
        multiplier=settings.booster_multiplier,
    )


def expire_due_drops(conn: sqlite3.Connection, now_ts: float) -> list[sqlite3.Row]:
    """Mark overdue open drops expired; returns them so the loop can edit
    the messages. Select-then-update is safe here: claims that race in
    between lose their row to the claim's own conditional UPDATE, and the
    expiry UPDATE below re-checks ``status = 'open'`` per row."""
    rows = list(
        conn.execute(
            """
            SELECT id, guild_id, channel_id, message_id, amount
            FROM econ_drops
            WHERE status = 'open' AND expires_at <= ?
            """,
            (now_ts,),
        )
    )
    out: list[sqlite3.Row] = []
    for row in rows:
        cur = conn.execute(
            "UPDATE econ_drops SET status = 'expired' WHERE id = ? AND status = 'open'",
            (int(row["id"]),),
        )
        if (cur.rowcount or 0) > 0:
            out.append(row)
    return out
