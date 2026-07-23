"""Pure rental-billing logic — no discord, no database (spec §6).

The weekly billing state machine's decision function plus the perk-entitlement
and color-mode derivations. Everything is deterministic on its inputs so the
billing matrix (state × due × grace-age × cancel × suspended) stays fully
table-testable.

``classify`` deliberately never returns ``ENTER_GRACE``: a due active rental
returns ``CHARGE`` and the service downgrades to grace only when the debit
actually fails (funds are a runtime fact the pure layer can't see). The enum
still carries ``ENTER_GRACE`` because it is part of the billing-outcome
vocabulary the service reports on ``BillingResult`` — it is an *outcome*, not a
*decision*.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum

WEEK_SECONDS = 7 * 86400
GRACE_SECONDS = 36 * 3600

# Perks whose entitlement grants a solid custom color (the beneficiary's).
# A gifted color is a role_color rental with a different beneficiary, so one
# kind covers both (gift_color retired in migration 091).
_SOLID_COLOR_PERKS = frozenset({"role_color"})


class BillingAction(Enum):
    """A billing decision (from ``classify``) or reported outcome.

    ``classify`` returns only NONE / CHARGE / RETRY / REVOKE /
    CANCEL_PERIOD_END. ENTER_GRACE is reported by the service after a failed
    debit — see the module docstring.
    """

    NONE = "none"
    CHARGE = "charge"
    ENTER_GRACE = "enter_grace"
    RETRY = "retry"
    REVOKE = "revoke"
    CANCEL_PERIOD_END = "cancel_period_end"


def classify(
    state: str,
    next_bill_at: float,
    grace_since: float | None,
    cancel_at_period_end: bool,
    suspended: bool,
    now: float,
) -> BillingAction:
    """Decide what the billing loop should do with a rental *right now*.

    - Suspended (a required guild feature vanished): NONE — the billing clock
      is frozen; the service pushes ``next_bill_at`` forward on resume.
    - Active and past its anniversary: CANCEL_PERIOD_END if the owner asked to
      cancel at period end, else CHARGE (the caller checks funds and downgrades
      to grace on failure). Not yet due: NONE.
    - Grace: RETRY while within the 36h window, REVOKE once it has elapsed
      (revoke fires exactly at 36h — ``>=`` GRACE_SECONDS).
    - lapsed / cancelled (terminal): NONE.
    """
    if suspended:
        return BillingAction.NONE
    if state == "active":
        if now < next_bill_at:
            return BillingAction.NONE
        if cancel_at_period_end:
            return BillingAction.CANCEL_PERIOD_END
        return BillingAction.CHARGE
    if state == "grace":
        if grace_since is None:
            # Defensive: a grace row with no anchor can't age out — revoke it
            # rather than retry forever.
            return BillingAction.REVOKE
        if now - grace_since < GRACE_SECONDS:
            return BillingAction.RETRY
        return BillingAction.REVOKE
    return BillingAction.NONE


def prorated_refund(price: int, next_bill_at: float, now: float) -> int:
    """The unused-time refund for cancelling an active rental right now.

    ``floor(price * remaining / WEEK_SECONDS)`` — floor (not round) so a
    refund never exceeds what's genuinely unused, and remaining is clamped to
    ``[0, WEEK_SECONDS]`` so an overdue or clock-skewed rental never refunds
    more than one week's price back.
    """
    remaining = min(WEEK_SECONDS, max(0.0, next_bill_at - now))
    return max(0, min(price, int(price * remaining / WEEK_SECONDS)))


def entitled_perks(rentals: Iterable[Mapping[str, object] | object]) -> set[str]:
    """The set of perks the given rentals currently entitle.

    A rental grants its perk while ``state`` is active or grace (the perk stays
    on during the grace window — it is only revoked on lapse/cancel). Accepts
    sqlite3.Row or any ``["state"]``/``["perk"]``-indexable rows.
    """
    granted: set[str] = set()
    for r in rentals:
        if r["state"] in ("active", "grace"):  # type: ignore[index]
            granted.add(str(r["perk"]))  # type: ignore[index]
    return granted


def effective_color_mode(perks: set[str]) -> str:
    """Resolve the member's color mode from their entitled perks (spec §6).

    Richest wins: 'holographic' (Discord's fixed three-colour preset) tops
    'gradient' (member-picked two-colour), which tops 'solid' (a role_color,
    self-rented or received as a gift), else 'none'. Holographic overrides the
    lower modes so a member who rents both wears the shimmer, not a stale fade.
    """
    if "role_holographic" in perks:
        return "holographic"
    if "role_gradient" in perks:
        return "gradient"
    if perks & _SOLID_COLOR_PERKS:
        return "solid"
    return "none"
