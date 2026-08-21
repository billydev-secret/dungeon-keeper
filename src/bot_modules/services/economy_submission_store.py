"""Shared SQL for the economy's paid-submission queues.

Three products let a member spend coins on something a mod then approves or
denies: pinned messages, sponsored questions of the day, and bounties. They
are separate features with separate tables, separate prices and — quite
deliberately — separate member-facing copy. What they don't need is three
copies of the ledger mechanics underneath.

Only the mechanism lives here. The wording of a receipt, the shape of a
review card, and which content field a product stores stay with the product,
because that's where someone editing them will look.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from bot_modules.core.db_utils import sql_identifier as _ident
from bot_modules.services.economy_service import apply_credit


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
