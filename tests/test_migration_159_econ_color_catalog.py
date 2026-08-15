"""Migration 159: booster cosmetic roles become the rentable colour palette.

The interesting part is the data carry-over. ``booster_roles`` never stored the
gradient — only the swatch *filename* the role was generated from — so the
migration parses ``ColorName_HEX1_HEX2.ext`` in SQL, walking in from the end with
the rtrim/replace last-delimiter idiom (SQLite has no ``reverse()`` or regexp).
That parse is the thing most likely to be subtly wrong, and it ran once against
11 live rows, so it is pinned here against the real production filenames.

Also pinned: a filename that does *not* parse still lands (dropping it would make
the palette silently short), the legacy Discord role id is preserved for the
grandfathered wearers, and the widened ``econ_rentals.perk`` CHECK really does
accept ``role_preset`` while still rejecting nonsense.
"""

from __future__ import annotations

import sqlite3

import pytest

import migrations

GUILD = 1469491362444480666
OTHER_GUILD = 1476525656115515484

# The production swatch paths, verbatim (guild 1469…666, read read-only before
# the migration was written). Underscored label, two hex codes, one dot.
PROD_ROWS = [
    ("sunbeam", "sunbeam", 0,
     "/home/ben/discord_bots/dungeon-keeper/assets/sunbeam_FDF77D_F9FCE1.png", 0),
    ("dusk_ember", "dusk ember", 1489716782766887062,
     "/home/ben/discord_bots/dungeon-keeper/assets/dusk_ember_F0A830_8842C8.png", 3752713),
    ("rose_gold", "rose gold", 1489716789549203516,
     "/home/ben/discord_bots/dungeon-keeper/assets/rose_gold_FADCA8_D87850.png", 3800176),
    ("molten_core", "molten core", 1489716787179163738,
     "/home/ben/discord_bots/dungeon-keeper/assets/molten_core_FFD86A_C84232.png", 4420064),
    ("midnight_poppy", "midnight poppy", 1489716786269257748,
     "/home/ben/discord_bots/dungeon-keeper/assets/midnight_poppy_F5C842_4A8AF5.png", 4492175),
    ("meadow_sunrise", "meadow sunrise", 1489716785304305774,
     "/home/ben/discord_bots/dungeon-keeper/assets/meadow_sunrise_F5C842_E8566A.png", 4493517),
    ("golden_hour", "golden hour", 1489716784390082761,
     "/home/ben/discord_bots/dungeon-keeper/assets/golden_hour_FFE17A_D47A18.png", 4640312),
    ("firefly", "firefly", 1489716783559479439,
     "/home/ben/discord_bots/dungeon-keeper/assets/firefly_F5D042_3DB87A.png", 4751497),
    ("neon_meadow", "neon meadow", 1489716788248707122,
     "/home/ben/discord_bots/dungeon-keeper/assets/neon_meadow_B8E842_2AB8D4.png", 7731898),
    ("velvet_dusk", "velvet dusk", 1489716790736195745,
     "/home/ben/discord_bots/dungeon-keeper/assets/velvet_dusk_E88AAE_7B68B8.png", 33702542),
    ("wildflower", "wildflower", 1489716792082432083,
     "/home/ben/discord_bots/dungeon-keeper/assets/wildflower_F06292_9C5FD4.png", 33972712),
]

EXPECTED_PAIRS = {
    "sunbeam": ("FDF77D", "F9FCE1"),
    "dusk_ember": ("F0A830", "8842C8"),
    "rose_gold": ("FADCA8", "D87850"),
    "molten_core": ("FFD86A", "C84232"),
    "midnight_poppy": ("F5C842", "4A8AF5"),
    "meadow_sunrise": ("F5C842", "E8566A"),
    "golden_hour": ("FFE17A", "D47A18"),
    "firefly": ("F5D042", "3DB87A"),
    "neon_meadow": ("B8E842", "2AB8D4"),
    "velvet_dusk": ("E88AAE", "7B68B8"),
    "wildflower": ("F06292", "9C5FD4"),
}


def _apply_before_159(db_path, monkeypatch) -> None:
    real = migrations._migration_files()
    monkeypatch.setattr(
        migrations,
        "_migration_files",
        lambda: [f for f in real if f.name < "159"],
    )
    migrations.apply_migrations_sync(db_path)
    monkeypatch.setattr(migrations, "_migration_files", lambda: real)


def _seed(db_path, rows=PROD_ROWS, *, guild=GUILD) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO booster_roles "
            "(guild_id, role_key, label, role_id, image_path, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(guild, key, label, role_id, path, sort) for key, label, role_id, path, sort in rows],
        )
        conn.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    _apply_before_159(path, monkeypatch)
    return path


def _catalog(db_path, guild=GUILD) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            r["key"]: r
            for r in conn.execute(
                "SELECT * FROM econ_color_catalog WHERE guild_id = ?", (guild,)
            )
        }


# ── the filename parse, against real production data ───────────────────


def test_migrates_every_production_row(db, monkeypatch):
    _seed(db)
    migrations.apply_migrations_sync(db)

    rows = _catalog(db)
    assert set(rows) == set(EXPECTED_PAIRS)
    for key, (hex1, hex2) in EXPECTED_PAIRS.items():
        assert (rows[key]["hex1"], rows[key]["hex2"]) == (hex1, hex2), key


def test_carries_label_order_and_legacy_role(db, monkeypatch):
    _seed(db)
    migrations.apply_migrations_sync(db)

    rows = _catalog(db)
    dusk = rows["dusk_ember"]
    assert dusk["name"] == "dusk ember"
    assert dusk["sort_order"] == 3752713
    assert dusk["image_path"].endswith("dusk_ember_F0A830_8842C8.png")
    # The Discord role the grandfathered wearers keep — recorded, never acted on.
    assert int(dusk["legacy_role_id"]) == 1489716782766887062
    # sunbeam's role was never created in Discord; 0 carries across as 0.
    assert int(rows["sunbeam"]["legacy_role_id"]) == 0


def test_migrated_colors_start_enabled_at_the_flat_price(db, monkeypatch):
    """Price 0 means "bill the flat price_role_preset" — one price for the set."""
    _seed(db)
    migrations.apply_migrations_sync(db)

    for row in _catalog(db).values():
        assert int(row["price"]) == 0
        assert int(row["enabled"]) == 1


@pytest.mark.parametrize(
    ("filename", "want"),
    [
        pytest.param("Ruby_ff0000_8b0000.png", ("FF0000", "8B0000"), id="upcased"),
        pytest.param("a_b_c_112233_445566.webp", ("112233", "445566"), id="many-parts"),
        pytest.param("plain.png", ("", ""), id="no-hexes"),
        # A half-parse: the last token is valid hex, the one before it is not.
        # Harmless either way — rentability needs BOTH hexes, so the colour is
        # never offered, exactly as if neither had parsed.
        pytest.param("Only_FF0000.png", ("", "FF0000"), id="one-hex"),
        pytest.param("Bad_GGGGGG_445566.png", ("", "445566"), id="non-hex-first"),
        pytest.param("Bad_112233_ZZZZZZ.png", ("112233", ""), id="non-hex-last"),
    ],
)
def test_parse_edge_cases(db, monkeypatch, filename, want):
    _seed(db, [("k", "K", 0, f"/swatches/{filename}", 0)])
    migrations.apply_migrations_sync(db)

    row = _catalog(db)["k"]
    assert (row["hex1"], row["hex2"]) == want


def test_unparseable_row_still_lands(db, monkeypatch):
    """A palette that silently lost a colour would be worse than one flagged.

    The service treats a hex-less row as unrentable and the dashboard asks for a
    re-sync, so keeping the row is the recoverable outcome.
    """
    _seed(db, [
        ("good", "Good", 11, "/swatches/Good_FF0000_00FF00.png", 1),
        ("bad", "Bad", 22, "/swatches/whoops.png", 2),
    ])
    migrations.apply_migrations_sync(db)

    rows = _catalog(db)
    assert set(rows) == {"good", "bad"}
    assert (rows["bad"]["hex1"], rows["bad"]["hex2"]) == ("", "")
    assert int(rows["bad"]["legacy_role_id"]) == 22


def test_migrates_each_guild_separately(db, monkeypatch):
    _seed(db, [("a", "A", 1, "/s/A_FF0000_00FF00.png", 0)], guild=GUILD)
    _seed(db, [("b", "B", 2, "/s/B_0000FF_FFFF00.png", 0)], guild=OTHER_GUILD)
    migrations.apply_migrations_sync(db)

    assert set(_catalog(db, GUILD)) == {"a"}
    assert set(_catalog(db, OTHER_GUILD)) == {"b"}


def test_empty_booster_table_migrates_cleanly(db, monkeypatch):
    """The second live guild has no booster roles at all."""
    migrations.apply_migrations_sync(db)
    assert _catalog(db) == {}


# ── the tables that go, and the ones that arrive ───────────────────────


def test_booster_tables_are_dropped(db, monkeypatch):
    _seed(db)
    migrations.apply_migrations_sync(db)

    with sqlite3.connect(db) as conn:
        names = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "booster_roles" not in names
    assert "booster_panel_messages" not in names
    assert "econ_color_catalog" in names
    assert "econ_color_panel_messages" in names


def test_panel_message_refs_carry_across(db, monkeypatch):
    """The posted showroom keeps working — only the table holding its ids moves."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO booster_panel_messages (guild_id, channel_id, message_id) "
            "VALUES (?, ?, ?)",
            (GUILD, 777, 888),
        )
        conn.commit()
    migrations.apply_migrations_sync(db)

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT channel_id, message_id FROM econ_color_panel_messages "
            "WHERE guild_id = ?",
            (GUILD,),
        ).fetchall()
    assert rows == [(777, 888)]


def test_key_is_unique_per_guild(db, monkeypatch):
    """Keys are how pre-migration panel buttons resolve, so they must be unique."""
    _seed(db, [("a", "A", 1, "/s/A_FF0000_00FF00.png", 0)])
    migrations.apply_migrations_sync(db)

    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO econ_color_catalog "
            "(guild_id, key, name, hex1, hex2, price, enabled, sort_order, "
            " legacy_role_id, created_at) "
            "VALUES (?, 'a', 'Dupe', 'FF0000', '00FF00', 0, 1, 0, 0, 0.0)",
            (GUILD,),
        )


# ── the widened rentals CHECK ──────────────────────────────────────────


def _insert_rental(conn, perk: str) -> None:
    conn.execute(
        "INSERT INTO econ_rentals "
        "(guild_id, user_id, perk, state, price, started_at, next_bill_at, "
        " beneficiary_id, created_at) "
        "VALUES (?, 1, ?, 'active', 10, 0.0, 0.0, 1, 0.0)",
        (GUILD, perk),
    )


def test_rentals_check_accepts_role_preset(db, monkeypatch):
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn:
        _insert_rental(conn, "role_preset")
        assert conn.execute(
            "SELECT COUNT(*) FROM econ_rentals WHERE perk = 'role_preset'"
        ).fetchone()[0] == 1


def test_rentals_check_still_rejects_an_unknown_perk(db, monkeypatch):
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        _insert_rental(conn, "role_sparkles")


def test_existing_rentals_survive_the_table_rebuild(db, monkeypatch):
    """The CHECK widening rebuilds econ_rentals — every row must come across.

    Production holds 60-odd rentals across three guilds; losing them would drop
    live entitlements and stop billing.
    """
    with sqlite3.connect(db) as conn:
        _insert_rental(conn, "role_gradient")
        conn.execute(
            "UPDATE econ_rentals SET catalog_icon_id = 42, meta = '{\"x\":1}'"
        )
        conn.commit()

    migrations.apply_migrations_sync(db)

    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM econ_rentals").fetchall()
    assert len(rows) == 1
    assert rows[0]["perk"] == "role_gradient"
    assert rows[0]["catalog_icon_id"] == 42
    assert rows[0]["meta"] == '{"x":1}'
    assert rows[0]["catalog_color_id"] is None


def test_live_rental_index_survives_the_rebuild(db, monkeypatch):
    """The partial unique index is what stops a double rental — it must return."""
    migrations.apply_migrations_sync(db)
    with sqlite3.connect(db) as conn:
        _insert_rental(conn, "role_preset")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_rental(conn, "role_preset")
