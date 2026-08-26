"""Tests for economy/rentals.py — the pure billing-decision logic.

``classify`` gets an exhaustive table drive (state × due/not-due × grace-age ×
cancel-flag × suspended). Note there is no ENTER_GRACE row: ``classify`` never
returns it — a due active rental returns CHARGE and the service downgrades to
grace only on an actual debit failure (see the module docstring).
"""

from __future__ import annotations

import pytest

from bot_modules.economy.rentals import (
    COMPED_PERKS,
    GRACE_SECONDS,
    WEEK_SECONDS,
    BillingAction,
    classify,
    comp_entitlements,
    effective_color_mode,
    entitled_perks,
    prorated_refund,
)

NOW = 1_000_000.0
A = BillingAction


# ── classify: exhaustive matrix ────────────────────────────────────────

# (state, next_bill_at, grace_since, cancel, suspended) -> expected
_CASES = [
    # active, not due → NONE (with/without cancel flag, cancel is period-END)
    ("active", NOW + 10, None, False, False, A.NONE),
    ("active", NOW + 10, None, True, False, A.NONE),
    # active, exactly due → CHARGE (>= boundary counts as due)
    ("active", NOW, None, False, False, A.CHARGE),
    # active, past due → CHARGE
    ("active", NOW - 1, None, False, False, A.CHARGE),
    ("active", NOW - WEEK_SECONDS, None, False, False, A.CHARGE),
    # active, due + cancel flag → CANCEL_PERIOD_END (no charge)
    ("active", NOW, None, True, False, A.CANCEL_PERIOD_END),
    ("active", NOW - 1, None, True, False, A.CANCEL_PERIOD_END),
    # suspended always → NONE, regardless of due/cancel/state
    ("active", NOW - 1, None, False, True, A.NONE),
    ("active", NOW - 1, None, True, True, A.NONE),
    ("grace", NOW - 1, NOW - GRACE_SECONDS - 5, False, True, A.NONE),
    # grace, within window → RETRY (age just under 36h, and fresh)
    ("grace", NOW, NOW - 5, False, False, A.RETRY),
    ("grace", NOW, NOW - (GRACE_SECONDS - 1), False, False, A.RETRY),
    # grace, exactly at 36h → REVOKE (boundary: >= GRACE_SECONDS revokes)
    ("grace", NOW, NOW - GRACE_SECONDS, False, False, A.REVOKE),
    # grace, past window → REVOKE
    ("grace", NOW, NOW - GRACE_SECONDS - 100, False, False, A.REVOKE),
    # grace with cancel flag set still ages out normally (cancel is a no-op here)
    ("grace", NOW, NOW - 5, True, False, A.RETRY),
    ("grace", NOW, NOW - GRACE_SECONDS, True, False, A.REVOKE),
    # grace with a missing anchor → REVOKE defensively
    ("grace", NOW, None, False, False, A.REVOKE),
    # terminal states → NONE
    ("lapsed", NOW - 1, None, False, False, A.NONE),
    ("cancelled", NOW - 1, None, True, False, A.NONE),
]


@pytest.mark.parametrize(
    ("state", "next_bill_at", "grace_since", "cancel", "suspended", "expected"), _CASES
)
def test_classify_matrix(state, next_bill_at, grace_since, cancel, suspended, expected):
    assert (
        classify(state, next_bill_at, grace_since, cancel, suspended, NOW) is expected
    )


@pytest.mark.parametrize(
    ("state", "next_bill_at", "grace_since", "cancel", "suspended", "expected"),
    _CASES,
)
def test_classify_matrix_is_unchanged_when_the_perk_is_still_sold(
    state, next_bill_at, grace_since, cancel, suspended, expected
):
    """perk_disabled=False must reproduce the whole pre-existing matrix.

    The switch is additive: passing it explicitly off is the same as the
    default, so no existing billing path can shift under it.
    """
    assert (
        classify(
            state, next_bill_at, grace_since, cancel, suspended, NOW,
            perk_disabled=False,
        )
        is expected
    )


# (state, next_bill_at, grace_since, cancel, suspended) -> expected, with the
# guild no longer selling the perk.
_DISABLED_CASES = [
    # The whole point: due, nobody cancelled, perk withdrawn → ends unbilled.
    pytest.param("active", NOW, None, False, False, A.DISCONTINUED, id="due-exactly"),
    pytest.param("active", NOW - 1, None, False, False, A.DISCONTINUED, id="past-due"),
    pytest.param(
        "active", NOW - WEEK_SECONDS, None, False, False, A.DISCONTINUED,
        id="long-past-due",
    ),
    # NOT yet due → NONE. This is what makes "run to expiry" true and the
    # checkbox reversible: the week they paid for runs out in full, and
    # re-checking the box before then renews them as if nothing happened.
    pytest.param("active", NOW + 10, None, False, False, A.NONE, id="not-due-yet"),
    pytest.param(
        "active", NOW + WEEK_SECONDS, None, False, False, A.NONE,
        id="not-due-a-whole-week-out",
    ),
    # The member's own cancel wins — same ending, but reporting DISCONTINUED
    # would DM them about a decision they made themselves.
    pytest.param(
        "active", NOW, None, True, False, A.CANCEL_PERIOD_END, id="own-cancel-wins",
    ),
    # Suspended still freezes everything, switch or no switch.
    pytest.param("active", NOW - 1, None, False, True, A.NONE, id="suspended-wins"),
    # In grace the anniversary has already passed and the debit FAILED, so
    # there is no paid week left to protect. End it instead of retrying — a
    # retry that succeeded would charge a fresh week for a withdrawn perk.
    pytest.param(
        "grace", NOW, NOW - 5, False, False, A.DISCONTINUED, id="grace-ends-early",
    ),
    pytest.param(
        "grace", NOW, NOW - GRACE_SECONDS, False, False, A.DISCONTINUED,
        id="grace-past-window-ends-too",
    ),
    pytest.param(
        "grace", NOW, None, False, False, A.DISCONTINUED, id="grace-no-anchor-ends",
    ),
    # Terminal rows stay terminal.
    pytest.param("lapsed", NOW - 1, None, False, False, A.NONE, id="lapsed-stays"),
    pytest.param(
        "cancelled", NOW - 1, None, False, False, A.NONE, id="cancelled-stays",
    ),
]


@pytest.mark.parametrize(
    ("state", "next_bill_at", "grace_since", "cancel", "suspended", "expected"),
    _DISABLED_CASES,
)
def test_classify_when_the_guild_stopped_selling_the_perk(
    state, next_bill_at, grace_since, cancel, suspended, expected
):
    assert (
        classify(
            state, next_bill_at, grace_since, cancel, suspended, NOW,
            perk_disabled=True,
        )
        is expected
    )


def test_classify_never_returns_enter_grace():
    # ENTER_GRACE is an outcome the service reports, never a classify decision.
    seen = {classify(c[0], c[1], c[2], c[3], c[4], NOW) for c in _CASES}
    seen |= {
        classify(*c.values[:5], NOW, perk_disabled=True) for c in _DISABLED_CASES
    }
    assert BillingAction.ENTER_GRACE not in seen


# ── entitled_perks ─────────────────────────────────────────────────────


def _r(perk, state):
    return {"perk": perk, "state": state}


def test_entitled_perks_active_and_grace_grant():
    rows = [
        _r("role_color", "active"),
        _r("role_icon", "grace"),
        _r("role_name", "lapsed"),
        _r("role_gradient", "cancelled"),
    ]
    assert entitled_perks(rows) == {"role_color", "role_icon"}


def test_entitled_perks_empty():
    assert entitled_perks([]) == set()
    assert entitled_perks([_r("role_color", "lapsed")]) == set()


# ── comp_entitlements (the staff perk comp) ────────────────────────────


@pytest.mark.parametrize(
    "rented,is_staff,comp_enabled,expected",
    [
        # The comp itself: staff + switch on = every rentable perk, free.
        pytest.param(
            set(), True, True, set(COMPED_PERKS), id="staff-on-gets-everything"
        ),
        # The refusal path. A non-mod is untouched by the comp whether the
        # guild has it switched on or not — this is what keeps the paid shop
        # a paid shop for everyone else.
        pytest.param(set(), False, True, set(), id="non-staff-gets-nothing"),
        pytest.param(
            {"role_color"}, False, True, {"role_color"},
            id="non-staff-keeps-only-what-they-rent",
        ),
        # Switch off = nobody is comped, mod or not. Default state for a
        # guild that never opts in.
        pytest.param(set(), True, False, set(), id="staff-comp-off-gets-nothing"),
        pytest.param(
            {"role_color"}, True, False, {"role_color"},
            id="staff-comp-off-keeps-own-rental",
        ),
        # Union, not replacement: a mod who was already paying keeps the
        # rental (we never cancel it for them), and it doesn't double up.
        pytest.param(
            {"role_color"}, True, True, set(COMPED_PERKS),
            id="staff-rental-absorbed-not-duplicated",
        ),
        # A perk outside the comped set (a retired/gifted kind still on a
        # live row) survives the union rather than being dropped.
        pytest.param(
            {"gift_color"}, True, True, set(COMPED_PERKS) | {"gift_color"},
            id="staff-keeps-non-comped-kinds",
        ),
    ],
)
def test_comp_entitlements(rented, is_staff, comp_enabled, expected):
    assert (
        comp_entitlements(rented, is_staff=is_staff, comp_enabled=comp_enabled)
        == expected
    )


def test_comp_entitlements_covers_every_rentable_perk():
    """The comp is "everything", so it must track the shop's perk list.

    A perk added to the shop but not to COMPED_PERKS would quietly become the
    one thing a mod still has to pay for.
    """
    from bot_modules.economy.perks import GIFTABLE_PERKS, PERK_LABELS

    assert COMPED_PERKS == set(GIFTABLE_PERKS) == set(PERK_LABELS)


def test_comp_entitlements_does_not_mutate_the_input():
    rented = {"role_color"}
    comp_entitlements(rented, is_staff=True, comp_enabled=True)
    # The caller's rental truth is what billing and refunds read — the comp
    # must never leak back into it.
    assert rented == {"role_color"}


# ── comp_entitlements × the shop switches ──────────────────────────────


def test_comp_narrows_to_what_the_guild_still_sells():
    """Switching a perk off takes it off the comped staff too.

    Otherwise the one group who can see the checkbox would be the one group it
    doesn't apply to, and a server that "turned gradient roles off" would still
    have its mods wearing them.
    """
    on_sale = {"role_color", "role_name"}
    assert comp_entitlements(
        set(), is_staff=True, comp_enabled=True, on_sale=on_sale
    ) == on_sale


def test_comp_with_nothing_on_sale_comps_nothing():
    # Billy's actual ask — every box unchecked — must leave staff with an
    # empty comp rather than falling back to "everything".
    assert comp_entitlements(
        set(), is_staff=True, comp_enabled=True, on_sale=set()
    ) == set()


def test_comp_never_strips_a_perk_the_mod_is_actually_paying_for():
    # A mod mid-rental on a withdrawn perk winds down like everyone else: they
    # keep it to their anniversary. The comp narrowing must not cut it short.
    assert comp_entitlements(
        {"role_gradient"}, is_staff=True, comp_enabled=True,
        on_sale={"role_color"},
    ) == {"role_gradient", "role_color"}


def test_comp_without_an_on_sale_list_is_unrestricted():
    # The default keeps the pure function usable by callers with no settings
    # in hand, and pins the pre-existing behaviour.
    assert comp_entitlements(
        set(), is_staff=True, comp_enabled=True
    ) == set(COMPED_PERKS)


def test_comp_narrowing_does_not_invent_perks():
    # on_sale is an intersection, never a source: a string that isn't a
    # rentable perk can't be comped into existence by listing it.
    assert comp_entitlements(
        set(), is_staff=True, comp_enabled=True,
        on_sale={"role_color", "not_a_perk"},
    ) == {"role_color"}


# ── prorated_refund ──────────────────────────────────────────────────────


def test_prorated_refund_full_week_remaining():
    assert prorated_refund(70, NOW + WEEK_SECONDS, NOW) == 70


def test_prorated_refund_half_week_remaining():
    assert prorated_refund(70, NOW + WEEK_SECONDS / 2, NOW) == 35


def test_prorated_refund_floors_not_rounds():
    # 1/3 of the week left on a price of 10 -> 3.33, floors to 3 (never 4).
    assert prorated_refund(10, NOW + WEEK_SECONDS / 3, NOW) == 3


def test_prorated_refund_no_time_left():
    assert prorated_refund(70, NOW, NOW) == 0


def test_prorated_refund_overdue_clamps_to_zero_not_negative():
    assert prorated_refund(70, NOW - 100, NOW) == 0


def test_prorated_refund_clock_skew_clamps_to_one_week_not_more():
    # A next_bill_at further than a week out (shouldn't normally happen, but
    # the clamp keeps a refund from ever exceeding the full price).
    assert prorated_refund(70, NOW + WEEK_SECONDS * 5, NOW) == 70


# ── effective_color_mode ───────────────────────────────────────────────


def test_color_mode_none():
    assert effective_color_mode(set()) == "none"
    assert effective_color_mode({"role_name", "role_icon"}) == "none"


def test_color_mode_solid_from_role_color():
    assert effective_color_mode({"role_color"}) == "solid"


def test_color_mode_gift_color_kind_retired():
    # A gifted color is a role_color rental since migration 091 — the old
    # gift_color kind must no longer grant anything on its own.
    assert effective_color_mode({"gift_color"}) == "none"


def test_color_mode_gradient_supersedes_solid():
    assert effective_color_mode({"role_gradient", "role_color"}) == "gradient"
    assert effective_color_mode({"role_gradient"}) == "gradient"


def test_color_mode_holographic_tops_everything():
    assert effective_color_mode({"role_holographic"}) == "holographic"
    # Holographic beats gradient and solid when a member holds several.
    assert (
        effective_color_mode({"role_holographic", "role_gradient", "role_color"})
        == "holographic"
    )


@pytest.mark.parametrize(
    "perks",
    [
        pytest.param({"role_preset"}, id="alone"),
        pytest.param({"role_preset", "role_color"}, id="over-solid"),
        pytest.param({"role_preset", "role_gradient"}, id="with-custom-gradient"),
    ],
)
def test_color_mode_palette_color_is_a_gradient(perks):
    """A curated palette colour projects as a two-colour fade.

    ``role_preset`` and ``role_gradient`` differ only in where the pair came
    from — the palette or the member — so the projector treats them alike and
    needs no fourth mode.
    """
    assert effective_color_mode(perks) == "gradient"


def test_color_mode_holographic_still_tops_the_palette():
    assert (
        effective_color_mode({"role_holographic", "role_preset"}) == "holographic"
    )
