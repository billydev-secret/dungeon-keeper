"""Route tests for the per-guild quote-card border upload endpoints."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

from bot_modules.services.quote_renderer import guild_border_path


def _frame_png() -> bytes:
    """A valid frame: opaque border ring around a transparent center."""
    img = Image.new("RGBA", (60, 40), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, 59, 39], outline=(200, 160, 40, 255), width=4)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _opaque_png() -> bytes:
    img = Image.new("RGBA", (60, 40), (10, 20, 30, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg() -> bytes:
    img = Image.new("RGB", (60, 40), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_quote_border_absent_by_default(authed_client):
    resp = authed_client.get("/api/config/quote-border")
    assert resp.status_code == 200
    assert resp.json()["exists"] is False
    # No image to serve yet.
    assert authed_client.get("/api/config/quote-border/image").status_code == 404


def test_quote_border_upload_get_delete_roundtrip(authed_client, fake_ctx):
    up = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("frame.png", _frame_png(), "image/png")},
    )
    assert up.status_code == 200, up.text
    body = up.json()
    assert body["exists"] is True
    assert body["width"] == 60 and body["height"] == 40

    # Landed at the exact path the bot renderer reads.
    target = guild_border_path(fake_ctx.db_path, fake_ctx.guild_id)
    assert target.is_file()

    img = authed_client.get("/api/config/quote-border/image")
    assert img.status_code == 200
    assert img.headers["content-type"] == "image/png"

    dele = authed_client.delete("/api/config/quote-border")
    assert dele.status_code == 200
    assert dele.json()["exists"] is False
    assert not target.exists()
    assert authed_client.get("/api/config/quote-border/image").status_code == 404


def test_quote_border_rejects_opaque_image(authed_client, fake_ctx):
    resp = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("solid.png", _opaque_png(), "image/png")},
    )
    assert resp.status_code == 400
    assert "transparent" in resp.json()["detail"].lower()
    assert not guild_border_path(fake_ctx.db_path, fake_ctx.guild_id).exists()


def test_quote_border_rejects_jpeg(authed_client, fake_ctx):
    resp = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("photo.jpg", _jpeg(), "image/jpeg")},
    )
    assert resp.status_code == 400
    assert not guild_border_path(fake_ctx.db_path, fake_ctx.guild_id).exists()


def _corners_only_png() -> bytes:
    # Transparent only in the corners → center covered → no usable opening.
    img = Image.new("RGBA", (240, 160), (10, 20, 30, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 30, 30], fill=(0, 0, 0, 0))
    d.rectangle([209, 129, 239, 159], fill=(0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_quote_border_rejects_no_usable_opening(authed_client, fake_ctx):
    resp = authed_client.post(
        "/api/config/quote-border",
        files={"file": ("corners.png", _corners_only_png(), "image/png")},
    )
    assert resp.status_code == 400
    assert "opening" in resp.json()["detail"].lower()
    assert not guild_border_path(fake_ctx.db_path, fake_ctx.guild_id).exists()
    # The temp probe file must not linger either.
    assert not (
        guild_border_path(fake_ctx.db_path, fake_ctx.guild_id).parent
        / "border.tmp.png"
    ).exists()
