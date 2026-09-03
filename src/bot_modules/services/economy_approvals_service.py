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
from bot_modules.services import economy_emoji_service as emoji_svc
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


def set_approval_card(
    conn: sqlite3.Connection,
    kind: str,
    submission_id: int,
    channel_id: int,
    message_id: int,
) -> bool:
    """Record where a request's review card was posted. False on unknown kind.

    The mirror of :func:`get_approval_row`, and the reason the channel card
    and the todo board can be two views of one ledger rather than two
    ledgers: whoever resolves a request — the card's own buttons, the board's
    Approvals picker, or the dashboard — can find and repaint the other
    surface from these two columns.

    Each product already has its own ``set_submission_card`` wrapper over the
    same store call. This exists so the shared poster doesn't have to import
    three of them and branch on which one to use, exactly as the queue merge
    above spares the board three pending queries.
    """
    queue = QUEUES_BY_KEY.get(kind)
    if queue is None:
        return False
    store.set_card(conn, queue.product, submission_id, channel_id, message_id)
    return True


def card_location(row: Any) -> tuple[int, int]:
    """``(channel_id, message_id)`` for a submission row, ``(0, 0)`` if uncarded.

    A row predating the channel surface — or one submitted while the dial was
    unset — carries 0s, which every caller reads as "there is no second
    surface to repaint".
    """
    try:
        return int(row["card_channel_id"] or 0), int(row["card_message_id"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0, 0


# ── The dashboard's wider queue ───────────────────────────────────────────
#
# The board above answers a moderator standing in Discord: "is anybody waiting
# on a yes/no?" The dashboard answers a slightly bigger question — "is anybody
# waiting on us at all?" — so it carries two more products the board leaves
# out, for reasons that are about Discord rather than about the work:
#
#   * The **sponsored emoji** has no yes/no button because approving it means
#     handing Discord an image file, which a board row cannot do. On a web page
#     that is just a row whose action opens the upload, so it belongs here.
#   * A **quest sign-off** is not a paid submission at all — no price, no
#     refund, and its payout is the quest's own reward — so it cannot join the
#     UNION above. It is merged in Python instead.
#   * A **custom-item order** lives in its own table and takes its summary from
#     the item it bought, so it needs a join the others don't. Merged the same
#     way. It is the one row whose action is a refusal rather than a decision:
#     delivering an order means ticking its todo off elsewhere.
#
# ``QUEUES`` is deliberately left alone: its keys ride in the board's select
# values and its signature, so widening it would change a Discord surface to
# serve a web one.

EMOJI_QUEUE = ApprovalQueue(
    "emoji", "sponsored emoji", emoji_svc.PRODUCT, "name"
)

#: The four paid-submission products, as the dashboard sees them.
DASHBOARD_QUEUES: tuple[ApprovalQueue, ...] = QUEUES + (EMOJI_QUEUE,)

#: Every row type the unified dashboard queue can hold, including the one that
#: is not a submission. Used to validate a filter value at the edge.
DASHBOARD_KINDS: tuple[str, ...] = tuple(q.key for q in DASHBOARD_QUEUES) + (
    "claim",
    "order",
)


def _pending_claims(conn: sqlite3.Connection, guild_id: int) -> list[dict[str, Any]]:
    """Quest sign-offs waiting on a staff decision, normalised to queue shape.

    ``amount`` is the quest's reward rather than a price, because nobody paid
    to claim — the coins flow the other way on approval. It is still the number
    a reviewer wants on the row, so it rides in the same field rather than
    inventing a second one the other four products would leave empty.

    A claim whose quest has since been deleted still appears, with an empty
    summary: the member is waiting either way, and hiding the row would strand
    them with no surface that shows it.
    """
    rows = conn.execute(
        """SELECT c.id, c.user_id, c.created_at,
                  COALESCE(q.title, '') AS summary,
                  COALESCE(q.reward, 0) AS amount
           FROM econ_quest_claims c
           LEFT JOIN econ_quests q ON q.id = c.quest_id
           WHERE c.guild_id = ? AND c.state = 'pending'""",
        (guild_id,),
    ).fetchall()
    return [
        {
            "kind": "claim",
            "id": int(r["id"]),
            "user_id": int(r["user_id"]),
            "summary": r["summary"],
            "amount": int(r["amount"] or 0),
            "created_at": float(r["created_at"]),
        }
        for r in rows
    ]


def _pending_orders(conn: sqlite3.Connection, guild_id: int) -> list[dict[str, Any]]:
    """Custom-item orders waiting on staff, normalised to queue shape.

    Another table that cannot join the UNION: an order lives in
    ``econ_shop_purchases`` and takes its summary from the item it bought, so
    it needs a join the four submission products don't have.

    Unlike the others, "resolving" an order is not a yes/no on this page — it
    is delivered by ticking its todo off, and the only decision surfaced here
    is the refusal. The row still belongs in the list: somebody paid and is
    waiting, which is the whole question this queue answers.
    """
    rows = conn.execute(
        """SELECT p.id, p.user_id, p.price, p.created_at,
                  COALESCE(i.name, '') AS summary
           FROM econ_shop_purchases p
           LEFT JOIN econ_shop_items i ON i.id = p.item_id
           WHERE p.guild_id = ? AND p.state = 'pending'""",
        (guild_id,),
    ).fetchall()
    return [
        {
            "kind": "order",
            "id": int(r["id"]),
            "user_id": int(r["user_id"]),
            "summary": r["summary"],
            "amount": int(r["price"] or 0),
            "created_at": float(r["created_at"]),
        }
        for r in rows
    ]


def pending_for_dashboard(
    conn: sqlite3.Connection, guild_id: int, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Everything waiting on a moderator, across all five products, oldest first.

    Oldest first for the same reason the board is: this is a work list, and the
    longest wait should be handled next. ``kind`` then ``id`` break ties so two
    requests made in the same instant keep a stable order between renders.

    The ``limit`` is generous on purpose. Production runs these queues at zero
    to two pending rows each, so the cap is a runaway guard rather than
    pagination — if it ever truncates, the caller should say so rather than
    quietly showing a short list.
    """
    sql = " UNION ALL ".join(_pending_select(q) for q in DASHBOARD_QUEUES)
    submissions = [
        {
            "kind": r["kind"],
            "id": int(r["id"]),
            "user_id": int(r["user_id"]),
            "summary": r["summary"] or "",
            "amount": int(r["price"] or 0),
            "created_at": float(r["created_at"]),
        }
        for r in conn.execute(sql, tuple(guild_id for _ in DASHBOARD_QUEUES))
    ]
    merged = (
        submissions
        + _pending_claims(conn, guild_id)
        + _pending_orders(conn, guild_id)
    )
    merged.sort(key=lambda r: (r["created_at"], r["kind"], r["id"]))
    return merged[: max(0, int(limit))]
