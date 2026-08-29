"""One queue over the economy's paid, mod-approved submissions.

Three products let a member spend coins on something a moderator then has to
say yes to: a **themed day**, a **sponsored question of the day**, and a
**pin**. They are separate features with separate tables and deliberately
separate member-facing copy (see ``economy_submission_store``), but to the
moderator who has to work them they are one job — somebody paid, somebody is
waiting — so the todo board shows them as one section behind one button.

This module is only the merge: which products are in it, and reading their
pending rows as one oldest-first list. Everything a *member* sees stays with
the product, and so does everything approval actually produces. Adding a
fourth product is one row in :data:`QUEUES`.

The emoji sponsorship is deliberately not here. Its approval is not a
decision but an upload — a mod claims the row, then hands Discord an image
file — so it has no yes/no button to put on a board, and it already has the
dashboard queue that Pin of the Day never had.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from bot_modules.core.db_utils import sql_identifier as _ident
from bot_modules.services import economy_pin_service as pin_svc
from bot_modules.services import economy_qotd_sponsor_service as sponsor_svc
from bot_modules.services import economy_submission_store as store
from bot_modules.services import economy_theme_service as theme_svc


@dataclass(frozen=True)
class ApprovalQueue:
    """One paid-submission product, as the shared queue sees it."""

    #: Stable key. It rides in the picker's select values and in the board's
    #: signature, so it is part of the wire format — don't rename one.
    key: str
    #: How the queue reads in a sentence ("a themed day").
    noun: str
    #: The product descriptor the shared store mechanics take.
    product: store.SubmissionProduct
    #: The column holding the one line that identifies the request on a board
    #: row and in a select option.
    summary_column: str


QUEUES: tuple[ApprovalQueue, ...] = (
    ApprovalQueue("theme", "themed day", theme_svc.PRODUCT, "title"),
    ApprovalQueue("sponsor", "sponsored question", sponsor_svc.PRODUCT, "question"),
    ApprovalQueue("pin", "pin", pin_svc.PRODUCT, "message"),
)

QUEUES_BY_KEY: dict[str, ApprovalQueue] = {q.key: q for q in QUEUES}


def _pending_select(queue: ApprovalQueue) -> str:
    """One product's slice of the union. Both identifiers come from the
    descriptor and go through ``sql_identifier``, never raw formatting."""
    return (
        f"SELECT '{queue.key}' AS kind, id, user_id, price, "
        f"{_ident(queue.summary_column)} AS summary, created_at "
        f"FROM {_ident(queue.product.table)} "
        "WHERE guild_id = ? AND state = 'pending'"
    )


def pending_approvals(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 25
) -> list[dict[str, Any]]:
    """Every paid request waiting on a moderator, oldest first.

    Oldest first because this is a work list and the longest wait should be
    handled next — the same rule ``economy_submission_store.list_rows`` uses
    for a single product's dashboard queue. ``kind`` then ``id`` break ties so
    two requests submitted in the same instant don't shuffle between renders
    (and so the board's signature doesn't churn because of it).

    Each product's ``(guild_id, state, created_at)`` index serves its arm of
    the union, so a guild with nothing waiting pays for three index probes.

    Products that have since been switched off are still listed: those rows
    are paid for and pending, and a mod must be able to deny and refund one
    whatever the dial says today.
    """
    sql = " UNION ALL ".join(_pending_select(q) for q in QUEUES)
    sql += " ORDER BY created_at ASC, kind ASC, id ASC LIMIT ?"
    rows = conn.execute(sql, (*(guild_id for _ in QUEUES), max(0, int(limit))))
    return [dict(r) for r in rows]


def pending_approval_count(conn: sqlite3.Connection, guild_id: int) -> int:
    """How many are waiting in total — the board shows a bounded slice."""
    sql = " UNION ALL ".join(
        f"SELECT COUNT(*) AS n FROM {_ident(q.product.table)} "
        "WHERE guild_id = ? AND state = 'pending'"
        for q in QUEUES
    )
    total = conn.execute(
        f"SELECT COALESCE(SUM(n), 0) FROM ({sql})",
        tuple(guild_id for _ in QUEUES),
    ).fetchone()[0]
    return int(total or 0)


def get_approval_row(
    conn: sqlite3.Connection, kind: str, submission_id: int
) -> sqlite3.Row | None:
    """One request in full, by the ``kind`` its board row carried.

    An unknown kind reads as nothing rather than raising: the value arrives
    from a select on a long-lived ephemeral message, and a stale one is a
    "that's gone now", not a crash.
    """
    queue = QUEUES_BY_KEY.get(kind)
    if queue is None:
        return None
    return store.get(conn, queue.product, submission_id)
