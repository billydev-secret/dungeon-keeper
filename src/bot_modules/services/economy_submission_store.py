"""Shared SQL for the economy's paid-submission queues.

Four products let a member spend coins on something a mod then approves or
denies: a pinned message, a sponsored question of the day, a custom emoji, and
a flash theme. They are separate features with separate tables, separate
prices and — quite deliberately — separate member-facing copy. What they don't
need is four copies of the ledger mechanics underneath.

Community bounties look superficially similar and are deliberately NOT here:
a bounty is a many-payer pot with a rake, so its money is escrowed per
contributor rather than per submission, and it has no pending state and no
approval queue at all. Nothing below would fit it without bending.

Only the mechanism lives here. The wording of a receipt, the shape of a review
card, the length caps, and what approval actually *produces* all stay with the
product, because that is where someone editing them will look. The sharpest
expression of that line is :func:`move_state`, which returns None when it
loses a race rather than raising: the "someone else just resolved that"
sentence is copy, and copy belongs to the product.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from bot_modules.core.db_utils import sql_identifier as _ident
from bot_modules.services.economy_service import apply_credit, apply_debit


def refund_once(
    conn: sqlite3.Connection,
    table: str,
    row: Any,
    reason: str,
    *,
    refund_kind: str,
) -> int:
    """Give the money back exactly once. Returns the amount actually refunded.

    Idempotency is the whole point, and it rides on the UPDATE's own
    predicate rather than on a prior read: the statement only matches a row
    whose ``refunded_at`` is still NULL, so a second call updates nothing and
    credits nothing even if the state moved in between. Two mods denying the
    same submission at the same moment therefore pay out once, not twice.

    A price below 1 is a no-op — free submissions exist and crediting zero
    would put a meaningless row in the ledger.
    """
    price = int(row["price"])
    if price < 1:
        return 0
    cur = conn.execute(
        f"UPDATE {_ident(table)} SET refunded_at = ? WHERE id = ? AND refunded_at IS NULL",
        (time.time(), int(row["id"])),
    )
    if (cur.rowcount or 0) == 0:
        return 0
    apply_credit(
        conn,
        int(row["guild_id"]),
        int(row["user_id"]),
        price,
        refund_kind,
        meta={"submission_id": int(row["id"]), "reason": reason},
        booster=False,
    )
    return price


def list_rows(
    conn: sqlite3.Connection,
    table: str,
    guild_id: int,
    state: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Rows for a dashboard queue.

    Filtered by state it reads oldest-first, because a queue is a work list
    and the longest wait should be handled next. Unfiltered it reads
    newest-first, because that view is a history rather than a queue. ``id``
    breaks ties both ways so equal timestamps don't shuffle between requests.

    ``limit`` is clamped rather than rejected: this backs a dashboard fetch,
    and an out-of-range page size is worth serving sensibly, not 400-ing.
    """
    table = _ident(table)
    limit = min(max(limit, 1), 500)
    if state:
        return conn.execute(
            f"SELECT * FROM {table} WHERE guild_id = ? AND state = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (guild_id, state, limit),
        ).fetchall()
    return conn.execute(
        f"SELECT * FROM {table} WHERE guild_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (guild_id, limit),
    ).fetchall()


# ── the product-shaped layer ──────────────────────────────────────────
#
# Everything above takes a bare table name and predates the descriptor. The
# functions below take a :class:`SubmissionProduct` instead, which is what a
# new product should reach for; the two older helpers stay as they are because
# they are load-bearing and well covered, and churning their signatures would
# throw away the characterization tests that make the rest of this safe.


@dataclass(frozen=True)
class SubmissionProduct:
    """What the shared mechanics need to know about one paid-submission product.

    Deliberately four fields. Anything that varies *and* is member-facing —
    the wording of a receipt, the length caps, what approval actually produces
    — is not in here, because a descriptor is a bad place to look for a
    sentence. See the module docstring.
    """

    #: The submissions table. Passed through ``sql_identifier``, never formatted raw.
    table: str
    #: Ledger kind for the debit taken at submit.
    spend_kind: str
    #: Ledger kind for the credit paid back on a refund.
    refund_kind: str
    #: States that count as "in flight" for the one-per-member rule.
    open_states: tuple[str, ...]


def open_submission(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    guild_id: int,
    user_id: int,
) -> sqlite3.Row | None:
    """The member's in-flight submission, if any.

    Backs the one-in-flight-per-member rule that stops someone buying ten
    queue slots at once. It reads rather than relies on the partial unique
    index so the caller can say *why* in its own words before the insert
    fails with a constraint error nobody can show a member.
    """
    placeholders = ", ".join("?" for _ in product.open_states)
    return conn.execute(
        f"SELECT * FROM {_ident(product.table)} "
        f"WHERE guild_id = ? AND user_id = ? AND state IN ({placeholders})",
        (guild_id, user_id, *product.open_states),
    ).fetchone()


def get(
    conn: sqlite3.Connection, product: SubmissionProduct, submission_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {_ident(product.table)} WHERE id = ?", (submission_id,)
    ).fetchone()


def charge_and_insert(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    guild_id: int,
    user_id: int,
    price: int,
    content: dict[str, Any],
    *,
    now: float | None = None,
) -> int | None:
    """Take the money, then queue the row. Returns the new id, or None if they can't afford it.

    Returning None rather than raising is what keeps the "costs X, you have Y"
    sentence at the product: only the product knows whether it is sponsoring a
    question or buying a themed day, and that sentence is the one a member
    actually reads.

    The debit lands **before** the insert so a failed insert can never strand a
    payment — both live in the caller's transaction, so a rollback takes the
    debit with it. ``content`` is both the row's content columns and the
    debit's ledger meta, which is why a submission's text is recoverable from
    the ledger alone.
    """
    if not apply_debit(
        conn, guild_id, user_id, price, product.spend_kind, meta=dict(content)
    ):
        return None
    columns = ", ".join(_ident(c) for c in content)
    marks = ", ".join("?" for _ in content)
    cur = conn.execute(
        f"INSERT INTO {_ident(product.table)} "
        f"(guild_id, user_id, {columns}, state, price, created_at) "
        f"VALUES (?, ?, {marks}, 'pending', ?, ?)",
        (guild_id, user_id, *content.values(), price,
         time.time() if now is None else now),
    )
    # shop_purchase quest trigger (one-time setup kind); deferred import — the
    # quests service imports the wider economy machinery.
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")
    return int(cur.lastrowid or 0)


def move_state(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    submission_id: int,
    *,
    from_state: str,
    to_state: str,
    resolver_id: int | None = None,
    deny_reason: str | None = None,
    refund_reason: str | None = None,
    extra: dict[str, Any] | None = None,
    now: float | None = None,
) -> sqlite3.Row | None:
    """Move one submission between states, optionally refunding. None if the guard missed.

    The ``state = from_state`` predicate on the UPDATE is the whole
    concurrency story: two mods clicking Approve at the same moment both run
    this, exactly one matches a row, and the loser gets None to word however
    the product likes. That is why this returns None instead of raising — the
    "someone else just did that" sentence differs per product, and a store
    that owned it would be owning copy.

    ``refund_reason`` of None means no refund (an approval); a string routes
    through :func:`refund_once`, so a refund stays exactly-once even if two
    callers race this same move.
    """
    now = time.time() if now is None else now
    row = get(conn, product, submission_id)
    if row is None:
        return None
    sets: dict[str, Any] = {"state": to_state, "resolved_at": now}
    if resolver_id is not None:
        sets["resolver_id"] = resolver_id
    if deny_reason is not None:
        sets["deny_reason"] = deny_reason[:500]
    sets.update(extra or {})
    assignments = ", ".join(f"{_ident(c)} = ?" for c in sets)
    cur = conn.execute(
        f"UPDATE {_ident(product.table)} SET {assignments} "
        "WHERE id = ? AND state = ?",
        (*sets.values(), submission_id, from_state),
    )
    if (cur.rowcount or 0) == 0:
        return None
    if refund_reason is not None:
        refund_once(
            conn, product.table, row, refund_reason, refund_kind=product.refund_kind
        )
    fresh = get(conn, product, submission_id)
    assert fresh is not None
    return fresh


def expire_stale_pending(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    guild_id: int,
    *,
    days: int,
    now: float,
) -> list[sqlite3.Row]:
    """Expire and refund pending submissions nobody reviewed. Returns the expired rows.

    Only ``pending`` is swept here on purpose: a submission that was *accepted*
    is waiting on staff, and timing that out would charge a member for staff
    latency. ``days`` of 0 disables the sweep, which is how a guild with a slow
    queue keeps submissions alive indefinitely.

    Rows are returned pre-move (the caller wants the price and the content for
    a DM), and each move is individually guarded, so a row resolved by a mod
    between the SELECT and the UPDATE is skipped rather than double-handled.
    """
    if days <= 0:
        return []
    cutoff = now - days * 86400.0
    stale = conn.execute(
        f"SELECT * FROM {_ident(product.table)} "
        "WHERE guild_id = ? AND state = 'pending' AND created_at < ?",
        (guild_id, cutoff),
    ).fetchall()
    out: list[sqlite3.Row] = []
    for row in stale:
        if move_state(
            conn, product, int(row["id"]),
            from_state="pending", to_state="expired",
            refund_reason="expired", now=now,
        ) is not None:
            out.append(row)
    return out


def set_card(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    submission_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Record where the mod-approval card lives so it can be edited on resolution."""
    conn.execute(
        f"UPDATE {_ident(product.table)} SET card_channel_id = ?, card_message_id = ? "
        "WHERE id = ?",
        (channel_id, message_id, submission_id),
    )


def list_for(
    conn: sqlite3.Connection,
    product: SubmissionProduct,
    guild_id: int,
    state: str | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """:func:`list_rows` for a product descriptor."""
    return list_rows(conn, product.table, guild_id, state, limit)
