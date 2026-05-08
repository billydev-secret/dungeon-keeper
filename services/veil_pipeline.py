"""Veil pipeline — pure geometry helpers and pipeline orchestrator.

Import this module freely; NudeNet / mediapipe / PIL are imported lazily
inside run_pipeline() only.
"""
from __future__ import annotations

import io
import random
from pathlib import Path

from services.veil_models import BoundingBox, Detection, PipelineResult


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
        return [max(detections, key=lambda d: d.score)]
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


def run_pipeline(
    image_path: Path,
    image_bytes: bytes,
    difficulty: str,
    *,
    candidate_count: int = 3,
    cache_dir: Path | None = None,
    jpeg_quality: int = 85,
) -> PipelineResult:
    """Run the full veil detection pipeline on *image_bytes*.

    Heavy dependencies (NudeNet, mediapipe, PIL) are imported lazily at the
    top of this function so the module can be safely imported without them.

    Args:
        image_path: Path to the source image file (used by nudenet and for
            cache file naming).
        image_bytes: Raw bytes of the source image (used for PIL operations
            and face detection).
        difficulty: Crop padding difficulty — "easy", "medium", or "hard".
        candidate_count: Maximum number of top-score candidates to crop.
        cache_dir: If given, write each crop JPEG to
            ``cache_dir/<stem>_<i>.jpg``.
        jpeg_quality: JPEG encoding quality passed to render_crop.

    Returns:
        PipelineResult with all filtered candidates and the rendered crop
        bytes for the top *candidate_count* of them.
    """
    # Lazy imports — done once at the top of the function, not inside the loop.
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415

    from services.veil_nudenet import detect as nudenet_detect  # noqa: PLC0415
    from services.veil_face_detector import detect_faces  # noqa: PLC0415
    from services.veil_crop_renderer import render_crop  # noqa: PLC0415

    detections = nudenet_detect(image_path)
    face_boxes = detect_faces(image_bytes)

    filtered_candidates = filter_candidates(detections, face_boxes)

    # Sort by score descending and take top candidate_count for rendering.
    top_candidates = sorted(filtered_candidates, key=lambda d: d.score, reverse=True)[
        :candidate_count
    ]

    # Get image dimensions once before the loop.
    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size

    crop_bytes_list: list[bytes] = []
    for i, det in enumerate(top_candidates):
        crop_box = enforce_min_size(compute_padded_crop(det.box, difficulty, img_w, img_h))
        clamped_box = BoundingBox(
            max(0.0, crop_box.x1),
            max(0.0, crop_box.y1),
            min(float(img_w), crop_box.x2),
            min(float(img_h), crop_box.y2),
        )
        cache_path: Path | None = None
        if cache_dir is not None:
            cache_path = cache_dir / f"{image_path.stem}_{i}.jpg"
        jpeg_bytes = render_crop(
            image_bytes, clamped_box, cache_path=cache_path, jpeg_quality=jpeg_quality
        )
        crop_bytes_list.append(jpeg_bytes)

    return PipelineResult(candidates=filtered_candidates, crops=crop_bytes_list)


def run_reroll(
    image_bytes: bytes,
    existing_crops: list[BoundingBox],
    difficulty: str,
    *,
    cache_dir: Path | None = None,
    jpeg_quality: int = 85,
) -> bytes | None:
    """Generate a reroll crop that minimises overlap with *existing_crops*.

    Picks from 5 randomly-offset centre crops the one with the lowest maximum
    IoU against all existing crops, then renders and returns it.

    Args:
        image_bytes: Raw bytes of the source image.
        existing_crops: BoundingBoxes of crops already shown to the user.
        difficulty: Unused directly in crop sizing here; reserved for future
            use (face detection still happens).
        cache_dir: Reserved for future use (not passed to render_crop).
        jpeg_quality: JPEG encoding quality passed to render_crop.

    Returns:
        JPEG bytes of the selected crop, or None if no candidates could be
        generated (should not happen in practice).
    """
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415

    from services.veil_face_detector import detect_faces  # noqa: PLC0415
    from services.veil_crop_renderer import render_crop  # noqa: PLC0415

    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
    detect_faces(image_bytes)  # called for side-effect / future use

    cx, cy = img_w / 2, img_h / 2
    half = min(img_w, img_h) * 0.3  # 30 % of shorter dimension

    candidates: list[BoundingBox] = []
    for _ in range(5):
        dx = random.uniform(-half * 0.5, half * 0.5)
        dy = random.uniform(-half * 0.5, half * 0.5)
        box = BoundingBox(
            max(0.0, cx - half + dx),
            max(0.0, cy - half + dy),
            min(float(img_w), cx + half + dx),
            min(float(img_h), cy + half + dy),
        )
        candidates.append(box)

    if not candidates:
        return None

    def max_iou_with_existing(box: BoundingBox) -> float:
        if not existing_crops:
            return 0.0
        return max(iou(box, e) for e in existing_crops)

    best = min(candidates, key=max_iou_with_existing)
    return render_crop(image_bytes, best, cache_path=None, jpeg_quality=jpeg_quality)
