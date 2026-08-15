"""Tests for the curated colour palette: the CRUD service plus its billing.

Covers what the palette adds over its icon-catalog sibling (migration 159):
the *rentable* gate — a colour whose swatch filename never parsed has no
gradient to project and must never be offered — the price-0-means-flat rule that
billing, the shop span and the picker all read through, the in-use guard that
stops a rented colour being deleted underneath its renter, and the fact that
``upsert`` (the swatch sync's writer) refreshes art without clobbering the
admin's price and enabled flag.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.rentals import WEEK_SECONDS
from bot_modules.services.economy_color_catalog_service import (
    catalog_price_range,
    color_in_use,
    color_ints,
    delete_catalog_color,
    get_catalog_color,
    get_catalog_color_by_key,
    list_catalog,
    palette_size,
    update_catalog_color,
    upsert_catalog_color,
    valid_hex,
)
from bot_modules.services.economy_rentals_service import (
    bill_rental,
    get_live_preset_rental,
    rent_perk,
    set_rental_catalog_color,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from tests.db_template import migrated_db

GUILD = 900
USER = 42
T0 = 2_000_000.0
# The flat palette price differs from every per-colour price below, so a test
# that bills the wrong one is unambiguous.
SETTINGS = EconSettings(enabled=True, price_role_preset=100)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _add(conn, *, key="dusk", name="dusk ember", hex1="F0A830", hex2="8842C8", sort=0):
    return upsert_catalog_color(
        conn, GUILD, key,
        name=name, hex1=hex1, hex2=hex2,
        image_path=f"/swatches/{key}.png", sort_order=sort,
    )


# ── the rentable gate ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "want"),
    [
        pytest.param("F0A830", True, id="upper"),
        pytest.param("f0a830", True, id="lower"),
        pytest.param("#F0A830", False, id="leading-hash"),
        pytest.param("F0A83", False, id="too-short"),
        pytest.param("F0A8300", False, id="too-long"),
        pytest.param("GGGGGG", False, id="not-hex"),
        pytest.param("", False, id="empty"),
    ],
)
def test_valid_hex(value, want):
    assert valid_hex(value) is want


def test_color_ints_converts_the_pair(db):
    with open_db(db) as conn:
        _add(conn)
        row = get_catalog_color_by_key(conn, GUILD, "dusk")
    assert color_ints(row) == (0xF0A830, 0x8842C8)


def test_unparsed_hexes_are_not_rentable(db):
    """A swatch whose filename didn't parse lands, but is never offered.

    Migration 159 keeps such a row on purpose — dropping it would make the
    palette silently short — so the guard has to live here instead.
    """
    with open_db(db) as conn:
        _add(conn, key="good")
        _add(conn, key="broken", hex1="", hex2="")

        assert color_ints(get_catalog_color_by_key(conn, GUILD, "broken")) is None
        assert [r["key"] for r in list_catalog(conn, GUILD)] == ["good", "broken"]
        rentable = list_catalog(conn, GUILD, rentable_only=True)
        assert [r["key"] for r in rentable] == ["good"]
        assert palette_size(conn, GUILD) == 1


def test_disabled_color_is_not_rentable(db):
    with open_db(db) as conn:
        color_id = _add(conn)
        update_catalog_color(conn, GUILD, color_id, enabled=False)
        assert list_catalog(conn, GUILD, rentable_only=True) == []
        assert palette_size(conn, GUILD) == 0
        # Still visible to the admin dashboard, which passes rentable_only=False.
        assert len(list_catalog(conn, GUILD)) == 1


def test_list_orders_by_sort_then_id(db):
    with open_db(db) as conn:
        _add(conn, key="c", sort=5)
        _add(conn, key="a", sort=1)
        _add(conn, key="b", sort=1)
        assert [r["key"] for r in list_catalog(conn, GUILD)] == ["a", "b", "c"]


# ── the sync's writer ──────────────────────────────────────────────────


def test_upsert_refreshes_art_but_keeps_admin_edits(db):
    """A re-sync owns name/hexes/art/order; price and enabled belong to the admin.

    Without this, deleting and re-adding a swatch — or any routine sync — would
    silently re-enable a colour the admin had retired, or reset its price to the
    flat default.
    """
    with open_db(db) as conn:
        color_id = _add(conn, key="dusk", name="dusk ember")
        update_catalog_color(conn, GUILD, color_id, price=250, enabled=False)

        again = _add(conn, key="dusk", name="Dusk Ember Two", hex1="AABBCC", sort=9)
        assert again == color_id  # same row, by key
        row = get_catalog_color(conn, GUILD, color_id)

    assert row["name"] == "Dusk Ember Two"
    assert row["hex1"] == "AABBCC"
    assert row["sort_order"] == 9
    assert int(row["price"]) == 250
    assert not int(row["enabled"])


def test_upsert_normalises_hex_case(db):
    with open_db(db) as conn:
        _add(conn, hex1="f0a830", hex2="8842c8")
        row = get_catalog_color_by_key(conn, GUILD, "dusk")
    assert (row["hex1"], row["hex2"]) == ("F0A830", "8842C8")


def test_get_by_key_is_guild_scoped(db):
    with open_db(db) as conn:
        _add(conn)
        assert get_catalog_color_by_key(conn, GUILD, "dusk") is not None
        assert get_catalog_color_by_key(conn, GUILD + 1, "dusk") is None


# ── price range (the shop row's span) ──────────────────────────────────


def test_price_range_none_without_a_palette(db):
    with open_db(db) as conn:
        assert catalog_price_range(conn, GUILD, 100) is None


def test_price_range_substitutes_the_flat_price_for_zero(db):
    """A palette left at the price-0 default must span the flat price, not 0."""
    with open_db(db) as conn:
        _add(conn, key="a")  # price 0 → flat
        _add(conn, key="b")
        assert catalog_price_range(conn, GUILD, 100) == (100, 100, 2)

        b = get_catalog_color_by_key(conn, GUILD, "b")
        update_catalog_color(conn, GUILD, int(b["id"]), price=250)
        assert catalog_price_range(conn, GUILD, 100) == (100, 250, 2)


def test_price_range_ignores_unrentable_colors(db):
    with open_db(db) as conn:
        _add(conn, key="good")
        _add(conn, key="broken", hex1="", hex2="")
        assert catalog_price_range(conn, GUILD, 100) == (100, 100, 1)


# ── the in-use guard ───────────────────────────────────────────────────


def _rent(conn, color_id, *, now=T0):
    apply_credit(conn, GUILD, USER, 10_000, "grant")
    return rent_perk(
        conn, SETTINGS, GUILD, USER, "role_preset",
        catalog_color_id=color_id, now=now,
    )


def test_color_in_use_tracks_live_rentals(db):
    with open_db(db) as conn:
        color_id = _add(conn)
        assert color_in_use(conn, GUILD, color_id) is False
        _rent(conn, color_id)
        assert color_in_use(conn, GUILD, color_id) is True


def test_delete_removes_an_unused_color(db):
    with open_db(db) as conn:
        color_id = _add(conn)
        delete_catalog_color(conn, GUILD, color_id)
        assert get_catalog_color(conn, GUILD, color_id) is None


# ── per-colour billing ─────────────────────────────────────────────────


def test_rent_charges_the_colors_own_price(db):
    with open_db(db) as conn:
        color_id = _add(conn)
        update_catalog_color(conn, GUILD, color_id, price=250)
        row = _rent(conn, color_id)
        assert int(row["price"]) == 250
        assert get_balance(conn, GUILD, USER) == 10_000 - 250


def test_rent_falls_back_to_the_flat_price_at_zero(db):
    """price 0 is "use the flat perk price", not "free"."""
    with open_db(db) as conn:
        color_id = _add(conn)
        row = _rent(conn, color_id)
        assert int(row["price"]) == 100
        assert get_balance(conn, GUILD, USER) == 10_000 - 100


def test_renewal_rereads_the_current_price(db):
    """An admin's price edit lands at the next anniversary, not mid-week."""
    with open_db(db) as conn:
        color_id = _add(conn)
        update_catalog_color(conn, GUILD, color_id, price=200)
        rental = _rent(conn, color_id)
        update_catalog_color(conn, GUILD, color_id, price=350)

        result = bill_rental(conn, SETTINGS, rental, T0 + WEEK_SECONDS)
        assert result.charged == 350
        assert result.previous_price == 200


def test_renewal_falls_back_when_the_color_row_vanishes(db):
    """A deleted colour must not wedge billing — it bills the flat price."""
    with open_db(db) as conn:
        color_id = _add(conn)
        update_catalog_color(conn, GUILD, color_id, price=250)
        rental = _rent(conn, color_id)
        delete_catalog_color(conn, GUILD, color_id)

        result = bill_rental(conn, SETTINGS, rental, T0 + WEEK_SECONDS)
        assert result.charged == 100


def test_switching_color_reprices_only_at_the_next_renewal(db):
    with open_db(db) as conn:
        cheap = _add(conn, key="cheap")
        dear = _add(conn, key="dear")
        update_catalog_color(conn, GUILD, cheap, price=120)
        update_catalog_color(conn, GUILD, dear, price=400)
        rental = _rent(conn, cheap)
        spent_after_rent = get_balance(conn, GUILD, USER)

        set_rental_catalog_color(conn, GUILD, int(rental["id"]), dear)
        # The switch itself is free — the week is already paid for.
        assert get_balance(conn, GUILD, USER) == spent_after_rent

        # Re-read the rental: billing prices from the row it is handed, so a
        # stale copy would bill the colour that was swapped out.
        live = get_live_preset_rental(conn, GUILD, USER)
        assert int(live["catalog_color_id"]) == dear
        result = bill_rental(conn, SETTINGS, live, T0 + WEEK_SECONDS)
        assert result.charged == 400


def test_live_preset_rental_is_beneficiary_matched(db):
    """A gifted palette colour resolves for the friend wearing it, not the payer."""
    friend = USER + 1
    with open_db(db) as conn:
        color_id = _add(conn)
        apply_credit(conn, GUILD, USER, 10_000, "grant")
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_preset",
            beneficiary_id=friend, catalog_color_id=color_id, now=T0,
        )
        assert get_live_preset_rental(conn, GUILD, friend) is not None
        assert get_live_preset_rental(conn, GUILD, USER) is None
