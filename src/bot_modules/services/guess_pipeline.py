"""Guess pipeline — pure geometry helpers and pipeline orchestrator.

Import this module freely; NudeNet / mediapipe / PIL are imported lazily
inside run_pipeline() only.
"""
from __future__ import annotations

import io
import random
from pathlib import Path

import logging

from bot_modules.services.guess_models import BoundingBox, Detection, PipelineResult

log = logging.getLogger("dungeonkeeper.guess")

_GENITAL_LABELS: frozenset[str] = frozenset({
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED",
})

# Labels that produce valid crops but rarely make a fun guess. Their score is
# halved so they only outrank stronger detections when there's nothing better.
LOW_INTEREST_LABELS: frozenset[str] = frozenset({
    "ARMPITS_COVERED",
    "ARMPITS_EXPOSED",
    "BELLY_COVERED",
    "BELLY_EXPOSED",
})
LOW_INTEREST_WEIGHT: float = 0.5

# Labels we want to feature when present — the game gets more interesting when
# crops focus on these. Boosted so a moderate-confidence interesting region
# outranks a confident belly or generic torso pose.
HIGH_INTEREST_LABELS: frozenset[str] = frozenset({
    "MALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_COVERED",
    "FEMALE_GENITALIA_EXPOSED",
    "FEMALE_GENITALIA_COVERED",
    "ANUS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_BREAST_COVERED",
    "BUTTOCKS_EXPOSED",
    "BUTTOCKS_COVERED",
    "SEX_ACT",
})
HIGH_INTEREST_WEIGHT: float = 1.5

DIFFICULTY_PADDING: dict[str, float] = {
    "easy": 0.60,
    "medium": 0.25,
    "hard": 0.05,
}


def apply_label_weights(detections: list[Detection]) -> list[Detection]:
    """Reweight detections by interest class.

    LOW_INTEREST_LABELS get scored at LOW_INTEREST_WEIGHT× so they only outrank
    stronger detections when there's nothing better. HIGH_INTEREST_LABELS get
    a HIGH_INTEREST_WEIGHT× boost (capped at 1.0) so a moderate-confidence
    interesting region beats a confident belly or pose-derived torso. Other
    labels pass through unchanged.
    """
    def _weight(d: Detection) -> Detection:
        if d.label in LOW_INTEREST_LABELS:
            return Detection(label=d.label, score=d.score * LOW_INTEREST_WEIGHT, box=d.box)
        if d.label in HIGH_INTEREST_LABELS:
            return Detection(
                label=d.label,
                score=min(1.0, d.score * HIGH_INTEREST_WEIGHT),
                box=d.box,
            )
        return d
    return [_weight(d) for d in detections]


def _box_gap(a: BoundingBox, b: BoundingBox) -> float:
    """Pixel distance between two boxes (0.0 when overlapping)."""
    dx = max(0.0, max(a.x1, b.x1) - min(a.x2, b.x2))
    dy = max(0.0, max(a.y1, b.y1) - min(a.y2, b.y2))
    return (dx * dx + dy * dy) ** 0.5


def merge_sex_act_detections(detections: list[Detection]) -> list[Detection]:
    """Merge overlapping/adjacent different-type genital detections into a SEX_ACT detection.

    When NudeNet finds two different genital labels close together they likely
    represent a penetration act. Merge their boxes so the pipeline crops the act
    area rather than individual parts. The merged detection gets a boosted score
    so it ranks first among candidates.
    """
    genitals = [d for d in detections if d.label in _GENITAL_LABELS]
    merged: list[Detection] = []
    for i in range(len(genitals)):
        for j in range(i + 1, len(genitals)):
            a, b = genitals[i], genitals[j]
            if a.label == b.label:
                continue
            avg_size = (max(a.box.width, a.box.height) + max(b.box.width, b.box.height)) / 2.0
            if avg_size == 0.0:
                continue
            if _box_gap(a.box, b.box) < avg_size * 0.5:
                union = BoundingBox(
                    min(a.box.x1, b.box.x1),
                    min(a.box.y1, b.box.y1),
                    max(a.box.x2, b.box.x2),
                    max(a.box.y2, b.box.y2),
                )
                merged.append(Detection(
                    label="SEX_ACT",
                    score=min(1.0, max(a.score, b.score) + 0.15),
                    box=union,
                ))
    return detections + merged


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
    *,
    rng: random.Random | None = None,
    jitter_scale: float = 0.8,
) -> BoundingBox:
    """Apply difficulty-tuned padding and clamp to image bounds.

    Padding fraction applies to the larger dimension of the input bbox.

    When *rng* is provided, the crop window is shifted asymmetrically by up to
    ``jitter_scale * pad`` in each axis so the framing varies between crops.
    The original detection bbox is guaranteed to remain inside the crop.

    Raises:
        ValueError: If difficulty is not "easy", "medium", or "hard".
    """
    if difficulty not in DIFFICULTY_PADDING:
        raise ValueError(
            f"Unknown difficulty {difficulty!r}; expected one of {list(DIFFICULTY_PADDING)}"
        )
    pad = DIFFICULTY_PADDING[difficulty] * max(bbox.width, bbox.height)
    dx = rng.uniform(-pad * jitter_scale, pad * jitter_scale) if rng is not None else 0.0
    dy = rng.uniform(-pad * jitter_scale, pad * jitter_scale) if rng is not None else 0.0
    return BoundingBox(
        max(0.0, bbox.x1 - pad + dx),
        max(0.0, bbox.y1 - pad + dy),
        min(float(img_w), bbox.x2 + pad + dx),
        min(float(img_h), bbox.y2 + pad + dy),
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


def _clamp_to_image(bbox: BoundingBox, img_w: int, img_h: int) -> BoundingBox:
    return BoundingBox(
        max(0.0, bbox.x1),
        max(0.0, bbox.y1),
        min(float(img_w), bbox.x2),
        min(float(img_h), bbox.y2),
    )


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


def move_crop_box(
    box: BoundingBox,
    dx: float,
    dy: float,
    img_w: int,
    img_h: int,
) -> BoundingBox:
    """Translate *box* by (dx, dy) while keeping it fully inside the image.

    Width and height are preserved exactly; the box is clamped to image bounds.
    """
    w, h = box.width, box.height
    new_x1 = max(0.0, min(box.x1 + dx, float(img_w) - w))
    new_y1 = max(0.0, min(box.y1 + dy, float(img_h) - h))
    return BoundingBox(new_x1, new_y1, new_x1 + w, new_y1 + h)


def zoom_crop_box(
    box: BoundingBox,
    factor: float,
    img_w: int,
    img_h: int,
    *,
    min_px: int = 200,
) -> BoundingBox:
    """Zoom *box* by *factor* around its center, respecting min_px and image bounds.

    factor < 1 zooms in (smaller box); factor > 1 zooms out (larger box).
    """
    cx = (box.x1 + box.x2) / 2.0
    cy = (box.y1 + box.y2) / 2.0
    half_w = max(float(min_px) / 2.0, min(box.width * factor / 2.0, float(img_w) / 2.0))
    half_h = max(float(min_px) / 2.0, min(box.height * factor / 2.0, float(img_h) / 2.0))
    return BoundingBox(
        max(0.0, cx - half_w),
        max(0.0, cy - half_h),
        min(float(img_w), cx + half_w),
        min(float(img_h), cy + half_h),
    )


def run_pipeline(
    image_path: Path,
    image_bytes: bytes,
    difficulty: str,
    *,
    candidate_count: int = 3,
    cache_dir: Path | None = None,
    jpeg_quality: int = 85,
) -> PipelineResult:
    """Run the full guess detection pipeline on *image_bytes*.

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

    from bot_modules.services.guess_nudenet import detect as nudenet_detect  # noqa: PLC0415
    from bot_modules.services.guess_face_detector import detect_faces  # noqa: PLC0415
    from bot_modules.services.guess_pose_detector import detect_pose  # noqa: PLC0415
    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size

    nudenet_dets = nudenet_detect(image_path)
    log.info("nudenet raw detections: %s", [(d.label, round(d.score, 2)) for d in nudenet_dets])
    nudenet_dets = merge_sex_act_detections(nudenet_dets)
    if any(d.label == "SEX_ACT" for d in nudenet_dets):
        log.info("sex act merge applied: %s", [(d.label, round(d.score, 2)) for d in nudenet_dets if d.label == "SEX_ACT"])

    pose_dets = detect_pose(image_bytes)

    detections = nudenet_dets + pose_dets

    if not detections:
        # No detections from any source: refuse rather than crop a random region
        # of an arbitrary (possibly off-topic) image. The cog turns an empty
        # PipelineResult into a clear ephemeral rejection.
        log.info("no detections from nudenet or pose — refusing submission")
        return PipelineResult(candidates=[], crops=[])

    face_boxes = detect_faces(image_bytes)

    filtered_candidates = filter_candidates(detections, face_boxes)
    filtered_candidates = apply_label_weights(filtered_candidates)
    log.info("filtered candidates: %s", [(d.label, round(d.score, 2)) for d in filtered_candidates])

    top_candidates = sorted(filtered_candidates, key=lambda d: d.score, reverse=True)[
        :candidate_count
    ]
    rng = random.Random()

    crop_bytes_list: list[bytes] = []
    for i, det in enumerate(top_candidates):
        crop_box = _clamp_to_image(
            enforce_min_size(compute_padded_crop(det.box, difficulty, img_w, img_h, rng=rng)),
            img_w, img_h,
        )
        cache_path: Path | None = None
        if cache_dir is not None:
            cache_path = cache_dir / f"{image_path.stem}_{i}.jpg"
        jpeg_bytes = render_crop(
            image_bytes, crop_box, cache_path=cache_path, jpeg_quality=jpeg_quality
        )
        crop_bytes_list.append(jpeg_bytes)

    return PipelineResult(candidates=filtered_candidates, crops=crop_bytes_list)


def run_reroll(
    image_bytes: bytes,
    existing_crops: list[BoundingBox],
    *,
    jpeg_quality: int = 85,
) -> bytes:
    """Generate a reroll crop that minimises overlap with *existing_crops*.

    Picks from 5 randomly-offset center crops the one with the lowest maximum
    IoU against all existing crops, then renders and returns it.
    """
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415

    from bot_modules.services.guess_crop_renderer import render_crop  # noqa: PLC0415

    img_w, img_h = Image.open(io.BytesIO(image_bytes)).size
    cx, cy = img_w / 2, img_h / 2
    half = min(img_w, img_h) * 0.3

    candidates: list[BoundingBox] = []
    for _ in range(5):
        dx = random.uniform(-half * 0.5, half * 0.5)
        dy = random.uniform(-half * 0.5, half * 0.5)
        candidates.append(_clamp_to_image(
            BoundingBox(cx - half + dx, cy - half + dy, cx + half + dx, cy + half + dy),
            img_w, img_h,
        ))

    def max_iou_with_existing(box: BoundingBox) -> float:
        if not existing_crops:
            return 0.0
        return max(iou(box, e) for e in existing_crops)

    best = min(candidates, key=max_iou_with_existing)
    return render_crop(image_bytes, best, cache_path=None, jpeg_quality=jpeg_quality)
