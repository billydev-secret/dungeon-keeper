"""Tests for the extracted Anonymous AMA pure-logic modules.

Covers ``bot_modules/games_ama/logic.py`` (ISO timestamps, question
status transitions, retention, recap stats, bottom-bar formatting,
idle-AI parsing) and ``bot_modules/games_ama/embeds.py`` (lobby,
main, question, answered, idle-AI, recap embed builders).
Mirrors the pressure_cooker / games_traditional / games_ttl pattern:
the cog file stays thin; this module proves the extracted pieces work
without spinning up Discord.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_modules.games_ama.embeds import (
    build_answered_embed,
    build_asker_dm_embed,
    build_lobby_embed,
    build_main_embed,
    build_panel_embed,
    build_question_embed,
    build_recap_embed,
)
from bot_modules.games_ama.logic import (
    AMA_FORMAT_HOT_SEAT,
    AMA_FORMAT_PANEL,
    RESOLVED_QUESTION_STATUSES,
    UNANSWERED_QUESTION_RETENTION,
    add_question,
    bottom_bar_label,
    build_question_entry,
    compute_recap_stats,
    first_content_line,
    is_panel_target,
    is_resolved_status,
    mark_question_answered,
    mark_question_approved,
    mark_question_expired,
    mark_question_message,
    mark_question_passed,
    mark_question_rejected,
    normalize_format,
    panel_bottom_bar_label,
    parse_iso_ts,
    recompute_totals,
    remaining_questions_text,
    should_expire,
    toggle_panel_member,
    unique_asker_count,
    utcnow_iso,
)


# ── utcnow_iso / parse_iso_ts ────────────────────────────────────────


def test_utcnow_iso_with_injected_clock_is_deterministic():
    fixed = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert utcnow_iso(fixed) == "2024-01-02T03:04:05+00:00"


def test_utcnow_iso_default_round_trips_through_parse():
    s = utcnow_iso()
    parsed = parse_iso_ts(s)
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_iso_ts_handles_none_and_empty():
    assert parse_iso_ts(None) is None
    assert parse_iso_ts("") is None


def test_parse_iso_ts_handles_unparseable():
    assert parse_iso_ts("not a date") is None


def test_parse_iso_ts_coerces_naive_to_utc():
    naive = "2024-05-01T12:00:00"
    parsed = parse_iso_ts(naive)
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_parse_iso_ts_converts_offset_to_utc():
    parsed = parse_iso_ts("2024-05-01T12:00:00+05:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)
    assert parsed.hour == 7  # 12 - 5


# ── is_resolved_status ───────────────────────────────────────────────


@pytest.mark.parametrize("status", ["answered", "passed", "rejected", "expired"])
def test_is_resolved_status_recognizes_terminal_states(status):
    assert is_resolved_status(status) is True


@pytest.mark.parametrize("status", ["pending", "approved", "", None, "unknown"])
def test_is_resolved_status_rejects_non_terminal(status):
    assert is_resolved_status(status) is False


def test_is_resolved_status_is_case_insensitive():
    assert is_resolved_status("ANSWERED") is True
    assert is_resolved_status("Passed") is True


def test_resolved_question_statuses_constant_matches_set():
    assert RESOLVED_QUESTION_STATUSES == {"answered", "passed", "rejected", "expired"}


# ── should_expire ────────────────────────────────────────────────────


def test_should_expire_returns_false_when_asked_at_is_none():
    now = datetime(2024, 5, 1, tzinfo=timezone.utc)
    assert should_expire(None, now) is False


def test_should_expire_returns_true_after_default_retention():
    now = datetime(2024, 5, 10, tzinfo=timezone.utc)
    asked = now - timedelta(days=8)
    assert should_expire(asked, now) is True


def test_should_expire_returns_false_within_default_retention():
    now = datetime(2024, 5, 10, tzinfo=timezone.utc)
    asked = now - timedelta(days=6)
    assert should_expire(asked, now) is False


def test_should_expire_uses_custom_retention():
    now = datetime(2024, 5, 10, tzinfo=timezone.utc)
    asked = now - timedelta(hours=2)
    assert should_expire(asked, now, retention=timedelta(hours=1)) is True
    assert should_expire(asked, now, retention=timedelta(hours=3)) is False


def test_should_expire_exact_retention_boundary_is_not_expired():
    """The cog used ``(now - asked) > retention`` (strict greater);
    equal should NOT expire."""
    now = datetime(2024, 5, 10, tzinfo=timezone.utc)
    asked = now - UNANSWERED_QUESTION_RETENTION
    assert should_expire(asked, now) is False


# ── build_question_entry / add_question ──────────────────────────────


def test_build_question_entry_minimum_fields():
    entry = build_question_entry(
        asker_id=42, text="hi", hot_seat_id=99, now_iso="2024-01-01T00:00:00+00:00"
    )
    assert entry == {
        "asker_id": 42,
        "text": "hi",
        "status": "pending",
        "asked_at": "2024-01-01T00:00:00+00:00",
        "hot_seat_id": 99,
    }


def test_build_question_entry_ai_idle_source_and_status():
    entry = build_question_entry(
        asker_id=0,
        text="some AI text",
        hot_seat_id=99,
        status="approved",
        source="ai_idle",
        now_iso="2024-01-01T00:00:00+00:00",
    )
    assert entry["status"] == "approved"
    assert entry["source"] == "ai_idle"
    assert entry["asker_id"] == 0


def test_build_question_entry_default_timestamp_uses_now():
    entry = build_question_entry(asker_id=1, text="x", hot_seat_id=2)
    # Round-trips through parse_iso_ts as a valid timestamp
    assert parse_iso_ts(entry["asked_at"]) is not None


def test_build_question_entry_omits_source_when_none():
    entry = build_question_entry(
        asker_id=1, text="x", hot_seat_id=2, now_iso="2024-01-01T00:00:00+00:00"
    )
    assert "source" not in entry


def test_add_question_appends_and_returns_index():
    payload: dict = {}
    e1 = build_question_entry(1, "q1", 99, now_iso="t1")
    idx1 = add_question(payload, e1)
    assert idx1 == 0
    assert payload["questions"][0] is e1
    assert payload["total_questions"] == 1

    e2 = build_question_entry(2, "q2", 99, now_iso="t2")
    idx2 = add_question(payload, e2)
    assert idx2 == 1
    assert payload["total_questions"] == 2


# ── mark_question_approved ───────────────────────────────────────────


def test_mark_question_approved_sets_status_and_message_id():
    payload = {
        "questions": [
            {
                "asker_id": 1,
                "text": "q",
                "status": "pending",
                "asked_at": "t",
                "hot_seat_id": 99,
            }
        ]
    }
    mark_question_approved(payload, 0, message_id=12345)
    q = payload["questions"][0]
    assert q["status"] == "approved"
    assert q["question_message_id"] == 12345
    # asked_at and hot_seat_id untouched
    assert q["asked_at"] == "t"
    assert q["hot_seat_id"] == 99


def test_mark_question_approved_setdefaults_asked_at_and_hot_seat():
    """Screened-approve path preserves existing asked_at / hot_seat_id."""
    payload = {
        "questions": [
            {
                "asker_id": 1,
                "text": "q",
                "status": "pending",
                "asked_at": "original",
                "hot_seat_id": 11,
            }
        ]
    }
    mark_question_approved(
        payload, 0, message_id=1, hot_seat_id=999, now_iso="new"
    )
    q = payload["questions"][0]
    assert q["asked_at"] == "original"  # setdefault left it alone
    assert q["hot_seat_id"] == 11  # setdefault left it alone


def test_mark_question_approved_fills_missing_asked_at_and_hot_seat():
    payload = {"questions": [{"asker_id": 1, "text": "q", "status": "pending"}]}
    mark_question_approved(
        payload, 0, message_id=42, hot_seat_id=999, now_iso="new"
    )
    q = payload["questions"][0]
    assert q["asked_at"] == "new"
    assert q["hot_seat_id"] == 999
    assert q["question_message_id"] == 42


def test_mark_question_approved_out_of_range_is_noop():
    payload = {"questions": []}
    mark_question_approved(payload, 0, message_id=42)
    assert payload["questions"] == []


# ── mark_question_message ────────────────────────────────────────────


def test_mark_question_message_writes_id_unconditionally():
    payload = {"questions": [{"asker_id": 0, "text": "x"}]}
    mark_question_message(payload, 0, 555)
    assert payload["questions"][0]["question_message_id"] == 555


def test_mark_question_message_overwrites_existing_id():
    """The cog uses an unconditional assignment in the idle-AI flow."""
    payload = {
        "questions": [
            {"asker_id": 0, "text": "x", "question_message_id": 111}
        ]
    }
    mark_question_message(payload, 0, 222)
    assert payload["questions"][0]["question_message_id"] == 222


def test_mark_question_message_out_of_range_is_noop():
    payload = {"questions": []}
    mark_question_message(payload, 0, 1)
    assert payload["questions"] == []


# ── mark_question_answered ───────────────────────────────────────────


def test_mark_question_answered_sets_status_and_timestamp():
    payload = {
        "questions": [
            {"status": "approved"},
            {"status": "approved"},
        ]
    }
    mark_question_answered(payload, 1, message_id=42, now_iso="2024-05-01")
    q = payload["questions"][1]
    assert q["status"] == "answered"
    assert q["answered_at"] == "2024-05-01"
    assert q["question_message_id"] == 42
    assert payload["total_answered"] == 1


def test_mark_question_answered_setdefaults_message_id():
    payload = {
        "questions": [
            {"status": "approved", "question_message_id": 999}
        ]
    }
    mark_question_answered(payload, 0, message_id=42, now_iso="t")
    # setdefault preserves existing
    assert payload["questions"][0]["question_message_id"] == 999


def test_mark_question_answered_recomputes_total_across_questions():
    payload = {
        "questions": [
            {"status": "answered"},
            {"status": "approved"},
            {"status": "answered"},
            {"status": "passed"},
        ]
    }
    mark_question_answered(payload, 1, now_iso="t")
    assert payload["total_answered"] == 3


def test_mark_question_answered_out_of_range_still_recomputes_totals():
    payload = {"questions": [{"status": "answered"}]}
    mark_question_answered(payload, 99, now_iso="t")
    # Out of range: status not flipped, but totals refreshed
    assert payload["total_answered"] == 1


# ── mark_question_passed ─────────────────────────────────────────────


def test_mark_question_passed_sets_fields_and_increments_total():
    payload = {"questions": [{"status": "approved"}], "total_passed": 0}
    mark_question_passed(payload, 0, message_id=1, now_iso="t")
    q = payload["questions"][0]
    assert q["status"] == "passed"
    assert q["passed_at"] == "t"
    assert q["question_message_id"] == 1
    assert payload["total_passed"] == 1


def test_mark_question_passed_increments_existing_total():
    payload = {"questions": [{"status": "approved"}], "total_passed": 4}
    mark_question_passed(payload, 0, now_iso="t")
    assert payload["total_passed"] == 5


def test_mark_question_passed_increments_when_total_missing():
    payload = {"questions": [{"status": "approved"}]}
    mark_question_passed(payload, 0, now_iso="t")
    assert payload["total_passed"] == 1


def test_mark_question_passed_setdefaults_message_id():
    payload = {
        "questions": [{"status": "approved", "question_message_id": 999}]
    }
    mark_question_passed(payload, 0, message_id=1, now_iso="t")
    assert payload["questions"][0]["question_message_id"] == 999


# ── mark_question_rejected ───────────────────────────────────────────


def test_mark_question_rejected_only_sets_status():
    payload = {"questions": [{"asker_id": 1, "text": "q", "status": "pending"}]}
    mark_question_rejected(payload, 0)
    q = payload["questions"][0]
    assert q["status"] == "rejected"
    # No timestamp or message id added
    assert "rejected_at" not in q
    assert "question_message_id" not in q


def test_mark_question_rejected_out_of_range_is_noop():
    payload = {"questions": []}
    mark_question_rejected(payload, 0)
    assert payload["questions"] == []


# ── mark_question_expired ────────────────────────────────────────────


def test_mark_question_expired_sets_status_and_timestamp():
    q = {"status": "approved"}
    mark_question_expired(q, now_iso="2024-05-01")
    assert q["status"] == "expired"
    assert q["expired_at"] == "2024-05-01"


def test_mark_question_expired_default_timestamp():
    q = {"status": "approved"}
    mark_question_expired(q)
    assert q["status"] == "expired"
    assert parse_iso_ts(q["expired_at"]) is not None


# ── recompute_totals ─────────────────────────────────────────────────


def test_recompute_totals_counts_only_answered():
    payload = {
        "questions": [
            {"status": "answered"},
            {"status": "approved"},
            {"status": "answered"},
            {"status": "passed"},
            {"status": "rejected"},
        ]
    }
    recompute_totals(payload)
    assert payload["total_answered"] == 2


def test_recompute_totals_empty_payload():
    payload: dict = {}
    recompute_totals(payload)
    assert payload["total_answered"] == 0


def test_recompute_totals_no_answered():
    payload = {"questions": [{"status": "passed"}]}
    recompute_totals(payload)
    assert payload["total_answered"] == 0


# ── first_content_line ───────────────────────────────────────────────


def test_first_content_line_strips_bullet():
    assert first_content_line("- Hello") == "Hello"


def test_first_content_line_strips_star_bullet():
    assert first_content_line("* Hello") == "Hello"


def test_first_content_line_strips_numeric_bullet():
    assert first_content_line("1. Hello") == "Hello"


def test_first_content_line_strips_surrounding_quotes():
    assert first_content_line('"Hello there"') == "Hello there"
    assert first_content_line("'Hello'") == "Hello"


def test_first_content_line_skips_blank_lines():
    assert first_content_line("\n  \n- Real Question\n") == "Real Question"


def test_first_content_line_empty_returns_none():
    assert first_content_line("") is None
    assert first_content_line("\n\n  \n") is None


def test_first_content_line_truncates_to_500():
    long = "x" * 600
    out = first_content_line(long)
    assert out is not None
    assert len(out) == 500


def test_first_content_line_uses_only_first_nonblank():
    """The cog only returns the first non-blank line, not joined text."""
    text = "Q1?\nQ2?"
    assert first_content_line(text) == "Q1?"


# ── unique_asker_count / compute_recap_stats ─────────────────────────


def test_unique_asker_count_excludes_ai_sentinel():
    qs = [
        {"asker_id": 0},  # AI
        {"asker_id": 1},
        {"asker_id": 1},  # dup
        {"asker_id": 2},
    ]
    assert unique_asker_count(qs) == 2


def test_unique_asker_count_handles_missing_asker_id():
    qs = [{"text": "no asker"}, {"asker_id": 5}]
    assert unique_asker_count(qs) == 1


def test_compute_recap_stats_pulls_all_fields():
    payload = {
        "questions": [
            {"asker_id": 1, "status": "answered"},
            {"asker_id": 2, "status": "passed"},
            {"asker_id": 0, "status": "answered"},
        ],
        "total_answered": 2,
        "total_passed": 1,
        "hot_seat_rotations": 3,
    }
    stats = compute_recap_stats(payload)
    assert stats == {
        "total_q": 3,
        "total_answered": 2,
        "total_passed": 1,
        "rotations": 3,
        "unique_askers": 2,
    }


def test_compute_recap_stats_empty_payload():
    stats = compute_recap_stats({})
    assert stats == {
        "total_q": 0,
        "total_answered": 0,
        "total_passed": 0,
        "rotations": 0,
        "unique_askers": 0,
    }


# ── bottom_bar_label ─────────────────────────────────────────────────


def test_bottom_bar_label_no_hot_seat_no_queue():
    assert bottom_bar_label(None, 0) == "🎙️ AMA"


def test_bottom_bar_label_with_hot_seat_no_queue():
    assert bottom_bar_label("Alice", 0) == "🎙️ AMA: @Alice"


def test_bottom_bar_label_with_hot_seat_and_queue():
    label = bottom_bar_label("Alice", 3)
    assert label == "🎙️ AMA: @Alice  •  📋 3 in queue"


def test_bottom_bar_label_no_hot_seat_ignores_queue():
    """When there's no hot seat the bar should be the bare label even
    if the queue is populated (matches the cog's existing precedence)."""
    assert bottom_bar_label(None, 5) == "🎙️ AMA"


# ── remaining_questions_text ─────────────────────────────────────────


@pytest.mark.parametrize(
    "asked,expected",
    [
        (0, "**4** questions left this turn."),
        (1, "**3** questions left this turn."),
        (2, "**2** questions left this turn."),
        (3, "**1** question left this turn."),
        (4, "**0** questions left this turn."),
    ],
)
def test_remaining_questions_text_pluralisation(asked, expected):
    assert remaining_questions_text(asked) == expected


def test_remaining_questions_text_custom_per_turn():
    assert remaining_questions_text(0, per_turn=2) == "**2** questions left this turn."


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_basic_fields():
    embed = build_lobby_embed("Alice", "unfiltered")
    assert embed.title is not None
    assert "ANONYMOUS AMA" in embed.title
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name.get("Host") == "Alice"
    assert by_name.get("Hot Seat") == "—"
    assert by_name.get("Mode") == "unfiltered"


def test_build_lobby_embed_has_footer():
    embed = build_lobby_embed("Alice", "screened")
    assert embed.footer.text is not None
    assert "Anonymous AMA" in embed.footer.text


# ── build_main_embed ─────────────────────────────────────────────────


def test_build_main_embed_no_hot_seat_shows_dash():
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name=None,
        questions_this_turn=0,
        queue=[],
        name_resolver=str,
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Hot Seat"] == "—"
    assert embed.description is not None
    assert "Who's taking" in embed.description


def test_build_main_embed_with_hot_seat_shows_remaining():
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name="HotPerson",
        questions_this_turn=1,
        queue=[],
        name_resolver=str,
    )
    assert embed.description is not None
    assert "HotPerson" in embed.description
    assert "**3** questions left" in embed.description


def test_build_main_embed_includes_queue_field_when_populated():
    resolver = {1: "Alice", 2: "Bob"}
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name="HotPerson",
        questions_this_turn=0,
        queue=[1, 2],
        name_resolver=lambda u: resolver.get(u, str(u)),
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    queue_field = next(k for k in by_name if "Queue" in k)
    assert "Queue (2)" in queue_field
    assert "Alice → Bob" in by_name[queue_field]


def test_build_main_embed_omits_queue_field_when_empty():
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name="HotPerson",
        questions_this_turn=0,
        queue=[],
        name_resolver=str,
    )
    assert not any("Queue" in (f.name or "") for f in embed.fields)


def test_build_main_embed_progress_field_shown_when_payload_given():
    payload = {
        "questions": [{"status": "answered"}, {"status": "approved"}],
        "total_answered": 1,
        "total_passed": 0,
    }
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name="HotPerson",
        questions_this_turn=0,
        queue=[],
        name_resolver=str,
        payload=payload,
    )
    progress_field = next((f for f in embed.fields if f.name == "📊 Progress"), None)
    assert progress_field is not None
    assert progress_field.value is not None
    assert "Questions: **2**" in progress_field.value
    assert "Answered: **1**" in progress_field.value


def test_build_main_embed_progress_field_omitted_when_no_payload():
    embed = build_main_embed(
        host_name="HostX",
        mode="unfiltered",
        hot_seat_name="HotPerson",
        questions_this_turn=0,
        queue=[],
        name_resolver=str,
        payload=None,
    )
    assert not any(f.name == "📊 Progress" for f in embed.fields)


def test_build_main_embed_progress_field_empty_payload_zero_bar():
    payload = {"questions": [], "total_answered": 0, "total_passed": 0}
    embed = build_main_embed(
        host_name="H",
        mode="unfiltered",
        hot_seat_name="X",
        questions_this_turn=0,
        queue=[],
        name_resolver=str,
        payload=payload,
    )
    progress = next(f for f in embed.fields if f.name == "📊 Progress")
    assert progress.value is not None
    assert "0%" in progress.value


# ── build_question_embed ─────────────────────────────────────────────


def test_build_question_embed_includes_text():
    embed = build_question_embed("What is your favorite color?")
    assert embed.description is not None
    assert "What is your favorite color?" in embed.description
    assert embed.title is not None
    assert "QUESTION" in embed.title


def test_build_question_embed_escapes_markdown():
    embed = build_question_embed("*emphasised*")
    assert embed.description is not None
    assert "\\*" in embed.description


# ── build_answered_embed ─────────────────────────────────────────────


def test_build_answered_embed_renders_q_and_a():
    embed = build_answered_embed("Q?", "Yes!", "Alice")
    assert embed.description is not None
    assert "Q?" in embed.description
    assert "Yes!" in embed.description
    assert embed.footer.text is not None
    assert "Alice" in embed.footer.text


def test_build_answered_embed_escapes_markdown_in_both():
    embed = build_answered_embed("*Q*", "_A_", "Alice")
    assert embed.description is not None
    assert "\\*Q\\*" in embed.description
    assert "\\_A\\_" in embed.description


# ── build_asker_dm_embed ─────────────────────────────────────────────


def test_build_asker_dm_embed_includes_channel_mention():
    embed = build_asker_dm_embed("<#42>")
    assert embed.description is not None
    assert "<#42>" in embed.description
    assert "anonymous question" in embed.description


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_renders_all_stats():
    stats = {
        "total_q": 10,
        "total_answered": 7,
        "total_passed": 2,
        "rotations": 3,
        "unique_askers": 4,
    }
    embed = build_recap_embed("unfiltered", stats)
    by_name = {f.name: f.value for f in embed.fields}
    stats_field = by_name["📊 Session Stats"]
    assert stats_field is not None
    assert "**10**" in stats_field
    assert "by **4** people" in stats_field
    assert "**7** answered" in stats_field
    assert "**2** passed" in stats_field
    assert by_name["🔄 Hot Seat Rotations"] == "3"
    assert by_name["🎙️ Mode"] == "Unfiltered"


def test_build_recap_embed_handles_zero_questions():
    stats = {
        "total_q": 0,
        "total_answered": 0,
        "total_passed": 0,
        "rotations": 0,
        "unique_askers": 0,
    }
    embed = build_recap_embed("screened", stats)
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "0%" in by_name["📊 Session Stats"]
    assert by_name["🎙️ Mode"] == "Screened"


def test_build_recap_embed_has_game_over_title_and_footer():
    embed = build_recap_embed("unfiltered", compute_recap_stats({}))
    assert embed.title is not None
    assert "GAME OVER" in embed.title
    assert embed.footer.text is not None
    assert "Thanks for playing" in embed.footer.text


# ── economy roster enrichment (Stage 2 faucet) ──────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

import bot_modules.cogs.games_ama_cog as ama_cog  # noqa: E402
from bot_modules.games.utils.game_manager import create_game  # noqa: E402
from bot_modules.services.games_db import GamesDb  # noqa: E402


class _SpyBot:
    def __init__(self, db_path) -> None:
        self.games_db = GamesDb(db_path)
        self.active_views: dict = {}
        self.ctx = SimpleNamespace(db_path=db_path)

    def get_cog(self, name):
        return None


async def test_do_close_pays_askers_and_hot_seats(monkeypatch, sync_db_path):
    """Participants = human askers (AI sentinel 0 excluded) plus the hot-seat
    occupants who answered."""
    spy = AsyncMock()
    monkeypatch.setattr(ama_cog, "end_game", spy)
    bot = _SpyBot(sync_db_path)
    payload = {"questions": [
        {"asker_id": 5, "hot_seat_id": 9},
        {"asker_id": 0, "hot_seat_id": 9},   # AI-asked, answered by hot seat 9
        {"asker_id": 7, "hot_seat_id": 5},   # asker 5 later took the hot seat
        {"asker_id": 5, "hot_seat_id": 9},
    ]}
    gid = await create_game(bot.games_db, 100, 1, "ama", payload=payload)
    view = ama_cog.AMAView(gid, 1, "public", bot.games_db, bot)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=100, guild=None, send=AsyncMock())
    await view._do_close(channel)
    call = spy.await_args
    assert call is not None and spy.await_count == 1
    assert call.kwargs["player_ids"] == [5, 7, 9]  # askers {5,7} ∪ hot seats {9,5}
    assert call.kwargs["bot"] is bot


# ── normalize_format ─────────────────────────────────────────────────


def test_normalize_format_recognizes_panel():
    assert normalize_format(AMA_FORMAT_PANEL) == AMA_FORMAT_PANEL


@pytest.mark.parametrize("value", [None, "", "hot_seat", "bogus", "HOT_SEAT", 0])
def test_normalize_format_defaults_to_hot_seat(value):
    # Old payloads have no format key and bad input shouldn't panic —
    # everything that isn't the explicit panel sentinel is hot seat.
    assert normalize_format(value) == AMA_FORMAT_HOT_SEAT


# ── toggle_panel_member / is_panel_target ────────────────────────────


def test_toggle_panel_member_adds_then_removes_preserving_order():
    panel: list[int] = []
    assert toggle_panel_member(panel, 10) is True   # joined
    assert toggle_panel_member(panel, 20) is True
    assert panel == [10, 20]                          # join order preserved
    assert toggle_panel_member(panel, 10) is False  # left
    assert panel == [20]


def test_is_panel_target_reflects_membership():
    panel = [1, 2, 3]
    assert is_panel_target(panel, 2) is True
    assert is_panel_target(panel, 99) is False
    assert is_panel_target([], 2) is False


# ── panel_bottom_bar_label ───────────────────────────────────────────


def test_panel_bottom_bar_label_empty_panel():
    assert panel_bottom_bar_label(0) == "🎙️ AMA Panel"


def test_panel_bottom_bar_label_singular_vs_plural():
    assert "1 answering question" in panel_bottom_bar_label(1)
    assert panel_bottom_bar_label(1).rstrip().endswith("question")
    assert "3 answering questions" in panel_bottom_bar_label(3)


# ── build_panel_embed ────────────────────────────────────────────────


def test_build_panel_embed_empty_prompts_to_volunteer():
    embed = build_panel_embed("Alice", "unfiltered", [], str)
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert embed.description is not None and "Volunteer" in embed.description
    assert by_name["🙋 Panel"] == "—"
    assert by_name["Host"] == "Alice"


def test_build_panel_embed_lists_roster_with_resolver():
    names = {7: "Bob", 8: "Cara"}
    embed = build_panel_embed("Alice", "screened", [7, 8], lambda uid: names[uid])
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    roster = by_name["🙋 Panel (2)"]
    assert "Bob" in roster and "Cara" in roster
    assert embed.description is not None and "anyone on the panel" in embed.description.lower()


def test_build_panel_embed_caps_large_roster_by_rendered_length():
    # Worst case: many members with max-length (32-char) display names — a
    # count-based cap would blow Discord's 1024-char field limit here.
    panel = list(range(200))
    embed = build_panel_embed("Alice", "unfiltered", panel, lambda uid: "N" * 32)
    roster_field = next(f for f in embed.fields if (f.name or "").startswith("🙋 Panel"))
    assert roster_field.name is not None and "(200)" in roster_field.name  # header shows the true total
    assert roster_field.value is not None
    assert "more" in roster_field.value  # truncation tail present
    assert len(roster_field.value) <= 1024  # Discord's per-field value hard limit


def test_build_panel_embed_short_roster_not_truncated():
    embed = build_panel_embed("Alice", "unfiltered", [1, 2, 3], lambda uid: f"user{uid}")
    roster_field = next(f for f in embed.fields if (f.name or "").startswith("🙋 Panel"))
    assert roster_field.value is not None
    assert "more" not in roster_field.value
    assert roster_field.value.count("•") == 3


def test_build_panel_embed_includes_progress_when_payload_given():
    payload = {"questions": [{}, {}, {}], "total_answered": 1, "total_passed": 1}
    embed = build_panel_embed("Alice", "unfiltered", [7], str, payload=payload)
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "📊 Progress" in by_name
    assert "**3**" in by_name["📊 Progress"]
