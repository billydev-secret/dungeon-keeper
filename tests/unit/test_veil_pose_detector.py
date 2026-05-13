"""Tier 1 unit tests: veil pose detector pure geometry helpers.

No images, no mediapipe. Only veil_models + veil_pose_detector imports.
"""
from __future__ import annotations

import pytest

from bot_modules.services.veil_pose_detector import (
    _Lm,
    landmarks_to_lower_body_box,
    landmarks_to_torso_box,
)


def _lms(visible: dict[int, tuple[float, float]], total: int = 33) -> list[_Lm]:
    """33-landmark list where indices in *visible* have visibility=1.0."""
    lms = [_Lm(x=0.5, y=0.5, visibility=0.0) for _ in range(total)]
    for idx, (x, y) in visible.items():
        lms[idx] = _Lm(x=x, y=y, visibility=1.0)
    return lms


# ── landmarks_to_torso_box ────────────────────────────────────────────────────

def test_torso_box_uses_all_four_landmarks():
    lms = _lms({11: (0.4, 0.2), 12: (0.6, 0.2), 23: (0.4, 0.6), 24: (0.6, 0.6)})
    r = landmarks_to_torso_box(lms, 100, 100)
    assert r is not None
    assert r.x1 == pytest.approx(40.0)
    assert r.y1 == pytest.approx(20.0)
    assert r.x2 == pytest.approx(60.0)
    assert r.y2 == pytest.approx(60.0)


def test_torso_box_returns_none_when_all_invisible():
    lms = [_Lm(x=0.5, y=0.5, visibility=0.0)] * 33
    assert landmarks_to_torso_box(lms, 100, 100) is None


def test_torso_box_partial_visibility_uses_visible_only():
    # Only right shoulder (12) and right hip (24) visible
    lms = _lms({12: (0.6, 0.2), 24: (0.6, 0.6)})
    r = landmarks_to_torso_box(lms, 100, 100)
    assert r is not None
    assert r.x1 == pytest.approx(60.0)
    assert r.y1 == pytest.approx(20.0)
    assert r.x2 == pytest.approx(60.0)
    assert r.y2 == pytest.approx(60.0)


def test_torso_box_visibility_threshold_excludes_low_vis():
    lms = _lms({11: (0.4, 0.2), 12: (0.6, 0.2)})
    lms[23] = _Lm(x=0.4, y=0.6, visibility=0.3)
    lms[24] = _Lm(x=0.6, y=0.6, visibility=0.3)
    r = landmarks_to_torso_box(lms, 100, 100)
    assert r is not None
    # Only shoulders visible — y should not extend down to hip level
    assert r.y2 == pytest.approx(20.0)


def test_torso_box_custom_vis_threshold():
    lms = _lms({11: (0.4, 0.2), 12: (0.6, 0.2)})
    lms[23] = _Lm(x=0.4, y=0.6, visibility=0.3)
    # With threshold=0.2, visibility=0.3 hip should be included
    r = landmarks_to_torso_box(lms, 100, 100, vis_threshold=0.2)
    assert r is not None
    assert r.y2 == pytest.approx(60.0)


# ── landmarks_to_lower_body_box ───────────────────────────────────────────────

def test_lower_body_box_uses_hips_and_knees():
    lms = _lms({23: (0.4, 0.5), 24: (0.6, 0.5), 25: (0.4, 0.8), 26: (0.6, 0.8)})
    r = landmarks_to_lower_body_box(lms, 100, 100)
    assert r is not None
    assert r.x1 == pytest.approx(40.0)
    assert r.y1 == pytest.approx(50.0)
    assert r.x2 == pytest.approx(60.0)
    assert r.y2 == pytest.approx(80.0)


def test_lower_body_box_returns_none_when_all_invisible():
    lms = [_Lm(x=0.5, y=0.5, visibility=0.0)] * 33
    assert landmarks_to_lower_body_box(lms, 100, 100) is None


def test_lower_body_box_only_knees_visible():
    lms = _lms({25: (0.4, 0.8), 26: (0.6, 0.8)})
    r = landmarks_to_lower_body_box(lms, 100, 100)
    assert r is not None
    assert r.y1 == pytest.approx(80.0)
    assert r.y2 == pytest.approx(80.0)


def test_lower_body_box_visibility_threshold_excludes_low_vis():
    lms = _lms({23: (0.4, 0.5), 24: (0.6, 0.5)})
    lms[25] = _Lm(x=0.4, y=0.8, visibility=0.2)
    lms[26] = _Lm(x=0.6, y=0.8, visibility=0.2)
    r = landmarks_to_lower_body_box(lms, 100, 100)
    assert r is not None
    # Only hips visible — should not extend down to knee level
    assert r.y2 == pytest.approx(50.0)
