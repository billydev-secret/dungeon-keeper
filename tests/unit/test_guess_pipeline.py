"""Tier 1 unit tests: guess pipeline pure geometry helpers.

No images, no models, no PIL. Only guess_models + guess_pipeline imports.
"""
from __future__ import annotations

import random

import pytest

from bot_modules.services.guess_models import BoundingBox, Detection
from bot_modules.services.guess_pipeline import (
    HIGH_INTEREST_WEIGHT,
    LOW_INTEREST_WEIGHT,
    apply_label_weights,
    compute_padded_crop,
    enforce_min_size,
    filter_candidates,
    iou,
    move_crop_box,
    zoom_crop_box,
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
    pose = _det("POSE_TORSO", 0.6, 60, 60, 110, 110)
    result = apply_label_weights([armpit, pose])
    assert result[0].score == pytest.approx(0.8 * LOW_INTEREST_WEIGHT)
    assert result[1].score == pytest.approx(0.6)
    # boxes preserved
    assert result[0].box == armpit.box


def test_apply_label_weights_penalises_bellies():
    belly_e = _det("BELLY_EXPOSED", 0.9, 0, 0, 50, 50)
    belly_c = _det("BELLY_COVERED", 0.85, 60, 0, 110, 50)
    result = apply_label_weights([belly_e, belly_c])
    assert result[0].score == pytest.approx(0.9 * LOW_INTEREST_WEIGHT)
    assert result[1].score == pytest.approx(0.85 * LOW_INTEREST_WEIGHT)


def test_apply_label_weights_boosts_high_interest_labels():
    genital = _det("MALE_GENITALIA_EXPOSED", 0.4, 0, 0, 50, 50)
    breast = _det("FEMALE_BREAST_COVERED", 0.5, 60, 0, 110, 50)
    result = apply_label_weights([genital, breast])
    assert result[0].score == pytest.approx(0.4 * HIGH_INTEREST_WEIGHT)
    assert result[1].score == pytest.approx(0.5 * HIGH_INTEREST_WEIGHT)


def test_apply_label_weights_caps_high_interest_boost_at_one():
    """A confident high-interest detection mustn't exceed 1.0."""
    genital = _det("MALE_GENITALIA_EXPOSED", 0.9, 0, 0, 50, 50)
    result = apply_label_weights([genital])
    assert result[0].score == pytest.approx(1.0)


def test_apply_label_weights_outranks_belly_with_low_confidence_genital():
    """A confident belly (0.85 → 0.425) loses to a moderate genital (0.45 → 0.675)."""
    belly = _det("BELLY_EXPOSED", 0.85, 0, 0, 50, 50)
    genital = _det("MALE_GENITALIA_EXPOSED", 0.45, 60, 0, 110, 50)
    result = apply_label_weights([belly, genital])
    assert result[1].score > result[0].score


def test_apply_label_weights_passes_through_neutral_labels():
    """Labels that are neither low- nor high-interest pass through unchanged
    (e.g. pose-derived regions that already carry a sensible fixed score)."""
    pose = _det("POSE_TORSO", 0.7, 0, 0, 50, 50)
    feet = _det("FEET_EXPOSED", 0.55, 60, 0, 110, 50)
    result = apply_label_weights([pose, feet])
    assert result == [pose, feet]


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


# ── move_crop_box ─────────────────────────────────────────────────────────────

def test_move_crop_box_right_shifts_x():
    box = _bb(100, 100, 300, 300)
    result = move_crop_box(box, dx=50, dy=0, img_w=500, img_h=500)
    assert result.x1 == pytest.approx(150.0)
    assert result.x2 == pytest.approx(350.0)
    assert result.y1 == pytest.approx(100.0)


def test_move_crop_box_down_shifts_y():
    box = _bb(100, 100, 300, 300)
    result = move_crop_box(box, dx=0, dy=50, img_w=500, img_h=500)
    assert result.y1 == pytest.approx(150.0)
    assert result.y2 == pytest.approx(350.0)
    assert result.x1 == pytest.approx(100.0)


def test_move_crop_box_left_shifts_x():
    box = _bb(100, 100, 300, 300)
    result = move_crop_box(box, dx=-50, dy=0, img_w=500, img_h=500)
    assert result.x1 == pytest.approx(50.0)
    assert result.x2 == pytest.approx(250.0)


def test_move_crop_box_up_shifts_y():
    box = _bb(100, 100, 300, 300)
    result = move_crop_box(box, dx=0, dy=-50, img_w=500, img_h=500)
    assert result.y1 == pytest.approx(50.0)
    assert result.y2 == pytest.approx(250.0)


def test_move_crop_box_clamps_left_edge():
    box = _bb(10, 100, 200, 300)
    result = move_crop_box(box, dx=-50, dy=0, img_w=500, img_h=500)
    assert result.x1 == pytest.approx(0.0)
    assert result.width == pytest.approx(box.width)


def test_move_crop_box_clamps_top_edge():
    box = _bb(100, 10, 300, 200)
    result = move_crop_box(box, dx=0, dy=-50, img_w=500, img_h=500)
    assert result.y1 == pytest.approx(0.0)
    assert result.height == pytest.approx(box.height)


def test_move_crop_box_clamps_right_edge():
    # box 200px wide, x1=400 → x2=600 would exceed 500; should clamp x2=500
    box = _bb(400, 100, 600, 300)
    # Artificially construct: width=200, starts at 400
    box = _bb(350, 100, 450, 300)  # 100px wide
    result = move_crop_box(box, dx=200, dy=0, img_w=500, img_h=500)
    assert result.x2 == pytest.approx(500.0)
    assert result.width == pytest.approx(100.0)


def test_move_crop_box_clamps_bottom_edge():
    box = _bb(100, 350, 300, 450)  # 100px tall
    result = move_crop_box(box, dx=0, dy=200, img_w=500, img_h=500)
    assert result.y2 == pytest.approx(500.0)
    assert result.height == pytest.approx(100.0)


def test_move_crop_box_preserves_dimensions():
    box = _bb(100, 100, 300, 250)
    result = move_crop_box(box, dx=20, dy=-20, img_w=500, img_h=500)
    assert result.width == pytest.approx(box.width)
    assert result.height == pytest.approx(box.height)


# ── zoom_crop_box ─────────────────────────────────────────────────────────────

def test_zoom_crop_box_zoom_in_shrinks_box():
    box = _bb(50, 50, 450, 450)  # 400x400 — above min_px=200 so zoom-in takes effect
    result = zoom_crop_box(box, factor=0.8, img_w=500, img_h=500)
    assert result.width < box.width
    assert result.height < box.height


def test_zoom_crop_box_zoom_out_expands_box():
    box = _bb(100, 100, 300, 300)  # 200x200
    result = zoom_crop_box(box, factor=1.25, img_w=500, img_h=500)
    assert result.width > box.width
    assert result.height > box.height


def test_zoom_crop_box_preserves_center():
    box = _bb(100, 100, 300, 300)
    cx, cy = 200.0, 200.0
    result = zoom_crop_box(box, factor=0.8, img_w=500, img_h=500)
    assert (result.x1 + result.x2) / 2.0 == pytest.approx(cx)
    assert (result.y1 + result.y2) / 2.0 == pytest.approx(cy)


def test_zoom_crop_box_respects_min_px():
    box = _bb(200, 200, 220, 220)  # 20x20 — well below min_px
    result = zoom_crop_box(box, factor=0.5, img_w=500, img_h=500, min_px=200)
    assert result.width >= 200.0
    assert result.height >= 200.0


def test_zoom_crop_box_clamps_to_image_bounds():
    box = _bb(10, 10, 490, 490)  # already near full image
    result = zoom_crop_box(box, factor=2.0, img_w=500, img_h=500)
    assert result.x1 >= 0.0
    assert result.y1 >= 0.0
    assert result.x2 <= 500.0
    assert result.y2 <= 500.0
