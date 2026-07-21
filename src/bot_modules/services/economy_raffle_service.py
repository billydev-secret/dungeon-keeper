"""Weekly raffle — tickets in, a free-perk-week voucher out (never coins).

Economy sinks round 3, stage 5 (spec §6, migration 093). Ticket revenue is a
pure burn: the prize is a ``free_week`` voucher covering ONE rental debit
(the winner's next renewal, or the first week of a new rent), so no currency
ever flows back out of the raffle. The weighted draw runs at the guild's
ISO-week roll; the ``econ_raffle_draws`` primary key is the exactly-once
claim — the draw row lands before any side effect, so a re-run of the week
roll after a crash cannot draw the same week twice.

Ledger kinds: ``raffle_ticket`` (the burn). A voucher redemption writes a
0-amount ``rental`` ledger row (``meta.voucher_id`` + what it covered) so the
register still narrates the renewal.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import apply_debit, get_balance

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

SPEND_KIND = "raffle_ticket"
VOUCHER_LIFETIME_DAYS = 28


@dataclass(frozen=True)
class TicketPurchase:
    quantity: int
    price: int  # total charged
    week_total: int  # the member's tickets this week after the buy


@dataclass(frozen=True)
class DrawResult:
    iso_week: str
    winner_id: int | None
    tickets: int
    entrants: int
    voucher_id: int | None


def raffle_enabled(settings: EconSettings) -> bool:
    return bool(settings.raffle_enabled) and int(settings.price_raffle_ticket) > 0


def member_tickets(
    conn: sqlite3.Connection, guild_id: int, iso_week: str, user_id: int
) -> int:
    row = conn.execute(
        "SELECT count FROM econ_raffle_tickets "
        "WHERE guild_id = ? AND iso_week = ? AND user_id = ?",
        (guild_id, iso_week, user_id),
    ).fetchone()
    return int(row["count"]) if row else 0


def week_totals(
    conn: sqlite3.Connection, guild_id: int, iso_week: str
) -> tuple[int, int]:
    """(total tickets, distinct entrants) for a week."""
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) AS t, COUNT(*) AS e "
        "FROM econ_raffle_tickets "
        "WHERE guild_id = ? AND iso_week = ? AND count > 0",
        (guild_id, iso_week),
    ).fetchone()
    return int(row["t"]), int(row["e"])


def buy_tickets(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    iso_week: str,
    quantity: int,
) -> TicketPurchase:
    """Buy tickets for the current week. ValueError = member-facing text.

    The per-member weekly cap keeps a whale from buying certainty; the debit
    is a single ledger row for the whole batch. No refunds — tickets are
    week-scoped and the draw is the payoff.
    """
    if not raffle_enabled(settings):
        raise ValueError("The raffle isn't running here.")
    if quantity < 1:
        raise ValueError("Buy at least one ticket.")
    cap = max(0, int(settings.raffle_max_tickets))
    held = member_tickets(conn, guild_id, iso_week, user_id)
    if held + quantity > cap:
        room = max(0, cap - held)
        raise ValueError(
            f"The cap is {cap} tickets a week — you have {held}, so you can "
            f"buy {room} more."
            if room
            else f"You're at the {cap}-ticket weekly cap."
        )
    price = int(settings.price_raffle_ticket) * quantity
    unit = settings.currency_plural or "coins"
    if not apply_debit(
        conn, guild_id, user_id, price, SPEND_KIND,
        actor_id=user_id, meta={"iso_week": iso_week, "quantity": quantity},
    ):
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"{quantity} ticket(s) cost {price} {unit} — you have {have}."
        )
    conn.execute(
        """
        INSERT INTO econ_raffle_tickets (guild_id, iso_week, user_id, count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, iso_week, user_id)
        DO UPDATE SET count = count + excluded.count
        """,
        (guild_id, iso_week, user_id, quantity),
    )
    # shop_purchase quest trigger (one-time setup kind); deferred import —
    # the quests service imports the wider economy machinery.
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")
    return TicketPurchase(
        quantity=quantity, price=price,
        week_total=member_tickets(conn, guild_id, iso_week, user_id),
    )


def draw_raffle(
    conn: sqlite3.Connection,
    guild_id: int,
    iso_week: str,
    *,
    now: float | None = None,
    rng: random.Random | None = None,
) -> DrawResult | None:
    """Draw the week's winner and issue their voucher, exactly once.

    Returns None when this week was already drawn (the INSERT claim lost) —
    the caller treats that as a no-op re-run. A zero-ticket week records a
    winnerless draw so the re-run guard still holds. The draw row and the
    voucher land in the caller's transaction BEFORE any Discord side effect.
    """
    now = time.time() if now is None else now
    tickets, entrants = week_totals(conn, guild_id, iso_week)
    rows = conn.execute(
        "SELECT user_id, count FROM econ_raffle_tickets "
        "WHERE guild_id = ? AND iso_week = ? AND count > 0",
        (guild_id, iso_week),
    ).fetchall()

    winner_id: int | None = None
    if rows:
        rng = rng or random.Random()
        winner_id = int(
            rng.choices(
                [int(r["user_id"]) for r in rows],
                weights=[int(r["count"]) for r in rows],
            )[0]
        )

    try:
        conn.execute(
            "INSERT INTO econ_raffle_draws "
            "(guild_id, iso_week, winner_id, tickets, entrants, drawn_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, iso_week, winner_id, tickets, entrants, now),
        )
    except sqlite3.IntegrityError:
        return None  # already drawn — a week-roll re-run

    voucher_id: int | None = None
    if winner_id is not None:
        cur = conn.execute(
            "INSERT INTO econ_vouchers "
            "(guild_id, user_id, kind, state, source, created_at, expires_at) "
            "VALUES (?, ?, 'free_week', 'issued', ?, ?, ?)",
            (
                guild_id, winner_id, f"raffle:{iso_week}", now,
                now + VOUCHER_LIFETIME_DAYS * 86400,
            ),
        )
        voucher_id = int(cur.lastrowid or 0)
        conn.execute(
            "UPDATE econ_raffle_draws SET voucher_id = ? "
            "WHERE guild_id = ? AND iso_week = ?",
            (voucher_id, guild_id, iso_week),
        )
    return DrawResult(
        iso_week=iso_week, winner_id=winner_id, tickets=tickets,
        entrants=entrants, voucher_id=voucher_id,
    )


def get_draw(
    conn: sqlite3.Connection, guild_id: int, iso_week: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_raffle_draws WHERE guild_id = ? AND iso_week = ?",
        (guild_id, iso_week),
    ).fetchone()


def try_redeem_voucher(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    rental_id: int,
    perk: str,
    covered: int,
    now: float | None = None,
) -> sqlite3.Row | None:
    """Redeem the member's oldest live voucher against a rental debit.

    Called by the billing paths BEFORE they debit: a hit means the charge is
    covered — the caller skips the debit and this writes the 0-amount
    ``rental`` ledger row narrating it. The guarded UPDATE (state + not
    expired) is the race anchor, and it lazily expires overdue vouchers on
    the way through. Returns the redeemed voucher row, or None.
    """
    now = time.time() if now is None else now
    conn.execute(
        "UPDATE econ_vouchers SET state = 'expired' "
        "WHERE guild_id = ? AND user_id = ? AND state = 'issued' "
        "AND expires_at IS NOT NULL AND expires_at < ?",
        (guild_id, user_id, now),
    )
    row = conn.execute(
        "UPDATE econ_vouchers SET state = 'redeemed', redeemed_at = ?, "
        "rental_id = ? "
        "WHERE id = (SELECT id FROM econ_vouchers "
        "            WHERE guild_id = ? AND user_id = ? AND state = 'issued' "
        "            ORDER BY created_at ASC, id ASC LIMIT 1) "
        "RETURNING *",
        (now, rental_id, guild_id, user_id),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "INSERT INTO econ_ledger "
        "(guild_id, user_id, amount, kind, actor_id, meta, created_at) "
        "VALUES (?, ?, 0, 'rental', ?, ?, ?)",
        (
            guild_id, user_id, user_id,
            json.dumps(
                {
                    "rental_id": rental_id,
                    "perk": perk,
                    "voucher_id": int(row["id"]),
                    "covered": covered,
                }
            ),
            now,
        ),
    )
    return row


def live_voucher(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, now: float | None = None
) -> sqlite3.Row | None:
    """The member's oldest unexpired issued voucher, for display."""
    now = time.time() if now is None else now
    return conn.execute(
        "SELECT * FROM econ_vouchers "
        "WHERE guild_id = ? AND user_id = ? AND state = 'issued' "
        "AND (expires_at IS NULL OR expires_at >= ?) "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (guild_id, user_id, now),
    ).fetchone()
