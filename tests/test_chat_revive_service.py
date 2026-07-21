"""Tests for services/chat_revive_service.py over the real migration schema."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.chat_revive.logic import DAY_BAND
from bot_modules.chat_revive.starter_pack import STARTER_QUESTIONS
from bot_modules.services.chat_revive_service import (
    RHYTHM_MAX_AGE_SECONDS,
    ChannelConfig,
    GuildConfig,
    add_question,
    bulk_add_questions,
    channel_activity,
    evaluate,
    frequency_state,
    get_channel_config,
    get_guild_config,
    get_rhythm,
    list_enabled_channels,
    list_questions,
    load_rhythm,
    measure_due_events,
    parse_bulk_line,
    pick_question,
    record_event,
    refresh_rhythm,
    retire_question,
    save_channel_config,
    save_guild_config,
    seed_starter_pack,
)
from migrations import apply_migrations_sync

GID, CID = 100, 200


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


def _ts(day: int, hour: int, minute: int = 0) -> float:
    return datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc).timestamp()


NOW = _ts(30, 19)

_msg_id = iter(range(1, 1_000_000))


def _msg(conn, ts: float, *, user_id: int = 1, channel_id: int = CID) -> None:
    conn.execute(
        "INSERT INTO processed_messages "
        "(guild_id, message_id, channel_id, user_id, created_at, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (GID, next(_msg_id), channel_id, user_id, ts, ts),
    )


# ── config ────────────────────────────────────────────────────────────


def test_guild_config_defaults(db):
    with open_db(db) as conn:
        cfg = get_guild_config(conn, GID)
    assert cfg == GuildConfig(guild_id=GID)
    assert not cfg.enabled
    assert cfg.daily_budget == 3
    assert cfg.ping_max_per_day == 3
    assert cfg.ping_cooldown_minutes == 60
    assert cfg.rhythm_max_age_seconds == RHYTHM_MAX_AGE_SECONDS


def test_guild_config_roundtrip(db):
    cfg = GuildConfig(
        guild_id=GID,
        enabled=True,
        role_id=555,
        quiet_start=1,
        quiet_end=9,
        daily_budget=2,
        guild_gap_minutes=120,
        flourish_enabled=False,
        ping_max_per_day=5,
        ping_cooldown_minutes=30,
        rhythm_max_age_seconds=1800.0,
    )
    with open_db(db) as conn:
        save_guild_config(conn, cfg)
        assert get_guild_config(conn, GID) == cfg


def test_channel_config_roundtrip_and_listing(db):
    a = ChannelConfig(
        guild_id=GID, channel_id=CID, categories=("deep", "music"), ping_enabled=True
    )
    b = ChannelConfig(guild_id=GID, channel_id=CID + 1, enabled=False)
    other = ChannelConfig(guild_id=GID + 1, channel_id=CID + 2)
    with open_db(db) as conn:
        for cfg in (a, b, other):
            save_channel_config(conn, cfg)
        assert get_channel_config(conn, GID, CID) == a
        assert get_channel_config(conn, GID, 999) is None
        assert list_enabled_channels(conn, GID) == [a]
        assert {c.channel_id for c in list_enabled_channels(conn)} == {CID, CID + 2}


# ── question bank ─────────────────────────────────────────────────────


def test_add_question_dedupes_case_insensitively(db):
    with open_db(db) as conn:
        qid = add_question(conn, GID, "What's  new?", created_by=1, now_ts=NOW)
        assert qid is not None
        assert add_question(conn, GID, "what's new?", created_by=2, now_ts=NOW) is None
        assert add_question(conn, GID, "   ", created_by=2, now_ts=NOW) is None
        qs = list_questions(conn, GID)
    assert [q.text for q in qs] == ["What's new?"]  # whitespace collapsed


def test_parse_bulk_line_variants():
    assert parse_bulk_line("Plain question?") == ("general", False, "Plain question?")
    assert parse_bulk_line("deep: Why though?") == ("deep", False, "Why though?")
    assert parse_bulk_line("spicy,nsfw: Hot take?") == ("spicy", True, "Hot take?")
    assert parse_bulk_line("nsfw: Adults only?") == ("general", True, "Adults only?")
    # A capitalized or spaced colon prefix is part of the question, not tags.
    assert parse_bulk_line("Confession: I did it") == (
        "general",
        False,
        "Confession: I did it",
    )
    assert parse_bulk_line("Real talk: honestly?") == (
        "general",
        False,
        "Real talk: honestly?",
    )
    assert parse_bulk_line("   ") is None


def test_bulk_add_counts_added_and_skipped(db):
    lines = ["One?", "deep: Two?", "One?", ""]
    with open_db(db) as conn:
        added, skipped = bulk_add_questions(conn, GID, lines, created_by=1, now_ts=NOW)
    assert (added, skipped) == (2, 1)


def test_retire_and_list_filters(db):
    with open_db(db) as conn:
        qid = add_question(conn, GID, "Old one?", created_by=1, now_ts=NOW)
        add_question(conn, GID, "Deep one?", category="deep", created_by=1, now_ts=NOW)
        assert qid is not None
        assert retire_question(conn, GID, qid)
        assert not retire_question(conn, GID, 9999)
        assert [q.text for q in list_questions(conn, GID)] == ["Deep one?"]
        assert len(list_questions(conn, GID, include_retired=True)) == 2
        assert [q.text for q in list_questions(conn, GID, category="deep")] == [
            "Deep one?"
        ]


def test_seed_starter_pack_once(db):
    with open_db(db) as conn:
        assert seed_starter_pack(conn, GID, NOW) == len(STARTER_QUESTIONS)
        assert seed_starter_pack(conn, GID, NOW) == 0
        qs = list_questions(conn, GID)
    assert len(qs) == len(STARTER_QUESTIONS)
    assert all(q.created_by is None for q in qs)


def test_pick_question_filters_nsfw_and_category(db):
    with open_db(db) as conn:
        add_question(conn, GID, "Safe?", category="general", created_by=1, now_ts=NOW)
        add_question(
            conn, GID, "Spicy?", category="spicy", nsfw=True, created_by=1, now_ts=NOW
        )
        sfw = pick_question(
            conn, GID, categories=(), allow_nsfw=False, now_ts=NOW
        )
        assert sfw is not None and sfw.text == "Safe?"
        spicy = pick_question(
            conn, GID, categories=("spicy",), allow_nsfw=True, now_ts=NOW
        )
        assert spicy is not None and spicy.text == "Spicy?"
        assert (
            pick_question(conn, GID, categories=("spicy",), allow_nsfw=False, now_ts=NOW)
            is None
        )


def test_pick_question_honors_anti_repeat(db):
    with open_db(db) as conn:
        qid = add_question(conn, GID, "Only one?", created_by=1, now_ts=NOW)
        record_event(
            conn,
            GID,
            CID,
            question_id=qid,
            message_id=1,
            trigger_kind="auto",
            pinged=False,
            now_ts=NOW - 86400.0,  # used yesterday -> inside the 30d window
            offset_hours=0,
        )
        assert (
            pick_question(conn, GID, categories=(), allow_nsfw=False, now_ts=NOW)
            is None
        )
        # A question last used 31 days ago is eligible again.
        conn.execute(
            "UPDATE revive_questions SET last_used_at = ? WHERE id = ?",
            (NOW - 31 * 86400.0, qid),
        )
        assert (
            pick_question(conn, GID, categories=(), allow_nsfw=False, now_ts=NOW)
            is not None
        )


def test_pick_question_favors_proven_sparkers(db):
    rng = random.Random(7)
    with open_db(db) as conn:
        hot = add_question(conn, GID, "Hot?", created_by=1, now_ts=NOW)
        dud = add_question(conn, GID, "Dud?", created_by=1, now_ts=NOW)
        # Same use count; only "Hot?" ever sparked conversation.
        for i, (qid, success) in enumerate([(hot, 1), (dud, 0)] * 5):
            conn.execute(
                "INSERT INTO revive_events (guild_id, channel_id, question_id, "
                "trigger_kind, pinged, local_day, created_at, measured_at, success) "
                "VALUES (?, ?, ?, 'auto', 0, '2026-05-01', ?, ?, ?)",
                (GID, CID, qid, NOW - 40 * 86400 + i, NOW, success),
            )
        conn.execute("UPDATE revive_questions SET use_count = 5")
        picks = [
            pick_question(
                conn, GID, categories=(), allow_nsfw=False, now_ts=NOW, rng=rng
            )
            for _ in range(100)
        ]
    hot_share = sum(1 for q in picks if q and q.id == hot)
    assert hot_share > 60


# ── events & frequency gates ─────────────────────────────────────────


def test_record_event_bumps_question_stats(db):
    with open_db(db) as conn:
        qid = add_question(conn, GID, "Q?", created_by=1, now_ts=NOW)
        record_event(
            conn,
            GID,
            CID,
            question_id=qid,
            message_id=42,
            trigger_kind="auto",
            pinged=True,
            now_ts=NOW,
            offset_hours=0,
        )
        q = list_questions(conn, GID)[0]
    assert q.use_count == 1
    assert q.last_used_at == NOW


def test_frequency_state_counts_local_day_and_pings(db):
    with open_db(db) as conn:
        # Two revives "today" (offset 0), one pinged; an older one yesterday.
        for ts, pinged in [(NOW - 600, True), (NOW - 7200, False), (NOW - 90000, True)]:
            record_event(
                conn,
                GID,
                CID,
                question_id=None,
                message_id=None,
                trigger_kind="auto",
                pinged=pinged,
                now_ts=ts,
                offset_hours=0,
            )
        st = frequency_state(conn, GID, CID, now_ts=NOW, offset_hours=0)
        other = frequency_state(conn, GID, CID + 1, now_ts=NOW, offset_hours=0)
    assert st.revives_today == 2
    assert st.last_guild_revive_ts == NOW - 600
    assert st.last_channel_revive_ts == NOW - 600
    assert st.last_ping_ts == NOW - 600
    assert other.last_channel_revive_ts is None
    assert other.last_guild_revive_ts == NOW - 600


def test_measure_due_events_success_and_dud(db):
    with open_db(db) as conn:
        lively = record_event(
            conn,
            GID,
            CID,
            question_id=None,
            message_id=None,
            trigger_kind="auto",
            pinged=False,
            now_ts=NOW - 2000,
            offset_hours=0,
        )
        dead = record_event(
            conn,
            GID,
            CID + 1,
            question_id=None,
            message_id=None,
            trigger_kind="auto",
            pinged=False,
            now_ts=NOW - 2000,
            offset_hours=0,
        )
        fresh = record_event(
            conn,
            GID,
            CID,
            question_id=None,
            message_id=None,
            trigger_kind="auto",
            pinged=False,
            now_ts=NOW - 60,
            offset_hours=0,
        )
        # Three humans answer in the lively channel; outside-window noise ignored.
        for uid, dt in [(1, 100), (2, 200), (3, 300)]:
            _msg(conn, NOW - 2000 + dt, user_id=uid)
        _msg(conn, NOW - 2000 + 3600, user_id=4)  # after the 30-min window
        assert measure_due_events(conn, NOW) == 2
        rows = {
            r["id"]: r
            for r in conn.execute("SELECT * FROM revive_events").fetchall()
        }
    assert rows[lively]["success"] == 1
    assert rows[lively]["follow_msgs"] == 3
    assert rows[dead]["success"] == 0
    assert rows[fresh]["measured_at"] is None  # window still open


# ── activity & rhythm cache ──────────────────────────────────────────


def test_channel_activity_empty_and_populated(db):
    with open_db(db) as conn:
        empty = channel_activity(conn, GID, CID, now_ts=NOW)
        _msg(conn, NOW - 10 * 86400)
        _msg(conn, NOW - 300)
        act = channel_activity(conn, GID, CID, now_ts=NOW)
    assert empty.last_human_ts is None and empty.history_days == 0.0
    assert act.last_human_ts == NOW - 300
    assert 9.9 < act.history_days < 10.1


def test_rhythm_refresh_load_roundtrip(db):
    with open_db(db) as conn:
        for day in (28, 29):
            for minute in (0, 10, 20):
                _msg(conn, _ts(day, 19, minute))
        profiles = refresh_rhythm(conn, GID, CID, now_ts=NOW, offset_hours=0)
        loaded, computed_at = load_rhythm(conn, GID, CID)
    assert computed_at == NOW
    assert loaded == profiles
    assert 9 in loaded and DAY_BAND in loaded


def test_get_rhythm_recomputes_when_stale(db):
    with open_db(db) as conn:
        _msg(conn, NOW - 5 * 86400)
        _msg(conn, NOW - 5 * 86400 + 600)
        first = get_rhythm(conn, GID, CID, now_ts=NOW - 8 * 3600, offset_hours=0)
        assert first != {}
        _msg(conn, NOW - 7.5 * 3600)  # new activity after the cache was built
        # Cache is only 1h old at this point -> served as-is, new data unseen.
        cached = get_rhythm(conn, GID, CID, now_ts=NOW - 7 * 3600, offset_hours=0)
        assert cached == first
        # 8h old at NOW -> stale -> recomputed, now including the new message.
        recomputed = get_rhythm(conn, GID, CID, now_ts=NOW, offset_hours=0)
        assert recomputed != first
        _, computed_at = load_rhythm(conn, GID, CID)
    assert computed_at == NOW


def test_evaluate_passes_configured_rhythm_max_age_not_hardcoded_default(db, monkeypatch):
    """evaluate() must feed get_rhythm the guild's configured staleness
    window, not the module's RHYTHM_MAX_AGE_SECONDS constant."""
    import bot_modules.services.chat_revive_service as svc

    with open_db(db) as conn:
        save_guild_config(conn, GuildConfig(guild_id=GID, rhythm_max_age_seconds=999.0))

    captured = {}
    real_get_rhythm = svc.get_rhythm

    def spy_get_rhythm(*args, **kwargs):
        captured.update(kwargs)
        return real_get_rhythm(*args, **kwargs)

    monkeypatch.setattr(svc, "get_rhythm", spy_get_rhythm)

    with open_db(db) as conn:
        evaluate(conn, GID, CID, now_ts=NOW, busy=False, slowmode_delay=0)

    assert captured["max_age_seconds"] == 999.0
    assert captured["max_age_seconds"] != RHYTHM_MAX_AGE_SECONDS
