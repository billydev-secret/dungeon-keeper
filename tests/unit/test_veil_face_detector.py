"""Unit tests for services/veil_face_detector.py — mocked mediapipe/PIL/numpy."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services.veil_models import BoundingBox


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_relative_bbox(xmin: float, ymin: float, width: float, height: float) -> MagicMock:
    """Build a mock mediapipe relative_bounding_box."""
    bbox = MagicMock()
    bbox.xmin = xmin
    bbox.ymin = ymin
    bbox.width = width
    bbox.height = height
    return bbox


def _make_detection(xmin: float, ymin: float, width: float, height: float) -> MagicMock:
    """Build a mock mediapipe Detection object."""
    det = MagicMock()
    det.location_data.relative_bounding_box = _make_relative_bbox(xmin, ymin, width, height)
    return det


def _make_mocks(
    detections: list[MagicMock] | None,
    img_w: int = 800,
    img_h: int = 600,
) -> tuple[dict, MagicMock, MagicMock]:
    """
    Build sys.modules patches for mediapipe, numpy, PIL, and PIL.Image.

    Returns (patch_dict, mock_pil_image_instance, mock_detector_instance).
    """
    # --- PIL mock ---
    mock_img_instance = MagicMock()
    mock_img_instance.size = (img_w, img_h)

    mock_pil_image = MagicMock()
    mock_pil_image.open.return_value.__enter__ = lambda s: s
    mock_pil_image.open.return_value.__exit__ = MagicMock(return_value=False)
    mock_pil_image.open.return_value.convert.return_value = mock_img_instance

    mock_pil = MagicMock()
    mock_pil.Image = mock_pil_image

    # --- numpy mock ---
    mock_np = MagicMock()
    mock_arr = MagicMock()
    mock_np.array.return_value = mock_arr

    # --- mediapipe mock ---
    mock_result = MagicMock()
    mock_result.detections = detections

    mock_detector_instance = MagicMock()
    mock_detector_instance.process.return_value = mock_result
    # Support context manager
    mock_detector_instance.__enter__ = MagicMock(return_value=mock_detector_instance)
    mock_detector_instance.__exit__ = MagicMock(return_value=False)

    mock_face_detection = MagicMock()
    mock_face_detection.FaceDetection.return_value = mock_detector_instance

    mock_mp = MagicMock()
    mock_mp.solutions.face_detection = mock_face_detection

    patch_dict = {
        "mediapipe": mock_mp,
        "numpy": mock_np,
        "PIL": mock_pil,
        "PIL.Image": mock_pil_image,
    }
    return patch_dict, mock_pil_image, mock_detector_instance


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detect_faces_one_face_returns_one_bounding_box():
    """detect_faces() with one face returns a list of one BoundingBox."""
    img_w, img_h = 800, 600
    det = _make_detection(xmin=0.1, ymin=0.2, width=0.25, height=0.3)
    patches, _, _ = _make_mocks([det], img_w=img_w, img_h=img_h)

    with patch.dict("sys.modules", patches):
        from services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake_image_bytes")

    assert len(result) == 1
    assert isinstance(result[0], BoundingBox)


def test_detect_faces_coords_denormalized_correctly():
    """Normalized coords must be multiplied by image dimensions."""
    img_w, img_h = 800, 600
    xmin, ymin, w_norm, h_norm = 0.1, 0.2, 0.25, 0.3
    det = _make_detection(xmin=xmin, ymin=ymin, width=w_norm, height=h_norm)
    patches, _, _ = _make_mocks([det], img_w=img_w, img_h=img_h)

    with patch.dict("sys.modules", patches):
        from services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake_image_bytes")

    box = result[0]
    assert box.x1 == pytest.approx(xmin * img_w)
    assert box.y1 == pytest.approx(ymin * img_h)
    assert box.x2 == pytest.approx((xmin + w_norm) * img_w)
    assert box.y2 == pytest.approx((ymin + h_norm) * img_h)


def test_detect_faces_none_detections_returns_empty():
    """When result.detections is None, detect_faces() returns []."""
    patches, _, _ = _make_mocks(None)

    with patch.dict("sys.modules", patches):
        from services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake_image_bytes")

    assert result == []


def test_detect_faces_empty_detections_returns_empty():
    """When result.detections is an empty list, detect_faces() returns []."""
    patches, _, _ = _make_mocks([])

    with patch.dict("sys.modules", patches):
        from services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake_image_bytes")

    assert result == []


def test_detect_faces_multiple_faces():
    """detect_faces() correctly handles multiple face detections."""
    img_w, img_h = 1000, 500
    det1 = _make_detection(xmin=0.05, ymin=0.1, width=0.2, height=0.4)
    det2 = _make_detection(xmin=0.6, ymin=0.05, width=0.15, height=0.35)
    patches, _, _ = _make_mocks([det1, det2], img_w=img_w, img_h=img_h)

    with patch.dict("sys.modules", patches):
        from services.veil_face_detector import detect_faces  # noqa: PLC0415
        result = detect_faces(b"fake_image_bytes")

    assert len(result) == 2
    assert result[0].x1 == pytest.approx(0.05 * img_w)
    assert result[1].x1 == pytest.approx(0.6 * img_w)
