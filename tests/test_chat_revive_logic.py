"""Tests for chat_revive/logic.py — bands, rhythm learning, the gate chain."""

from __future__ import annotations

import random
from datetime import datetime, timezone

from bot_modules.chat_revive.logic import (
    DAY_BAND,
    BandProfile,
    GateInputs,
    band_label,
    band_of,
    compute_band_profiles,
    decide,
    is_quiet_hours,
    pick_weighted,
    question_weight,
    render_revive,
    render_revive_caption,
    revive_succeeded,
    should_ping,
)


def _ts(day: int, hour: int, minute: int = 0) -> float:
    """Epoch for 2026-06-<day> <hour>:<minute> UTC (tests use offset 0)."""
    return datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp()


# --- bands & quiet hours -------------------------------------------------


def test_band_of_respects_offset():
    ts = _ts(1, 23)  # 23:00 UTC
    assert band_of(ts, 0) == 11
    assert band_of(ts, -7) == 8  # 16:00 local
    assert band_of(ts, 2) == 0  # 01:00 next local day


def test_band_label():
    assert band_label(9) == "18:00–20:00"
    assert band_label(DAY_BAND) == "all day"


def test_quiet_hours_plain_window():
    assert is_quiet_hours(3, 0, 8)
    assert not is_quiet_hours(8, 0, 8)
    assert not is_quiet_hours(12, 0, 8)


def test_quiet_hours_wraps_midnight():
    assert is_quiet_hours(23, 22, 6)
    assert is_quiet_hours(2, 22, 6)
    assert not is_quiet_hours(12, 22, 6)


def test_quiet_hours_disabled_when_equal():
    assert not is_quiet_hours(3, 0, 0)


# --- rhythm learning ------------------------------------------------------


def test_profiles_empty_history():
    assert compute_band_profiles([], now_ts=_ts(30, 12), offset_hours=0) == {}


def test_profiles_window_excludes_old_messages():
    now = _ts(30, 12)
    old = now - 70 * 86400
    profiles = compute_band_profiles([old], now_ts=now, offset_hours=0)
    assert profiles == {}


def test_profiles_band_attribution_and_day_fallback():
    # Three messages one evening: 18:05, 18:15, 20:05.
    stamps = [_ts(29, 18, 5), _ts(29, 18, 15), _ts(29, 20, 5)]
    profiles = compute_band_profiles(stamps, now_ts=_ts(29, 21), offset_hours=0)
    nine = profiles[9]
    # Both gaps start inside band 9 (the 18:15->20:05 lull belongs to band 9).
    # The 600s gap is intra-conversation and discarded; only the 6600s gap is a
    # between-conversation lull, so it alone sets the threshold.
    assert nine.session_count == 1
    assert nine.fire_threshold == 6600.0
    assert profiles[10].session_count == 0  # band 10's only msg has no gap after
    assert profiles[10].fire_threshold == 900.0  # floor when no sessions sampled
    day = profiles[DAY_BAND]
    assert day.session_count == 1
    assert day.fire_threshold == 6600.0
    assert day.msgs_per_day == 3.0  # observed window clamps to one day


def _evening_channel(days: int = 30) -> list[float]:
    """Active 18:00-21:59 daily: a 3-message burst (60s apart) every 30 min.

    Within-burst gaps are 60s (intra-conversation); between-burst gaps are
    ~28 min. So band 9's only between-conversation (>10 min) gaps are the four
    ~1680s ones each evening (18:02->18:30, 18:32->19:00, 19:02->19:30,
    19:32->20:00), which set its ~1680s fire threshold.
    """
    stamps: list[float] = []
    for day in range(1, days + 1):
        for hour in (18, 19, 20, 21):
            for half in (0, 30):
                base = _ts(day, hour, half)
                stamps.extend(base + i * 60 for i in range(3))
    return stamps


def test_profiles_evening_channel_rates():
    now = _ts(30, 22)
    profiles = compute_band_profiles(_evening_channel(), now_ts=now, offset_hours=0)
    assert profiles[9].fire_threshold == 1680.0
    assert profiles[9].msgs_per_day > 10
    assert 1 not in profiles  # 02:00-04:00 never saw a message


# --- the gate chain -------------------------------------------------------

NOW = _ts(30, 19)  # 19:00 local, band 9
PROFILES = compute_band_profiles(_evening_channel(29), now_ts=NOW, offset_hours=0)


def gates(**overrides) -> GateInputs:
    base = dict(
        now_ts=NOW,
        offset_hours=0.0,
        guild_enabled=True,
        channel_enabled=True,
        busy=False,
        slowmode_delay=0,
        quiet_start=0,
        quiet_end=8,
        revives_today=0,
        daily_budget=3,
        last_guild_revive_ts=None,
        guild_gap_minutes=90.0,
        last_channel_revive_ts=None,
        rest_hours=8.0,
        human_spoke_since_revive=True,
        last_human_ts=NOW - 3000,
        history_days=29.0,
        fire_multiplier=1.0,
        profiles=PROFILES,
    )
    base.update(overrides)
    return GateInputs(**base)


def test_fires_on_genuine_evening_lull():
    v = decide(gates())
    assert v.fire
    assert v.mode == "rhythm"
    assert v.band == 9
    # threshold = 1680s between-conversation lull x 1.0 patience; 3000s beats it
    assert v.threshold_s == 1680.0


def test_refuses_below_threshold():
    v = decide(gates(last_human_ts=NOW - 1000))
    assert not v.fire
    assert "real lull" in v.reason


def test_gate_guild_disabled():
    assert "not enabled for this server" in decide(gates(guild_enabled=False)).reason


def test_gate_channel_disabled():
    assert "not enabled for revives" in decide(gates(channel_enabled=False)).reason


def test_gate_busy():
    assert "game or event" in decide(gates(busy=True)).reason


def test_gate_slowmode():
    assert "slowmode" in decide(gates(slowmode_delay=5)).reason


def test_gate_quiet_hours():
    v = decide(gates(now_ts=_ts(30, 3), last_human_ts=_ts(30, 3) - 30000))
    assert "Quiet hours" in v.reason


def test_gate_daily_budget():
    assert "daily budget" in decide(gates(revives_today=3)).reason


def test_gate_guild_breathing_room():
    v = decide(gates(last_guild_revive_ts=NOW - 1800))
    assert "breathing room" in v.reason


def test_gate_channel_rest():
    v = decide(gates(last_channel_revive_ts=NOW - 3600))
    assert "Channel rest" in v.reason


def test_gate_never_chains():
    v = decide(
        gates(
            last_channel_revive_ts=NOW - 9 * 3600,
            human_spoke_since_revive=False,
        )
    )
    assert not v.fire
    assert "never chain" in v.reason


def test_gate_no_history():
    assert "No message history" in decide(gates(last_human_ts=None)).reason


def test_gate_normally_quiet_band():
    # 03:00 with quiet hours disabled: the band itself is dead -> refuse.
    v = decide(
        gates(
            now_ts=_ts(30, 3),
            last_human_ts=_ts(30, 3) - 30000,
            quiet_start=0,
            quiet_end=0,
        )
    )
    assert not v.fire
    assert "normally quiet" in v.reason


def test_sparse_band_falls_back_to_day_profile():
    sparse = {
        9: BandProfile(
            band=9, fire_threshold=1000, sessions_per_day=1, msgs_per_day=50,
            session_count=3,
        ),
        DAY_BAND: BandProfile(
            band=DAY_BAND, fire_threshold=2000, sessions_per_day=5,
            msgs_per_day=60, session_count=200,
        ),
    }
    v = decide(gates(profiles=sparse, last_human_ts=NOW - 2100))
    # Band 9 sampled only 3 conversation gaps (< MIN_BAND_SESSIONS) -> day
    # profile: threshold 2000 x 1.0 patience -> 2100s of silence fires.
    assert v.fire
    assert v.threshold_s == 2000.0


def test_cold_start_uses_fallback_mode():
    v = decide(gates(history_days=5.0, last_human_ts=NOW - 7 * 3600))
    assert v.fire
    assert v.mode == "fallback"


def test_fallback_refuses_short_silence():
    v = decide(gates(history_days=5.0, last_human_ts=NOW - 3 * 3600))
    assert not v.fire
    assert v.mode == "fallback"


def test_fallback_refuses_outside_daytime():
    late = _ts(30, 23)
    v = decide(
        gates(history_days=5.0, now_ts=late, last_human_ts=late - 8 * 3600)
    )
    assert not v.fire
    assert "fallback mode only fires" in v.reason


def test_no_profiles_at_all_refuses_in_rhythm_mode():
    v = decide(gates(profiles={DAY_BAND: BandProfile(DAY_BAND, 0, 0, 0, 0)}))
    assert not v.fire
    assert "Not enough activity history" in v.reason


# --- pings, weighting, rendering -----------------------------------------


def _ping(last, now, today, *, cap=3, cooldown_min=60):
    return should_ping(
        last, now, today, max_per_day=cap, cooldown_seconds=cooldown_min * 60
    )


def test_should_ping_cooldown():
    # cooldown is supplied in seconds; a 60-minute dial blocks at 59 min, frees at 61.
    assert _ping(None, NOW, 0)  # never pinged → allowed
    assert not _ping(NOW - 59 * 60, NOW, 0)  # inside the 60-min cooldown
    assert _ping(NOW - 61 * 60, NOW, 0)  # cooldown elapsed


def test_should_ping_daily_cap():
    # The per-day cap wins even when the cooldown has long elapsed.
    assert _ping(None, NOW, 2, cap=3)
    assert not _ping(None, NOW, 3, cap=3)
    assert not _ping(NOW - 90000, NOW, 3, cap=3)


def test_should_ping_cooldown_zero_means_cap_only():
    # A 0-minute cooldown lets consecutive revives ping until the cap is hit.
    assert _ping(NOW - 1, NOW, 2, cap=3, cooldown_min=0)
    assert not _ping(NOW - 1, NOW, 3, cap=3, cooldown_min=0)


def test_question_weight_smoothing():
    assert question_weight(0, 0) == 0.5  # unproven questions start mid-pack
    assert question_weight(10, 9) > question_weight(10, 1)
    assert question_weight(50, 0) < question_weight(0, 0)  # duds fade


def test_pick_weighted_favors_heavy():
    rng = random.Random(42)
    picks = [pick_weighted([1, 2], [0.95, 0.05], rng) for _ in range(200)]
    assert picks.count(1) > 150


def test_revive_succeeded_thresholds():
    assert revive_succeeded(3, 2)
    assert not revive_succeeded(2, 2)
    assert not revive_succeeded(5, 1)


def test_render_revive_full_footprint():
    text = render_revive("What's new?", role_id=123, flourish="*stirring…*")
    assert text == "\U0001f525 *stirring…* <@&123> What's new?"


def test_render_revive_bare():
    assert render_revive("Q?", role_id=None, flourish=None) == "\U0001f525 Q?"


def test_render_revive_caption_carries_ping_and_flourish():
    assert (
        render_revive_caption(role_id=123, flourish="*stirring…*")
        == "\U0001f525 *stirring…* <@&123>"
    )


def test_render_revive_caption_ping_only_keeps_the_fire():
    assert render_revive_caption(role_id=7, flourish=None) == "\U0001f525 <@&7>"


def test_render_revive_caption_empty_when_neither():
    assert render_revive_caption(role_id=None, flourish=None) == ""
