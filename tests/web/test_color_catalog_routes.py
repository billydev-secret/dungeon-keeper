"""Admin routes for the curated colour palette (``/api/economy/color-catalog``).

These moved here from the config router with the feature (migration 159, todo
#76) — the palette is a shop catalogue now, so its admin lives beside the icon
catalogue's under Economy → Sinks.

The showroom-permission tests came across with it and are the ones worth having:
the poster bulk-deletes the existing panel *before* its unguarded sends, so
reaching it without Attach Files leaves the guild with no showroom at all and a
repost that fails identically. A 400 before the service runs is the whole point.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_color_catalog_service import (
    get_catalog_color_by_key,
    upsert_catalog_color,
)
from bot_modules.services.economy_rentals_service import rent_perk
from bot_modules.services.economy_service import EconSettings, apply_credit

SETTINGS = EconSettings(enabled=True, price_role_preset=100)


def _add_color(fake_ctx, *, key="dusk", name="dusk ember", hex1="F0A830", hex2="8842C8"):
    with open_db(fake_ctx.db_path) as conn:
        color_id = upsert_catalog_color(
            conn, fake_ctx.guild_id, key,
            name=name, hex1=hex1, hex2=hex2,
            image_path=f"/swatches/{key}.png", sort_order=0,
        )
        conn.commit()
    return color_id


# ── listing ────────────────────────────────────────────────────────────


def test_list_is_empty_without_a_palette(authed_client):
    resp = authed_client.get("/api/economy/color-catalog")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_exposes_gradient_usage_and_sync_state(authed_client, fake_ctx):
    _add_color(fake_ctx)
    _add_color(fake_ctx, key="broken", name="broken", hex1="", hex2="")

    rows = {r["key"]: r for r in authed_client.get("/api/economy/color-catalog").json()}
    assert rows["dusk"]["hex1"] == "F0A830"
    assert rows["dusk"]["rentable"] is True
    assert rows["dusk"]["in_use"] is False
    assert rows["dusk"]["enabled"] is True
    # A row whose filename never parsed is flagged for a re-sync, not hidden.
    assert rows["broken"]["rentable"] is False


def test_list_returns_snowflakes_as_strings(authed_client, fake_ctx):
    """The legacy role id is a snowflake — a bare number loses precision in JS."""
    color_id = _add_color(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "UPDATE econ_color_catalog SET legacy_role_id = ? WHERE id = ?",
            (1489716782766887062, color_id),
        )
        conn.commit()

    row = authed_client.get("/api/economy/color-catalog").json()[0]
    assert row["legacy_role_id"] == "1489716782766887062"


# ── patch ──────────────────────────────────────────────────────────────


def test_patch_renames_reprices_and_disables(authed_client, fake_ctx):
    color_id = _add_color(fake_ctx)
    resp = authed_client.patch(
        f"/api/economy/color-catalog/{color_id}",
        json={"name": "Dusk", "price": 250, "enabled": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["name"], body["price"], body["enabled"]) == ("Dusk", 250, False)


def test_patch_rejects_unknown_fields(authed_client, fake_ctx):
    """The gradient is owned by the swatch filename, so it is not patchable."""
    color_id = _add_color(fake_ctx)
    resp = authed_client.patch(
        f"/api/economy/color-catalog/{color_id}", json={"hex1": "FFFFFF"}
    )
    assert resp.status_code == 422


def test_patch_404s_for_a_missing_color(authed_client):
    resp = authed_client.patch(
        "/api/economy/color-catalog/9999", json={"name": "Nope"}
    )
    assert resp.status_code == 404


def test_patch_is_guild_scoped(authed_client, fake_ctx):
    """A colour belonging to another guild is invisible, not editable."""
    with open_db(fake_ctx.db_path) as conn:
        other = upsert_catalog_color(
            conn, fake_ctx.guild_id + 1, "elsewhere",
            name="Elsewhere", hex1="FF0000", hex2="00FF00",
            image_path="/x.png", sort_order=0,
        )
        conn.commit()
    resp = authed_client.patch(
        f"/api/economy/color-catalog/{other}", json={"name": "Hijacked"}
    )
    assert resp.status_code == 404


# ── delete ─────────────────────────────────────────────────────────────


def test_delete_removes_an_unused_color(authed_client, fake_ctx):
    color_id = _add_color(fake_ctx)
    assert authed_client.delete(f"/api/economy/color-catalog/{color_id}").status_code == 200
    with open_db(fake_ctx.db_path) as conn:
        assert get_catalog_color_by_key(conn, fake_ctx.guild_id, "dusk") is None


def test_delete_409s_while_a_member_is_renting_it(authed_client, fake_ctx):
    """The renter keeps what they paid for — disable is the retire path."""
    color_id = _add_color(fake_ctx)
    with open_db(fake_ctx.db_path) as conn:
        apply_credit(conn, fake_ctx.guild_id, 42, 10_000, "grant")
        rent_perk(
            conn, SETTINGS, fake_ctx.guild_id, 42, "role_preset",
            catalog_color_id=color_id, now=1_000.0,
        )
        conn.commit()

    resp = authed_client.delete(f"/api/economy/color-catalog/{color_id}")
    assert resp.status_code == 409
    assert "disable" in resp.json()["detail"].lower()
    with open_db(fake_ctx.db_path) as conn:
        assert get_catalog_color_by_key(conn, fake_ctx.guild_id, "dusk") is not None


def test_delete_404s_for_a_missing_color(authed_client):
    assert authed_client.delete("/api/economy/color-catalog/9999").status_code == 404


# ── sync ───────────────────────────────────────────────────────────────


def test_sync_400s_with_no_swatches(authed_client):
    """An empty folder must not be allowed to retire the whole palette."""
    resp = authed_client.post("/api/economy/color-catalog/sync")
    assert resp.status_code == 400
    assert "swatch" in resp.json()["detail"].lower()


def test_sync_builds_the_palette_from_uploads(authed_client, fake_ctx):
    directory = fake_ctx.db_path.parent / "swatches" / str(fake_ctx.guild_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dusk_ember_F0A830_8842C8.png").write_bytes(b"png")

    resp = authed_client.post("/api/economy/color-catalog/sync")
    assert resp.status_code == 200
    assert resp.json()["added"] == ["dusk ember"]


# ── taking down the old showroom channel ───────────────────────────────
#
# The showroom is built inside `/bank shop` now, so there is no posting route
# left to guard — only the one that clears the messages an older version left
# sitting in a channel.


def test_remove_panel_requires_bot(authed_client):
    """Without a live bot, a 503 rather than a 500."""
    resp = authed_client.post("/api/economy/color-catalog/remove-panel")
    assert resp.status_code == 503


def _panel_guild(fake_ctx):
    """bot/guild scaffolding for the take-down tests."""
    guild = MagicMock()
    guild.id = fake_ctx.guild_id
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    fake_ctx.bot = bot
    return guild


def test_remove_panel_reports_what_it_deleted(authed_client, fake_ctx, monkeypatch):
    calls: list[tuple] = []

    async def _svc(db_path, guild):
        calls.append((db_path, guild))
        return 21

    monkeypatch.setattr("web_server.routes.economy.take_down_palette_panel", _svc)
    _panel_guild(fake_ctx)

    resp = authed_client.post("/api/economy/color-catalog/remove-panel")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 21
    assert len(calls) == 1


def test_remove_panel_with_nothing_posted_is_a_success(
    authed_client, fake_ctx, monkeypatch
):
    """An admin on a server that never posted a showroom got what they asked for."""

    async def _svc(*a, **kw):
        return 0

    monkeypatch.setattr("web_server.routes.economy.take_down_palette_panel", _svc)
    _panel_guild(fake_ctx)

    resp = authed_client.post("/api/economy/color-catalog/remove-panel")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 0}


def test_post_panel_route_is_gone():
    """The channel showroom cannot be re-posted — the shop is the showroom.

    Asserted against the router rather than a status code: the surviving
    ``/color-catalog/{color_id}`` routes happily match ``post-panel`` as an id
    and answer 405, which would let the old endpoint come back unnoticed.
    """
    from web_server.routes.economy import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/economy/color-catalog/remove-panel" in paths
    assert "/economy/color-catalog/post-panel" not in paths


# ── swatch uploads ─────────────────────────────────────────────────────


def test_swatch_listing_starts_empty(authed_client):
    resp = authed_client.get("/api/economy/color-catalog/swatches")
    assert resp.status_code == 200
    assert resp.json()["files"] == []


def test_swatch_upload_rejects_a_non_image(authed_client):
    resp = authed_client.post(
        "/api/economy/color-catalog/swatches",
        files={"files": ("notes.txt", b"nope", "text/plain")},
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "filename",
    [
        pytest.param("../escape.png", id="parent-traversal"),
        pytest.param("/abs/escape.png", id="absolute"),
    ],
)
def test_swatch_upload_refuses_path_traversal(authed_client, filename):
    """The upload writes to disk by name, so the name must not be able to walk out."""
    resp = authed_client.post(
        "/api/economy/color-catalog/swatches",
        files={"files": (filename, b"png", "image/png")},
    )
    # Either rejected outright, or basenamed into the managed folder — never
    # written outside it.
    assert resp.status_code in (200, 400)
    if resp.status_code == 200:
        assert all("/" not in f["name"] for f in resp.json()["files"])


def test_swatch_upload_then_delete_roundtrips(authed_client):
    resp = authed_client.post(
        "/api/economy/color-catalog/swatches",
        files={"files": ("Ruby_ff0000_8b0000.png", b"png", "image/png")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] == ["Ruby_ff0000_8b0000.png"]
    listed = {f["name"]: f for f in body["files"]}
    assert listed["Ruby_ff0000_8b0000.png"]["valid"] is True
    assert listed["Ruby_ff0000_8b0000.png"]["label"] == "Ruby"

    resp = authed_client.delete(
        "/api/economy/color-catalog/swatches/Ruby_ff0000_8b0000.png"
    )
    assert resp.status_code == 200
    assert resp.json()["files"] == []


def test_swatch_delete_404s_for_a_missing_file(authed_client):
    resp = authed_client.delete(
        "/api/economy/color-catalog/swatches/Nope_ff0000_8b0000.png"
    )
    assert resp.status_code == 404
