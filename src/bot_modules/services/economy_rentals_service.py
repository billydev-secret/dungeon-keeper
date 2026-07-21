"""Economy rentals — the DB layer for weekly perk rentals and personal roles.

The rental billing state machine (spec §6) is the money-critical core here.
Every rental row walks active → grace → lapsed/cancelled; the pure decision
lives in ``economy/rentals.classify`` and this module applies it, riding the
caller's connection/transaction so a rental's state change and its ledger row
always land together (no internal commits — the caller's ``with open_db(...)``
is the commit boundary, matching stage 0/1/2 economy functions). Discord
effects are the caller's job and always run post-commit.

The partial unique index ``idx_econ_rentals_live`` — one live rental per
(guild, user, perk, beneficiary) — is the race anchor: ``rent_perk`` lets the
index decide the winner (catch IntegrityError → ValueError "already rented")
rather than read-before-write. ``beneficiary_id`` is always non-NULL so the
index actually fires (SQLite treats NULLs as distinct).

Two billing invariants worth stating up front:

- **No drift.** A successful charge advances ``next_bill_at`` from the
  *scheduled* anniversary, not from ``now``, so the weekly cadence never slips
  when the loop runs late.
- **Charge once after downtime.** If the loop was down for several weeks the
  rental is charged exactly ONCE and ``next_bill_at`` jumps to the next
  anniversary strictly in the future — charging once per elapsed week would
  double-bill after an outage.

``BillingResult.action`` is the string vocabulary the loop matches on for its
post-commit Discord effects (values are ``BillingAction.*.value``):
  ``none``               — nothing due; no side effect.
  ``charge``             — renewed OR recovered from grace; silent. (Includes a
                           successful retry: the role was never revoked during
                           grace, so no re-projection is needed.)
  ``enter_grace``        — the debit failed and the rental just entered grace;
                           DM the owner once ("payment failed, Xh grace").
  ``retry``              — still in grace, retry debit failed again; silent (no
                           repeat DM).
  ``revoke``             — grace elapsed (>=36h); state → lapsed; revoke perks
                           + DM.
  ``cancel_period_end``  — owner-cancelled active rental hit its anniversary;
                           state → cancelled, no charge; revoke perks, silent.

Ledger kinds added here: ``rental`` (upfront charge + weekly renewal).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot_modules.economy import rentals
from bot_modules.economy.rentals import WEEK_SECONDS, BillingAction, classify
from bot_modules.services.economy_raffle_service import try_redeem_voucher
from bot_modules.services.economy_service import apply_debit

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings

# The perks rent_perk will open a rental for. ``emoji`` (in the 091 CHECK) is
# deliberately absent: an emoji rental is opened by
# ``economy_emoji_service.finalize_upload`` after mod approval — never by a
# direct shop rent. A gift is any of these rented with
# ``beneficiary_id`` != ``user_id`` (the gift_color kind retired in 091).
_PERKS = ("role_color", "role_name", "role_gradient", "role_icon", "voice_style")

_RENTAL_COLS = (
    "id, guild_id, user_id, perk, state, price, started_at, next_bill_at, "
    "grace_since, cancel_at_period_end, suspended, suspended_since, "
    "beneficiary_id, catalog_icon_id, meta, created_at"
)

# Personal-role columns a caller may write via upsert_personal_role.
# ``projected_icon_path`` is projector bookkeeping (what icon is currently on the
# Discord role) — writable so the projector can record an icon switch it applied.
_PERSONAL_ROLE_FIELDS = frozenset(
    {"role_id", "name", "color", "color2", "icon_path", "projected_icon_path"}
)


@dataclass(frozen=True)
class BillingResult:
    """One rental's billing outcome for a single tick (for the loop's effects)."""

    rental_id: int
    action: str  # a BillingAction value — see module docstring
    charged: int
    perk: str
    user_id: int
    beneficiary_id: int


def _catalog_icon_price(
    conn: sqlite3.Connection, guild_id: int, icon_id: int
) -> int | None:
    """The current weekly price of a catalog icon, or None if the row is gone."""
    row = conn.execute(
        "SELECT price FROM econ_icon_catalog WHERE guild_id = ? AND id = ?",
        (guild_id, icon_id),
    ).fetchone()
    return int(row["price"]) if row is not None else None


def _price_for(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    perk: str,
    catalog_icon_id: int | None,
    meta_json: str | None = None,
) -> int:
    """The current weekly price for a rental.

    A ``role_icon`` rental tied to a catalog icon bills that icon's CURRENT
    price (re-read every renewal, so an admin price edit lands at the next
    anniversary — spec §6/§9); if the icon row has vanished it falls back to the
    flat ``settings.price_role_icon``. Every other rental bills the flat
    ``settings.price_<perk>``.
    """
    if perk == "role_icon" and catalog_icon_id:
        price = _catalog_icon_price(conn, guild_id, int(catalog_icon_id))
        if price is not None:
            return price
    if perk == "emoji":
        # Animated emojis bill their own rate; the flag rides the rental meta
        # written at upload time (economy_emoji_service.finalize_upload).
        try:
            animated = bool(json.loads(meta_json or "{}").get("animated"))
        except (TypeError, ValueError):
            animated = False
        return int(
            settings.price_emoji_animated if animated else settings.price_emoji
        )
    return int(getattr(settings, f"price_{perk}"))


def _get_rental(conn: sqlite3.Connection, rental_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_RENTAL_COLS} FROM econ_rentals WHERE id = ?", (rental_id,)
    ).fetchone()


def _advance_from_scheduled(next_bill_at: float, now: float) -> float:
    """Next anniversary strictly after ``now``, stepping from the scheduled time.

    Advances at least one week (a charge always consumes the due anniversary),
    then keeps stepping until future — so a multi-week outage yields ONE future
    anniversary, not a backlog, and the cadence never drifts off ``now``.
    """
    nb = next_bill_at + WEEK_SECONDS
    while nb <= now:
        nb += WEEK_SECONDS
    return nb


# ── rentals: create / cancel / list ───────────────────────────────────


def rent_perk(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    user_id: int,
    perk: str,
    *,
    beneficiary_id: int | None = None,
    catalog_icon_id: int | None = None,
    now: float | None = None,
) -> sqlite3.Row:
    """Rent a perk: charge the first week upfront and open a live rental row.

    The price is snapshotted at rent time (renewals re-read the then-current
    price). ``catalog_icon_id`` ties a ``role_icon`` rental to a curated catalog
    icon, whose per-icon price is billed instead of the flat
    ``settings.price_role_icon`` (NULL = a bring-your-own icon at the flat
    price). ``beneficiary_id`` defaults to ``user_id``; a gift passes the
    friend's id, making them the beneficiary of the base perk. It is always
    stored non-NULL so the live-rental unique index fires. Raises ValueError: unknown ``perk``; "already rented" when a live
    rental for this (perk, beneficiary) exists (IntegrityError on the partial
    unique index); "insufficient" when the upfront debit fails (guarded UPDATE →
    zero writes, and the whole insert rolls back with it).
    """
    if perk not in _PERKS:
        # Validate BEFORE the insert so the perk CHECK can never masquerade as
        # the live-rental collision (only that index may fire in the try below).
        raise ValueError(f"unknown perk: {perk!r}")
    now = time.time() if now is None else now
    beneficiary = user_id if beneficiary_id is None else beneficiary_id
    price = _price_for(conn, settings, guild_id, perk, catalog_icon_id)
    next_bill_at = now + WEEK_SECONDS

    try:
        cur = conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at,
                 next_bill_at, cancel_at_period_end, suspended,
                 beneficiary_id, catalog_icon_id, created_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, 0, 0, ?, ?, ?)
            """,
            (
                guild_id, user_id, perk, price, now, next_bill_at, beneficiary,
                catalog_icon_id, now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Only idx_econ_rentals_live can fire — perk/state CHECKs are guarded
        # above (perk) or literals (state).
        raise ValueError("already rented") from exc

    rental_id = int(cur.lastrowid or 0)
    # A raffle free-week voucher covers the first week of a new rent (spec
    # §6 stage 5) — the redeem writes its own 0-amount ledger row.
    if not try_redeem_voucher(
        conn, guild_id, user_id, rental_id=rental_id, perk=perk,
        covered=price, now=now,
    ):
        ok = apply_debit(
            conn, guild_id, user_id, price, "rental",
            actor_id=user_id, meta={"rental_id": rental_id, "perk": perk},
        )
        if not ok:
            # Roll back the whole insert by raising — the caller's transaction
            # unwinds, so an unaffordable rent leaves zero writes.
            raise ValueError("insufficient")

    # shop_purchase quest trigger (one-time setup kind) — the voluntary rent
    # only; renewal billing in bill_rental never fires. Voucher-covered rents
    # count too: the quest rewards engaging with the shop, not the spend
    # itself. Deferred import (the quests service imports this module's
    # sibling machinery).
    from bot_modules.services.economy_quests_service import (  # noqa: PLC0415
        fire_trigger_inline,
    )

    fire_trigger_inline(conn, guild_id, "shop_purchase", user_id, occurrence="set")

    row = _get_rental(conn, rental_id)
    assert row is not None  # just inserted in this transaction
    return row


def cancel_rental(
    conn: sqlite3.Connection,
    guild_id: int,
    rental_id: int,
    *,
    requester_id: int,
    force: bool = False,
    now: float | None = None,
) -> sqlite3.Row:
    """Cancel a rental. Owner-only unless ``force`` (manager/system cleanup).

    An active rental is marked ``cancel_at_period_end`` — the perk runs out the
    paid week and the anniversary tick finalises it (no refund; ``ended_at`` is
    stamped then, by ``bill_rental``). A grace rental is cancelled immediately
    (nothing paid to run out) and ``ended_at`` is stamped now. Raises ValueError
    when the rental is missing, not the requester's (and not ``force``), or
    already terminal.
    """
    now = time.time() if now is None else now
    row = _get_rental(conn, rental_id)
    if row is None or int(row["guild_id"]) != guild_id:
        raise ValueError("rental not found")
    if not force and int(row["user_id"]) != requester_id:
        raise ValueError("not your rental")
    state = row["state"]
    if state == "active":
        conn.execute(
            "UPDATE econ_rentals SET cancel_at_period_end = 1 WHERE id = ?",
            (rental_id,),
        )
    elif state == "grace":
        conn.execute(
            "UPDATE econ_rentals SET state = 'cancelled', ended_at = ? WHERE id = ?",
            (now, rental_id),
        )
    else:
        raise ValueError("rental is not live")
    updated = _get_rental(conn, rental_id)
    assert updated is not None
    return updated


def cancel_all_for_member(
    conn: sqlite3.Connection, guild_id: int, user_id: int, *, now: float | None = None
) -> list[sqlite3.Row]:
    """Immediately cancel every live rental touching a member (leave/ban).

    Cancels rentals the member OWNS *and* rentals where they are the
    beneficiary (a gifted perk lapses when the recipient leaves; the giver's
    gift rental is cancelled when the giver leaves). Returns the affected rows
    (post-update) so the caller can re-project / clean up Discord roles.
    """
    now = time.time() if now is None else now
    rows = conn.execute(
        f"""
        SELECT {_RENTAL_COLS} FROM econ_rentals
        WHERE guild_id = ? AND state IN ('active', 'grace')
          AND (user_id = ? OR beneficiary_id = ?)
        """,
        (guild_id, user_id, user_id),
    ).fetchall()
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE econ_rentals SET state = 'cancelled', ended_at = ? "
        f"WHERE id IN ({placeholders})",
        (now, *ids),
    )
    return [r for r in (_get_rental(conn, rid) for rid in ids) if r is not None]


def list_rentals(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    states: tuple[str, ...] = ("active", "grace"),
) -> list[sqlite3.Row]:
    """All rentals in a guild in the given states (default: the live ones)."""
    placeholders = ",".join("?" for _ in states)
    return conn.execute(
        f"""
        SELECT {_RENTAL_COLS} FROM econ_rentals
        WHERE guild_id = ? AND state IN ({placeholders})
        ORDER BY next_bill_at ASC, id ASC
        """,
        (guild_id, *states),
    ).fetchall()


def list_member_rentals(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> list[sqlite3.Row]:
    """A member's live rentals — ones they own OR are the beneficiary of."""
    return conn.execute(
        f"""
        SELECT {_RENTAL_COLS} FROM econ_rentals
        WHERE guild_id = ? AND state IN ('active', 'grace')
          AND (user_id = ? OR beneficiary_id = ?)
        ORDER BY next_bill_at ASC, id ASC
        """,
        (guild_id, user_id, user_id),
    ).fetchall()


def get_live_role_icon_rental(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's live (active|grace) ``role_icon`` rental, or None.

    Self-perk, so the renter is the beneficiary — used by the catalog flow to
    decide between renting a new icon and switching an existing rental's icon.
    """
    return conn.execute(
        f"""
        SELECT {_RENTAL_COLS} FROM econ_rentals
        WHERE guild_id = ? AND user_id = ? AND perk = 'role_icon'
          AND state IN ('active', 'grace')
        LIMIT 1
        """,
        (guild_id, user_id),
    ).fetchone()


def set_rental_catalog_icon(
    conn: sqlite3.Connection, guild_id: int, rental_id: int, catalog_icon_id: int
) -> None:
    """Point a live rental at a different catalog icon (an in-week icon switch).

    Only the icon tag changes here — no charge and no price snapshot update, so
    the member finishes the week they already paid for and the newly chosen
    icon's price takes effect at the next renewal (``bill_rental`` re-reads it),
    matching how a mid-rental price change behaves.
    """
    conn.execute(
        "UPDATE econ_rentals SET catalog_icon_id = ? WHERE guild_id = ? AND id = ?",
        (catalog_icon_id, guild_id, rental_id),
    )


# ── billing ────────────────────────────────────────────────────────────


def bill_rental(
    conn: sqlite3.Connection, settings: EconSettings, rental: sqlite3.Row, now: float
) -> BillingResult:
    """Apply one tick of the billing state machine to a rental.

    Delegates the decision to ``rentals.classify`` and executes it, writing the
    new state (and, on a charge, the debit + refreshed price snapshot) in the
    caller's transaction. Renewals bill the CURRENT guild price
    (``settings.price_<perk>``) — a mid-rental price change takes effect at the
    next anniversary (spec §6/§9). A suspended rental returns ``none`` (billing
    frozen; the clock resumes via ``set_rental_suspended``).
    """
    rental_id = int(rental["id"])
    perk = str(rental["perk"])
    user_id = int(rental["user_id"])
    beneficiary_id = int(rental["beneficiary_id"])

    def _result(action: BillingAction, charged: int = 0) -> BillingResult:
        return BillingResult(
            rental_id=rental_id, action=action.value, charged=charged,
            perk=perk, user_id=user_id, beneficiary_id=beneficiary_id,
        )

    action = classify(
        state=str(rental["state"]),
        next_bill_at=float(rental["next_bill_at"]),
        grace_since=(
            None if rental["grace_since"] is None else float(rental["grace_since"])
        ),
        cancel_at_period_end=bool(rental["cancel_at_period_end"]),
        suspended=bool(rental["suspended"]),
        now=now,
    )

    if action is BillingAction.NONE:
        return _result(BillingAction.NONE)

    if action is BillingAction.CANCEL_PERIOD_END:
        conn.execute(
            "UPDATE econ_rentals SET state = 'cancelled', ended_at = ? WHERE id = ?",
            (now, rental_id),
        )
        return _result(BillingAction.CANCEL_PERIOD_END)

    if action is BillingAction.REVOKE:
        conn.execute(
            "UPDATE econ_rentals SET state = 'lapsed', ended_at = ? WHERE id = ?",
            (now, rental_id),
        )
        return _result(BillingAction.REVOKE)

    # CHARGE (first attempt this period) or RETRY (in grace) both try the
    # debit — unless a raffle free-week voucher covers this renewal, which
    # counts as a successful payment (grace recovery included).
    price = _price_for(
        conn, settings, int(rental["guild_id"]), perk,
        rental["catalog_icon_id"], meta_json=rental["meta"],
    )
    ok = try_redeem_voucher(
        conn, int(rental["guild_id"]), user_id, rental_id=rental_id,
        perk=perk, covered=price, now=now,
    ) is not None or apply_debit(
        conn, rental["guild_id"], user_id, price, "rental",
        actor_id=user_id, meta={"rental_id": rental_id, "perk": perk, "renewal": True},
    )

    if ok:
        # Renewal or grace recovery: advance from the ORIGINAL scheduled time
        # (no drift, one charge after downtime), clear grace, refresh price.
        next_bill_at = _advance_from_scheduled(float(rental["next_bill_at"]), now)
        conn.execute(
            """
            UPDATE econ_rentals
            SET state = 'active', next_bill_at = ?, grace_since = NULL, price = ?
            WHERE id = ?
            """,
            (next_bill_at, price, rental_id),
        )
        return _result(BillingAction.CHARGE, charged=price)

    # Debit failed.
    if action is BillingAction.RETRY:
        # Already in grace — stay there (no state write, no repeat DM).
        return _result(BillingAction.RETRY)
    # First failure this period — enter grace, anchoring the 36h window at now.
    conn.execute(
        "UPDATE econ_rentals SET state = 'grace', grace_since = ? WHERE id = ?",
        (now, rental_id),
    )
    return _result(BillingAction.ENTER_GRACE)


def set_rental_suspended(
    conn: sqlite3.Connection, rental_id: int, suspended: bool, *, now: float | None = None
) -> None:
    """Freeze or resume a rental's billing clock (feature gained/lost).

    Suspend records ``suspended_since`` so resume can push ``next_bill_at``
    (and ``grace_since`` if set) forward by the frozen span — no charge accrues
    while suspended and the anniversary lands the same distance out as before.
    Idempotent: suspending an already-suspended rental (or resuming a live one)
    is a no-op.
    """
    now = time.time() if now is None else now
    row = _get_rental(conn, rental_id)
    if row is None:
        return
    currently = bool(row["suspended"])
    if suspended:
        if currently:
            return
        conn.execute(
            "UPDATE econ_rentals SET suspended = 1, suspended_since = ? WHERE id = ?",
            (now, rental_id),
        )
        return
    # Resume.
    if not currently:
        return
    since = row["suspended_since"]
    delta = now - float(since) if since is not None else 0.0
    grace_since = row["grace_since"]
    new_grace = None if grace_since is None else float(grace_since) + delta
    conn.execute(
        """
        UPDATE econ_rentals
        SET suspended = 0, suspended_since = NULL,
            next_bill_at = next_bill_at + ?, grace_since = ?
        WHERE id = ?
        """,
        (delta, new_grace, rental_id),
    )


# ── entitlements + personal roles ──────────────────────────────────────


def entitlements(conn: sqlite3.Connection, guild_id: int, user_id: int) -> set[str]:
    """Perks the member is currently entitled to AS BENEFICIARY.

    Beneficiary-based so a gifted perk counts for the friend, not the payer.
    Live states (active|grace) grant the perk — see ``rentals.entitled_perks``.
    """
    rows = conn.execute(
        """
        SELECT perk, state FROM econ_rentals
        WHERE guild_id = ? AND beneficiary_id = ? AND state IN ('active', 'grace')
        """,
        (guild_id, user_id),
    ).fetchall()
    return rentals.entitled_perks(rows)


def get_personal_role(
    conn: sqlite3.Connection, guild_id: int, user_id: int
) -> sqlite3.Row | None:
    """The member's desired personal-role state, or None if never configured."""
    return conn.execute(
        """
        SELECT guild_id, user_id, role_id, name, color, color2, icon_path,
               projected_icon_path, updated_at
        FROM econ_personal_roles WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    ).fetchone()


def upsert_personal_role(
    conn: sqlite3.Connection, guild_id: int, user_id: int, values: dict[str, object]
) -> None:
    """Create or patch the member's desired personal-role state.

    ``values`` may set any of role_id / name / color / color2 / icon_path; an
    unknown key raises KeyError so callers can't write dead state. Omitted
    fields keep their stored value (or the column default on first insert).
    """
    unknown = set(values) - _PERSONAL_ROLE_FIELDS
    if unknown:
        raise KeyError(f"unknown personal-role field(s): {sorted(unknown)}")
    now = time.time()
    existing = get_personal_role(conn, guild_id, user_id)
    merged: dict[str, object] = {
        "role_id": None,
        "name": "",
        "color": -1,
        "color2": -1,
        "icon_path": "",
        "projected_icon_path": "",
    }
    if existing is not None:
        for k in merged:
            merged[k] = existing[k]
    merged.update(values)
    conn.execute(
        """
        INSERT INTO econ_personal_roles
            (guild_id, user_id, role_id, name, color, color2, icon_path,
             projected_icon_path, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id) DO UPDATE SET
            role_id             = excluded.role_id,
            name                = excluded.name,
            color               = excluded.color,
            color2              = excluded.color2,
            icon_path           = excluded.icon_path,
            projected_icon_path = excluded.projected_icon_path,
            updated_at          = excluded.updated_at
        """,
        (
            guild_id, user_id, merged["role_id"], merged["name"], merged["color"],
            merged["color2"], merged["icon_path"], merged["projected_icon_path"], now,
        ),
    )


def delete_personal_role(conn: sqlite3.Connection, guild_id: int, user_id: int) -> None:
    """Drop the member's desired personal-role row (after all perks lapse)."""
    conn.execute(
        "DELETE FROM econ_personal_roles WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
