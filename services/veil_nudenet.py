"""Lazy NudeNet ONNX wrapper for the Veil pipeline."""
from __future__ import annotations

from pathlib import Path

from services.veil_models import BoundingBox, Detection


def detect(image_path: str | Path) -> list[Detection]:
    """Run NudeNet detection on *image_path* and return a list of Detections.

    NudeNet is imported lazily so this module is safe to import even when
    nudenet is not installed.

    Box conversion: NudeNet returns ``[x, y, width, height]``; we convert to
    ``BoundingBox(x1=x, y1=y, x2=x+w, y2=y+h)``.
    """
    from nudenet import NudeDetector  # type: ignore[import-untyped]  # noqa: PLC0415

    detector = NudeDetector()
    raw_results = detector.detect(str(image_path))

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
