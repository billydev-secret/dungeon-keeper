"""Pin of the Day — the paid, mod-approved daily pinned message (migration 108).

A member pays to pin a short message; a mod approves it; the bot posts a
"Pinned by @X" card in the pin channel and pins it for 24h, then auto-unpins.
The shape is deliberately sponsor-a-QOTD's
(:mod:`economy_qotd_sponsor_service`): a small state machine with the
uniqueness work pushed into partial indexes, and Discord I/O kept out — the
caller posts/pins/unpins and hands the resulting message ids back here.

Money conventions (identical to the QOTD sponsor):

* **Charged at submit.** A free queue invites spam, and the price is the whole
  point of the sink — so denial and *pending* expiry are refund paths. A pin
  that actually went live is NOT refunded on expiry: the member got their day.
* **Refunds are exactly-once**, guarded by a ``refunded_at IS NULL`` predicate
  in the same UPDATE that moves the state — never a caller-set flag, so a replay
  or double-click can't pay twice.
* A refund is a plain ``apply_credit`` with kind ``pin_sponsor_refund``.

State machine::

    pending ──approve+go_live──> live ──(24h)──> expired
       │                          │
       │                          └──replaced by a newer approval──> superseded
       ├──deny────> denied   (refund)
       └──expire──> expired  (refund; pending only)
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

# A pinned card should read at a glance; longer than this and it stops being a
# "message" and starts being a wall. Matches the modal's own cap.
MAX_PIN_LEN = 280
MIN_PIN_LEN = 1

# How long a live pin stays up before the hourly sweep unpins it.
PIN_LIFETIME_SECONDS = 24 * 3600

_OPEN_STATES = ("pending", "live")
SPEND_KIND = "pin_sponsor"
REFUND_KIND = "pin_sponsor_refund"

#: What the shared paid-submission mechanics need. The one-live-per-guild
#: supersede and the 24h clock are this product's own and stay below.
PRODUCT = store.SubmissionProduct(
    table="econ_pin_submissions",
    spend_kind=SPEND_KIND,
    refund_kind=REFUND_KIND,
    open_states=_OPEN_STATES,
)


@dataclass(frozen=True)
class PinOutcome:
    """Result of a submit: the row id and what it cost."""

    submission_id: int
    price: int


def pin_price(settings: EconSettings) -> int:
    """Configured price, or 0 when the feature is switched off."""
    return max(0, int(settings.price_pin_of_day))


def pin_enabled(settings: EconSettings) -> bool:
    """On only when a price AND a destination channel are both set.

    Price 0 disables it the way the other consumables do; a missing pin channel
    means there's nowhere to post, so the whole flow is dark until an admin sets
    both on the dashboard (announce before flipping it on — it's a public sink).
    """
    return pin_price(settings) > 0 and int(settings.pin_channel_id) > 0


def open_submission(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's in-flight submission (pending or live), if any."""
    return store.open_submission(conn, PRODUCT, guild_id, user_id)


def submit_pin(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    message: str,
) -> PinOutcome:
    """Charge for and queue one pin. ValueError carries member-facing text.

    Validation runs before the debit, and the debit before the insert, so a
    rejected submission never costs anything and a failed insert never strands a
    payment (both live in the caller's transaction).
    """
    if not pin_enabled(settings):
        raise ValueError("Pinning a message isn't enabled here.")
    text = "\n".join(line.rstrip() for line in message.splitlines()).strip()
    if len(text) < MIN_PIN_LEN:
        raise ValueError("There's nothing to pin — type a message first.")
    if len(text) > MAX_PIN_LEN:
        raise ValueError(f"Pinned messages are limited to {MAX_PIN_LEN} characters.")
    if open_submission(conn, guild_id, user_id) is not None:
        raise ValueError(
            "You already have a pin waiting or up — once it's declined, expires, "
            "or its day is over you can buy another."
        )

    price = pin_price(settings)
    unit = settings.currency_plural or "coins"
    submission_id = store.charge_and_insert(
        conn, PRODUCT, guild_id, user_id, price, {"message": text}
    )
    if submission_id is None:
        have = get_balance(conn, guild_id, user_id)
        raise ValueError(f"Pinning a message costs {price} {unit} — you have {have}.")
    return PinOutcome(submission_id=submission_id, price=price)


def _refund(conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> int:
    """Give the money back exactly once. Returns the amount actually refunded."""
    return store.refund_once(
        conn, PRODUCT.table, row, reason, refund_kind=PRODUCT.refund_kind
    )


def deny(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    resolver_id: int,
    deny_reason: str = "",
) -> sqlite3.Row:
    """Decline a pending submission and refund it. Returns the fresh row.

    Only ``pending`` declines here — a live pin is pulled with :func:`take_down`.
    """
    row = store.get(conn, PRODUCT, submission_id)
    if row is None:
        raise ValueError("That submission no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"That submission is already {row['state']}.")

    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="pending", to_state="denied",
        resolver_id=resolver_id, deny_reason=deny_reason,
        refund_reason="denied",
    )
    if fresh is None:
        raise ValueError("That submission was just resolved by someone else.")
    return fresh


@dataclass(frozen=True)
class GoLiveResult:
    """The freshly-live row plus the prior live pin it replaced (to unpin)."""

    live: sqlite3.Row
    superseded: sqlite3.Row | None


def go_live(
    conn: sqlite3.Connection,
    submission_id: int,
    *,
    resolver_id: int,
    pin_channel_id: int,
    pin_message_id: int,
    now: float | None = None,
) -> GoLiveResult:
    """Promote an approved-pending submission to the guild's single live pin.

    Supersedes any current live pin FIRST (in the same transaction) so the
    one-live-per-guild unique index never collides, returning that superseded
    row so the caller can unpin its Discord message. Sets the 24h ``expires_at``.
    Raises ValueError if the row isn't pending any more (declined / raced).
    """
    now = time.time() if now is None else now
    row = conn.execute(
        "SELECT * FROM econ_pin_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise ValueError("That submission no longer exists.")
    if str(row["state"]) != "pending":
        raise ValueError(f"That submission is already {row['state']}.")
    guild_id = int(row["guild_id"])

    # Retire the prior live pin (if any) before promoting this one.
    prior = conn.execute(
        "SELECT * FROM econ_pin_submissions WHERE guild_id = ? AND state = 'live'",
        (guild_id,),
    ).fetchone()
    if prior is not None:
        conn.execute(
            "UPDATE econ_pin_submissions SET state = 'superseded', resolved_at = ? "
            "WHERE id = ? AND state = 'live'",
            (now, int(prior["id"])),
        )

    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="pending", to_state="live",
        resolver_id=resolver_id, now=now,
        extra={
            "went_live_at": now,
            "expires_at": now + PIN_LIFETIME_SECONDS,
            "pin_channel_id": pin_channel_id,
            "pin_message_id": pin_message_id,
        },
    )
    if fresh is None:
        raise ValueError("That submission was just resolved by someone else.")
    return GoLiveResult(live=fresh, superseded=prior)


def take_down(
    conn: sqlite3.Connection, submission_id: int, *, resolver_id: int
) -> sqlite3.Row:
    """Pull a live pin down early (a mod yank). No refund — its day was up.

    Returns the row (carrying the pin's channel/message ids) so the caller can
    unpin the Discord message.
    """
    fresh = store.move_state(
        conn, PRODUCT, submission_id,
        from_state="live", to_state="expired", resolver_id=resolver_id,
    )
    if fresh is None:
        raise ValueError("That pin isn't up any more.")
    return fresh


def expire_live_pins(
    conn: sqlite3.Connection, guild_id: int, *, now: float
) -> list[sqlite3.Row]:
    """Retire live pins past their 24h lifetime. Returns rows to unpin.

    No refund: a pin that ran its day is a completed purchase. The caller unpins
    and deletes each returned row's Discord message after the transaction commits.
    """
    due = conn.execute(
        "SELECT * FROM econ_pin_submissions "
        "WHERE guild_id = ? AND state = 'live' AND expires_at IS NOT NULL "
        "AND expires_at <= ?",
        (guild_id, now),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in due:
        if store.move_state(
            conn, PRODUCT, int(row["id"]),
            from_state="live", to_state="expired", now=now,
        ) is not None:
            out.append(row)
    return out


def expire_stale_pending(
    conn: sqlite3.Connection, settings: EconSettings, guild_id: int, *, now: float
) -> list[sqlite3.Row]:
    """Expire and refund pending submissions no mod resolved. Returns the rows.

    Only ``pending`` expires (a live pin is time-boxed by :func:`expire_live_pins`).
    ``pin_expire_days`` of 0 disables the sweep, keeping a slow queue alive.
    """
    return store.expire_stale_pending(
        conn, PRODUCT, guild_id, days=max(0, int(settings.pin_expire_days)), now=now,
    )


def refund_failed_golive(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Refund a pin that was approved but couldn't be posted (channel/perm error).

    The state is already moved off pending by the failed :func:`go_live` attempt
    only if it succeeded in DB — this path is for when the caller decided the
    Discord post failed. It flips the row to ``denied`` (with a reason) and
    refunds, exactly-once.
    """
    conn.execute(
        "UPDATE econ_pin_submissions SET state = 'denied', "
        "deny_reason = 'Could not post the pin — refunded.', resolved_at = ? "
        "WHERE id = ? AND state IN ('pending', 'live')",
        (time.time(), int(row["id"])),
    )
    _refund(conn, row, "post_failed")


def list_submissions(
    conn: sqlite3.Connection, guild_id: int, state: str | None = None, limit: int = 100
) -> list[sqlite3.Row]:
    """Submissions for a dashboard queue, oldest first (or newest when unfiltered)."""
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
