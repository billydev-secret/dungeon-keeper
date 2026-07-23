"""Tests for bot_modules.services.attention_report.

Exercises the gating (volume floor + asymmetry), the concentration/HHI math,
escalation-after-silence, burstiness, and the evidence/caution annotations.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.attention_report import (
    ASYM_CUT,
    compute_one_sided_attention,
)
from migrations import apply_migrations_sync

GUILD = 7
NOW = 1_000_000_000
DAY = 86400


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ar.db"
    apply_migrations_sync(path)
    with open_db(path) as conn:
        yield conn


def _text(conn, frm, to, ts):
    conn.execute(
        "INSERT INTO user_interactions_log (guild_id, from_user_id, to_user_id, ts, message_id) VALUES (?,?,?,?,?)",
        (GUILD, frm, to, ts, None),
    )


def _react(conn, frm, to, ts, mid):
    conn.execute(
        "INSERT INTO reaction_log (guild_id, reactor_id, author_id, channel_id, message_id, ts) VALUES (?,?,?,?,?,?)",
        (GUILD, frm, to, 1, mid, ts),
    )


def _voice(conn, frm, to, ts):
    conn.execute(
        "INSERT INTO voice_follow_log (guild_id, from_user_id, to_user_id, channel_id, ts) VALUES (?,?,?,?,?)",
        (GUILD, frm, to, 99, ts),
    )


def _find(cands, frm, to):
    return next(
        (c for c in cands if c.initiator_id == frm and c.target_id == to), None
    )


def test_lopsided_pair_is_flagged(db):
    # 20 one-directional text events, target never responds.
    for i in range(20):
        _text(db, 1, 2, NOW - (20 - i) * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert c.asymmetry == pytest.approx(1.0)
    assert c.ever_reciprocated is False
    assert "target never responded in-window" in c.reasons


def test_excluded_ids_drop_pairs_on_either_endpoint(db):
    """A bot (excluded) as initiator OR target never surfaces as a candidate."""
    # Human 1 → bot 99: textbook lopsided, would flag if not excluded.
    for i in range(20):
        _text(db, 1, 99, NOW - (20 - i) * 3600)
    # Bot 99 → human 2: also lopsided in the other direction.
    for i in range(20):
        _text(db, 99, 2, NOW - (20 - i) * 3600)

    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW, exclude_ids={99})
    assert _find(cands, 1, 99) is None
    assert _find(cands, 99, 2) is None
    assert all(99 not in (c.initiator_id, c.target_id) for c in cands)


def test_excluded_ids_removed_from_concentration_evidence(db):
    """Attention toward an excluded id doesn't inflate a human's concentration/HHI."""
    # Human 1 spreads text across humans 2..7 (6 distinct human targets),
    # and also hammers bot 99 hard. The real one-sided pair is 1→2 (target
    # silent). Bot volume must not enter 1's outbound totals.
    for t in range(2, 8):
        for i in range(16):
            _text(db, 1, t, NOW - (16 - i) * 3600 - t)
    for i in range(40):
        _text(db, 1, 99, NOW - (40 - i) * 600)

    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW, exclude_ids={99})
    c = _find(cands, 1, 2)
    assert c is not None
    # 6 human targets, none of them the bot.
    assert c.distinct_targets == 6
    # Concentration is w_out/(sum over human targets) — bot weight excluded, so
    # each of 6 equal targets is ~1/6, well under any bot-inflated figure.
    assert c.concentration == pytest.approx(1 / 6, abs=0.02)


def test_balanced_pair_is_not_flagged(db):
    for i in range(15):
        _text(db, 1, 2, NOW - i * 3600)
        _text(db, 2, 1, NOW - i * 3600 - 60)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    assert _find(cands, 1, 2) is None
    assert _find(cands, 2, 1) is None


def test_below_volume_floor_is_not_flagged(db):
    # Fully one-sided but only 5 events — under the floor of 15.
    for i in range(5):
        _text(db, 1, 2, NOW - i * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    assert _find(cands, 1, 2) is None


def test_asymmetry_just_below_cut_excluded(db):
    # 16 out, 4 back → asym = 0.8 < 0.85 cut.
    for i in range(16):
        _text(db, 1, 2, NOW - i * 3600)
    for i in range(4):
        _text(db, 2, 1, NOW - i * 3600 - 30)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    assert _find(cands, 1, 2) is None


def test_voice_follow_weighted_heavily(db):
    # 8 voice-follows (×2.0 = 16 weighted) clears the floor alone.
    for i in range(8):
        _voice(db, 1, 2, NOW - i * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert c.weight_out == pytest.approx(16.0)
    assert c.voice_follow_out == 8
    assert any("followed into voice" in r for r in c.reasons)


def test_out_of_window_events_ignored(db):
    for i in range(20):
        _text(db, 1, 2, NOW - 60 * DAY - i * 3600)  # ~60 days ago
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW, window_days=30)
    assert _find(cands, 1, 2) is None


def test_escalation_after_silence(db):
    # Target's last reply at pivot; initiator contacts far more afterward.
    pivot = NOW - 15 * DAY
    _text(db, 2, 1, pivot)  # target's only reciprocation
    for i in range(3):  # before: 3 events in the 14 days prior to pivot
        _text(db, 1, 2, pivot - (i + 1) * DAY)
    for i in range(12):  # after: 12 events in the 14 days following pivot
        _text(db, 1, 2, pivot + (i + 1) * 12 * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert c.ever_reciprocated is True
    assert c.escalation is not None and c.escalation > 1.0
    assert any("after they went quiet" in r for r in c.reasons)


def test_concentration_and_hhi(db):
    # Initiator 1 pours everything into target 2, plus token contact with 6 others.
    for i in range(20):
        _text(db, 1, 2, NOW - i * 1800)
    for other in range(3, 9):  # 6 other distinct targets, 1 event each
        _text(db, 1, other, NOW - other * 100)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert c.distinct_targets == 7
    assert c.concentration > 0.7  # 20 of 26 outbound events
    assert any("attention on this one person" in r for r in c.reasons)


def test_small_circle_caution(db):
    # Lopsided, but initiator only ever engages this one person.
    for i in range(20):
        _text(db, 1, 2, NOW - i * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert any("small social circle" in caution for caution in c.cautions)


def test_burst_descriptor(db):
    # 10 events inside 5 minutes → a legible burst.
    base = NOW - 3 * DAY
    for i in range(10):
        _text(db, 1, 2, base + i * 30)
    for i in range(10):  # pad volume across the window so it clears the floor cleanly
        _text(db, 1, 2, NOW - i * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW)
    c = _find(cands, 1, 2)
    assert c is not None
    assert c.max_burst >= 6
    assert any("burst of" in r for r in c.reasons)


def test_exclude_ids_filters_pairs(db):
    for i in range(20):
        _text(db, 1, 2, NOW - i * 3600)
    cands = compute_one_sided_attention(db, GUILD, now_ts=NOW, exclude_ids={2})
    assert _find(cands, 1, 2) is None


def test_asym_cut_constant_sanity():
    assert 0.5 < ASYM_CUT < 1.0
