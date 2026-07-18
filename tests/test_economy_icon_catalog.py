"""Tests for the rentable icon catalog: the CRUD service layer plus the
per-icon billing it drives through the rental engine.

Covers catalog list/filter/order, the price range, the live-rental "in use"
guard, and the money-critical paths that make catalog icons per-icon priced:
the upfront charge at the icon's price, renewals re-reading the CURRENT icon
price (so an admin edit lands at the next anniversary), the flat-price fallback
for a bring-your-own icon and for a vanished catalog row, and an in-week icon
switch that re-prices only at the next renewal.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import WEEK_SECONDS
from bot_modules.services.economy_icon_catalog_service import (
    add_catalog_icon,
    catalog_price_range,
    delete_catalog_icon,
    get_catalog_icon,
    icon_in_use,
    list_catalog,
    set_catalog_icon_image,
    update_catalog_icon,
)
from bot_modules.services.economy_rentals_service import (
    bill_rental,
    get_live_role_icon_rental,
    rent_perk,
    set_rental_catalog_icon,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from migrations import apply_migrations_sync

GUILD = 900
USER = 42
T0 = 2_000_000.0
# Flat role_icon price is deliberately different from every catalog price so a
# test that bills the wrong one is unambiguous.
SETTINGS = EconSettings(enabled=True, price_role_icon=75)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    apply_migrations_sync(path)
    return path


def _add_icon(conn, *, name="Crown", price=100, enabled=True, sort=0):
    icon_id = add_catalog_icon(conn, GUILD, name=name, price=price, sort_order=sort)
    set_catalog_icon_image(conn, GUILD, icon_id, f"/icons/{icon_id}.png")
    if not enabled:
        update_catalog_icon(conn, GUILD, icon_id, enabled=False)
    return icon_id


def _fund(db, amount, user=USER):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, user, amount, "grant")


# ── catalog CRUD ────────────────────────────────────────────────────────


def test_add_list_and_enabled_filter(db):
    with open_db(db) as conn:
        a = _add_icon(conn, name="Alpha", sort=1)
        b = _add_icon(conn, name="Beta", sort=0, enabled=False)
        all_rows = list_catalog(conn, GUILD)
        enabled = list_catalog(conn, GUILD, enabled_only=True)
    # Ordered by sort_order then id: Beta (0) before Alpha (1).
    assert [r["id"] for r in all_rows] == [b, a]
    assert [r["id"] for r in enabled] == [a]


def test_update_and_get(db):
    with open_db(db) as conn:
        icon_id = _add_icon(conn, name="Crown", price=100)
        update_catalog_icon(conn, GUILD, icon_id, name="Gold Crown", price=250)
        row = get_catalog_icon(conn, GUILD, icon_id)
    assert row["name"] == "Gold Crown"
    assert row["price"] == 250


def test_price_range(db):
    with open_db(db) as conn:
        assert catalog_price_range(conn, GUILD) is None  # empty catalog
        _add_icon(conn, price=50)
        _add_icon(conn, price=300)
        _add_icon(conn, price=999, enabled=False)  # disabled excluded
        assert catalog_price_range(conn, GUILD) == (50, 300)


def test_in_use_guard_tracks_live_rentals(db):
    _fund(db, 1000)
    with open_db(db) as conn:
        rented = _add_icon(conn, name="Rented", price=100)
        spare = _add_icon(conn, name="Spare", price=100)
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_icon",
            catalog_icon_id=rented, now=T0,
        )
        assert icon_in_use(conn, GUILD, rented) is True
        assert icon_in_use(conn, GUILD, spare) is False


def test_delete_removes_row(db):
    with open_db(db) as conn:
        icon_id = _add_icon(conn)
        delete_catalog_icon(conn, GUILD, icon_id)
        assert get_catalog_icon(conn, GUILD, icon_id) is None


# ── per-icon billing ────────────────────────────────────────────────────


def test_rent_charges_icon_price_not_flat(db):
    _fund(db, 1000)
    with open_db(db) as conn:
        icon_id = _add_icon(conn, price=100)
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_icon",
            catalog_icon_id=icon_id, now=T0,
        )
        # 100 (icon), not 75 (flat price_role_icon).
        assert get_balance(conn, GUILD, USER) == 900


def test_flat_price_when_no_catalog_icon(db):
    _fund(db, 1000)
    with open_db(db) as conn:
        rent_perk(conn, SETTINGS, GUILD, USER, "role_icon", now=T0)
        assert get_balance(conn, GUILD, USER) == 925  # 1000 - 75 flat


def test_renewal_reads_current_icon_price(db):
    _fund(db, 1000)
    with open_db(db) as conn:
        icon_id = _add_icon(conn, price=100)
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_icon",
            catalog_icon_id=icon_id, now=T0,
        )
        # Admin raises the price mid-rental; renewal should bill the NEW price.
        update_catalog_icon(conn, GUILD, icon_id, price=150)
        rental = get_live_role_icon_rental(conn, GUILD, USER)
        result = bill_rental(conn, SETTINGS, rental, T0 + WEEK_SECONDS)
        assert result.action == "charge"
        assert result.charged == 150
        # 1000 - 100 (upfront) - 150 (renewal at the new price).
        assert get_balance(conn, GUILD, USER) == 750


def test_switch_icon_reprices_only_at_next_renewal(db):
    _fund(db, 2000)
    with open_db(db) as conn:
        cheap = _add_icon(conn, name="Cheap", price=100)
        dear = _add_icon(conn, name="Dear", price=400)
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_icon",
            catalog_icon_id=cheap, now=T0,
        )
        rental = get_live_role_icon_rental(conn, GUILD, USER)
        # Switching mid-week does NOT charge — only re-tags the rental.
        set_rental_catalog_icon(conn, GUILD, int(rental["id"]), dear)
        assert get_balance(conn, GUILD, USER) == 1900  # only the 100 upfront
        # The next renewal bills the newly chosen icon's price.
        rental = get_live_role_icon_rental(conn, GUILD, USER)
        result = bill_rental(conn, SETTINGS, rental, T0 + WEEK_SECONDS)
        assert result.charged == 400
        assert get_balance(conn, GUILD, USER) == 1500


def test_renewal_falls_back_to_flat_when_icon_deleted(db):
    _fund(db, 1000)
    with open_db(db) as conn:
        icon_id = _add_icon(conn, price=100)
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_icon",
            catalog_icon_id=icon_id, now=T0,
        )
        # Defensive path: the catalog row vanishes (routes block this while in
        # use, but billing must never crash) → renewal bills the flat price.
        delete_catalog_icon(conn, GUILD, icon_id)
        rental = get_live_role_icon_rental(conn, GUILD, USER)
        result = bill_rental(conn, SETTINGS, rental, T0 + WEEK_SECONDS)
        assert result.charged == 75  # flat price_role_icon fallback
