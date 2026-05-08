"""Unit tests for pure validation helpers in veil_cog."""
from __future__ import annotations

import io
from PIL import Image

from tests.fakes import FakeMember, FakeRole


def _make_jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(128, 0, 64)).save(buf, format="JPEG")
    return buf.getvalue()


class TestHasVeilRole:
    def test_member_with_role_returns_true(self):
        from cogs.veil_cog import _has_veil_role
        m = FakeMember(roles=[FakeRole(id=777)])
        assert _has_veil_role(m, 777) is True

    def test_member_without_role_returns_false(self):
        from cogs.veil_cog import _has_veil_role
        m = FakeMember(roles=[FakeRole(id=999)])
        assert _has_veil_role(m, 777) is False

    def test_member_with_no_roles_returns_false(self):
        from cogs.veil_cog import _has_veil_role
        m = FakeMember(roles=[])
        assert _has_veil_role(m, 777) is False


class TestValidateMime:
    def test_jpeg_accepted(self):
        from cogs.veil_cog import _validate_mime
        assert _validate_mime("image/jpeg") is True

    def test_png_accepted(self):
        from cogs.veil_cog import _validate_mime
        assert _validate_mime("image/png") is True

    def test_video_rejected(self):
        from cogs.veil_cog import _validate_mime
        assert _validate_mime("video/mp4") is False

    def test_none_rejected(self):
        from cogs.veil_cog import _validate_mime
        assert _validate_mime(None) is False


class TestValidateSize:
    def test_within_limit(self):
        from cogs.veil_cog import _validate_size
        assert _validate_size(5 * 1024 * 1024, max_mb=10) is True

    def test_at_limit(self):
        from cogs.veil_cog import _validate_size
        assert _validate_size(10 * 1024 * 1024, max_mb=10) is True

    def test_over_limit(self):
        from cogs.veil_cog import _validate_size
        assert _validate_size(10 * 1024 * 1024 + 1, max_mb=10) is False


class TestValidateDimensions:
    def test_large_enough_image(self):
        from cogs.veil_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(500, 500), min_px=400)
        assert ok is True
        assert w == 500
        assert h == 500

    def test_too_narrow(self):
        from cogs.veil_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(300, 600), min_px=400)
        assert ok is False

    def test_too_short(self):
        from cogs.veil_cog import _validate_dimensions
        ok, w, h = _validate_dimensions(_make_jpeg(600, 300), min_px=400)
        assert ok is False

    def test_exactly_at_limit(self):
        from cogs.veil_cog import _validate_dimensions
        ok, _, _ = _validate_dimensions(_make_jpeg(400, 400), min_px=400)
        assert ok is True
