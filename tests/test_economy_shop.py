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


# ── the curated colour palette row ─────────────────────────────────────
#
# The palette is the one row whose very presence is conditional: a guild with no
# curated colours has no such product, and ``color_catalog=None`` is how the
# shop is told so.


def test_shop_hides_the_palette_row_without_a_palette(db):
    _enable(db)
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    with pytest.raises(AssertionError):
        _shop_row(embed, "Palette")


def test_shop_keeps_the_palette_row_for_a_renter_when_it_empties(db):
    """A palette can empty out under a live rental — its last colour disabled,
    or its swatch deleted. Hiding the row then would bill someone weekly for a
    perk with no row, no price and no ✅ anywhere in the shop, reachable only
    through the generic refund picker.
    """
    _enable(db, price_role_preset=100)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, owned={"role_preset"}
    )
    row = _shop_row(embed, "Palette")
    assert "**100**" in row
    assert "✅" in row


def test_shop_shows_the_palette_row_with_one(db):
    _enable(db, price_role_preset=100)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, color_catalog=(100, 100, 11)
    )
    row = _shop_row(embed, "Palette")
    assert "**100**" in row
    assert "11 to choose from" in row


def test_shop_palette_row_spans_per_colour_prices(db):
    """Individually priced colours read as a span, like the icon catalog."""
    _enable(db, price_role_preset=100)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, color_catalog=(100, 250, 11)
    )
    assert "**100–250**" in _shop_row(embed, "Palette")


def test_shop_palette_row_ignores_the_flat_price_in_its_span(db):
    """Unlike icons there is no bring-your-own entry, so the flat price is not
    a price the row offers — the catalog's own span is the whole truth (the
    catalog service has already substituted the flat price for 0-priced rows)."""
    _enable(db, price_role_preset=999)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, color_catalog=(100, 250, 11)
    )
    row = _shop_row(embed, "Palette")
    assert "**100–250**" in row
    assert "999" not in row


def test_shop_palette_undercuts_the_custom_gradient(db):
    """The product story: curated is the value pick, custom is the splurge.

    Both live in the Signature tier, which sorts by price, so the palette must
    render above the gradient.
    """
    _enable(db, price_role_preset=100, price_role_gradient=300)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=True, color_catalog=(100, 100, 11)
    )
    signature = next(f for f in embed.fields if f.name == "Signature")
    lines = [ln for ln in signature.value.splitlines() if ln.startswith("`")]
    labels = [ln.split("`")[1].split("  ")[0].strip() for ln in lines]
    assert labels.index("Palette") < labels.index("Gradient")


def test_shop_shows_balance_to_a_member_but_not_in_the_panel(db):
    """The wallet anchors the prices — but the channel panel is member-agnostic."""
    _enable(db)
    settings = _settings(db)
    mine = build_shop_embed(settings, set(), None, balance=1240)
    assert "1,240" in mine.description

    panel = build_shop_embed(settings, set(), None, panel=True)
    assert "1,240" not in panel.description
    assert "you have" not in panel.description


# ── conditional rows ──────────────────────────────────────────────────────


def test_shop_embed_shield_row_and_held_marker(db):
    _enable(db)
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    row = next(f for f in embed.fields if f.name == "One-shot")
    assert "Streak shield" in row.value
    assert "held" not in row.value
    held = build_shop_embed(_settings(db), set(), None, shields_held=1)
    assert "held" in next(f for f in held.fields if f.name == "One-shot").value


@pytest.mark.parametrize(
    ("overrides", "field", "token"),
    [
        # token None → the row must be absent entirely.
        # Visibility follows the Shop & Perks checkbox now, not the price: a
        # price of 0 means free, and "don't sell it" is its own switch.
        ({"shop_streak_shield_enabled": False}, "One-shot", None),
        ({"price_streak_shield": 0}, "One-shot", "0"),  # free, still on sale
        ({}, "Voice", None),  # the lease ships unsold
        ({"price_voice_style": 30}, "Voice", None),  # priced but not switched on
        ({"price_voice_style": 30, "shop_voice_style_enabled": True}, "Voice", "30"),
        ({}, "Weekly Raffle", None),
        ({"raffle_enabled": True}, "Weekly Raffle", "10"),  # ticket price
    ],
)
def test_shop_embed_row_visibility(db, overrides, field, token):
    _enable(db, **overrides)
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    if token is None:
        assert not any(f.name == field for f in embed.fields)
    else:
        row = next(f for f in embed.fields if f.name == field)
        assert token in row.value


# ── the per-perk shop switches (Shop & Perks: What's On Sale) ───────────


def _labels(embed) -> set[str]:
    """Every shop-table cell label in the embed, across all tiers."""
    found = set()
    for field in embed.fields:
        for line in field.value.splitlines():
            if line.startswith("`"):
                found.add(line[1:].split("`")[0].strip().split("  ")[0].strip())
    return found


def test_a_switched_off_perk_leaves_the_shop_table(db):
    _enable(db, shop_role_gradient_enabled=False)
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    labels = _labels(embed)
    assert "Gradient" not in labels
    assert "Color" in labels  # the rest of the shop is untouched


def test_a_switched_off_perk_does_not_pad_the_rows_that_remain(db):
    """A hidden row must leave the width calculation too.

    The palette row already worked this way; a switched-off perk has to match,
    or the widest hidden label would pad every visible row and the table would
    render with a column of unexplained whitespace.
    """
    _enable(db, price_role_name=35, price_role_color=50)
    with_holo = _shop_row(
        build_shop_embed(_settings(db), set(), None, panel=True), "Color"
    )
    _enable(db, shop_role_holographic_enabled=False)  # "Holo" is not the widest…
    _enable(db, shop_role_gradient_enabled=False)  # …but "Gradient" is
    without = _shop_row(
        build_shop_embed(_settings(db), set(), None, panel=True), "Color"
    )
    assert len(without) < len(with_holo)


def test_the_voice_tier_follows_its_checkbox_not_its_price(db):
    # Priced but unsold → absent. This is the pairing that used to be
    # impossible to express: price 0 meant "free", never "don't sell it".
    _enable(db, price_voice_style=40, shop_voice_style_enabled=False)
    assert "Voice" not in _labels(build_shop_embed(_settings(db), set(), None))

    # Sold at 0 → present, and free. Also newly expressible.
    _enable(db, price_voice_style=0, shop_voice_style_enabled=True)
    assert "Voice" in _labels(build_shop_embed(_settings(db), set(), None))


def test_the_streak_shield_field_follows_its_checkbox(db):
    _enable(db, price_streak_shield=30, shop_streak_shield_enabled=True)
    assert "One-shot" in [f.name for f in build_shop_embed(_settings(db), set(), None).fields]
    _enable(db, shop_streak_shield_enabled=False)
    assert "One-shot" not in [
        f.name for f in build_shop_embed(_settings(db), set(), None).fields
    ]


def test_a_member_mid_rental_keeps_their_row_after_the_perk_is_withdrawn(db):
    """They're still being billed until their anniversary, so they still see it.

    Hiding the row of a live rental is how you get "I'm paying for something
    that isn't in the shop and I can't find the cancel button".
    """
    _enable(db, shop_role_gradient_enabled=False)
    embed = build_shop_embed(
        _settings(db), set(), None, panel=False, owned={"role_gradient"}
    )
    row = _shop_row(embed, "Gradient")
    assert "✅" in row


def test_the_whole_shop_can_be_switched_off(db):
    """Billy's literal ask: every box unchecked, and the embed still renders.

    The perk table empties out entirely — the width calculation used to raise
    on an empty table — and the gifting note goes with it, since there is
    nothing left to gift.
    """
    from bot_modules.services.economy_service import SHOP_TOGGLE_PERKS

    _enable(db, **{f"shop_{p}_enabled": False for p in SHOP_TOGGLE_PERKS})
    embed = build_shop_embed(_settings(db), set(), None, panel=True)
    assert _labels(embed) == set()
    assert "For a Friend" not in [f.name for f in embed.fields]
    assert embed.title == "🛍️ Perk Shop"


def test_switching_everything_off_still_shows_the_server_store(db):
    """The custom items keep their own per-item switch, so they survive.

    Two independent controls, and this is the test that proves the perk
    checkboxes don't reach past their own eight lines.
    """
    from bot_modules.economy.shop_items import ItemView
    from bot_modules.services.economy_service import SHOP_TOGGLE_PERKS

    _enable(db, **{f"shop_{p}_enabled": False for p in SHOP_TOGGLE_PERKS})
    item = ItemView(item_id=1, name="Tuckshop Voucher", blurb="a treat", price=25)
    embed = build_shop_embed(_settings(db), set(), None, items=[item])
    assert "Server Store" in [f.name for f in embed.fields]


# ── the Server Store: leading the shop, and its own paged storefront ────────


def _items(n: int, **kw) -> list:
    from bot_modules.economy.shop_items import ItemView

    return [
        ItemView(item_id=i, name=f"Item {i:02d}", blurb="a thing", price=100 + i, **kw)
        for i in range(1, n + 1)
    ]


def test_the_server_store_leads_the_shop_embed(db):
    """Billy's ask: the server's own goods stop arriving fourth of five.

    The perk ladder is the same six rows in every guild; what the guild stocks
    itself is the part that changes and the part members come for.
    """
    _enable(db)
    embed = build_shop_embed(_settings(db), set(), None, items=_items(2))
    assert [f.name for f in embed.fields][0] == "Server Store"


def test_the_store_section_stays_a_preview(db):
    """It is a teaser inside the combined menu, not the store.

    Eight rows and a count of the rest — the storefront is where the whole
    thing is browsable, and duplicating it here would push the perk table off
    a phone.
    """
    _enable(db)
    embed = build_shop_embed(_settings(db), set(), None, items=_items(20))
    block = next(f for f in embed.fields if f.name == "Server Store").value
    assert "Item 08" in block
    assert "Item 09" not in block
    assert "…and **12** more." in block


@pytest.mark.parametrize(
    ("count", "pages"),
    [(0, 1), (1, 1), (10, 1), (11, 2), (20, 2), (21, 3)],
)
def test_store_page_count(count, pages):
    """One page minimum, so an empty store still renders as page 1 of 1."""
    from bot_modules.economy.shop import store_page_count

    assert store_page_count(_items(count)) == pages


def test_store_page_clamps_out_of_range():
    """An admin can delete items while a member sits on page 3.

    Clamping shows them the last page that exists; trusting the number would
    hand them an empty picker.
    """
    from bot_modules.economy.shop import store_page

    items = _items(12)
    assert [i.item_id for i in store_page(items, 0)] == list(range(1, 11))
    assert [i.item_id for i in store_page(items, 1)] == [11, 12]
    assert [i.item_id for i in store_page(items, 9)] == [11, 12]
    assert [i.item_id for i in store_page(items, -3)] == list(range(1, 11))


def test_store_embed_pages_a_stocked_store(db):
    """The whole point of the rework: twenty items are all reachable.

    The old picker cut at 25 and the old preview at 8, so a guild with a real
    store showed less than half of it to anyone who did not click through.
    """
    from bot_modules.economy.shop import build_store_embed

    _enable(db)
    settings = _settings(db)
    items = _items(20)

    first = build_store_embed(settings, items, None, page=0)
    assert first.title == "🎁 Server Store"
    assert "Item 01" in first.description and "Item 10" in first.description
    assert "Item 11" not in first.description
    assert first.footer.text is not None and first.footer.text.startswith("Page 1 of 2")

    second = build_store_embed(settings, items, None, page=1)
    assert "Item 11" in second.description and "Item 20" in second.description
    assert "Item 01" not in second.description
    assert second.footer.text is not None and second.footer.text.startswith(
        "Page 2 of 2"
    )


def test_store_embed_hides_the_page_counter_for_one_page(db):
    """A single-page store should not advertise navigation it doesn't have."""
    from bot_modules.economy.shop import build_store_embed

    _enable(db)
    embed = build_store_embed(_settings(db), _items(3), None)
    assert embed.footer.text == "Pick one below to buy it."


def test_store_embed_shows_the_wallet_and_marks_what_you_hold(db):
    """Same header contract as the perk shop: your balance travels with you."""
    from bot_modules.economy.shop import build_store_embed

    _enable(db)
    embed = build_store_embed(
        _settings(db), _items(3), None, owned_item_ids={2}, balance=1234
    )
    assert "1,234" in embed.description
    lines = [ln for ln in embed.description.split("\n") if "Item 02" in ln]
    assert lines and "✅" in lines[0]


def test_store_embed_survives_an_emptied_store(db):
    """`max()` over nothing raises, and the last item can be withdrawn."""
    from bot_modules.economy.shop import build_store_embed

    _enable(db)
    embed = build_store_embed(_settings(db), [], None)
    assert "Nothing on the shelves yet" in embed.description
