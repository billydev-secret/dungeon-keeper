"""Live auctions — mod-run, ascending, the winning bid burned (migration 117).

A mod opens a freeform auction; members bid up in the open; the winning bid is
destroyed (the sink). The bounty's sibling — escrow via ``apply_debit``, refunds
via ``apply_credit`` — but instead of many contributions pooling, exactly ONE
bid is live at a time and the previous high bidder is refunded the instant they
are outbid.

Money::

    bid (win the slot) -> apply_debit  (auction_bid)     escrow
    outbid             -> apply_credit (auction_refund)  loser back in full
    close              -> (nothing)   winning escrow never refunded = burned
    cancel             -> apply_credit (auction_refund)  standing bid back

Escrow-at-bid means a member can never bid coins they don't have, and the winner
is already charged before close — settlement moves no money, it just freezes the
result and leaves the escrow destroyed. A mod-curated prize is granted out of
band, so nothing flows back in.

The standing high bid lives on the auction row, guarded by **compare-and-swap**:
the claim UPDATE wins the slot only if the row's high bid is still exactly what
the bidder validated against (``high_bid IS :old``). This is the house
``UPDATE … WHERE … RETURNING`` idiom (see casino settle), applied to the high-bid
slot. **Money is safe under any concurrency** — no bid can win on a stale read.

Concurrency: bids MUST run under ``BEGIN IMMEDIATE`` (``place_bid_now`` /
``open_db_immediate``), which takes the write lock before the read so concurrent
bidders serialize and each re-reads the latest state — no stale WAL snapshot, no
``SQLITE_BUSY_SNAPSHOT``. The compare-and-swap is then a correctness backstop
(it also covers same-connection stale claims). ``place_bid_now`` additionally
retries a handful of times on a transient ``OperationalError`` and, if it still
can't get the lock, raises a friendly ValueError rather than a raw sqlite error.
Calling ``place_bid`` under plain ``open_db`` (DEFERRED) is only safe for a
single connection (the tests) — never wire the cog to it directly.

Pure DB, no Discord — the caller owns the card and the DMs, matching the bounty
and pin services. Every function rides the caller's transaction, so a raised
ValueError rolls back any escrow taken on the way.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import open_db_immediate
from bot_modules.services.economy_service import apply_credit, apply_debit

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.services.economy_service import EconSettings

BID_KIND = "auction_bid"
REFUND_KIND = "auction_refund"

MAX_TITLE_LEN = 100
MIN_TITLE_LEN = 3
MAX_DESC_LEN = 500


@dataclass(frozen=True)
class BidResult:
    auction_id: int
    amount: int
    outbid_user_id: int | None  # previous high bidder, refunded (None = first bid)
    outbid_amount: int
    ends_at: float  # possibly extended by the soft close
    extended: bool


@dataclass(frozen=True)
class SettledAuction:
    auction_id: int
    title: str
    channel_id: int
    message_id: int
    created_by: int
    winner_id: int | None
    winning_bid: int


# ── config ────────────────────────────────────────────────────────────────


def _min_bid(settings: EconSettings) -> int:
    return max(1, int(settings.auction_min_bid))


def _min_increment(settings: EconSettings) -> int:
    return max(1, int(settings.auction_min_increment))


def _soft_close(settings: EconSettings) -> int:
    return max(0, int(settings.auction_soft_close_seconds))


def _max_duration_hours(settings: EconSettings) -> int:
    return max(1, int(settings.auction_max_duration_hours))


# ── reads ─────────────────────────────────────────────────────────────────


def get_open_auction(
    conn: sqlite3.Connection, guild_id: int
) -> sqlite3.Row | None:
    """The guild's single live auction, or None. (v1: one at a time.)"""
    return conn.execute(
        "SELECT * FROM econ_auctions WHERE guild_id = ? AND state = 'open' "
        "ORDER BY id LIMIT 1",
        (guild_id,),
    ).fetchone()


def open_auction_guild_ids(conn: sqlite3.Connection) -> set[int]:
    """Every guild id with a live auction — one indexed read for the settle loop.

    Lets the periodic sweep skip guilds that have no auction (almost all of
    them, since auctions are rare and dark by default) instead of opening a
    connection per guild each tick.
    """
    return {
        int(r["guild_id"])
        for r in conn.execute(
            "SELECT DISTINCT guild_id FROM econ_auctions WHERE state = 'open'"
        )
    }


def get_auction(
    conn: sqlite3.Connection, auction_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_auctions WHERE id = ?", (auction_id,)
    ).fetchone()


def bid_count(conn: sqlite3.Connection, auction_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_auction_bids WHERE auction_id = ?",
        (auction_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def min_next_bid(settings: EconSettings, auction: sqlite3.Row) -> int:
    """The lowest amount that would win the slot right now."""
    high = auction["high_bid"]
    if high is None:
        return _min_bid(settings)
    return int(high) + _min_increment(settings)


# ── open ──────────────────────────────────────────────────────────────────


def open_auction(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    *,
    created_by: int,
    title: str,
    description: str,
    duration_hours: float,
    channel_id: int = 0,
    now: float | None = None,
) -> int:
    """Open a new auction. Returns its id. Rejects while one is already live.

    Raises ValueError (rolls the caller's txn back) on a bad title/description,
    a duration outside 1h..max, or a second concurrent auction.
    """
    now = time.time() if now is None else now
    title = (title or "").strip()
    description = (description or "").strip()
    if len(title) < MIN_TITLE_LEN:
        raise ValueError("Give the auction a title (a few words at least).")
    if len(title) > MAX_TITLE_LEN:
        raise ValueError(f"Titles are limited to {MAX_TITLE_LEN} characters.")
    if len(description) > MAX_DESC_LEN:
        raise ValueError(
            f"Descriptions are limited to {MAX_DESC_LEN} characters."
        )
    max_h = _max_duration_hours(settings)
    if not (1 <= duration_hours <= max_h):
        raise ValueError(f"Duration must be between 1 and {max_h} hours.")
    if get_open_auction(conn, guild_id) is not None:
        raise ValueError(
            "There's already a live auction — end or cancel it first."
        )
    ends_at = now + duration_hours * 3600.0
    cur = conn.execute(
        """
        INSERT INTO econ_auctions
            (guild_id, channel_id, title, description, created_by, state,
             min_bid, min_increment, soft_close_seconds, ends_at, created_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
        """,
        (
            guild_id, channel_id, title, description, created_by,
            _min_bid(settings), _min_increment(settings), _soft_close(settings),
            ends_at, now,
        ),
    )
    return int(cur.lastrowid or 0)


def attach_card(
    conn: sqlite3.Connection, auction_id: int, channel_id: int, message_id: int
) -> None:
    """Record where the sticky card was posted (for later repaints)."""
    conn.execute(
        "UPDATE econ_auctions SET channel_id = ?, message_id = ? WHERE id = ?",
        (channel_id, message_id, auction_id),
    )


# ── bid (the atomic heart) ──────────────────────────────────────────────────


def _claim_high_slot(
    conn: sqlite3.Connection,
    auction_id: int,
    old_high: int | None,
    old_bidder: int | None,
    *,
    new_amount: int,
    new_bidder: int,
    new_end: float,
) -> bool:
    """Compare-and-swap the standing high bid; True iff this bid won the slot.

    The guard is ``high_bid IS :old_high AND high_bidder_id IS :old_bidder`` —
    the exact values the caller validated against. If anything moved the slot in
    between (a bid that committed after the caller's read), the WHERE misses,
    zero rows update, and this returns False without touching a coin. ``IS``
    (not ``=``) so a first bid, where both are NULL, matches correctly.
    """
    claimed = conn.execute(
        """
        UPDATE econ_auctions
        SET high_bid = ?, high_bidder_id = ?, ends_at = ?
        WHERE id = ? AND state = 'open'
          AND high_bid IS ? AND high_bidder_id IS ?
        RETURNING id
        """,
        (new_amount, new_bidder, new_end, auction_id, old_high, old_bidder),
    ).fetchone()
    return claimed is not None


def place_bid(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    auction_id: int,
    user_id: int,
    amount: int,
    *,
    now: float | None = None,
) -> BidResult:
    """Place an ascending bid; escrow it and refund whoever it outbids.

    Compare-and-swap on the standing high bid makes the two-bids race safe: the
    claim UPDATE only wins the slot while ``high_bid`` is still exactly the value
    read during validation, so a bid that slipped in between makes this one miss
    and raise 'just outbid' — before any coins move.

    Raises ValueError (rolling back the caller's txn) for: no such/closed
    auction, an ended auction, the current high bidder bidding again, a bid
    below the next-min, insufficient balance, or losing the CAS race.
    """
    now = time.time() if now is None else now
    if amount < 1:
        raise ValueError("Bid something above zero.")

    row = conn.execute(
        "SELECT * FROM econ_auctions WHERE id = ? AND guild_id = ?",
        (auction_id, guild_id),
    ).fetchone()
    if row is None:
        raise ValueError("That auction doesn't exist.")
    if row["state"] != "open":
        raise ValueError(f"That auction is {row['state']} — bidding is over.")
    if now >= float(row["ends_at"]):
        raise ValueError("That auction has ended.")

    old_high = row["high_bid"]
    old_bidder = row["high_bidder_id"]
    if old_bidder is not None and int(old_bidder) == user_id:
        raise ValueError("You're already the high bidder.")

    floor = (
        int(old_high) + _min_increment(settings)
        if old_high is not None
        else _min_bid(settings)
    )
    if amount < floor:
        raise ValueError(f"Bid at least {floor}.")

    # Soft close: a bid inside the window pushes the end out so the auction
    # isn't won on the timer. Fold the new end into the claim so it lands
    # atomically with the slot.
    window = _soft_close(settings)
    end = float(row["ends_at"])
    extended = window > 0 and (end - now) < window
    new_end = now + window if extended else end

    # Escrow FIRST, before claiming the slot. A failed debit (insufficient
    # balance) returns False having written nothing, so the common rejection
    # touches no auction state at all — no rollback needed. Only the rare
    # lost-CAS-race below relies on the caller's transaction to undo this.
    if not apply_debit(conn, guild_id, user_id, amount, BID_KIND,
                       meta={"auction_id": auction_id}):
        raise ValueError("You don't have enough to cover that bid.")

    # Compare-and-swap: claim the high slot ONLY if it still reads what we
    # validated against. A racer that committed a higher bid in between changes
    # high_bid/high_bidder, the WHERE misses, and this bid is rejected clean —
    # raising rolls back the escrow just taken.
    # Reachable only for a same-connection stale claim; a cross-connection race
    # conflicts on the debit above with OperationalError first (see module
    # docstring's concurrency caveat — Stage 1 must run bids BEGIN IMMEDIATE).
    if not _claim_high_slot(
        conn, auction_id, old_high, old_bidder,
        new_amount=amount, new_bidder=user_id, new_end=new_end,
    ):
        raise ValueError("Someone just outbid you — try again a bit higher.")

    # Refund the member we just displaced, and retire their escrow row.
    if old_bidder is not None and old_high is not None:
        apply_credit(
            conn, guild_id, int(old_bidder), int(old_high), REFUND_KIND,
            meta={"auction_id": auction_id, "outbid": True},
        )
        conn.execute(
            "UPDATE econ_auction_bids SET state = 'refunded', refunded_at = ? "
            "WHERE auction_id = ? AND user_id = ? AND state = 'escrowed'",
            (now, auction_id, int(old_bidder)),
        )

    conn.execute(
        "INSERT INTO econ_auction_bids "
        "(auction_id, guild_id, user_id, amount, state, created_at) "
        "VALUES (?, ?, ?, ?, 'escrowed', ?)",
        (auction_id, guild_id, user_id, amount, now),
    )
    return BidResult(
        auction_id=auction_id,
        amount=amount,
        outbid_user_id=int(old_bidder) if old_bidder is not None else None,
        outbid_amount=int(old_high) if old_high is not None else 0,
        ends_at=new_end,
        extended=extended,
    )


def place_bid_now(
    db_path: Path,
    settings: EconSettings,
    guild_id: int,
    auction_id: int,
    user_id: int,
    amount: int,
    *,
    retries: int = 5,
    now: float | None = None,
) -> BidResult:
    """Run :func:`place_bid` in its own ``BEGIN IMMEDIATE`` transaction.

    The entry point the cog uses. BEGIN IMMEDIATE takes the write lock before
    the read, so concurrent bidders serialize instead of racing on a stale
    snapshot. Each attempt uses a *short* busy_timeout so a contended lock fails
    fast and retries rather than blocking the worker for the full 30s — the
    retry budget (retries × ~2s) caps the worst-case wait at ~10s, not ~150s. A
    persisting ``OperationalError`` surfaces as a friendly ValueError, not a raw
    sqlite error. A ValueError from ``place_bid`` (bad bid, outbid, insufficient)
    is a real rejection and propagates unretried.
    """
    last: sqlite3.OperationalError | None = None
    for _ in range(max(1, retries)):
        try:
            with open_db_immediate(db_path, busy_timeout_ms=2000) as conn:
                return place_bid(
                    conn, settings, guild_id, auction_id, user_id, amount, now=now
                )
        except sqlite3.OperationalError as exc:
            last = exc  # busy/locked — queue and try again
    raise ValueError(
        "The auction is busy right now — give that bid another try."
    ) from last


# ── close & cancel ──────────────────────────────────────────────────────────


def _settle_row(row: sqlite3.Row) -> SettledAuction:
    winner = row["high_bidder_id"]
    return SettledAuction(
        auction_id=int(row["id"]),
        title=str(row["title"]),
        channel_id=int(row["channel_id"] or 0),
        message_id=int(row["message_id"] or 0),
        created_by=int(row["created_by"]),
        winner_id=int(winner) if winner is not None else None,
        winning_bid=int(row["high_bid"] or 0),
    )


def _close_won_escrow(conn: sqlite3.Connection, auction_id: int, winner_id: int) -> None:
    """Flip the winner's escrow to 'won'. The coins stay gone — that's the burn."""
    conn.execute(
        "UPDATE econ_auction_bids SET state = 'won' "
        "WHERE auction_id = ? AND user_id = ? AND state = 'escrowed'",
        (auction_id, winner_id),
    )


def _finalize(conn: sqlite3.Connection, row: sqlite3.Row, now: float) -> SettledAuction:
    """Freeze an already-claimed auction row into a closed result."""
    settled = _settle_row(row)
    conn.execute(
        "UPDATE econ_auctions SET winner_id = ?, winning_bid = ?, closed_at = ? "
        "WHERE id = ?",
        (settled.winner_id, settled.winning_bid, now, settled.auction_id),
    )
    if settled.winner_id is not None:
        _close_won_escrow(conn, settled.auction_id, settled.winner_id)
    return settled


def settle_due_auctions(
    conn: sqlite3.Connection, guild_id: int, *, now: float | None = None
) -> list[SettledAuction]:
    """Close every open auction past its end. Exactly-once via the state claim.

    Moves no money — the winner was charged at bid time, so closing just freezes
    the result and leaves the escrow burned. Returns the settled auctions (with
    winner + burned amount) for the caller to post result cards.
    """
    now = time.time() if now is None else now
    settled: list[SettledAuction] = []
    while True:
        claimed = conn.execute(
            "UPDATE econ_auctions SET state = 'closed' "
            "WHERE state = 'open' AND id = (SELECT id FROM econ_auctions "
            "            WHERE guild_id = ? AND state = 'open' AND ends_at <= ? "
            "            ORDER BY id LIMIT 1) "
            "RETURNING *",
            (guild_id, now),
        ).fetchone()
        if claimed is None:
            return settled
        settled.append(_finalize(conn, claimed, now))


def end_auction_now(
    conn: sqlite3.Connection, guild_id: int, auction_id: int, *, now: float | None = None
) -> SettledAuction | None:
    """Mod force-close: settle a specific open auction immediately.

    None = it wasn't open (already closed/cancelled, or lost the claim race).
    Same money semantics as the timed close — the standing bid is burned.
    """
    now = time.time() if now is None else now
    claimed = conn.execute(
        "UPDATE econ_auctions SET state = 'closed' "
        "WHERE id = ? AND guild_id = ? AND state = 'open' RETURNING *",
        (auction_id, guild_id),
    ).fetchone()
    if claimed is None:
        return None
    return _finalize(conn, claimed, now)


def cancel_auction(
    conn: sqlite3.Connection,
    guild_id: int,
    auction_id: int,
    *,
    resolver_id: int,
    now: float | None = None,
) -> sqlite3.Row | None:
    """Mod cancel: refund the standing high bidder, no burn. Exactly-once.

    None = it wasn't open. Returns the (pre-cancel) auction row so the caller
    can repaint the card and tell the refunded bidder.
    """
    now = time.time() if now is None else now
    claimed = conn.execute(
        "UPDATE econ_auctions SET state = 'cancelled', resolver_id = ?, closed_at = ? "
        "WHERE id = ? AND guild_id = ? AND state = 'open' RETURNING *",
        (resolver_id, now, auction_id, guild_id),
    ).fetchone()
    if claimed is None:
        return None
    bidder = claimed["high_bidder_id"]
    high = claimed["high_bid"]
    if bidder is not None and high is not None:
        apply_credit(
            conn, guild_id, int(bidder), int(high), REFUND_KIND,
            meta={"auction_id": auction_id, "cancelled": True},
        )
        conn.execute(
            "UPDATE econ_auction_bids SET state = 'refunded', refunded_at = ? "
            "WHERE auction_id = ? AND user_id = ? AND state = 'escrowed'",
            (now, auction_id, int(bidder)),
        )
    return claimed
