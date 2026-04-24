"""Tests for message_xp_service.split_award_into_text_and_reply."""

from __future__ import annotations

import pytest

from services.message_xp_service import split_award_into_text_and_reply


# ── no reply bonus ────────────────────────────────────────────────────


def test_no_reply_bonus_all_text():
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=0.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 10.0
    assert reply == 0.0


def test_negative_reply_bonus_treated_as_none():
    # Defensive: breakdown should never have negative reply_bonus_xp, but
    # if it did we'd still want the split to stay consistent.
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=-5.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 10.0
    assert reply == 0.0


def test_zero_total_zero_result():
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=0.0,
        reply_bonus_xp=0.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert text == 0.0
    assert reply == 0.0


# ── with reply bonus, no multiplier penalties ────────────────────────


def test_reply_bonus_at_full_multipliers():
    # Total was 10 (8 text + 2 reply), no penalties
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=10.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 2.0
    assert text == 8.0


# ── with multipliers applied ──────────────────────────────────────────


def test_reply_scaled_by_cooldown():
    # reply_bonus=2.0, cooldown=0.5 → reply_award=1.0
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=5.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=0.5,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 1.0
    assert text == 4.0


def test_reply_scaled_by_all_multipliers():
    # reply_bonus=4.0, cooldown=0.5, duplicate=0.5, pair=0.5 → reply=0.5
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=3.0,
        reply_bonus_xp=4.0,
        cooldown_multiplier=0.5,
        duplicate_multiplier=0.5,
        pair_multiplier=0.5,
    )
    assert reply == 0.5
    assert text == 2.5


def test_zero_multiplier_kills_reply_award():
    # Duplicate multiplier of 0 means duplicate message → no reply XP
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=5.0,
        reply_bonus_xp=2.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=0.0,
        pair_multiplier=1.0,
    )
    assert reply == 0.0
    assert text == 5.0


# ── rounding and floor behavior ──────────────────────────────────────


def test_results_rounded_to_two_decimals():
    # 1/3 = 0.333... → rounded
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=1.0,
        reply_bonus_xp=1.0,
        cooldown_multiplier=1 / 3,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 0.33
    assert text == 0.67


def test_text_award_floored_at_zero():
    # If reply_award somehow exceeds total (rounding quirk / weird breakdown),
    # text should floor at 0, never go negative.
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=1.0,
        reply_bonus_xp=5.0,
        cooldown_multiplier=1.0,
        duplicate_multiplier=1.0,
        pair_multiplier=1.0,
    )
    assert reply == 5.0
    assert text == 0.0
    assert text >= 0


# ── invariant: split always sums close to total ──────────────────────


@pytest.mark.parametrize(
    "total,reply_bonus,cd,dup,pair",
    [
        (10.0, 2.0, 1.0, 1.0, 1.0),
        (5.0, 2.0, 0.5, 1.0, 1.0),
        (8.5, 3.0, 0.75, 0.9, 0.8),
        (100.0, 20.0, 1.0, 1.0, 1.0),
    ],
)
def test_sum_within_rounding_of_total(total, reply_bonus, cd, dup, pair):
    text, reply = split_award_into_text_and_reply(
        total_awarded_xp=total,
        reply_bonus_xp=reply_bonus,
        cooldown_multiplier=cd,
        duplicate_multiplier=dup,
        pair_multiplier=pair,
    )
    # Sum matches total within a couple of cents (2x rounding tolerance)
    assert abs((text + reply) - total) <= 0.02
