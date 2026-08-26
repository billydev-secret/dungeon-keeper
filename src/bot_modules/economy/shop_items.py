"""Custom shop items — pure availability and purchasability logic.

No discord, no database (docs/plans/economy-shop-items.md). An item is
admin-defined on the dashboard rather than compiled into ``perks.py``: a name,
a price, a fulfilment kind (grant a role, or spawn a staff to-do) and a billing
mode (pay once, or rent weekly), plus optional stock, a per-member limit and an
availability window.

Everything a caller needs in order to say *why* a member can't buy something
lives here, deterministic on its inputs, so the refusal matrix stays
table-testable. The service layer holds the database and the money; it asks
this module for the verdict first and never re-derives one of its own.

The verdict deliberately reports the FIRST failing gate in a fixed order
(existence → enabled → window → stock → per-member limit → funds) rather than
a set. A member who is both out of budget and past the deadline is told about
the deadline: telling them to earn more coins for something they can no longer
buy sends them off to waste a week.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

KIND_ROLE = "role"
KIND_MANUAL = "manual"
KINDS = frozenset({KIND_ROLE, KIND_MANUAL})

BILLING_ONCE = "once"
BILLING_WEEKLY = "weekly"
BILLINGS = frozenset({BILLING_ONCE, BILLING_WEEKLY})

# Purchase states. `pending` is the manual queue; `fulfilled` and `lapsed` are
# terminal ends of a delivered order; the middle three are the refund paths.
STATE_PENDING = "pending"
STATE_FULFILLED = "fulfilled"
STATE_LIVE = "live"
STATE_DENIED = "denied"
STATE_CANCELLED = "cancelled"
STATE_EXPIRED = "expired"
STATE_LAPSED = "lapsed"

# States whose money was returned to the buyer. They neither hold stock nor
# count against a per-member limit — a refunded order has to leave no trace on
# what the member may buy next, or a mod's refusal would quietly consume the
# one purchase they were allowed.
REFUNDED_STATES = frozenset({STATE_DENIED, STATE_CANCELLED, STATE_EXPIRED})

# States that still hold their unit of stock and their place under the limit:
# everything that was paid for and not given back, whether or not it has been
# delivered yet.
HOLDING_STATES = frozenset(
    {STATE_PENDING, STATE_FULFILLED, STATE_LIVE, STATE_LAPSED}
)

# A member may withdraw an order only while nobody has acted on it.
CANCELLABLE_STATES = frozenset({STATE_PENDING})


class Refusal(Enum):
    """Why a purchase can't proceed. ``OK`` is the only passing value."""

    OK = "ok"
    UNKNOWN = "unknown"
    DISABLED = "disabled"
    NOT_YET = "not_yet"
    ENDED = "ended"
    SOLD_OUT = "sold_out"
    LIMIT_REACHED = "limit_reached"
    ALREADY_RENTED = "already_rented"
    INSUFFICIENT = "insufficient"


# Member-facing text. Deliberately plain: an item that is out of stock, past
# its window or beyond a member's limit is a fact about the shop, not a
# telling-off. Kept here rather than in the view so the refusal a test asserts
# on is the refusal the member reads.
REFUSAL_TEXT: dict[Refusal, str] = {
    Refusal.UNKNOWN: "❌ That item isn't in the shop.",
    Refusal.DISABLED: "❌ That item isn't for sale right now.",
    Refusal.NOT_YET: "❌ That item isn't on sale yet.",
    Refusal.ENDED: "❌ That item is no longer on sale.",
    Refusal.SOLD_OUT: "❌ That one's sold out.",
    Refusal.LIMIT_REACHED: "❌ You've already bought as many of those as you can.",
    Refusal.ALREADY_RENTED: "❌ You're already renting that one.",
    Refusal.INSUFFICIENT: "❌ You can't afford that yet.",
}


@dataclass(frozen=True)
class ItemView:
    """The fields of an item this module needs, lifted off the row.

    A frozen snapshot rather than a live row so the verdict can be computed in
    a test without a database, and so the service can't accidentally mutate
    the thing it is asking about.
    """

    item_id: int
    name: str
    price: int
    #: The longer text shown on the buy confirmation and carried onto the
    #: staff todo, where there is room for it.
    description: str = ""
    kind: str = KIND_MANUAL
    billing: str = BILLING_ONCE
    role_id: int | None = None
    stock: int | None = None
    sold: int = 0
    per_member_limit: int | None = None
    available_from: float | None = None
    available_until: float | None = None
    ask_note: bool = False
    enabled: bool = True

    @property
    def is_rental(self) -> bool:
        return self.billing == BILLING_WEEKLY

    @property
    def needs_staff(self) -> bool:
        return self.kind == KIND_MANUAL

    @property
    def remaining(self) -> int | None:
        """Units left, or None when the item is unlimited.

        Clamped at 0: `sold` can exceed `stock` if an admin lowers the stock
        under live orders, and a negative count would render as "-2 left".
        """
        if self.stock is None:
            return None
        return max(0, self.stock - self.sold)


def visible(item: ItemView, now: float, *, owned: bool = False) -> bool:
    """Should this item appear in the shop listing at all?

    Disabled items and items outside their window are hidden rather than shown
    refused — an item nobody can buy is noise in a table read on a phone.

    A **sold-out** item stays visible: "sold out" is information the member
    wants, and silently removing the row reads as the shop being broken.

    ``owned`` overrides everything, for the same reason the palette row does:
    an item can be disabled or run past its window while someone is still
    renting it, and hiding the row then would bill a member weekly for
    something with no price and no name anywhere in the shop.
    """
    if owned:
        return True
    if not item.enabled:
        return False
    if item.available_from is not None and now < item.available_from:
        return False
    return not (item.available_until is not None and now >= item.available_until)


def evaluate_purchase(
    item: ItemView | None,
    *,
    now: float,
    balance: int,
    owned_count: int = 0,
    holds_rental: bool = False,
) -> Refusal:
    """Can this member buy this item right now, and if not, why not?

    ``owned_count`` is how many non-refunded purchases of this item the member
    already holds; ``holds_rental`` says whether a live rental of it exists.
    Order of gates is fixed and documented in the module docstring.
    """
    if item is None:
        return Refusal.UNKNOWN
    if not item.enabled:
        return Refusal.DISABLED
    if item.available_from is not None and now < item.available_from:
        return Refusal.NOT_YET
    if item.available_until is not None and now >= item.available_until:
        return Refusal.ENDED
    if item.remaining == 0:
        return Refusal.SOLD_OUT
    # A live rental is a clearer answer than "you've hit your limit" for the
    # member who simply already has the thing, so it is checked first.
    if holds_rental:
        return Refusal.ALREADY_RENTED
    if item.per_member_limit is not None and owned_count >= item.per_member_limit:
        return Refusal.LIMIT_REACHED
    if balance < item.price:
        return Refusal.INSUFFICIENT
    return Refusal.OK


def refusal_text(refusal: Refusal) -> str:
    """The member-facing line for a refusal. ``OK`` has no text."""
    if refusal is Refusal.OK:
        raise ValueError("OK is not a refusal")
    return REFUSAL_TEXT[refusal]


def todo_task_text(item_name: str) -> str:
    """The task line for the todo a manual purchase spawns.

    **Names the item, never the buyer.** `docs/data_register.md` anonymises a
    `todos` row on erasure rather than deleting it, on the stated ground that
    the task text is server work product and not member disclosure — the ids
    are blanked and the work stands. A task reading "Deliver X to @someone"
    would leave the member inside the half of the row that survives. The buyer
    is rendered live by joining `todos.purchase_id`, so an erased buyer's order
    shows the same "unknown member" every other surface shows.
    """
    return f"Deliver {item_name}"


def expiry_cutoff(now: float, expire_days: int) -> float:
    """Orders created before this instant have gone unresolved too long.

    ``expire_days <= 0`` disables the sweep (returns ``-inf``, matching
    nothing) rather than expiring every open order at once.
    """
    if expire_days <= 0:
        return float("-inf")
    return now - expire_days * 86400
