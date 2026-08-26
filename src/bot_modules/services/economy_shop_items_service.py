"""Custom shop items — the database and the money.

Staff define items on the dashboard (`docs/plans/economy-shop-items.md`,
spec §6) instead of the shop's catalogue being the fixed list compiled into
``economy/perks.py``. Two axes, both chosen per item:

    kind     'role'   → grant one role, automatically, on purchase
             'manual' → a staff to-do: escrow the coins and spawn a todo
    billing  'once'   → pay once, done
             'weekly' → an ordinary ``econ_rentals`` row (perk 'custom_item')

so four flows, all of which land in ``econ_shop_purchases``:

    role   + once    debit → row born 'fulfilled'. Nothing to wait for.
    role   + weekly  rent_perk() → 'live'. Lapse strips the role.
    manual + once    escrow → 'pending' + a todo. Tick → 'fulfilled'.
    manual + weekly  escrow week one → 'pending' + a todo. Tick → 'live',
                     with the rental's first anniversary a week out (the
                     escrow already paid it) — the emoji-sponsor shape.

**The manual queue is the todo board, not a new queue.** Mods already work
through `todos`; a purchase spawns a real row there (`todos.purchase_id`),
and delivery hangs off ``todo_service.complete_todo``'s existing guarded
UPDATE, which returns True exactly once precisely so the board button and the
dashboard can race. Refusing an order instead calls ``mark_missed`` — the
function that exists to close outstanding work *without* crediting it, which
is exactly a refunded order and must never render as delivered.

Ledger kinds: ``shop_item`` (the escrow debit), ``shop_item_refund`` (a plain
credit, never boosted). Weekly renewals bill the ordinary ``rental`` kind at
the item's then-current price. Refunds are guarded on ``refunded_at IS NULL``,
so no path can pay one twice.

Every function rides the caller's connection: the caller's transaction is the
commit boundary. That matters most in ``purchase``, where the stock decrement,
the debit and the row insert must land together or not at all — an unaffordable
purchase must not leave a consumed unit of stock behind.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.economy.shop_items import (
    BILLINGS,
    CANCELLABLE_STATES,
    HOLDING_STATES,
    KIND_MANUAL,
    KIND_ROLE,
    KINDS,
    STATE_CANCELLED,
    STATE_DENIED,
    STATE_EXPIRED,
    STATE_FULFILLED,
    STATE_LAPSED,
    STATE_LIVE,
    STATE_PENDING,
    ItemView,
    Refusal,
    evaluate_purchase,
    expiry_cutoff,
    todo_task_text,
    visible,
)
from bot_modules.services.economy_service import apply_credit, apply_debit, get_balance

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

SPEND_KIND = "shop_item"
REFUND_KIND = "shop_item_refund"

#: Savepoint name for the mutating half of ``purchase`` — see its docstring.
_SAVEPOINT = "shop_purchase"

NAME_MAX_LEN = 60
BLURB_MAX_LEN = 40
DESCRIPTION_MAX_LEN = 500
NOTE_MAX_LEN = 300

#: Ceiling on a listing query, matching the todo list's own.
LIST_LIMIT = 200

_ITEM_COLS = (
    "id, guild_id, name, blurb, description, price, kind, billing, role_id,"
    " stock, sold, per_member_limit, available_from, available_until,"
    " ask_note, enabled, sort_order, created_by, created_at"
)

_PURCHASE_COLS = (
    "id, guild_id, user_id, item_id, state, price, note, todo_id, rental_id,"
    " resolver_id, deny_reason, refunded_at, created_at, resolved_at"
)

#: Fields an admin may write through ``update_item``. `sold` is deliberately
#: absent — it is bookkeeping the purchase path owns, and an admin editing it
#: by hand would desynchronise stock from the orders that consumed it.
_EDITABLE = frozenset(
    {
        "name", "blurb", "description", "price", "kind", "billing", "role_id",
        "stock", "per_member_limit", "available_from", "available_until",
        "ask_note", "enabled", "sort_order",
    }
)


@dataclass(frozen=True)
class PurchaseOutcome:
    """What a successful purchase produced, for the caller's Discord effects.

    ``refusal`` is ``Refusal.OK`` on success; every other value means nothing
    was written and the other fields are unset. Callers render
    ``shop_items.refusal_text(outcome.refusal)`` rather than composing their
    own copy.
    """

    refusal: Refusal
    purchase_id: int = 0
    item_id: int = 0
    item_name: str = ""
    price: int = 0
    state: str = ""
    #: Set when the purchase should grant a role right now.
    grant_role_id: int | None = None
    #: Set when a staff to-do was spawned, for the board refresh.
    todo_id: int | None = None
    rental_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is Refusal.OK


def _row_to_view(row: sqlite3.Row) -> ItemView:
    return ItemView(
        item_id=int(row["id"]),
        name=str(row["name"]),
        price=int(row["price"]),
        blurb=str(row["blurb"] or ""),
        description=str(row["description"] or ""),
        kind=str(row["kind"]),
        billing=str(row["billing"]),
        role_id=int(row["role_id"]) if row["role_id"] else None,
        stock=None if row["stock"] is None else int(row["stock"]),
        sold=int(row["sold"]),
        per_member_limit=(
            None if row["per_member_limit"] is None
            else int(row["per_member_limit"])
        ),
        available_from=(
            None if row["available_from"] is None else float(row["available_from"])
        ),
        available_until=(
            None if row["available_until"] is None else float(row["available_until"])
        ),
        ask_note=bool(row["ask_note"]),
        enabled=bool(row["enabled"]),
    )


# ── items: read ────────────────────────────────────────────────────


def list_items(
    conn: sqlite3.Connection, guild_id: int, *, enabled_only: bool = False
) -> list[sqlite3.Row]:
    """Every item this guild has defined, in display order."""
    where = "guild_id = ?" + (" AND enabled = 1" if enabled_only else "")
    return list(
        conn.execute(
            f"SELECT {_ITEM_COLS} FROM econ_shop_items WHERE {where}"
            " ORDER BY sort_order, id LIMIT ?",
            (guild_id, LIST_LIMIT),
        )
    )


def get_item(
    conn: sqlite3.Connection, guild_id: int, item_id: int
) -> ItemView | None:
    row = conn.execute(
        f"SELECT {_ITEM_COLS} FROM econ_shop_items WHERE guild_id = ? AND id = ?",
        (guild_id, item_id),
    ).fetchone()
    return None if row is None else _row_to_view(row)


def shop_items_for(
    conn: sqlite3.Connection, guild_id: int, *, now: float, user_id: int | None = None
) -> list[ItemView]:
    """The items to render in the shop, for this viewer.

    Hidden: disabled items and items outside their availability window — an
    item nobody can buy is noise in a table read on a phone. Sold-out items
    stay, because "sold out" is information; so does anything the viewer is
    currently renting, so a member is never billed for a row with no name and
    no price anywhere in the shop.
    """
    owned = _rented_item_ids(conn, guild_id, user_id) if user_id else frozenset()
    return [
        view
        for view in (_row_to_view(r) for r in list_items(conn, guild_id))
        if visible(view, now, owned=view.item_id in owned)
    ]


def _rented_item_ids(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> frozenset[int]:
    return frozenset(
        int(r["catalog_item_id"])
        for r in conn.execute(
            "SELECT DISTINCT catalog_item_id FROM econ_rentals"
            " WHERE guild_id = ? AND beneficiary_id = ? AND perk = 'custom_item'"
            "   AND catalog_item_id IS NOT NULL AND state IN ('active', 'grace')",
            (guild_id, user_id),
        )
    )


def owned_count(
    conn: sqlite3.Connection, guild_id: int, user_id: int, item_id: int
) -> int:
    """Purchases of this item by this member that were never refunded.

    The per-member limit counts these. A denied, cancelled or expired order is
    excluded on purpose: a mod's refusal must not quietly consume the one
    purchase the member was allowed.
    """
    marks = ",".join("?" * len(HOLDING_STATES))
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM econ_shop_purchases"
        f" WHERE guild_id = ? AND user_id = ? AND item_id = ?"
        f"   AND state IN ({marks})",
        (guild_id, user_id, item_id, *sorted(HOLDING_STATES)),
    ).fetchone()
    return int(row["n"]) if row else 0


# ── items: write ───────────────────────────────────────────────────


def create_item(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    name: str,
    price: int,
    kind: str = KIND_MANUAL,
    billing: str = "once",
    created_by: int = 0,
    now: float | None = None,
    **fields: object,
) -> int:
    """Define a new item. Returns its id. Raises ValueError on bad input."""
    name = (name or "").strip()
    if not name:
        raise ValueError("An item needs a name.")
    if len(name) > NAME_MAX_LEN:
        raise ValueError(f"Name must be {NAME_MAX_LEN} characters or fewer.")
    if price < 0:
        raise ValueError("Price can't be negative.")
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")
    if billing not in BILLINGS:
        raise ValueError(f"unknown billing: {billing!r}")
    if kind == KIND_ROLE and not fields.get("role_id"):
        raise ValueError("A role item needs a role.")
    _check_window(fields.get("available_from"), fields.get("available_until"))

    now = time.time() if now is None else now
    cols = {
        "guild_id": guild_id, "name": name, "price": int(price), "kind": kind,
        "billing": billing, "created_by": created_by, "created_at": now,
    }
    for key, value in fields.items():
        if key not in _EDITABLE:
            raise KeyError(key)
        cols[key] = value
    names = ", ".join(cols)
    marks = ", ".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO econ_shop_items ({names}) VALUES ({marks})",
        tuple(cols.values()),
    )
    return int(cur.lastrowid or 0)


def update_item(
    conn: sqlite3.Connection, guild_id: int, item_id: int, values: dict
) -> bool:
    """Edit an item. Returns False when it doesn't exist. Raises on bad input.

    A price edit lands on *new* purchases and on the next renewal of a live
    rental (`_price_for` re-reads it), never retroactively on an open order —
    ``econ_shop_purchases.price`` is snapshotted so a refund returns what was
    actually taken.
    """
    current = get_item(conn, guild_id, item_id)
    if current is None:
        return False
    unknown = set(values) - _EDITABLE
    if unknown:
        raise KeyError(next(iter(sorted(unknown))))
    if "name" in values and not str(values["name"] or "").strip():
        raise ValueError("An item needs a name.")
    if "price" in values and int(values["price"]) < 0:
        raise ValueError("Price can't be negative.")
    # `in values`, not `.get(...) is None`: a *present* None would otherwise
    # read as "not supplied" and write NULL into a NOT NULL column.
    if "kind" in values and values["kind"] not in KINDS:
        raise ValueError(f"unknown kind: {values['kind']!r}")
    if "billing" in values and values["billing"] not in BILLINGS:
        raise ValueError(f"unknown billing: {values['billing']!r}")
    kind = str(values.get("kind", current.kind))
    role_id = values.get("role_id", current.role_id)
    if kind == KIND_ROLE and not role_id:
        raise ValueError("A role item needs a role.")
    _check_window(
        values.get("available_from", current.available_from),
        values.get("available_until", current.available_until),
    )
    if not values:
        return True
    sets = ", ".join(f"{k} = ?" for k in values)
    conn.execute(
        f"UPDATE econ_shop_items SET {sets} WHERE guild_id = ? AND id = ?",
        (*values.values(), guild_id, item_id),
    )
    return True


def _check_window(start: object, end: object) -> None:
    if start is not None and end is not None and float(end) <= float(start):  # type: ignore[arg-type]
        raise ValueError("The sale must end after it starts.")


def open_order_count(
    conn: sqlite3.Connection, guild_id: int, item_id: int
) -> int:
    """Orders that would be orphaned by deleting this item.

    Pending orders (money escrowed, staff yet to act) and live rentals (billing
    weekly). Delivered and refunded orders are settled history and don't block.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_shop_purchases"
        " WHERE guild_id = ? AND item_id = ? AND state IN (?, ?)",
        (guild_id, item_id, STATE_PENDING, STATE_LIVE),
    ).fetchone()
    return int(row["n"]) if row else 0


def delete_item(conn: sqlite3.Connection, guild_id: int, item_id: int) -> bool:
    """Remove an item. Returns False when it doesn't exist.

    Raises ValueError while any order is still open: deleting would strand
    escrowed money and leave a live rental billing for a row nobody can price
    or name. Disabling is the way to retire an item that people still hold —
    ``visible()`` keeps a disabled item on the shelf for its own renters.
    """
    if get_item(conn, guild_id, item_id) is None:
        return False
    open_orders = open_order_count(conn, guild_id, item_id)
    if open_orders:
        raise ValueError(
            f"{open_orders} order(s) are still open — disable the item instead."
        )
    conn.execute(
        "DELETE FROM econ_shop_items WHERE guild_id = ? AND id = ?",
        (guild_id, item_id),
    )
    return True


# ── buying ─────────────────────────────────────────────────────────


def _take_stock(conn: sqlite3.Connection, guild_id: int, item_id: int) -> bool:
    """Consume one unit. False (no writes) when the last one just went.

    A guarded UPDATE, the same shape as ``apply_debit``'s guarded balance
    write: two simultaneous buyers cannot both take the final unit, because
    only one of them sees a rowcount of 1.
    """
    cur = conn.execute(
        "UPDATE econ_shop_items SET sold = sold + 1"
        " WHERE guild_id = ? AND id = ? AND (stock IS NULL OR sold < stock)",
        (guild_id, item_id),
    )
    return cur.rowcount > 0


def _release_stock(conn: sqlite3.Connection, guild_id: int, item_id: int) -> None:
    """Give a refunded order's unit back. Floors at 0."""
    conn.execute(
        "UPDATE econ_shop_items SET sold = MAX(0, sold - 1)"
        " WHERE guild_id = ? AND id = ?",
        (guild_id, item_id),
    )


def purchase(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    item_id: int,
    *,
    note: str = "",
    now: float | None = None,
) -> PurchaseOutcome:
    """Buy one custom item.

    Returns a ``PurchaseOutcome`` whose ``refusal`` says why not; on refusal
    NOTHING is written.

    That guarantee needs a **savepoint**, not just the caller's transaction.
    Two gates can only fail after the stock decrement has already run: the
    debit, and ``rent_perk`` — which inserts the rental row and *then* charges,
    raising on a failed debit precisely so the caller's transaction unwinds.
    Reporting either as a returned refusal instead of propagating leaves those
    writes pending, and ``open_db`` commits on normal exit — so a caller that
    simply rendered the refusal would hand out a free, silently billing rental
    and burn a unit of stock with no order behind it. The savepoint (the
    ``intake_rewards`` idiom) undoes exactly this call's writes and leaves
    anything the caller did before it alone.
    """
    now = time.time() if now is None else now
    item = get_item(conn, guild_id, item_id)
    verdict = evaluate_purchase(
        item,
        now=now,
        balance=get_balance(conn, guild_id, user_id),
        owned_count=owned_count(conn, guild_id, user_id, item_id),
        holds_rental=(
            item is not None
            and item.is_rental
            and item_id in _rented_item_ids(conn, guild_id, user_id)
        ),
    )
    if verdict is not Refusal.OK or item is None:
        return PurchaseOutcome(refusal=verdict)

    conn.execute(f"SAVEPOINT {_SAVEPOINT}")
    try:
        outcome = _purchase_inner(conn, settings, guild_id, user_id, item, note, now)
    except Exception:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
        conn.execute(f"RELEASE {_SAVEPOINT}")
        raise
    if not outcome.ok:
        conn.execute(f"ROLLBACK TO {_SAVEPOINT}")
    conn.execute(f"RELEASE {_SAVEPOINT}")
    return outcome


def _purchase_inner(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    item: ItemView,
    note: str,
    now: float,
) -> PurchaseOutcome:
    """The mutating half of ``purchase``, run inside its savepoint."""
    item_id = item.item_id
    # Stock first: taking it before the money means a loser never has a debit
    # to unwind. The savepoint returns the unit if a later gate refuses.
    if not _take_stock(conn, guild_id, item_id):
        return PurchaseOutcome(refusal=Refusal.SOLD_OUT)

    rental_id: int | None = None
    if item.is_rental and item.kind == KIND_ROLE:
        # A role rental opens immediately — rent_perk takes the first week.
        from bot_modules.services.economy_rentals_service import (  # noqa: PLC0415
            rent_perk,
        )

        try:
            rental = rent_perk(
                conn, settings, guild_id, user_id, "custom_item",
                catalog_item_id=item_id, now=now,
            )
        except ValueError as exc:
            # Guarded above, so these are races, not ordinary refusals. Report
            # them as refusals anyway rather than raising into a button
            # handler; the caller rolls back and the stock goes with it.
            return PurchaseOutcome(
                refusal=(
                    Refusal.ALREADY_RENTED
                    if "already" in str(exc)
                    else Refusal.INSUFFICIENT
                )
            )
        rental_id = int(rental["id"])
        state = STATE_LIVE
    else:
        # Every other flow escrows the price here: a manual order's coins are
        # held until staff resolve it, and a manual *weekly* item's escrow is
        # its first week (the rental opens at fulfilment with its anniversary
        # already a week out).
        if item.price > 0 and not apply_debit(
            conn, guild_id, user_id, item.price, SPEND_KIND,
            actor_id=user_id, meta={"item_id": item_id},
        ):
            return PurchaseOutcome(refusal=Refusal.INSUFFICIENT)
        state = STATE_PENDING if item.kind == KIND_MANUAL else STATE_FULFILLED

    resolved_at = now if state == STATE_FULFILLED else None
    cur = conn.execute(
        "INSERT INTO econ_shop_purchases"
        " (guild_id, user_id, item_id, state, price, note, rental_id,"
        "  created_at, resolved_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            guild_id, user_id, item_id, state, item.price,
            (note or "").strip()[:NOTE_MAX_LEN] if item.ask_note else "",
            rental_id, now, resolved_at,
        ),
    )
    purchase_id = int(cur.lastrowid or 0)

    todo_id: int | None = None
    if state == STATE_PENDING:
        todo_id = _spawn_todo(conn, guild_id, user_id, purchase_id, item, now)
        conn.execute(
            "UPDATE econ_shop_purchases SET todo_id = ? WHERE id = ?",
            (todo_id, purchase_id),
        )

    _fire_shop_quest(conn, guild_id, user_id)
    return PurchaseOutcome(
        refusal=Refusal.OK,
        purchase_id=purchase_id,
        item_id=item_id,
        item_name=item.name,
        price=item.price,
        state=state,
        grant_role_id=item.role_id if item.kind == KIND_ROLE else None,
        todo_id=todo_id,
        rental_id=rental_id,
    )


def _spawn_todo(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    purchase_id: int,
    item: ItemView,
    now: float,
) -> int:
    """File the order on the mods' todo board.

    ``added_by`` is the buyer — they are who caused the work, and it is the
    same kind of value the column already holds. The task text names the ITEM
    ONLY; see ``shop_items.todo_task_text`` for why the buyer must not be
    baked into it.
    """
    from bot_modules.services.todo_service import create_todo  # noqa: PLC0415

    return create_todo(
        conn,
        guild_id,
        user_id,
        todo_task_text(item.name),
        description=item.description or None,
        purchase_id=purchase_id,
        now_ts=now,
    )


def _fire_shop_quest(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Count a custom-item purchase for the ``shop_purchase`` quest.

    The quest rewards engaging with the shop, and a custom item is the shop.
    Deferred import, matching ``rent_perk``'s own call.
    """
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")


# ── resolving an order ─────────────────────────────────────────────


def get_purchase(
    conn: sqlite3.Connection, purchase_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_PURCHASE_COLS} FROM econ_shop_purchases WHERE id = ?",
        (purchase_id,),
    ).fetchone()


def pending_orders(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """Open orders for the dashboard queue, oldest first, with item names."""
    return list(
        conn.execute(
            "SELECT p.*, i.name AS item_name, i.kind AS item_kind,"
            "       i.billing AS item_billing, i.role_id AS item_role_id"
            " FROM econ_shop_purchases p"
            " JOIN econ_shop_items i ON i.id = p.item_id"
            " WHERE p.guild_id = ? AND p.state = ?"
            " ORDER BY p.created_at LIMIT ?",
            (guild_id, STATE_PENDING, LIST_LIMIT),
        )
    )


def fulfil_for_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    resolver_id: int,
    *,
    now: float | None = None,
) -> int | None:
    """Deliver the order behind a just-completed todo. Returns its rental id.

    Called from ``todo_service.complete_todo`` only after its guarded UPDATE
    reported a row — so this runs exactly once per order, inheriting the
    idempotence that already lets the board button and the dashboard race.

    A manual *weekly* item opens its rental here, with the first anniversary a
    week out because the escrow at purchase already paid week one (the
    emoji-sponsor shape). A manual *once* item just settles.
    """
    now = time.time() if now is None else now
    row = conn.execute(
        f"SELECT {_PURCHASE_COLS} FROM econ_shop_purchases"
        f" WHERE todo_id = ? AND state = ?",
        (todo_id, STATE_PENDING),
    ).fetchone()
    if row is None:
        return None

    guild_id = int(row["guild_id"])
    item = get_item(conn, guild_id, int(row["item_id"]))
    rental_id: int | None = None
    state = STATE_FULFILLED
    if item is not None and item.is_rental:
        rental_id = _open_paid_rental(
            conn, guild_id, int(row["user_id"]), item, int(row["price"]), now
        )
        state = STATE_LIVE
    conn.execute(
        "UPDATE econ_shop_purchases SET state = ?, resolver_id = ?,"
        " rental_id = ?, resolved_at = ? WHERE id = ?",
        (state, resolver_id, rental_id, now, int(row["id"])),
    )
    return rental_id


def _open_paid_rental(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    item: ItemView,
    price: int,
    now: float,
) -> int | None:
    """Open a rental whose first week is already paid.

    Inserted directly rather than through ``rent_perk`` because that function
    charges — and the money moved at purchase.

    Returns None when a live rental for this item already exists. Two pending
    orders for one manual weekly item are reachable — the purchase-time gate
    sees only *live* rentals, and a manual order opens none until delivery — so
    the second tick would otherwise raise IntegrityError straight out of
    ``complete_todo``, rolling back every other completion in the mod's
    multi-select batch. The order still settles; it just joins the rental
    already running.
    """
    from bot_modules.economy.rentals import WEEK_SECONDS  # noqa: PLC0415

    try:
        cur = conn.execute(
            "INSERT INTO econ_rentals"
            " (guild_id, user_id, perk, state, price, started_at, next_bill_at,"
            "  cancel_at_period_end, suspended, beneficiary_id, catalog_item_id,"
            "  created_at)"
            " VALUES (?, ?, 'custom_item', 'active', ?, ?, ?, 0, 0, ?, ?, ?)",
            (
                guild_id, user_id, price, now, now + WEEK_SECONDS, user_id,
                item.item_id, now,
            ),
        )
    except sqlite3.IntegrityError:
        # Only idx_econ_rentals_live can fire here.
        return None
    return int(cur.lastrowid or 0)


def refund_order(
    conn: sqlite3.Connection,
    guild_id: int,
    purchase_id: int,
    *,
    state: str,
    resolver_id: int = 0,
    reason: str = "",
    now: float | None = None,
) -> int | None:
    """Close an open order and give the money back. Returns the amount.

    Returns None when the order is missing, already closed, or already
    refunded — the ``refunded_at IS NULL`` guard is the exactly-once
    predicate, so two racing resolvers can't both pay.

    Also closes the order's todo as **missed**, never as complete: a refunded
    order must leave the board without ever rendering as delivered, which is
    precisely what ``mark_missed`` exists for.
    """
    if state not in (STATE_DENIED, STATE_CANCELLED, STATE_EXPIRED):
        raise ValueError(f"not a refund state: {state!r}")
    now = time.time() if now is None else now
    cur = conn.execute(
        "UPDATE econ_shop_purchases SET state = ?, resolver_id = ?,"
        " deny_reason = ?, refunded_at = ?, resolved_at = ?"
        " WHERE id = ? AND guild_id = ? AND state = ? AND refunded_at IS NULL",
        (
            state, resolver_id, (reason or "")[:DESCRIPTION_MAX_LEN], now, now,
            purchase_id, guild_id, STATE_PENDING,
        ),
    )
    if cur.rowcount == 0:
        return None

    row = get_purchase(conn, purchase_id)
    if row is None:  # pragma: no cover - just updated inside this transaction
        return None
    amount = int(row["price"])
    if amount > 0:
        apply_credit(
            conn, guild_id, int(row["user_id"]), amount, REFUND_KIND,
            actor_id=resolver_id or None,
            meta={"purchase_id": purchase_id, "item_id": int(row["item_id"])},
        )
    _release_stock(conn, guild_id, int(row["item_id"]))
    if row["todo_id"]:
        from bot_modules.services.todo_service import mark_missed  # noqa: PLC0415

        mark_missed(conn, int(row["todo_id"]), now_ts=now)
    return amount


def cancel_own_order(
    conn: sqlite3.Connection,
    guild_id: int,
    purchase_id: int,
    *,
    user_id: int,
    now: float | None = None,
) -> int | None:
    """A member withdraws their own pending order. Returns the refund.

    Returns None when the order isn't theirs or isn't cancellable — the same
    answer either way, so probing someone else's order id tells you nothing.
    """
    row = get_purchase(conn, purchase_id)
    if (
        row is None
        or int(row["guild_id"]) != guild_id
        or int(row["user_id"]) != user_id
        or str(row["state"]) not in CANCELLABLE_STATES
    ):
        return None
    return refund_order(
        conn, guild_id, purchase_id, state=STATE_CANCELLED,
        resolver_id=user_id, now=now,
    )


def expire_orders(
    conn: sqlite3.Connection,
    guild_id: int,
    settings: EconSettings,
    *,
    now: float | None = None,
) -> list[int]:
    """Refund orders nobody resolved in time. Returns the ids expired.

    The sponsored-QOTD / emoji sweep: an order left pending past
    ``shop_item_expire_days`` gives the member their coins back rather than
    holding them indefinitely against work that is not going to happen.
    """
    now = time.time() if now is None else now
    cutoff = expiry_cutoff(now, int(settings.shop_item_expire_days))
    stale = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM econ_shop_purchases"
            " WHERE guild_id = ? AND state = ? AND created_at < ?",
            (guild_id, STATE_PENDING, cutoff),
        )
    ]
    expired = []
    for purchase_id in stale:
        if refund_order(
            conn, guild_id, purchase_id, state=STATE_EXPIRED, now=now
        ) is not None:
            expired.append(purchase_id)
    return expired


def release_open_orders(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> list[int]:
    """Settle a member's open orders ahead of erasing them. Returns the ids.

    Called by ``econ_purge_user`` before it deletes the rows. A pending order
    holds a unit of the item's stock and a todo on the mods' board; both have
    to be let go, or the erasure leaves work nobody can deliver and a shelf
    permanently one short.

    **No refund is written.** The wallet and the ledger rows are being erased
    in the same sweep, so crediting coins into an account about to vanish would
    only leave a dangling ledger entry. The todo closes as *missed*, which is
    the truth: the order was never delivered.
    """
    now = time.time()
    ids: list[int] = []
    rows = conn.execute(
        "SELECT id, item_id, todo_id FROM econ_shop_purchases"
        " WHERE guild_id = ? AND user_id = ? AND state = ?",
        (guild_id, user_id, STATE_PENDING),
    ).fetchall()
    for row in rows:
        _release_stock(conn, guild_id, int(row["item_id"]))
        if row["todo_id"]:
            from bot_modules.services.todo_service import mark_missed  # noqa: PLC0415

            # Detach first: the purchase row is about to be deleted, and a todo
            # still pointing at a missing order would settle nothing when
            # ticked. Nulling it turns the row back into an ordinary task.
            conn.execute(
                "UPDATE todos SET purchase_id = NULL WHERE id = ?",
                (int(row["todo_id"]),),
            )
            mark_missed(conn, int(row["todo_id"]), now_ts=now)
        ids.append(int(row["id"]))
    return ids


def end_rental_order(
    conn: sqlite3.Connection, rental_id: int, *, now: float | None = None
) -> None:
    """Mark the order behind a lapsed/cancelled rental as ended.

    Purely bookkeeping so the member's purchase history reads correctly; the
    rental engine owns the money and the role.
    """
    conn.execute(
        "UPDATE econ_shop_purchases SET state = ?, resolved_at = ?"
        " WHERE rental_id = ? AND state = ?",
        (STATE_LAPSED, time.time() if now is None else now, rental_id, STATE_LIVE),
    )
