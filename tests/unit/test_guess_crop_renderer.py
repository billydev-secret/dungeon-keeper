"""Unit tests for services/guess_crop_renderer.py — real PIL, no mocks."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from bot_modules.services.guess_models import BoundingBox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg(w: int = 200, h: int = 200, color: tuple = (255, 0, 0)) -> bytes:
    """Create a minimal in-memory JPEG image and return its bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color=color).save(buf, format="JPEG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_render_crop_returns_jpeg_bytes():
    """render_crop should return bytes that start with JPEG magic bytes."""
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=10, y1=10, x2=60, y2=60)
    result = render_crop(image_bytes, box)

    assert isinstance(result, bytes)
    assert result[:2] == b"\xff\xd8", "Expected JPEG magic bytes at start"


def test_render_crop_writes_cache_file(tmp_path: Path):
    """render_crop should write JPEG bytes to cache_path when given."""
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=0, y1=0, x2=100, y2=100)
    cache_path = tmp_path / "crop.jpg"

    result = render_crop(image_bytes, box, cache_path=cache_path)

    assert cache_path.exists(), "Cache file should have been created"
    assert cache_path.read_bytes() == result, "Cache file content should match return value"


def test_render_crop_no_cache_path_no_file_written(tmp_path: Path):
    """render_crop should not create any file when cache_path is None."""
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=0, y1=0, x2=50, y2=50)

    render_crop(image_bytes, box, cache_path=None)

    # Confirm no files were written to tmp_path
    written_files = list(tmp_path.iterdir())
    assert written_files == [], f"Expected no files written, found: {written_files}"


def test_render_crop_creates_parent_dirs(tmp_path: Path):
    """render_crop should create intermediate parent directories as needed."""
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=0, y1=0, x2=50, y2=50)
    cache_path = tmp_path / "nested" / "dir" / "crop.jpg"

    render_crop(image_bytes, box, cache_path=cache_path)

    assert cache_path.exists(), "File should exist after creating parent dirs"


def test_render_crop_respects_box():
    """Cropped JPEG dimensions should exactly match the crop box dimensions."""
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    image_bytes = _make_jpeg(200, 200)
    box = BoundingBox(x1=10, y1=20, x2=60, y2=70)  # 50x50 region
    expected_w = int(box.x2) - int(box.x1)
    expected_h = int(box.y2) - int(box.y1)

    result = render_crop(image_bytes, box)

    # Re-open result to check dimensions
    cropped_img = Image.open(io.BytesIO(result))
    assert cropped_img.width == expected_w, f"Expected width {expected_w}, got {cropped_img.width}"
    assert cropped_img.height == expected_h, f"Expected height {expected_h}, got {cropped_img.height}"


# ---------------------------------------------------------------------------
# render_crop_editor tests
# ---------------------------------------------------------------------------


def test_render_crop_editor_returns_jpeg_bytes():
    from bot_modules.services.guess_crop_renderer import render_crop_editor  # noqa: PLC0415

    image_bytes = _make_jpeg(400, 400)
    box = BoundingBox(x1=50, y1=50, x2=200, y2=200)
    result = render_crop_editor(image_bytes, box)

    assert isinstance(result, bytes)
    assert result[:2] == b"\xff\xd8"


def test_render_crop_editor_scales_down_large_image():
    from bot_modules.services.guess_crop_renderer import render_crop_editor  # noqa: PLC0415

    image_bytes = _make_jpeg(2000, 2000)
    box = BoundingBox(x1=0, y1=0, x2=2000, y2=2000)
    result = render_crop_editor(image_bytes, box, max_display_px=1280)

    out = Image.open(io.BytesIO(result))
    assert max(out.width, out.height) <= 1280


def test_render_crop_editor_doesnt_upscale_small_image():
    from bot_modules.services.guess_crop_renderer import render_crop_editor  # noqa: PLC0415

    image_bytes = _make_jpeg(400, 300)
    box = BoundingBox(x1=0, y1=0, x2=400, y2=300)
    result = render_crop_editor(image_bytes, box, max_display_px=1280)

    out = Image.open(io.BytesIO(result))
    assert out.width == 400
    assert out.height == 300


def test_render_crop_editor_full_image_box_doesnt_error():
    from bot_modules.services.guess_crop_renderer import render_crop_editor  # noqa: PLC0415

    image_bytes = _make_jpeg(500, 500)
    box = BoundingBox(x1=0, y1=0, x2=500, y2=500)
    result = render_crop_editor(image_bytes, box)

    assert result[:2] == b"\xff\xd8"
