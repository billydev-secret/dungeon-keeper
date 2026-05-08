"""Veil pipeline — pure geometry helpers and pipeline orchestrator.

Import this module freely; NudeNet / mediapipe / PIL are imported lazily
inside run_pipeline() only.
"""
from __future__ import annotations


from services.veil_models import BoundingBox, Detection


DIFFICULTY_PADDING: dict[str, float] = {
    "easy": 0.60,
    "medium": 0.25,
    "hard": 0.05,
}


def iou(box_a: BoundingBox, box_b: BoundingBox) -> float:
    """Intersection over Union of two bounding boxes. Returns 0.0 on no overlap."""
    ix1 = max(box_a.x1, box_b.x1)
    iy1 = max(box_a.y1, box_b.y1)
    ix2 = min(box_a.x2, box_b.x2)
    iy2 = min(box_a.y2, box_b.y2)
    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if intersection == 0.0:
        return 0.0
    union = box_a.area + box_b.area - intersection
    return intersection / union if union > 0.0 else 0.0


def compute_padded_crop(
    bbox: BoundingBox,
    difficulty: str,
    img_w: int,
    img_h: int,
) -> BoundingBox:
    """Apply difficulty-tuned padding and clamp to image bounds.

    Padding fraction applies to the larger dimension of the input bbox.

    Raises:
        ValueError: If difficulty is not "easy", "medium", or "hard".
    """
    if difficulty not in DIFFICULTY_PADDING:
        raise ValueError(
            f"Unknown difficulty {difficulty!r}; expected one of {list(DIFFICULTY_PADDING)}"
        )
    pad = DIFFICULTY_PADDING[difficulty] * max(bbox.width, bbox.height)
    return BoundingBox(
        max(0.0, bbox.x1 - pad),
        max(0.0, bbox.y1 - pad),
        min(float(img_w), bbox.x2 + pad),
        min(float(img_h), bbox.y2 + pad),
    )


def filter_candidates(
    detections: list[Detection],
    face_boxes: list[BoundingBox],
    *,
    fallback: bool = True,
    iou_threshold: float = 0.1,
) -> list[Detection]:
    """Remove detections with face IoU above threshold.

    If all candidates are eliminated and fallback=True, returns the single
    highest-score detection so the crop renderer can clip to non-face area.
    """
    def _overlaps_face(det: Detection) -> bool:
        return any(iou(det.box, fb) > iou_threshold for fb in face_boxes)

    filtered = [d for d in detections if not _overlaps_face(d)]
    if not filtered and fallback and detections:
        return [detections[0]]
    return filtered


def enforce_min_size(bbox: BoundingBox, min_px: int = 200) -> BoundingBox:
    """Expand bbox symmetrically so both width and height are >= min_px.

    Does not clamp to image bounds — caller should clamp if needed.
    Center of the original bbox is preserved.
    """
    cx = (bbox.x1 + bbox.x2) / 2.0
    cy = (bbox.y1 + bbox.y2) / 2.0
    half_w = max(bbox.width, float(min_px)) / 2.0
    half_h = max(bbox.height, float(min_px)) / 2.0
    return BoundingBox(cx - half_w, cy - half_h, cx + half_w, cy + half_h)


# run_pipeline() and run_reroll() appended in Task 9
