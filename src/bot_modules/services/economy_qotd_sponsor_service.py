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
    get_balance,
)
from bot_modules.services import economy_submission_store as store

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

# Questions longer than this don't render on the QOTD card and don't read as a
# question anyway. Matches the modal's own cap.
MAX_QUESTION_LEN = 300
MIN_QUESTION_LEN = 8

_OPEN_STATES = ("pending", "approved")
SPEND_KIND = "qotd_sponsor"
REFUND_KIND = "qotd_sponsor_refund"

#: Everything the shared paid-submission mechanics need to know about this
#: product. What approval *produces* — a queue ``/qotd post`` draws from — is
#: this module's own business and stays below.
PRODUCT = store.SubmissionProduct(
    table="econ_qotd_submissions",
    spend_kind=SPEND_KIND,
    refund_kind=REFUND_KIND,
    open_states=_OPEN_STATES,
)


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
    return store.open_submission(conn, PRODUCT, guild_id, user_id)


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
    submission_id = store.charge_and_insert(
        conn, PRODUCT, guild_id, user_id, price, {"question": text}
    )
    if submission_id is None:
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(
            f"Sponsoring a question costs {price} {unit} — you have {have}."
        )
    return SponsorOutcome(submission_id=submission_id, price=price)


def _refund(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> int:
    """Give the money back exactly once. Returns the amount actually refunded."""
    return store.refund_once(
        conn, PRODUCT.table, row, reason, refund_kind=PRODUCT.refund_kind
    )


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
    row = store.get(conn, PRODUCT, submission_id)
    if row is None:
        raise ValueError("❌ That submission no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"❌ That submission is already {row['state']}.")

    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="pending",
        to_state="approved" if approve else "denied",
        resolver_id=resolver_id,
        deny_reason=deny_reason,
        refund_reason=None if approve else "denied",
    )
    if fresh is None:
        # Lost a race with another resolver; their write stands.
        raise ValueError("❌ That submission was just resolved by someone else.")
    return fresh


def withdraw_approved(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int, reason: str = ""
) -> sqlite3.Row:
    """Pull an already-approved question back out of the queue, refunding it."""
    if store.get(conn, PRODUCT, submission_id) is None:
        raise ValueError("That submission no longer exists.")
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="approved", to_state="denied",
        resolver_id=resolver_id,
        deny_reason=reason,
        refund_reason="withdrawn",
    )
    if fresh is None:
        raise ValueError("That question isn't waiting to be posted.")
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
    return store.expire_stale_pending(
        conn, PRODUCT, guild_id,
        days=max(0, int(settings.qotd_sponsor_expire_days)), now=now,
    )


def list_submissions(
    conn: sqlite3.Connection, guild_id: int, state: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    """Submissions for the dashboard queue, oldest first."""
    return store.list_for(conn, PRODUCT, guild_id, state, limit)


def get_submission(
    conn: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return store.get(conn, PRODUCT, submission_id)


def set_submission_card(
    conn: sqlite3.Connection, submission_id: int, channel_id: int, message_id: int
) -> None:
    """Record where the approval card lives so it can be edited on resolution."""
    store.set_card(conn, PRODUCT, submission_id, channel_id, message_id)
