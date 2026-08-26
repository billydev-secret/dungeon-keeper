"""The per-perk shop switches, checked across the four layers that must agree.

A checkbox is only real if the same eight names appear in all of:

  * ``SHOP_TOGGLE_PERKS`` + the ``shop_<perk>_enabled`` fields (the truth);
  * the dashboard's PATCH whitelist (or saving 422s / silently drops it);
  * the Shop & Perks panel's own list (or the box never renders);
  * the perk vocabulary (or the switch names a perk that doesn't exist).

Each of those can drift on its own without any other test noticing — the
failure mode is a checkbox that renders, saves, and does nothing, which is the
exact thing CLAUDE.md forbids shipping. So they are asserted against each
other here rather than trusted to stay in step.

``encoding="utf-8"`` on every read: the gate's Windows runner defaults to
cp1252 and dies on the em-dashes in these files.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from bot_modules.economy.perks import (
    GIFTABLE_PERKS,
    PERK_LABELS,
    perk_on_sale,
    perks_on_sale,
)
from bot_modules.services.economy_service import SHOP_TOGGLE_PERKS, EconSettings

REPO = Path(__file__).resolve().parents[1]
PANEL = REPO / "src/web_server/static/js/panels/economy-sinks.js"
ROUTES = REPO / "src/web_server/routes/economy.py"
MIGRATION = REPO / "src/migrations/182_shop_perk_toggles.sql"


def _toggle_keys() -> set[str]:
    return {f"shop_{perk}_enabled" for perk in SHOP_TOGGLE_PERKS}


def test_every_toggle_perk_has_a_settings_field():
    names = {f.name for f in fields(EconSettings)}
    assert _toggle_keys() <= names


def test_every_toggle_field_is_a_bool():
    by_name = {f.name: f for f in fields(EconSettings)}
    for key in _toggle_keys():
        assert by_name[key].type in ("bool", bool), key


def test_no_stray_shop_enabled_field_escapes_the_tuple():
    # The reverse direction: a field added without its SHOP_TOGGLE_PERKS entry
    # would be a switch nothing consults.
    declared = {
        f.name for f in fields(EconSettings)
        if f.name.startswith("shop_") and f.name.endswith("_enabled")
    }
    assert declared == _toggle_keys()


def test_the_dashboard_can_actually_save_every_toggle():
    """The staged-config trap: a key with no writer is a silent no-op."""
    text = ROUTES.read_text(encoding="utf-8")
    for key in sorted(_toggle_keys()):
        assert re.search(rf"^\s+{key}:\s*bool \| None", text, re.M), key


def test_the_panel_renders_a_checkbox_for_every_toggle():
    text = PANEL.read_text(encoding="utf-8")
    listed = set(re.findall(r'\["(shop_\w+_enabled)"', text))
    assert listed == _toggle_keys()


def test_every_switchable_perk_is_a_real_perk_or_the_shield():
    # streak_shield is the one non-rental line on the card; everything else
    # must name a perk the shop actually knows how to sell.
    rentable = set(GIFTABLE_PERKS) | set(PERK_LABELS)
    assert set(SHOP_TOGGLE_PERKS) - {"streak_shield"} <= rentable


def test_every_giftable_perk_can_be_switched_off():
    """No perk may be unswitchable — that would be a hole in the card."""
    assert set(GIFTABLE_PERKS) <= set(SHOP_TOGGLE_PERKS)


def test_defaults_preserve_what_guilds_already_had():
    """Everything on, except the lease, which shipped dark at price 0.

    Guild 1358…618 in production runs entirely on these defaults — it has not
    one ``econ_price_*`` row — so a default of True here would arm a paywall on
    a server that never asked for one.
    """
    s = EconSettings()
    assert s.shop_voice_style_enabled is False
    for perk in SHOP_TOGGLE_PERKS:
        if perk != "voice_style":
            assert getattr(s, f"shop_{perk}_enabled") is True, perk


@pytest.mark.parametrize("perk", ["custom_item", "emoji", "gift_color", ""])
def test_perk_on_sale_passes_through_anything_unswitchable(perk):
    # Custom items own their own `enabled` column and emoji sponsorships are
    # priced dark; neither may be caught by these checkboxes.
    all_off = EconSettings(**{k: False for k in _toggle_keys()})
    assert perk_on_sale(all_off, perk) is True


def test_perks_on_sale_preserves_order():
    s = EconSettings(shop_role_name_enabled=False)
    assert perks_on_sale(s, GIFTABLE_PERKS) == tuple(
        p for p in GIFTABLE_PERKS
        if p != "role_name" and getattr(s, f"shop_{p}_enabled")
    )


# ── the backfill ───────────────────────────────────────────────────────


def _run_migration(conn):
    conn.executescript(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


@pytest.fixture
def config_db(tmp_path):
    import sqlite3

    conn = sqlite3.connect(tmp_path / "config.db")
    conn.execute(
        "CREATE TABLE config (guild_id INTEGER NOT NULL DEFAULT 0, "
        "key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (guild_id, key))"
    )
    return conn


def _rows(conn):
    return {
        (g, k): v
        for g, k, v in conn.execute(
            "SELECT guild_id, key, value FROM config WHERE key LIKE 'econ_shop_%'"
        )
    }


def test_backfill_switches_on_a_priced_voice_lease(config_db):
    """The row that matters in production: both live economies price the lease.

    Without it, the checkbox default (off) would disarm a live paywall on
    upgrade and hand the voice controls out free.
    """
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_price_voice_style", "40")
    )
    _run_migration(config_db)
    assert _rows(config_db)[(1, "econ_shop_voice_style_enabled")] == "1"


def test_backfill_leaves_an_unconfigured_guild_alone(config_db):
    """A guild with no econ config keeps the defaults, so the lease stays free."""
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_enabled", "1")
    )
    _run_migration(config_db)
    assert _rows(config_db) == {}


def test_backfill_carries_a_zeroed_shield_price_onto_the_checkbox(config_db):
    # Price 0 was that dial's off switch; the intent has to survive.
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_price_streak_shield", "0")
    )
    _run_migration(config_db)
    assert _rows(config_db)[(1, "econ_shop_streak_shield_enabled")] == "0"


def test_backfill_leaves_a_priced_shield_on_sale(config_db):
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_price_streak_shield", "30")
    )
    _run_migration(config_db)
    assert _rows(config_db) == {}  # default (on) already says the right thing


def test_backfill_never_writes_a_global_row(config_db):
    """Guild 1476…484 is a second live economy run by someone else.

    A guild_id 0 row would leak one server's shop decisions into another's.
    """
    for gid in (1, 2):
        config_db.execute(
            "INSERT INTO config VALUES (?, ?, ?)",
            (gid, "econ_price_voice_style", "900"),
        )
    _run_migration(config_db)
    assert {g for g, _ in _rows(config_db)} == {1, 2}


def test_backfill_is_idempotent(config_db):
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_price_voice_style", "40")
    )
    _run_migration(config_db)
    before = _rows(config_db)
    _run_migration(config_db)
    assert _rows(config_db) == before


def test_backfill_does_not_overwrite_an_explicit_choice(config_db):
    # A guild that has already unchecked the box keeps it unchecked.
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)", (1, "econ_price_voice_style", "40")
    )
    config_db.execute(
        "INSERT INTO config VALUES (?, ?, ?)",
        (1, "econ_shop_voice_style_enabled", "0"),
    )
    _run_migration(config_db)
    assert _rows(config_db)[(1, "econ_shop_voice_style_enabled")] == "0"


# ── the seam with custom shop items ────────────────────────────────────


def test_a_custom_item_rental_survives_every_perk_being_switched_off():
    """The two features share ``econ_rentals`` but not their switches.

    Custom shop items landed the day before these checkboxes and carry their
    own per-item ``enabled`` column. They ride the same rental table under perk
    ``custom_item``, so the guard in ``rent_perk`` and the ``perk_disabled``
    arm of ``classify`` both run against them — and both have to decline to
    act, or unchecking an unrelated role perk would quietly stop the guild's
    whole store from renewing.
    """
    import tempfile
    from pathlib import Path

    from bot_modules.core.db_utils import open_db
    from bot_modules.economy.rentals import WEEK_SECONDS
    from bot_modules.services.economy_rentals_service import bill_rental, rent_perk
    from bot_modules.services.economy_service import apply_credit
    from tests.db_template import migrated_db

    db = Path(tempfile.mkdtemp()) / "t.db"
    migrated_db(db)
    all_off = EconSettings(enabled=True, **{k: False for k in _toggle_keys()})

    with open_db(db) as conn:
        apply_credit(conn, 1, 2, 10_000, "grant")
        conn.execute(
            "INSERT INTO econ_shop_items"
            " (guild_id, name, price, kind, billing, enabled, created_at)"
            " VALUES (1, 'Headpats', 50, 'manual', 'weekly', 1, 0)"
        )
        item_id = conn.execute("SELECT id FROM econ_shop_items").fetchone()["id"]

        rental = rent_perk(
            conn, all_off, 1, 2, "custom_item", catalog_item_id=item_id, now=0.0
        )
        assert rental["state"] == "active"

        row = conn.execute(
            "SELECT * FROM econ_rentals WHERE id = ?", (rental["id"],)
        ).fetchone()
        result = bill_rental(conn, all_off, row, WEEK_SECONDS)

    # Renews and bills as normal — not discontinued.
    assert result.action == "charge"
    assert result.charged == 50
