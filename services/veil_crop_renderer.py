"""PIL-based crop renderer for the Veil pipeline.

PIL is imported lazily so this module is safe to import even when Pillow
is not installed.
"""
from __future__ import annotations

import io
from pathlib import Path

from services.veil_models import BoundingBox


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
