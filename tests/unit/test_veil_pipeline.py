"""Tier 1 unit tests: veil pipeline pure geometry helpers.

No images, no models, no PIL. Only veil_models + veil_pipeline imports.
"""
from __future__ import annotations

import random

import pytest

from services.veil_models import BoundingBox, Detection
from services.veil_pipeline import (
    LOW_INTEREST_WEIGHT,
    apply_label_weights,
    compute_padded_crop,
    enforce_min_size,
    filter_candidates,
    iou,
)


def _bb(x1, y1, x2, y2) -> BoundingBox:
    return BoundingBox(float(x1), float(y1), float(x2), float(y2))


def _det(label, score, x1, y1, x2, y2) -> Detection:
    return Detection(label=label, score=score, box=_bb(x1, y1, x2, y2))


# ── iou ───────────────────────────────────────────────────────────────────────

def test_iou_identical_boxes_is_one():
    box = _bb(0, 0, 10, 10)
    assert iou(box, box) == pytest.approx(1.0)


def test_iou_non_overlapping_is_zero():
    assert iou(_bb(0, 0, 10, 10), _bb(20, 20, 30, 30)) == 0.0


def test_iou_partial_overlap():
    # a=[0,0,10,10] area=100; b=[5,5,15,15] area=100; overlap=5x5=25; union=175
    assert iou(_bb(0, 0, 10, 10), _bb(5, 5, 15, 15)) == pytest.approx(25 / 175, rel=1e-5)


def test_iou_a_inside_b():
    # a area=36, b area=100, intersection=36, union=100
    assert iou(_bb(2, 2, 8, 8), _bb(0, 0, 10, 10)) == pytest.approx(36 / 100, rel=1e-5)


def test_iou_zero_area_box_returns_zero():
    assert iou(_bb(5, 5, 5, 5), _bb(0, 0, 10, 10)) == 0.0


def test_iou_touching_edge_is_zero():
    assert iou(_bb(0, 0, 10, 10), _bb(10, 0, 20, 10)) == 0.0


# ── compute_padded_crop ───────────────────────────────────────────────────────

def test_padded_crop_medium_adds_25_percent():
    # bbox 100x100, medium=25%, pad=25 each side
    bb = _bb(100, 100, 200, 200)
    r = compute_padded_crop(bb, "medium", 500, 500)
    assert r.x1 == pytest.approx(75.0)
    assert r.y1 == pytest.approx(75.0)
    assert r.x2 == pytest.approx(225.0)
    assert r.y2 == pytest.approx(225.0)


def test_padded_crop_clamps_to_zero():
    # easy pad = 0.60 * 50 = 30 > origin offset of 5 → x1 would be -25 without clamp
    r = compute_padded_crop(_bb(5, 5, 55, 55), "easy", 100, 100)
    assert r.x1 == pytest.approx(0.0)
    assert r.y1 == pytest.approx(0.0)


def test_padded_crop_clamps_to_image_edge():
    # bbox near bottom-right; easy pad would exceed 500px
    r = compute_padded_crop(_bb(450, 450, 490, 490), "easy", 500, 500)
    assert r.x2 <= 500.0
    assert r.y2 <= 500.0


def test_padded_crop_unknown_difficulty_raises():
    with pytest.raises(ValueError, match="Unknown difficulty"):
        compute_padded_crop(_bb(0, 0, 100, 100), "ultra", 500, 500)


def test_padded_crop_jitter_shifts_position():
    bb = _bb(100, 100, 200, 200)
    no_jitter = compute_padded_crop(bb, "medium", 500, 500)
    with_jitter = compute_padded_crop(bb, "medium", 500, 500, rng=random.Random(42))
    assert (with_jitter.x1, with_jitter.y1) != (no_jitter.x1, no_jitter.y1)


def test_padded_crop_jitter_detection_stays_inside():
    bb = _bb(100, 100, 200, 200)
    for seed in range(20):
        r = compute_padded_crop(bb, "medium", 500, 500, rng=random.Random(seed))
        assert r.x1 <= bb.x1
        assert r.y1 <= bb.y1
        assert r.x2 >= bb.x2
        assert r.y2 >= bb.y2


@pytest.mark.parametrize("difficulty", ["easy", "medium", "hard"])
def test_padded_crop_all_difficulties_produce_valid_box(difficulty):
    r = compute_padded_crop(_bb(100, 100, 200, 200), difficulty, 500, 500)
    assert r.x1 < r.x2
    assert r.y1 < r.y2


# ── filter_candidates ─────────────────────────────────────────────────────────

def test_filter_no_faces_returns_all():
    dets = [_det("BREAST", 0.9, 0, 0, 10, 10)]
    assert filter_candidates(dets, face_boxes=[]) == dets


def test_filter_full_face_overlap_drops_candidate():
    face = _bb(0, 0, 100, 100)
    det = _det("BREAST", 0.9, 0, 0, 100, 100)
    assert filter_candidates([det], face_boxes=[face], fallback=False) == []


def test_filter_partial_overlap_above_threshold_dropped():
    # iou ~0.14 > 0.1 threshold
    face = _bb(5, 5, 15, 15)
    det = _det("BREAST", 0.9, 0, 0, 10, 10)
    assert filter_candidates([det], face_boxes=[face], iou_threshold=0.1, fallback=False) == []


def test_filter_tiny_overlap_below_threshold_kept():
    # iou ~1/220 << 0.1 → keep
    face = _bb(9, 9, 20, 20)
    det = _det("BREAST", 0.9, 0, 0, 10, 10)
    result = filter_candidates([det], face_boxes=[face], iou_threshold=0.1, fallback=False)
    assert len(result) == 1


def test_filter_fallback_returns_highest_score():
    face = _bb(0, 0, 200, 200)
    det1 = _det("BREAST", 0.9, 0, 0, 100, 100)
    det2 = _det("GENITALIA", 0.6, 0, 0, 80, 80)
    result = filter_candidates([det1, det2], face_boxes=[face], fallback=True)
    assert len(result) == 1
    assert result[0].score == 0.9


def test_filter_fallback_false_empty_when_all_dropped():
    face = _bb(0, 0, 200, 200)
    det = _det("BREAST", 0.9, 0, 0, 100, 100)
    assert filter_candidates([det], face_boxes=[face], fallback=False) == []


# ── apply_label_weights ───────────────────────────────────────────────────────

def test_apply_label_weights_penalises_armpits():
    armpit = _det("ARMPITS_EXPOSED", 0.8, 0, 0, 50, 50)
    breast = _det("FEMALE_BREAST_EXPOSED", 0.6, 60, 60, 110, 110)
    result = apply_label_weights([armpit, breast])
    assert result[0].score == pytest.approx(0.8 * LOW_INTEREST_WEIGHT)
    assert result[1].score == pytest.approx(0.6)
    # boxes preserved
    assert result[0].box == armpit.box


def test_apply_label_weights_passes_through_other_labels():
    dets = [_det("MALE_GENITALIA_EXPOSED", 0.7, 0, 0, 50, 50)]
    assert apply_label_weights(dets) == dets


# ── enforce_min_size ──────────────────────────────────────────────────────────

def test_enforce_min_size_large_box_unchanged():
    r = enforce_min_size(_bb(0, 0, 300, 300), min_px=200)
    assert r.width == pytest.approx(300.0)
    assert r.height == pytest.approx(300.0)


def test_enforce_min_size_small_box_expands_centered():
    # 50x50 centered at (75,75); should expand to 200x200 keeping center
    r = enforce_min_size(_bb(50, 50, 100, 100), min_px=200)
    assert r.width == pytest.approx(200.0)
    assert r.height == pytest.approx(200.0)
    assert (r.x1 + r.x2) / 2 == pytest.approx(75.0)
    assert (r.y1 + r.y2) / 2 == pytest.approx(75.0)


def test_enforce_min_size_wide_short_expands_height_only():
    # 300 wide, 50 tall — width stays 300, height expands to 200
    r = enforce_min_size(_bb(0, 0, 300, 50), min_px=200)
    assert r.width == pytest.approx(300.0)
    assert r.height == pytest.approx(200.0)


def test_enforce_min_size_default_min_is_200():
    r = enforce_min_size(_bb(0, 0, 100, 100))
    assert r.width == pytest.approx(200.0)


# ── full-image fallback ───────────────────────────────────────────────────────

def test_full_image_fallback_passes_filter():
    # Simulates the fallback Detection created when NudeNet finds nothing.
    full = _det("FULL_IMAGE_FALLBACK", 0.0, 0, 0, 500, 500)
    result = filter_candidates([full], face_boxes=[])
    assert len(result) == 1


def test_full_image_fallback_produces_valid_padded_crop():
    # hard difficulty (5% pad) on 500x500 full-image bbox stays within bounds.
    r = compute_padded_crop(_bb(0, 0, 500, 500), "hard", 500, 500)
    assert r.x1 >= 0.0
    assert r.y1 >= 0.0
    assert r.x2 <= 500.0
    assert r.y2 <= 500.0
    assert r.x1 < r.x2
    assert r.y1 < r.y2
