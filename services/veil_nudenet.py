"""Lazy NudeNet ONNX wrapper for the Veil pipeline."""
from __future__ import annotations

import logging
from pathlib import Path

from services.veil_models import BoundingBox, Detection

log = logging.getLogger("dungeonkeeper.veil")

_detector = None


def _get_detector():  # type: ignore[return]
    global _detector
    if _detector is None:
        from nudenet import NudeDetector  # type: ignore[import-untyped]  # noqa: PLC0415
        # Use the 640m model from models/ if present; fall back to the 320n bundled
        # with the nudenet package so no manual download is required.
        custom = Path(__file__).parent.parent / "models" / "640m.onnx"
        if custom.exists() and custom.stat().st_size > 10 * 1024 * 1024:
            log.info("using custom 640m.onnx from models/")
            _detector = NudeDetector(model_path=str(custom), inference_resolution=640)
        else:
            log.info("using bundled 320n.onnx")
            _detector = NudeDetector(inference_resolution=320)
    return _detector


def detect(image_path: str | Path) -> list[Detection]:
    """Run NudeNet detection on *image_path* and return a list of Detections.

    NudeNet is imported lazily so this module is safe to import even when
    nudenet is not installed.

    Box conversion: NudeNet returns ``[x, y, width, height]``; we convert to
    ``BoundingBox(x1=x, y1=y, x2=x+w, y2=y+h)``.
    """
    raw_results = _get_detector().detect(str(image_path))

    detections: list[Detection] = []
    for item in raw_results:
        x, y, w, h = item["box"]
        detections.append(
            Detection(
                label=item["class"],
                score=item["score"],
                box=BoundingBox(x1=x, y1=y, x2=x + w, y2=y + h),
            )
        )
    return detections
