"""The colour palette's swatch sync and filename contract.

The sync is where the grandfathering promise is actually kept. Before migration
159 this reconcile *deleted a Discord role* whose swatch file had gone, and 15
members wear one of those roles permanently and for free. It now touches no
Discord roles at all, and a vanished swatch retires its colour by *disabling* it
whenever somebody is renting it — deleting outright only when nobody holds it.

``sync_palette`` takes a guild id rather than a ``discord.Guild`` precisely so
this is testable without Discord: there is nothing left in it that needs one.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services.color_palette import (
    _hex_sort_key,
    _parse_swatch_filename,
    count_valid_swatches,
    resolve_swatch_directory,
    swatch_file_info,
    sync_palette,
)
from bot_modules.services.economy_color_catalog_service import (
    get_catalog_color_by_key,
    list_catalog,
    update_catalog_color,
)
from bot_modules.services.economy_rentals_service import rent_perk
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD = 900
USER = 42
T0 = 2_000_000.0
SETTINGS = EconSettings(enabled=True, price_role_preset=100)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _swatch_dir(db, guild_id: int = GUILD):
    """The managed folder sync reads for this guild."""
    directory = db.parent / "swatches" / str(guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _write(directory, name: str) -> None:
    (directory / name).write_bytes(b"\x89PNG fake")


# ── the filename contract ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "want"),
    [
        pytest.param(
            "dusk_ember_F0A830_8842C8.png", ("dusk ember", "F0A830", "8842C8"),
            id="two-word-label",
        ),
        pytest.param(
            "firefly_F5D042_3DB87A.png", ("firefly", "F5D042", "3DB87A"),
            id="one-word-label",
        ),
        pytest.param(
            "Ruby_ff0000_8b0000.webp", ("Ruby", "ff0000", "8b0000"), id="lowercase-hex",
        ),
        pytest.param("no_hex_here.png", None, id="not-hex"),
        pytest.param("only_F0A830.png", None, id="one-hex"),
        # A colour needs a name, so hexes alone are not a swatch.
        pytest.param("F0A830_8842C8.png", None, id="no-label"),
        pytest.param("plain.png", None, id="no-underscores"),
    ],
)
def test_parse_swatch_filename(filename, want):
    assert _parse_swatch_filename(filename) == want


def test_hex_sort_key_orders_by_hue():
    """Sort order is the gradient's hue, so the showroom reads as a colour wheel."""
    red = _hex_sort_key("FF0000", "FF0000")
    green = _hex_sort_key("00FF00", "00FF00")
    blue = _hex_sort_key("0000FF", "0000FF")
    assert red < green < blue


def test_swatch_file_info_flags_invalid_names(db):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "oops.png")
    _write(directory, "notes.txt")  # not an image — omitted entirely

    info = {f["name"]: f for f in swatch_file_info(directory)}
    assert set(info) == {"dusk_ember_F0A830_8842C8.png", "oops.png"}
    assert info["dusk_ember_F0A830_8842C8.png"]["valid"] is True
    assert info["dusk_ember_F0A830_8842C8.png"]["hex1"] == "F0A830"
    assert info["oops.png"]["valid"] is False
    assert count_valid_swatches(str(directory)) == 1


def test_resolve_prefers_managed_then_falls_back(db, tmp_path):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with open_db(db) as conn:
        set_config_value(conn, "booster_swatch_dir", str(legacy), 0)

    managed = _swatch_dir(db)
    # Empty managed folder → the configured legacy path wins.
    assert resolve_swatch_directory(db, GUILD) == str(legacy)
    # One validly named file is enough to take over.
    _write(managed, "dusk_ember_F0A830_8842C8.png")
    assert resolve_swatch_directory(db, GUILD) == str(managed)


# ── sync: adding and refreshing ────────────────────────────────────────


def test_sync_builds_the_palette_from_filenames(db):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "firefly_F5D042_3DB87A.png")

    added, disabled, removed = sync_palette(db, GUILD)

    assert sorted(added) == ["dusk ember", "firefly"]
    assert (disabled, removed) == ([], [])
    with open_db(db) as conn:
        rows = {r["key"]: r for r in list_catalog(conn, GUILD)}
    assert set(rows) == {"dusk_ember", "firefly"}
    assert rows["dusk_ember"]["hex1"] == "F0A830"
    assert rows["dusk_ember"]["hex2"] == "8842C8"
    assert rows["dusk_ember"]["image_path"].endswith("dusk_ember_F0A830_8842C8.png")
    # Nothing is granted a Discord role any more.
    assert int(rows["dusk_ember"]["legacy_role_id"]) == 0


def test_sync_skips_badly_named_files(db):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "oops.png")

    added, _disabled, _removed = sync_palette(db, GUILD)
    assert added == ["dusk ember"]


def test_sync_is_idempotent(db):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    sync_palette(db, GUILD)

    added, disabled, removed = sync_palette(db, GUILD)
    assert (added, disabled, removed) == ([], [], [])


def test_sync_refuses_an_empty_folder(db):
    """The guard that stops one bad deploy retiring the whole palette."""
    _swatch_dir(db)
    with pytest.raises(ValueError, match="No valid swatch files"):
        sync_palette(db, GUILD)


def test_sync_refuses_when_every_file_is_invalid(db):
    directory = _swatch_dir(db)
    _write(directory, "oops.png")
    with pytest.raises(ValueError, match="No valid swatch files"):
        sync_palette(db, GUILD)


# ── sync: retiring a colour whose swatch is gone ───────────────────────


def _rent(db, color_id):
    with open_db(db) as conn:
        apply_credit(conn, GUILD, USER, 10_000, "grant")
        rent_perk(
            conn, SETTINGS, GUILD, USER, "role_preset",
            catalog_color_id=color_id, now=T0,
        )


def test_sync_deletes_an_unheld_color_whose_swatch_is_gone(db):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "firefly_F5D042_3DB87A.png")
    sync_palette(db, GUILD)

    (directory / "firefly_F5D042_3DB87A.png").unlink()
    _added, disabled, removed = sync_palette(db, GUILD)

    assert removed == ["firefly"]
    assert disabled == []
    with open_db(db) as conn:
        assert get_catalog_color_by_key(conn, GUILD, "firefly") is None


def test_sync_disables_rather_than_deletes_a_rented_color(db):
    """A renter keeps the colour they are paying for.

    Deleting the row would take the colour off a live rental mid-week and leave
    billing pointing at nothing. Disabling stops it being offered to anyone new
    while the renter finishes.
    """
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "firefly_F5D042_3DB87A.png")
    sync_palette(db, GUILD)
    with open_db(db) as conn:
        firefly = get_catalog_color_by_key(conn, GUILD, "firefly")
    _rent(db, int(firefly["id"]))

    (directory / "firefly_F5D042_3DB87A.png").unlink()
    _added, disabled, removed = sync_palette(db, GUILD)

    assert disabled == ["firefly"]
    assert removed == []
    with open_db(db) as conn:
        row = get_catalog_color_by_key(conn, GUILD, "firefly")
    assert row is not None
    assert not int(row["enabled"])


def test_sync_leaves_the_legacy_role_alone(db):
    """The grandfathering guarantee, asserted where it could regress.

    ``legacy_role_id`` is the Discord role a booster was given. The old sync
    deleted that role when its swatch vanished; nothing in the palette may touch
    it now, so a retired colour keeps the id on record untouched.
    """
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    _write(directory, "firefly_F5D042_3DB87A.png")
    sync_palette(db, GUILD)
    with open_db(db) as conn:
        firefly = get_catalog_color_by_key(conn, GUILD, "firefly")
        conn.execute(
            "UPDATE econ_color_catalog SET legacy_role_id = ? WHERE id = ?",
            (555_000_111, int(firefly["id"])),
        )
    _rent(db, int(firefly["id"]))

    (directory / "firefly_F5D042_3DB87A.png").unlink()
    sync_palette(db, GUILD)

    with open_db(db) as conn:
        row = get_catalog_color_by_key(conn, GUILD, "firefly")
    assert int(row["legacy_role_id"]) == 555_000_111


def test_sync_does_not_re_enable_an_admin_disabled_color(db):
    """A colour the admin retired stays retired across a routine re-sync."""
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    sync_palette(db, GUILD)
    with open_db(db) as conn:
        row = get_catalog_color_by_key(conn, GUILD, "dusk_ember")
        update_catalog_color(conn, GUILD, int(row["id"]), enabled=False)

    sync_palette(db, GUILD)

    with open_db(db) as conn:
        row = get_catalog_color_by_key(conn, GUILD, "dusk_ember")
    assert not int(row["enabled"])


def test_sync_is_guild_scoped(db):
    """One guild's swatches never touch another's palette."""
    _write(_swatch_dir(db, GUILD), "dusk_ember_F0A830_8842C8.png")
    _write(_swatch_dir(db, GUILD + 1), "firefly_F5D042_3DB87A.png")

    sync_palette(db, GUILD)
    sync_palette(db, GUILD + 1)

    with open_db(db) as conn:
        assert [r["key"] for r in list_catalog(conn, GUILD)] == ["dusk_ember"]
        assert [r["key"] for r in list_catalog(conn, GUILD + 1)] == ["firefly"]


# ── wearing a colour: the gate that replaced "are you boosting?" ───────
#
# Until migration 159 the swatch button asked `member.premium_since is None`.
# It now asks whether the member is entitled to `role_preset`, and it never
# charges — buying happens in the shop, where the price is on screen. These are
# the assertions that make the new gate real.

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from bot_modules.services.color_palette import wear_palette_color  # noqa: E402
from bot_modules.services.economy_rentals_service import (  # noqa: E402
    get_live_preset_rental,
    get_personal_role,
)


def _bot(db, *, is_mod=False):
    ctx = SimpleNamespace(
        db_path=db,
        open_db=lambda: open_db(db),
        member_is_mod=lambda _m: is_mod,
    )
    return SimpleNamespace(ctx=ctx)


def _interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _wear(db, key, *, is_mod=False, features=("ENHANCED_ROLE_COLORS",), monkeypatch=None):
    bot = _bot(db, is_mod=is_mod)
    guild = SimpleNamespace(id=GUILD, features=features)
    member = SimpleNamespace(id=USER)
    interaction = _interaction()

    async def _fake_apply(*a, **kw):
        return True

    async def _fake_gate(_bot, _guild_id, _perk):
        return "ENHANCED_ROLE_COLORS" in features

    monkeypatch.setattr(
        "bot_modules.economy.perk_actions.apply_role_perks", _fake_apply
    )
    monkeypatch.setattr(
        "bot_modules.economy.perk_actions.feature_gate_ok", _fake_gate
    )
    asyncio.run(
        wear_palette_color(bot, interaction, guild, member, key=key)  # type: ignore[arg-type]
    )
    return interaction


def _seed_one_color(db, *, key="dusk_ember"):
    directory = _swatch_dir(db)
    _write(directory, "dusk_ember_F0A830_8842C8.png")
    sync_palette(db, GUILD)
    with open_db(db) as conn:
        return int(get_catalog_color_by_key(conn, GUILD, key)["id"])


def test_wearing_refuses_a_member_without_the_perk(db, monkeypatch):
    """Boosting is no longer the gate — renting is, and the refusal says so."""
    color_id = _seed_one_color(db)
    with open_db(db) as conn:
        update_catalog_color(conn, GUILD, color_id, price=250)
        conn.commit()

    interaction = _wear(db, "dusk_ember", monkeypatch=monkeypatch)

    interaction.response.send_message.assert_awaited_once()
    said = interaction.response.send_message.await_args.args[0]
    assert "/bank shop" in said
    # The refusal quotes what THIS colour costs, so an individually priced
    # colour is not misreported at the flat price.
    assert "250" in said
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD, USER) is None


def test_wearing_projects_the_gradient_for_a_renter(db, monkeypatch):
    color_id = _seed_one_color(db)
    _rent(db, color_id)

    interaction = _wear(db, "dusk_ember", monkeypatch=monkeypatch)

    interaction.followup.send.assert_awaited_once()
    assert "dusk ember" in interaction.followup.send.await_args.args[0]
    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD, USER)
    assert (int(row["color"]), int(row["color2"])) == (0xF0A830, 0x8842C8)


def test_wearing_retags_the_rental_so_billing_follows(db, monkeypatch):
    """Switching colour must move the rental's tag, or renewals bill the old one."""
    first = _seed_one_color(db)
    _write(_swatch_dir(db), "firefly_F5D042_3DB87A.png")
    sync_palette(db, GUILD)
    _rent(db, first)

    _wear(db, "firefly", monkeypatch=monkeypatch)

    with open_db(db) as conn:
        live = get_live_preset_rental(conn, GUILD, USER)
        firefly = get_catalog_color_by_key(conn, GUILD, "firefly")
    assert int(live["catalog_color_id"]) == int(firefly["id"])


def test_wearing_never_charges(db, monkeypatch):
    """A public button that debits a wallet on a press would be a trap.

    The showroom only ever *applies* a colour the member is already entitled to;
    every purchase goes through the shop, where the price is on screen.
    """
    color_id = _seed_one_color(db)
    _rent(db, color_id)
    with open_db(db) as conn:
        before = conn.execute(
            "SELECT balance FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()["balance"]

    _wear(db, "dusk_ember", monkeypatch=monkeypatch)

    with open_db(db) as conn:
        after = conn.execute(
            "SELECT balance FROM econ_wallets WHERE guild_id = ? AND user_id = ?",
            (GUILD, USER),
        ).fetchone()["balance"]
    assert after == before


def test_a_comped_mod_wears_without_a_rental(db, monkeypatch):
    """The staff comp covers role_preset, so it must cover the palette too."""
    _seed_one_color(db)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True, "mod_perk_comp": True})
        conn.commit()

    _wear(db, "dusk_ember", is_mod=True, monkeypatch=monkeypatch)

    with open_db(db) as conn:
        row = get_personal_role(conn, GUILD, USER)
        # A comp is never a purchase — no rental row is opened.
        assert get_live_preset_rental(conn, GUILD, USER) is None
    assert (int(row["color"]), int(row["color2"])) == (0xF0A830, 0x8842C8)


def test_wearing_refuses_an_unknown_key(db, monkeypatch):
    """A button on a panel posted before its colour was retired."""
    color_id = _seed_one_color(db)
    _rent(db, color_id)

    interaction = _wear(db, "no_such_color", monkeypatch=monkeypatch)

    said = interaction.response.send_message.await_args.args[0]
    assert "isn't available" in said


def test_wearing_refuses_a_disabled_color(db, monkeypatch):
    color_id = _seed_one_color(db)
    _rent(db, color_id)
    with open_db(db) as conn:
        update_catalog_color(conn, GUILD, color_id, enabled=False)
        conn.commit()

    interaction = _wear(db, "dusk_ember", monkeypatch=monkeypatch)

    assert "isn't available" in interaction.response.send_message.await_args.args[0]


def test_wearing_refuses_without_the_guild_feature(db, monkeypatch):
    """A palette colour is a gradient; without ENHANCED_ROLE_COLORS it can't render."""
    color_id = _seed_one_color(db)
    _rent(db, color_id)

    interaction = _wear(db, "dusk_ember", features=(), monkeypatch=monkeypatch)

    said = interaction.response.send_message.await_args.args[0]
    assert "gradient roles" in said
    with open_db(db) as conn:
        assert get_personal_role(conn, GUILD, USER) is None
