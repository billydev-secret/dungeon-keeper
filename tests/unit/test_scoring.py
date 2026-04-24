"""Tier 1 unit tests: scoring math (spec §9.3)."""

from hypothesis import given, settings, strategies as st

from scoring import clamp_score, compute_score_from_components
from xp_system import (
    cooldown_multiplier,
    level_for_xp,
    xp_required_for_level,
)


# ── compute_score_from_components ─────────────────────────────────────

def test_weights_sum_to_one():
    """All components at 100 should produce exactly 100."""
    assert compute_score_from_components(100, 100, 100, 100) == 100.0


def test_all_zero_produces_zero():
    assert compute_score_from_components(0, 0, 0, 0) == 0.0


def test_engagement_dominates_activity():
    """engagement weight (0.40) > activity weight (0.15)."""
    high_engagement = compute_score_from_components(100, 0, 0, 0)
    high_activity = compute_score_from_components(0, 0, 0, 100)
    assert high_engagement > high_activity


def test_individual_weights():
    assert compute_score_from_components(100, 0, 0, 0) == pytest.approx(40.0)
    assert compute_score_from_components(0, 100, 0, 0) == pytest.approx(25.0)
    assert compute_score_from_components(0, 0, 100, 0) == pytest.approx(20.0)
    assert compute_score_from_components(0, 0, 0, 100) == pytest.approx(15.0)


@given(
    e=st.floats(0, 100, allow_nan=False),
    c=st.floats(0, 100, allow_nan=False),
    r=st.floats(0, 100, allow_nan=False),
    a=st.floats(0, 100, allow_nan=False),
)
@settings(max_examples=500)
def test_score_bounded(e, c, r, a):
    result = compute_score_from_components(e, c, r, a)
    assert 0 <= result <= 100


# ── clamp_score ───────────────────────────────────────────────────────

def test_clamp_within_range():
    assert clamp_score(50) == 50.0


def test_clamp_below_zero():
    assert clamp_score(-10) == 0.0


def test_clamp_above_100():
    assert clamp_score(150) == 100.0


# ── XP math ───────────────────────────────────────────────────────────

def test_level_for_xp_starts_at_one():
    assert level_for_xp(0) == 1


def test_xp_required_for_level_increases():
    """Higher levels require more XP."""
    assert xp_required_for_level(2) < xp_required_for_level(3)
    assert xp_required_for_level(5) < xp_required_for_level(10)


def test_level_for_xp_round_trip():
    """level_for_xp(xp_required_for_level(n)) == n."""
    for level in range(1, 11):
        xp = xp_required_for_level(level)
        assert level_for_xp(xp) == level


def test_cooldown_multiplier_returns_float():
    from xp_system import DEFAULT_XP_SETTINGS

    result = cooldown_multiplier(seconds_since_last_message=0, settings=DEFAULT_XP_SETTINGS)
    assert isinstance(result, float)
    assert 0.0 <= result <= 1.0


# Need pytest for approx
import pytest  # noqa: E402 (must come after hypothesis imports)
