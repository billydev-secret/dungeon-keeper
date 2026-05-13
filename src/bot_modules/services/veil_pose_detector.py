"""Pose-based region detector for the Veil pipeline.

Pure geometry helpers (landmarks_to_*) are importable without mediapipe.
detect_pose() imports mediapipe, PIL, and numpy lazily.
"""
from __future__ import annotations

import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from bot_modules.services.veil_models import BoundingBox, Detection

log = logging.getLogger("dungeonkeeper.veil")

_POSE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task"
)
_MODELS_DIR = Path(__file__).parent.parent / "models"

_L_SHOULDER, _R_SHOULDER = 11, 12
_L_HIP, _R_HIP = 23, 24
_L_KNEE, _R_KNEE = 25, 26


@dataclass
class _Lm:
    """Normalized pose landmark (coordinates and visibility in 0–1 range)."""
    x: float
    y: float
    visibility: float


def _ensure_pose_model() -> Path:
    _MODELS_DIR.mkdir(exist_ok=True)
    model_path = _MODELS_DIR / "pose_landmarker_full.task"
    if not model_path.exists():
        log.info("downloading pose_landmarker_full.task…")
        tmp_path = model_path.with_suffix(model_path.suffix + ".tmp")
        try:
            urllib.request.urlretrieve(_POSE_MODEL_URL, tmp_path)
            tmp_path.replace(model_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    return model_path


def landmarks_to_torso_box(
    lms: list[_Lm],
    img_w: int,
    img_h: int,
    vis_threshold: float = 0.5,
) -> BoundingBox | None:
    """Bounding box spanning the shoulder-to-hip torso region.

    Returns None when no landmark at the required indices clears *vis_threshold*.
    """
    indices = [_L_SHOULDER, _R_SHOULDER, _L_HIP, _R_HIP]
    visible = [lms[i] for i in indices if lms[i].visibility >= vis_threshold]
    if not visible:
        return None
    xs = [lm.x * img_w for lm in visible]
    ys = [lm.y * img_h for lm in visible]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def landmarks_to_lower_body_box(
    lms: list[_Lm],
    img_w: int,
    img_h: int,
    vis_threshold: float = 0.5,
) -> BoundingBox | None:
    """Bounding box spanning the hip-to-knee lower body region.

    Returns None when no landmark at the required indices clears *vis_threshold*.
    """
    indices = [_L_HIP, _R_HIP, _L_KNEE, _R_KNEE]
    visible = [lms[i] for i in indices if lms[i].visibility >= vis_threshold]
    if not visible:
        return None
    xs = [lm.x * img_w for lm in visible]
    ys = [lm.y * img_h for lm in visible]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def detect_pose(image_bytes: bytes) -> list[Detection]:
    """Run MediaPipe pose landmark detection and return body-zone Detections.

    Returns up to two Detections per the first detected person:
    ``POSE_TORSO`` (shoulders→hips) and ``POSE_LOWER_BODY`` (hips→knees).
    Returns [] if no pose is detected or required landmarks lack visibility.

    mediapipe, PIL, and numpy are imported lazily so this module is safe to
    import without those packages installed.
    """
    import io  # noqa: PLC0415

    import mediapipe as mp  # type: ignore[import-untyped]  # noqa: PLC0415
    import numpy as np  # type: ignore[import-untyped]  # noqa: PLC0415
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415
    from mediapipe.tasks.python.core.base_options import BaseOptions  # noqa: PLC0415
    from mediapipe.tasks.python.vision import (  # noqa: PLC0415
        PoseLandmarker,
        PoseLandmarkerOptions,
    )

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_w, img_h = img.size
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.array(img))

    options = PoseLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(_ensure_pose_model())),
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
    )
    with PoseLandmarker.create_from_options(options) as detector:
        result = detector.detect(mp_image)

    if not result.pose_landmarks:
        log.info("pose: no landmarks detected")
        return []

    raw_lms = result.pose_landmarks[0]
    lms = [_Lm(x=lm.x, y=lm.y, visibility=lm.visibility or 0.0) for lm in raw_lms]

    detections: list[Detection] = []

    # Lower visibility threshold (0.3) so partial-body shots — common for NSFW
    # close-ups — still produce torso/lower-body boxes.
    torso = landmarks_to_torso_box(lms, img_w, img_h, vis_threshold=0.3)
    if torso is not None:
        detections.append(Detection(label="POSE_TORSO", score=0.7, box=torso))

    lower = landmarks_to_lower_body_box(lms, img_w, img_h, vis_threshold=0.3)
    if lower is not None:
        detections.append(Detection(label="POSE_LOWER_BODY", score=0.7, box=lower))

    if not detections:
        vis_summary = {
            "L_SHOULDER": round(lms[_L_SHOULDER].visibility, 2),
            "R_SHOULDER": round(lms[_R_SHOULDER].visibility, 2),
            "L_HIP": round(lms[_L_HIP].visibility, 2),
            "R_HIP": round(lms[_R_HIP].visibility, 2),
            "L_KNEE": round(lms[_L_KNEE].visibility, 2),
            "R_KNEE": round(lms[_R_KNEE].visibility, 2),
        }
        log.info("pose: landmarks present but no torso/lower-body box (vis<0.3): %s", vis_summary)
    else:
        log.info("pose detections: %s", [(d.label, round(d.score, 2)) for d in detections])
    return detections
