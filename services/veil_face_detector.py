"""Lazy mediapipe face detection wrapper for the Veil pipeline."""
from __future__ import annotations

import logging
import urllib.request
from pathlib import Path

from services.veil_models import BoundingBox

log = logging.getLogger("dungeonkeeper.veil")

_FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
_MODELS_DIR = Path(__file__).parent.parent / "models"


def _ensure_face_model() -> Path:
    _MODELS_DIR.mkdir(exist_ok=True)
    model_path = _MODELS_DIR / "blaze_face_short_range.tflite"
    if not model_path.exists():
        log.info("downloading blaze_face_short_range.tflite…")
        tmp_path = model_path.with_suffix(model_path.suffix + ".tmp")
        try:
            urllib.request.urlretrieve(_FACE_MODEL_URL, tmp_path)
            tmp_path.replace(model_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    return model_path


def detect_faces(image_bytes: bytes) -> list[BoundingBox]:
    """Run mediapipe face detection on *image_bytes* and return BoundingBoxes.

    Uses the MediaPipe Tasks API (mediapipe>=0.10). Returns pixel-coordinate
    bounding boxes; the Tasks API reports these directly (no de-normalisation
    needed, unlike the deprecated mp.solutions API).

    Returns [] if no faces are found.
    """
    import io  # noqa: PLC0415

    import mediapipe as mp  # type: ignore[import-untyped]  # noqa: PLC0415
    import numpy as np  # type: ignore[import-untyped]  # noqa: PLC0415
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415
    from mediapipe.tasks.python.core.base_options import BaseOptions  # noqa: PLC0415
    from mediapipe.tasks.python.vision import FaceDetector, FaceDetectorOptions  # noqa: PLC0415

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.array(img))

    options = FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=str(_ensure_face_model())),
        running_mode=mp.tasks.vision.RunningMode.IMAGE,
    )
    with FaceDetector.create_from_options(options) as detector:
        result = detector.detect(mp_image)

    if not result.detections:
        return []

    boxes: list[BoundingBox] = []
    for det in result.detections:
        bb = det.bounding_box
        boxes.append(BoundingBox(
            x1=float(bb.origin_x),
            y1=float(bb.origin_y),
            x2=float(bb.origin_x + bb.width),
            y2=float(bb.origin_y + bb.height),
        ))
    return boxes
