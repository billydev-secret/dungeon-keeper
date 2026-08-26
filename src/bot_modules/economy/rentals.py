"""Pure rental-billing logic — no discord, no database (spec §6).

The weekly billing state machine's decision function plus the perk-entitlement
and color-mode derivations. Everything is deterministic on its inputs so the
billing matrix (state × due × grace-age × cancel × suspended × discontinued)
stays fully table-testable.

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

from bot_modules.economy.perks import GIFTABLE_PERKS

WEEK_SECONDS = 7 * 86400
GRACE_SECONDS = 36 * 3600

# What a staff comp covers: every rentable perk, as one block. Running the
# server shouldn't cost the people running it, and a partial comp would just
# be a second price list to keep in step with the first.
COMPED_PERKS = frozenset(GIFTABLE_PERKS)

# Perks whose entitlement grants a solid custom color (the beneficiary's).
# A gifted color is a role_color rental with a different beneficiary, so one
# kind covers both (gift_color retired in migration 091).
_SOLID_COLOR_PERKS = frozenset({"role_color"})

# Perks whose entitlement grants a two-colour fade. Both write the same
# ``econ_personal_roles.color``/``color2`` pair and project identically — they
# differ only in where the pair comes from (``role_gradient`` is member-picked,
# ``role_preset`` is chosen from the curated palette), which is a shop concern,
# not a projection one.
_GRADIENT_PERKS = frozenset({"role_gradient", "role_preset"})


class BillingAction(Enum):
    """A billing decision (from ``classify``) or reported outcome.

    ``classify`` returns only NONE / CHARGE / RETRY / REVOKE /
    CANCEL_PERIOD_END / DISCONTINUED. ENTER_GRACE is reported by the service
    after a failed debit — see the module docstring.
    """

    NONE = "none"
    CHARGE = "charge"
    ENTER_GRACE = "enter_grace"
    RETRY = "retry"
    REVOKE = "revoke"
    CANCEL_PERIOD_END = "cancel_period_end"
    #: The admin switched this perk off and the rental has reached its
    #: anniversary. Ends it exactly like CANCEL_PERIOD_END — no charge, perk
    #: revoked, the week they paid for honoured in full — but stays a separate
    #: value because the two differ in who decided: a member cancelling their
    #: own rental is told nothing (they already know), while someone whose perk
    #: stopped being sold out from under them is owed a DM.
    DISCONTINUED = "discontinued"


def classify(
    state: str,
    next_bill_at: float,
    grace_since: float | None,
    cancel_at_period_end: bool,
    suspended: bool,
    now: float,
    perk_disabled: bool = False,
) -> BillingAction:
    """Decide what the billing loop should do with a rental *right now*.

    - Suspended (a required guild feature vanished): NONE — the billing clock
      is frozen; the service pushes ``next_bill_at`` forward on resume.
    - Active and past its anniversary: CANCEL_PERIOD_END if the owner asked to
      cancel at period end, DISCONTINUED if the guild stopped selling the perk,
      else CHARGE (the caller checks funds and downgrades to grace on failure).
      Not yet due: NONE.
    - Grace: RETRY while within the 36h window, REVOKE once it has elapsed
      (revoke fires exactly at 36h — ``>=`` GRACE_SECONDS).
    - lapsed / cancelled (terminal): NONE.

    ``perk_disabled`` is read fresh from guild settings every tick and stored
    nowhere, which is what makes an admin's checkbox reversible: a rental only
    notices the perk is off at the moment it comes due, so re-checking the box
    any time before someone's anniversary renews them as if nothing happened.
    An own-cancel wins over it when both apply — the member asked first, and
    reporting DISCONTINUED there would DM them about a decision they made.
    A rental in grace is discontinued outright rather than retried: grace means
    the anniversary passed and the debit failed, so no paid week is left to
    honour, and a retry that succeeded would bill a fresh week for a perk that
    is no longer sold.
    """
    if suspended:
        return BillingAction.NONE
    if state == "active":
        if now < next_bill_at:
            return BillingAction.NONE
        if cancel_at_period_end:
            return BillingAction.CANCEL_PERIOD_END
        if perk_disabled:
            return BillingAction.DISCONTINUED
        return BillingAction.CHARGE
    if state == "grace":
        if perk_disabled:
            # End it rather than retry. Grace means the anniversary already
            # passed and the debit FAILED, so there is no paid week left to
            # honour — and a retry that succeeded would charge them a fresh
            # week for a perk the server has stopped selling, which is the one
            # thing this switch must never do.
            return BillingAction.DISCONTINUED
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


def comp_entitlements(
    rented: set[str],
    *,
    is_staff: bool,
    comp_enabled: bool,
    on_sale: Iterable[str] | None = None,
) -> set[str]:
    """Fold the staff comp into a member's rented entitlements.

    A comp is *not* a rental: no ``econ_rentals`` row, no ledger entry, no
    billing clock — mod status alone is the entitlement, so it appears the
    moment the role lands and evaporates the moment it goes. That is why this
    is a derivation over live inputs rather than persisted state; nothing can
    go stale against the member's real roles, and the economy's spend metrics
    never see a purchase that didn't happen.

    Union, not replacement: a mod who was already paying for a rental keeps
    that rental (we never cancel or refund it on their behalf), so their
    entitlement set is the same either way and cancelling stays their call.

    ``on_sale`` narrows the comp to the perks the guild still sells: switching
    a perk off has to take it off the staff too, or the one group who can see
    the checkbox would be the one group it doesn't apply to. It never touches
    ``rented`` — a mod who is genuinely paying for a discontinued perk keeps it
    until their week runs out, exactly like everyone else. None means "no
    restriction", for callers with no settings in hand.
    """
    if is_staff and comp_enabled:
        comped = COMPED_PERKS if on_sale is None else COMPED_PERKS & set(on_sale)
        return set(rented) | set(comped)
    return set(rented)


def effective_color_mode(perks: set[str]) -> str:
    """Resolve the member's color mode from their entitled perks (spec §6).

    Richest wins: 'holographic' (Discord's fixed three-colour preset) tops
    'gradient' (a two-colour fade, member-picked via ``role_gradient`` or chosen
    from the curated palette via ``role_preset``), which tops 'solid' (a
    role_color, self-rented or received as a gift), else 'none'. Holographic
    overrides the lower modes so a member who rents both wears the shimmer, not
    a stale fade.
    """
    if "role_holographic" in perks:
        return "holographic"
    if perks & _GRADIENT_PERKS:
        return "gradient"
    if perks & _SOLID_COLOR_PERKS:
        return "solid"
    return "none"
