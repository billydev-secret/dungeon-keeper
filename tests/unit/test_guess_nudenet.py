"""Unit tests for services/guess_nudenet.py — mocked NudeNet."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot_modules.services.guess_models import BoundingBox, Detection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_nudenet_result(class_: str, score: float, x: int, y: int, w: int, h: int) -> dict:
    """Build a raw NudeNet result dict."""
    return {"class": class_, "score": score, "box": [x, y, w, h]}


def _mock_nudenet(raw_results: list[dict]):
    """Return a context manager that patches nudenet.NudeDetector."""
    mock_detector_instance = MagicMock()
    mock_detector_instance.detect.return_value = raw_results

    mock_detector_cls = MagicMock(return_value=mock_detector_instance)
    mock_nudenet_module = MagicMock()
    mock_nudenet_module.NudeDetector = mock_detector_cls

    return patch.dict("sys.modules", {"nudenet": mock_nudenet_module})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detect_returns_detection_objects():
    """detect() should return a list of Detection instances."""
    raw = [_make_nudenet_result("BREAST_EXPOSED", 0.87, 120, 80, 60, 90)]
    with _mock_nudenet(raw):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect("/fake/image.jpg")

    assert len(result) == 1
    assert isinstance(result[0], Detection)


def test_detect_converts_box_coords():
    """Box [x, y, w, h] must be converted to BoundingBox(x, y, x+w, y+h)."""
    x, y, w, h = 120, 80, 60, 90
    raw = [_make_nudenet_result("BREAST_EXPOSED", 0.87, x, y, w, h)]
    with _mock_nudenet(raw):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect("/fake/image.jpg")

    box = result[0].box
    assert isinstance(box, BoundingBox)
    assert box.x1 == x
    assert box.y1 == y
    assert box.x2 == x + w
    assert box.y2 == y + h


def test_detect_passes_label_and_score_through():
    """label and score must be passed through unchanged."""
    raw = [_make_nudenet_result("BUTTOCKS_EXPOSED", 0.55, 0, 0, 10, 10)]
    with _mock_nudenet(raw):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect("/fake/image.jpg")

    assert result[0].label == "BUTTOCKS_EXPOSED"
    assert result[0].score == pytest.approx(0.55)


def test_detect_empty_results_returns_empty_list():
    """When NudeNet returns no detections, detect() returns []."""
    with _mock_nudenet([]):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect("/fake/image.jpg")

    assert result == []


def test_detect_accepts_path_object():
    """detect() should accept a pathlib.Path, not just str."""
    raw = [_make_nudenet_result("FACE_FEMALE", 0.99, 10, 20, 30, 40)]
    with _mock_nudenet(raw):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect(Path("/fake/image.jpg"))

    assert len(result) == 1


def test_detect_multiple_detections():
    """detect() should handle multiple detections correctly."""
    raw = [
        _make_nudenet_result("BREAST_EXPOSED", 0.90, 10, 20, 50, 60),
        _make_nudenet_result("BELLY_EXPOSED", 0.70, 100, 200, 80, 40),
    ]
    with _mock_nudenet(raw):
        from bot_modules.services.guess_nudenet import detect  # noqa: PLC0415
        result = detect("/fake/image.jpg")

    assert len(result) == 2
    assert result[0].label == "BREAST_EXPOSED"
    assert result[0].box == BoundingBox(10, 20, 60, 80)
    assert result[1].label == "BELLY_EXPOSED"
    assert result[1].box == BoundingBox(100, 200, 180, 240)
