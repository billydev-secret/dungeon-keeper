"""PIL-based crop renderer for the Veil pipeline.

PIL is imported lazily so this module is safe to import even when Pillow
is not installed.
"""
from __future__ import annotations

import io
from pathlib import Path

from bot_modules.services.veil_models import BoundingBox


def render_crop_editor(
    image_bytes: bytes,
    crop_box: BoundingBox,
    *,
    max_display_px: int = 1280,
    jpeg_quality: int = 80,
) -> bytes:
    """Return the full image scaled to fit *max_display_px*, with a red crop box drawn on it.

    The image is scaled down (never up) so its longest dimension fits within
    *max_display_px*.  The *crop_box* coordinates are scaled proportionally before
    drawing.  Suitable for sending as an ephemeral Discord attachment so the
    submitter can preview and adjust their crop region.

    Args:
        image_bytes: Raw source image bytes (any PIL-supported format).
        crop_box: Crop region in original image pixel coordinates.
        max_display_px: Maximum pixel dimension for the output image (default 1280).
        jpeg_quality: JPEG encoding quality (default 80).

    Returns:
        JPEG bytes of the annotated full image.
    """
    from PIL import Image, ImageDraw  # type: ignore[import-untyped]  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    scale = min(1.0, max_display_px / max(orig_w, orig_h))
    if scale < 1.0:
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    sx = img.width / orig_w
    sy = img.height / orig_h
    rect = (
        int(crop_box.x1 * sx),
        int(crop_box.y1 * sy),
        int(crop_box.x2 * sx),
        int(crop_box.y2 * sy),
    )

    draw = ImageDraw.Draw(img)
    draw.rectangle(rect, outline=(255, 0, 0), width=3)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()


def render_reveal(
    image_bytes: bytes,
    crop_box: BoundingBox,
    *,
    max_display_px: int = 1920,
    jpeg_quality: int = 85,
) -> bytes:
    """Return the full image with a semi-transparent crop box drawn on it for the reveal.

    Same as render_crop_editor but uses 50% opacity so the box is visible
    without obscuring the underlying image detail.
    """
    from PIL import Image, ImageDraw  # type: ignore[import-untyped]  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    orig_w, orig_h = img.size

    scale = min(1.0, max_display_px / max(orig_w, orig_h))
    if scale < 1.0:
        new_w = max(1, int(orig_w * scale))
        new_h = max(1, int(orig_h * scale))
        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    sx = img.width / orig_w
    sy = img.height / orig_h
    rect = (
        int(crop_box.x1 * sx),
        int(crop_box.y1 * sy),
        int(crop_box.x2 * sx),
        int(crop_box.y2 * sy),
    )

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(rect, outline=(255, 0, 0, 128), width=3)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=jpeg_quality)
    return buf.getvalue()


def render_crop(
    image_bytes: bytes,
    crop_box: BoundingBox,
    *,
    cache_path: Path | None = None,
    jpeg_quality: int = 85,
) -> bytes:
    """Crop *image_bytes* to *crop_box* and return JPEG-encoded bytes.

    Args:
        image_bytes: Raw bytes of the source image (any PIL-supported format).
        crop_box: Region to crop, in absolute pixel coordinates.
        cache_path: If given, write the JPEG bytes to this path.  Parent
            directories are created automatically.
        jpeg_quality: JPEG encoding quality (1–95).  Defaults to 85.

    Returns:
        JPEG bytes of the cropped region.
    """
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cropped = img.crop((int(crop_box.x1), int(crop_box.y1), int(crop_box.x2), int(crop_box.y2)))

    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=jpeg_quality)
    jpeg_bytes = buf.getvalue()

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(jpeg_bytes)

    return jpeg_bytes
