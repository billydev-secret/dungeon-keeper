"""Perk shop table — pricing spans, tier ordering and the two render modes.

These are pure embed-builder tests: they moved here with ``build_shop_embed``
when it left ``economy_cog.py``, and need no cog, interaction or Discord mock.
The shop's *flows* (renting, gifting, refunds, the buttons) stay in
``test_economy_cog.py``, where the wiring they exercise lives.
"""
from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.shop import build_shop_embed
from bot_modules.services.economy_service import (
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD_ID = 9001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _settings(db):
    with open_db(db) as conn:
        return load_econ_settings(conn, GUILD_ID)


def _shop_row(embed, label: str) -> str:
    """The shop-table line whose first code cell is ``label``."""
    for field in embed.fields:
        for line in field.value.splitlines():
            if line.startswith(f"`{label}") and "`" in line[1:]:
                return line
    raise AssertionError(f"no {label!r} row in {[f.name for f in embed.fields]}")


def test_shop_table_aligns_cells_and_tiers_by_price(db):
    """Rows are fixed-width code cells, grouped in tiers, cheapest first."""
    _enable(
        db,
        price_role_name=35,
        price_role_color=50,
        price_role_gradient=120,
        price_role_icon=400,
    )
    embed = build_shop_embed(_settings(db), set(), None, panel=True)

    tiers = {f.name: f.value for f in embed.fields}
    assert list(tiers) == ["Essentials", "Signature", "One-shot", "For a Friend"]

    # Every row's cells share one width across the whole embed, so the columns
    # line up across tier headings rather than restarting at each one.
    rows = [
        line
        for value in tiers.values()
        for line in value.splitlines()
        if line.startswith("`")
    ]
    # Five self-perk rows — the "For a friend" tier is prose since gifting
    # generalized to every perk (no single gift price to tabulate).
    assert len(rows) == 5
    # One `label  blurb` cell per row (quest-board shape), all the same
    # width so columns align across tier headings — and narrow enough that
    # the price doesn't wrap onto its own line on a phone-width embed.
    cells = {line.split("`")[1] for line in rows}
    assert len({len(c) for c in cells}) == 1
    assert all(len(c) <= 27 for c in cells)

    # Ascending price inside each tier, and the blurb is present.
    assert tiers["Essentials"].index("**35**") < tiers["Essentials"].index("**50**")
    assert tiers["Signature"].index("**120**") < tiers["Signature"].index("**400**")
    assert "nickname + role" in _shop_row(embed, "Name")


def test_shop_table_reorders_when_prices_are_reconfigured(db):
    """The ladder follows the guild's prices, not the hardcoded tier order."""
    _enable(db, price_role_name=90, price_role_color=10)
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    essentials = next(f.value for f in embed.fields if f.name == "Essentials")
    assert essentials.index("**10**") < essentials.index("**90**")


# The flat custom price (default 75) folds into a curated catalog's span: the
# picker's bring-your-own entry sells at it, so the row's floor is
# min(catalog floor, flat) and its ceiling max(catalog ceiling, flat).
@pytest.mark.parametrize(
    ("overrides", "icon_catalog", "expected", "absent"),
    [
        pytest.param(
            {}, (120, 400, 40), "**75–400**", None, id="flat-below-catalog",
        ),
        pytest.param(
            {"price_role_icon": 500}, (120, 400, 40), "**120–500**", None,
            id="catalog-below-flat",
        ),
        pytest.param(
            {"price_role_icon": 200}, (200, 200, 3), "**200**", "–",
            id="single-price-collapses",
        ),
    ],
)
def test_shop_icon_row_price_span(db, overrides, icon_catalog, expected, absent):
    _enable(db, **overrides)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, icon_catalog=icon_catalog
    )
    row = _shop_row(embed, "Icon")
    assert expected in row
    if absent is not None:
        assert absent not in row


def test_shop_icon_row_shows_catalog_size(db):
    """The row says how many curated icons there are, plus bring-your-own."""
    _enable(db)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, icon_catalog=(120, 400, 40)
    )
    assert "40 + your own" in _shop_row(embed, "Icon")


def test_shop_shows_balance_to_a_member_but_not_in_the_panel(db):
    """The wallet anchors the prices — but the channel panel is member-agnostic."""
    _enable(db)
    settings = _settings(db)
    mine = build_shop_embed(settings, set(), None, balance=1240)
    assert "1,240" in mine.description

    panel = build_shop_embed(settings, set(), None, panel=True)
    assert "1,240" not in panel.description
    assert "you have" not in panel.description
