"""Sponsor-a-QOTD — the paid, mod-approved question queue (spec §6, migration 090).

A member pays to put a question in front of the server; a mod approves it; the
approved question waits in a queue that ``/qotd post`` draws from, credited to
the sponsor. The shape is deliberately the quest sign-off claim's
(``economy_quests_service.resolve_claim``): a small state machine with the
uniqueness work pushed into partial indexes.

Money conventions:

* **Charged at submit.** A free queue invites spam, and the price is the whole
  point of the sink. That makes denial and expiry *refund* paths, not no-ops.
* **Refunds are exactly-once**, guarded by a ``refunded_at IS NULL`` predicate
  in the same UPDATE that moves the state — not by a caller-set flag. A replay
  or a double-click therefore cannot pay twice.
* A refund is a plain ``apply_credit`` with kind ``qotd_sponsor_refund``, never
  a negative debit, so the register reads as money returning.

State machine::

    pending ──approve──> approved ──post──> posted
       │                     │
       ├──deny────> denied   └──deny────> denied   (both refund)
       └──expire──> expired                        (refunds)
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.services.economy_service import (
    apply_credit,
    apply_debit,
    get_balance,
)

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

# Questions longer than this don't render on the QOTD card and don't read as a
# question anyway. Matches the modal's own cap.
MAX_QUESTION_LEN = 300
MIN_QUESTION_LEN = 8

_OPEN_STATES = ("pending", "approved")
SPEND_KIND = "qotd_sponsor"
REFUND_KIND = "qotd_sponsor_refund"


@dataclass(frozen=True)
class SponsorOutcome:
    """Result of a submit: the row id and what it cost."""

    submission_id: int
    price: int


def sponsor_price(settings: EconSettings) -> int:
    """Configured price, or 0 when sponsoring is switched off."""
    return max(0, int(settings.price_qotd_sponsor))


def sponsor_enabled(settings: EconSettings) -> bool:
    """Sponsoring is off at price 0, matching how the other consumables disable."""
    return sponsor_price(settings) > 0


def open_submission(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's in-flight submission (pending or approved), if any."""
    placeholders = ", ".join("?" for _ in _OPEN_STATES)
    return conn.execute(
        "SELECT * FROM econ_qotd_submissions "
        f"WHERE guild_id = ? AND user_id = ? AND state IN ({placeholders})",
        (guild_id, user_id, *_OPEN_STATES),
    ).fetchone()


def submit_sponsor(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    question: str,
) -> SponsorOutcome:
    """Charge for and queue one sponsored question. ValueError with member-facing text.

    Validation runs before the debit, and the debit before the insert, so a
    rejected submission never costs anything and a failed insert never
    strands a payment (both live in the caller's transaction).
    """
    if not sponsor_enabled(settings):
        raise ValueError("Sponsoring a question of the day isn't enabled here.")
    text = " ".join(question.split())
    if len(text) < MIN_QUESTION_LEN:
        raise ValueError("That's a bit short for a question of the day.")
    if len(text) > MAX_QUESTION_LEN:
        raise ValueError(
            f"Questions are limited to {MAX_QUESTION_LEN} characters."
        )
    if open_submission(conn, guild_id, user_id) is not None:
        raise ValueError(
            "You already have a question waiting — once it runs (or gets "
            "turned down) you can sponsor another."
        )

    price = sponsor_price(settings)
    unit = settings.currency_plural or "coins"
    if not apply_debit(
        conn, guild_id, user_id, price, SPEND_KIND, meta={"question": text}
    ):
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"Sponsoring a question costs {price} {unit} — you have {have}."
        )
    now = time.time()
    cur = conn.execute(
        "INSERT INTO econ_qotd_submissions "
        "(guild_id, user_id, question, state, price, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (guild_id, user_id, text, price, now),
    )
    return SponsorOutcome(submission_id=int(cur.lastrowid or 0), price=price)


def _refund(
    conn: sqlite3.Connection, row: sqlite3.Row, reason: str
) -> int:
    """Give the money back exactly once. Returns the amount actually refunded."""
    price = int(row["price"])
    if price < 1:
        return 0
    now = time.time()
    # The refunded_at predicate is the guard: a second call updates 0 rows and
    # credits nothing, even if the state moved in between.
    cur = conn.execute(
        "UPDATE econ_qotd_submissions SET refunded_at = ? "
        "WHERE id = ? AND refunded_at IS NULL",
        (now, int(row["id"])),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    apply_credit(
        conn,
        int(row["guild_id"]),
        int(row["user_id"]),
        price,
        REFUND_KIND,
        meta={"submission_id": int(row["id"]), "reason": reason},
        booster=False,
    )
    return price


def resolve_submission(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    approve: bool,
    resolver_id: int,
    deny_reason: str = "",
) -> sqlite3.Row:
    """Approve or deny a pending submission. Denial refunds. Returns the fresh row.

    Only ``pending`` resolves — an approved question is already in the post
    queue and is withdrawn with :func:`withdraw_approved`, not re-resolved.
    """
    row = conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"That submission is already {row['state']}.")

    now = time.time()
    state = "approved" if approve else "denied"
    cur = conn.execute(
        "UPDATE econ_qotd_submissions SET state = ?, resolver_id = ?, "
        "resolved_at = ?, deny_reason = ? WHERE id = ? AND state = 'pending'",
        (state, resolver_id, now, deny_reason[:500], submission_id),
    )
    if (cur.rowcount or 0) == 0:
        # Lost a race with another resolver; their write stands.
        raise ValueError("That submission was just resolved by someone else.")
    if not approve:
        _refund(conn, row, "denied")
    fresh = conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    assert fresh is not None
    return fresh


def withdraw_approved(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int, reason: str = ""
) -> sqlite3.Row:
    """Pull an already-approved question back out of the queue, refunding it."""
    row = conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    cur = conn.execute(
        "UPDATE econ_qotd_submissions SET state = 'denied', resolver_id = ?, "
        "resolved_at = ?, deny_reason = ? WHERE id = ? AND state = 'approved'",
        (resolver_id, time.time(), reason[:500], submission_id),
    )
    if (cur.rowcount or 0) == 0:
        raise ValueError("That question isn't waiting to be posted.")
    _refund(conn, row, "withdrawn")
    fresh = conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    assert fresh is not None
    return fresh


def next_approved(
    conn: sqlite3.Connection, guild_id: int
) -> sqlite3.Row | None:
    """The oldest approved question waiting to be posted (FIFO)."""
    return conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE guild_id = ? AND state = 'approved' "
        "ORDER BY created_at ASC, id ASC LIMIT 1",
        (guild_id,),
    ).fetchone()


def claim_next_approved(
    conn: sqlite3.Connection, guild_id: int
) -> sqlite3.Row | None:
    """Atomically take the oldest approved question off the queue for posting.

    Flips approved → posted and returns the row, or None when the queue is
    empty. Claiming *before* the message sends is what stops two mods racing
    ``/qotd post`` from both posting the same question; the caller must call
    :func:`release_claim` if the send then fails, so a member's paid slot is
    never silently eaten by a Discord error.
    """
    row = conn.execute(
        "UPDATE econ_qotd_submissions SET state = 'posted', posted_at = ? "
        "WHERE id = (SELECT id FROM econ_qotd_submissions "
        "            WHERE guild_id = ? AND state = 'approved' "
        "            ORDER BY created_at ASC, id ASC LIMIT 1) "
        "RETURNING *",
        (time.time(), guild_id),
    ).fetchone()
    return row


def release_claim(conn: sqlite3.Connection, submission_id: int) -> bool:
    """Put a claimed-but-unposted question back in the queue (send failed)."""
    cur = conn.execute(
        "UPDATE econ_qotd_submissions SET state = 'approved', posted_at = NULL "
        "WHERE id = ? AND state = 'posted' AND qotd_id IS NULL",
        (submission_id,),
    )
    return (cur.rowcount or 0) > 0


def attach_qotd(conn: sqlite3.Connection, submission_id: int, qotd_id: int) -> None:
    """Record which posted QOTD a claimed submission became."""
    conn.execute(
        "UPDATE econ_qotd_submissions SET qotd_id = ? WHERE id = ?",
        (qotd_id, submission_id),
    )


def mark_posted(
    conn: sqlite3.Connection, submission_id: int, qotd_id: int
) -> bool:
    """Flip an approved submission to posted. False if it wasn't approved any more.

    Guarded on ``state = 'approved'`` so two mods racing ``/qotd post`` can't
    both claim the same queued question.
    """
    cur = conn.execute(
        "UPDATE econ_qotd_submissions SET state = 'posted', posted_at = ?, "
        "qotd_id = ? WHERE id = ? AND state = 'approved'",
        (time.time(), qotd_id, submission_id),
    )
    return (cur.rowcount or 0) > 0


def expire_stale_submissions(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, *, now: float
) -> list[sqlite3.Row]:
    """Expire and refund pending submissions nobody got to. Returns the expired rows.

    Only ``pending`` expires: an approved question has been accepted and is
    waiting on a mod to run ``/qotd post``, and timing that out would punish
    the member for staff latency.
    """
    days = max(0, int(settings.qotd_sponsor_expire_days))
    if days <= 0:
        return []
    cutoff = now - days * 86400.0
    stale = conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE guild_id = ? AND state = 'pending' "
        "AND created_at < ?",
        (guild_id, cutoff),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in stale:
        cur = conn.execute(
            "UPDATE econ_qotd_submissions SET state = 'expired', resolved_at = ? "
            "WHERE id = ? AND state = 'pending'",
            (now, int(row["id"])),
        )
        if (cur.rowcount or 0) == 0:
            continue
        _refund(conn, row, "expired")
        out.append(row)
    return out


def list_submissions(
    conn: sqlite3.Connection, guild_id: int, state: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    """Submissions for the dashboard queue, oldest first."""
    limit = min(max(limit, 1), 500)
    if state:
        return conn.execute(
            "SELECT * FROM econ_qotd_submissions WHERE guild_id = ? AND state = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (guild_id, state, limit),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE guild_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


def get_submission(
    conn: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM econ_qotd_submissions WHERE id = ?", (submission_id,)
    ).fetchone()


def set_submission_card(
    conn: sqlite3.Connection, submission_id: int, channel_id: int, message_id: int
) -> None:
    """Record where the approval card lives so it can be edited on resolution."""
    conn.execute(
        "UPDATE econ_qotd_submissions SET card_channel_id = ?, card_message_id = ? "
        "WHERE id = ?",
        (channel_id, message_id, submission_id),
    )
