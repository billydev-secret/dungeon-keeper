"""Tier 2 component tests: veil model structure."""
from __future__ import annotations

import time

from services.veil_models import (
    BoundingBox,
    Detection,
    PipelineResult,
    VeilConfig,
    VeilRound,
)


def test_bounding_box_area_zero_when_degenerate():
    bb = BoundingBox(10.0, 10.0, 10.0, 10.0)
    assert bb.area == 0.0


def test_bounding_box_area_positive():
    bb = BoundingBox(0.0, 0.0, 100.0, 50.0)
    assert bb.area == 5000.0


def test_bounding_box_width_height():
    bb = BoundingBox(10.0, 20.0, 60.0, 80.0)
    assert bb.width == 50.0
    assert bb.height == 60.0


def test_detection_fields():
    bb = BoundingBox(0.0, 0.0, 10.0, 10.0)
    d = Detection(label="EXPOSED_BREAST_F", score=0.87, box=bb)
    assert d.label == "EXPOSED_BREAST_F"
    assert d.score == 0.87
    assert d.box is bb


def test_veil_config_defaults():
    cfg = VeilConfig(guild_id=9001)
    assert cfg.crop_difficulty == "medium"
    assert cfg.guess_cooldown_seconds == 30
    assert cfg.reuse_enabled is True


def test_pipeline_result_empty_crops_by_default():
    pr = PipelineResult(candidates=[])
    assert pr.crops == []


def test_veil_round_optional_fields_are_none():
    r = VeilRound(
        id=1, guild_id=9001, submitter_id=111, answer_id=222,
        channel_id=333, message_id=444, crop_path="", crop_url="",
        difficulty="medium", candidate_count=1, reroll_count=0,
        allow_reuse=False, is_reuse=False, original_round_id=None,
        reuse_blocked=False, created_at=time.time(), solved_at=None,
        solver_id=None, guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )
    assert r.solver_id is None
    assert r.deleted_at is None
