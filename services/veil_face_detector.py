"""Lazy mediapipe face detection wrapper for the Veil pipeline."""
from __future__ import annotations

from services.veil_models import BoundingBox


def detect_faces(image_bytes: bytes) -> list[BoundingBox]:
    """Run mediapipe face detection on *image_bytes* and return a list of BoundingBoxes.

    mediapipe, PIL, and numpy are imported lazily so this module is safe to
    import even when those packages are not installed.

    Coordinate conversion: mediapipe returns normalized coordinates (0.0–1.0
    relative to image dimensions).  We convert to absolute pixel coordinates:
        x1 = xmin * img_w
        y1 = ymin * img_h
        x2 = (xmin + width) * img_w
        y2 = (ymin + height) * img_h
    """
    import io  # noqa: PLC0415

    import mediapipe as mp  # type: ignore[import-untyped]  # noqa: PLC0415
    import numpy as np  # type: ignore[import-untyped]  # noqa: PLC0415
    from PIL import Image  # type: ignore[import-untyped]  # noqa: PLC0415

    # Determine image dimensions for de-normalisation.
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img_w, img_h = img.size
    arr = np.array(img)

    mp_face = mp.solutions.face_detection  # type: ignore[attr-defined]
    with mp_face.FaceDetection(model_selection=0, min_detection_confidence=0.5) as detector:
        result = detector.process(arr)

    if not result.detections:
        return []

    boxes: list[BoundingBox] = []
    for detection in result.detections:
        rbb = detection.location_data.relative_bounding_box
        x1 = rbb.xmin * img_w
        y1 = rbb.ymin * img_h
        x2 = (rbb.xmin + rbb.width) * img_w
        y2 = (rbb.ymin + rbb.height) * img_h
        boxes.append(BoundingBox(x1=x1, y1=y1, x2=x2, y2=y2))

    return boxes
