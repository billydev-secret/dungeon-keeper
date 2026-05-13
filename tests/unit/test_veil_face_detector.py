"""Unit tests for services/veil_face_detector.py — mocked mediapipe/PIL/numpy."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot_modules.services.veil_models import BoundingBox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pixel_bbox(origin_x: int, origin_y: int, width: int, height: int) -> MagicMock:
    """Build a mock Tasks API bounding_box (pixel coordinates)."""
    bb = MagicMock()
    bb.origin_x = origin_x
    bb.origin_y = origin_y
    bb.width = width
    bb.height = height
    return bb


def _make_detection(origin_x: int, origin_y: int, width: int, height: int) -> MagicMock:
    """Build a mock Tasks API Detection object."""
    det = MagicMock()
    det.bounding_box = _make_pixel_bbox(origin_x, origin_y, width, height)
    return det


def _make_mocks(
    detections: list[MagicMock] | None,
    img_w: int = 800,
    img_h: int = 600,
) -> dict:
    """Build sys.modules patches for mediapipe (Tasks API), numpy, and PIL."""
    # PIL
    mock_img = MagicMock()
    mock_img.size = (img_w, img_h)
    mock_PIL_Image = MagicMock()
    mock_PIL_Image.open.return_value.convert.return_value = mock_img
    mock_PIL = MagicMock()
    mock_PIL.Image = mock_PIL_Image

    # numpy
    mock_np = MagicMock()

    # mediapipe — Tasks API
    mock_result = MagicMock()
    mock_result.detections = detections

    mock_detector = MagicMock()
    mock_detector.__enter__ = MagicMock(return_value=mock_detector)
    mock_detector.__exit__ = MagicMock(return_value=False)
    mock_detector.detect.return_value = mock_result

    mock_FaceDetector = MagicMock()
    mock_FaceDetector.create_from_options.return_value = mock_detector

    mock_vision = MagicMock()
    mock_vision.FaceDetector = mock_FaceDetector
    mock_vision.FaceDetectorOptions = MagicMock()

    mock_mp = MagicMock()
    mock_mp.ImageFormat.SRGB = "SRGB"

    return {
        "mediapipe": mock_mp,
        "mediapipe.tasks": MagicMock(),
        "mediapipe.tasks.python": MagicMock(),
        "mediapipe.tasks.python.core": MagicMock(),
        "mediapipe.tasks.python.core.base_options": MagicMock(),
        "mediapipe.tasks.python.vision": mock_vision,
        "numpy": mock_np,
        "PIL": mock_PIL,
        "PIL.Image": mock_PIL_Image,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detect_faces_one_face_returns_one_bounding_box():
    det = _make_detection(origin_x=80, origin_y=120, width=200, height=180)
    patches = _make_mocks([det])

    with patch.dict("sys.modules", patches), \
         patch("bot_modules.services.veil_face_detector._ensure_face_model", return_value=Path("/fake/model.tflite")):
        from bot_modules.services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake")

    assert len(result) == 1
    assert isinstance(result[0], BoundingBox)


def test_detect_faces_pixel_coords_used_directly():
    """Tasks API returns pixel coords; no de-normalisation needed."""
    det = _make_detection(origin_x=80, origin_y=120, width=200, height=180)
    patches = _make_mocks([det])

    with patch.dict("sys.modules", patches), \
         patch("bot_modules.services.veil_face_detector._ensure_face_model", return_value=Path("/fake/model.tflite")):
        from bot_modules.services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake")

    box = result[0]
    assert box.x1 == pytest.approx(80.0)
    assert box.y1 == pytest.approx(120.0)
    assert box.x2 == pytest.approx(280.0)   # 80 + 200
    assert box.y2 == pytest.approx(300.0)   # 120 + 180


def test_detect_faces_none_detections_returns_empty():
    patches = _make_mocks(None)

    with patch.dict("sys.modules", patches), \
         patch("bot_modules.services.veil_face_detector._ensure_face_model", return_value=Path("/fake/model.tflite")):
        from bot_modules.services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake")

    assert result == []


def test_detect_faces_empty_detections_returns_empty():
    patches = _make_mocks([])

    with patch.dict("sys.modules", patches), \
         patch("bot_modules.services.veil_face_detector._ensure_face_model", return_value=Path("/fake/model.tflite")):
        from bot_modules.services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake")

    assert result == []


def test_detect_faces_multiple_faces():
    det1 = _make_detection(origin_x=50, origin_y=100, width=200, height=400)
    det2 = _make_detection(origin_x=600, origin_y=50, width=150, height=350)
    patches = _make_mocks([det1, det2])

    with patch.dict("sys.modules", patches), \
         patch("bot_modules.services.veil_face_detector._ensure_face_model", return_value=Path("/fake/model.tflite")):
        from bot_modules.services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake")

    assert len(result) == 2
    assert result[0].x1 == pytest.approx(50.0)
    assert result[1].x1 == pytest.approx(600.0)
