"""Tests for the extracted whisper pure-logic + embed modules.

Covers ``bot_modules/whisper/logic.py`` (status pills, time formatting,
fuzzy search, message builders) and ``bot_modules/whisper/embeds.py``
(inbox embed, mod-log audit embeds). Mirrors the starboard/jail pattern:
the cog file stays thin; this module proves the extracted helpers work
without spinning up Discord views.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import discord

from bot_modules.services.whisper_models import (
    STATE_HIDDEN,
    STATE_PENDING,
    STATE_SHARED,
    Whisper,
    WhisperReply,
    WhisperState,
)
from bot_modules.whisper.embeds import (
    build_inbox_embed,
    build_reply_audit_embed,
    build_reply_report_audit_embed,
    build_report_audit_embed,
    build_send_feed_embed,
    build_share_feed_embed,
    inbox_option_description,
    inbox_option_label,
)
from bot_modules.whisper.logic import (
    LAUNCHER_MESSAGE_BODY,
    check_send_cooldown,
    filter_whispers_by_message,
    format_cooldown_message,
    format_expose_dm_suffix,
    format_hourly_cap_message,
    format_reply_dm_body,
    format_send_dm_body,
    format_time_ago,
    fuzzy_score_members,
    inbox_action_buttons,
    inbox_footer,
    inbox_select_placeholder,
    member_picker_placeholder,
    preview,
    prune_recent_target_sends,
    recompute_inbox_after_delete,
    status_pill,
)


# ── Test fixtures ─────────────────────────────────────────────────────


def _whisper(
    *,
    id: int = 1,
    guild_id: int = 100,
    sender_id: int = 10,
    target_id: int = 20,
    message: str = "hello",
    created_at: float = 1_000.0,
    state: WhisperState = STATE_PENDING,
    solved: bool = False,
    exposed: bool = False,
    guesses_left: int = 3,
    channel_msg_id: int | None = None,
    dm_msg_id: int | None = None,
    deleted_at: float | None = None,
) -> Whisper:
    return Whisper(
        id=id,
        guild_id=guild_id,
        sender_id=sender_id,
        target_id=target_id,
        message=message,
        created_at=created_at,
        state=state,
        solved=solved,
        exposed=exposed,
        guesses_left=guesses_left,
        channel_msg_id=channel_msg_id,
        dm_msg_id=dm_msg_id,
        deleted_at=deleted_at,
    )


@dataclass
class _FakeMember:
    """Minimal stand-in — only display_name is read by the helpers."""

    display_name: str


# ── format_time_ago ───────────────────────────────────────────────────


def test_format_time_ago_seconds():
    assert format_time_ago(1_000.0, now=1_030.0) == "30s ago"


def test_format_time_ago_minutes():
    assert format_time_ago(1_000.0, now=1_000.0 + 5 * 60) == "5m ago"


def test_format_time_ago_hours():
    assert format_time_ago(1_000.0, now=1_000.0 + 3 * 3600) == "3h ago"


def test_format_time_ago_one_day_singular():
    # Singular: 1 day → "1 day ago" (no plural s)
    assert format_time_ago(1_000.0, now=1_000.0 + 86400) == "1 day ago"


def test_format_time_ago_multiple_days_plural():
    assert format_time_ago(1_000.0, now=1_000.0 + 86400 * 4) == "4 days ago"


def test_format_time_ago_clamps_to_zero_for_future_inputs():
    # Negative delta should not produce negative output
    assert format_time_ago(2_000.0, now=1_000.0) == "0s ago"


# ── status_pill ───────────────────────────────────────────────────────


def test_status_pill_exposed_beats_everything():
    w = _whisper(exposed=True, solved=True, guesses_left=0)
    assert status_pill(w, now=1_000.0) == "Exposed"


def test_status_pill_solved_when_not_exposed():
    w = _whisper(solved=True)
    assert status_pill(w, now=w.created_at + 1) == "Solved"


def test_status_pill_locked_after_30_days():
    w = _whisper()
    # 30 days + 1 second past creation
    assert status_pill(w, now=w.created_at + 30 * 86400 + 1) == "Locked"


def test_status_pill_no_guesses_when_exhausted_and_not_locked():
    w = _whisper(guesses_left=0)
    assert status_pill(w, now=w.created_at + 1) == "No guesses"


def test_status_pill_shared_for_pending_shared_state():
    w = _whisper(state=STATE_SHARED)
    assert status_pill(w, now=w.created_at + 1) == "Shared"


def test_status_pill_new_for_pending_fresh_whisper():
    w = _whisper(state=STATE_PENDING)
    assert status_pill(w, now=w.created_at + 1) == "New"


def test_status_pill_hidden_state_falls_through_to_new():
    # Hidden state with no other flags is treated as "New" — there's no
    # dedicated pill for hidden because hidden whispers don't appear in
    # the inbox in the first place.
    w = _whisper(state=STATE_HIDDEN)
    assert status_pill(w, now=w.created_at + 1) == "New"


# ── preview ───────────────────────────────────────────────────────────


def test_preview_short_text_unchanged():
    assert preview("hi there") == "hi there"


def test_preview_collapses_newlines_to_spaces():
    assert preview("line1\nline2") == "line1 line2"


def test_preview_truncates_with_ellipsis():
    long = "a" * 100
    out = preview(long, n=10)
    assert len(out) == 10
    assert out.endswith("…")


def test_preview_respects_custom_n():
    out = preview("0123456789abcdef", n=5)
    assert out == "0123…"


# ── fuzzy_score_members ────────────────────────────────────────────────


def test_fuzzy_exact_match_ranks_first():
    members = [
        _FakeMember(display_name="alice"),
        _FakeMember(display_name="bob"),
        _FakeMember(display_name="alicia"),
    ]
    result = fuzzy_score_members(members, "alice")
    assert result[0].display_name == "alice"


def test_fuzzy_prefix_beats_substring():
    members = [
        _FakeMember(display_name="malice"),  # substring
        _FakeMember(display_name="alibaba"),  # prefix
    ]
    result = fuzzy_score_members(members, "ali")
    assert [m.display_name for m in result] == ["alibaba", "malice"]


def test_fuzzy_case_insensitive():
    members = [_FakeMember(display_name="ALICE")]
    assert len(fuzzy_score_members(members, "alice")) == 1


def test_fuzzy_filters_out_non_matches():
    members = [
        _FakeMember(display_name="alice"),
        _FakeMember(display_name="zzz"),
    ]
    result = fuzzy_score_members(members, "alice")
    assert [m.display_name for m in result] == ["alice"]


def test_fuzzy_subsequence_match():
    # 'abc' is a subsequence of 'aXbXc'
    members = [_FakeMember(display_name="aXbXc")]
    result = fuzzy_score_members(members, "abc")
    assert len(result) == 1


def test_fuzzy_empty_query_matches_all_via_subsequence():
    # Empty query has all chars trivially in any name — score 1 for everyone
    members = [_FakeMember(display_name="alice"), _FakeMember(display_name="bob")]
    result = fuzzy_score_members(members, "")
    assert len(result) == 2


# ── filter_whispers_by_message ────────────────────────────────────────


def test_filter_whispers_substring_match():
    ws = [_whisper(id=1, message="hello world"), _whisper(id=2, message="goodbye")]
    out = filter_whispers_by_message(ws, "hello")
    assert [w.id for w in out] == [1]


def test_filter_whispers_case_insensitive():
    ws = [_whisper(id=1, message="HELLO")]
    assert filter_whispers_by_message(ws, "hello") == ws


def test_filter_whispers_returns_fresh_list():
    ws = [_whisper(id=1, message="hi")]
    out = filter_whispers_by_message(ws, "hi")
    assert out is not ws


def test_filter_whispers_no_match_empty():
    ws = [_whisper(id=1, message="hi")]
    assert filter_whispers_by_message(ws, "zzz") == []


# ── build_share_feed_embed ────────────────────────────────────────────


def test_build_share_feed_embed_mentions_target_and_message():
    w = _whisper(target_id=42, message="secret")
    emb = build_share_feed_embed(w)
    assert "fresh Whisper was shared" in (emb.title or "")
    assert "<@42>" in (emb.description or "")
    assert "secret" in (emb.description or "")


def test_build_share_feed_embed_escapes_markdown():
    # Headers / formatting in anonymous content must not render in the
    # public feed — escape_markdown backslash-escapes them.
    w = _whisper(message="# big *bold*")
    emb = build_share_feed_embed(w)
    desc = emb.description or ""
    assert "\\# big" in desc
    assert "\\*bold\\*" in desc


# ── format_expose_dm_suffix ────────────────────────────────────────────


def test_format_expose_dm_suffix_uses_label():
    out = format_expose_dm_suffix("<@123>")
    assert "Sender: <@123>" in out
    assert out.startswith("\n\n")


# ── format_reply_dm_body ───────────────────────────────────────────────


def test_format_reply_dm_body_short_preview_intact():
    out = format_reply_dm_body(
        whisper_id=7, whisper_message="hello", reply_content="thanks!"
    )
    assert "Whisper #7" in out
    assert '"hello"' in out
    assert "```thanks!```" in out


def test_format_reply_dm_body_truncates_long_preview():
    long = "x" * 300
    out = format_reply_dm_body(
        whisper_id=1, whisper_message=long, reply_content="ok"
    )
    # Truncated to 197 + … = 198 chars in the preview position
    assert "…" in out


def test_format_reply_dm_body_escapes_codefences():
    out = format_reply_dm_body(
        whisper_id=1,
        whisper_message="msg with ``` fence",
        reply_content="reply ``` inside",
    )
    assert "ʼʼʼ" in out


# ── inbox_footer ──────────────────────────────────────────────────────


def test_inbox_footer_locked_first():
    w = _whisper()
    # well past lock duration
    out = inbox_footer(w, mode="received", now=w.created_at + 100 * 86400)
    assert "Locked" in out


def test_inbox_footer_solved():
    w = _whisper(solved=True)
    assert inbox_footer(w, mode="received", now=w.created_at + 1) == "Solved."


def test_inbox_footer_out_of_guesses():
    w = _whisper(guesses_left=0)
    out = inbox_footer(w, mode="received", now=w.created_at + 1)
    assert "Out of guesses" in out


def test_inbox_footer_received_mode_shows_left():
    w = _whisper(guesses_left=2)
    assert (
        inbox_footer(w, mode="received", now=w.created_at + 1)
        == "2 guesses left."
    )


def test_inbox_footer_sent_mode_says_for_the_target():
    w = _whisper(guesses_left=2)
    assert (
        inbox_footer(w, mode="sent", now=w.created_at + 1)
        == "2 guesses remain for the target."
    )


# ── build_inbox_embed ─────────────────────────────────────────────────


def test_inbox_embed_empty_received():
    emb = build_inbox_embed(whispers=[], selected=None, mode="received")
    assert emb.title is not None
    assert "Your Inbox" in emb.title
    assert "(0)" in emb.title
    assert emb.description is not None
    assert "No whispers" in emb.description


def test_inbox_embed_empty_sent():
    emb = build_inbox_embed(whispers=[], selected=None, mode="sent")
    assert emb.title is not None
    assert "Sent" in emb.title
    assert emb.description is not None
    assert "haven't sent" in emb.description


def test_inbox_embed_no_selection_prompts_picker():
    w = _whisper()
    emb = build_inbox_embed(whispers=[w], selected=None, mode="received")
    assert emb.description is not None
    assert "Pick a whisper" in emb.description


def test_inbox_embed_received_header_no_arrow():
    w = _whisper(id=42, target_id=999, message="secret", created_at=1000.0)
    emb = build_inbox_embed(
        whispers=[w], selected=w, mode="received", now=1000.0 + 60,
    )
    assert emb.description is not None
    # received mode shouldn't include the "→ target" segment
    assert "Whisper #42" in emb.description
    assert "→ <@999>" not in emb.description
    assert "```secret```" in emb.description


def test_inbox_embed_sent_header_includes_target_arrow():
    w = _whisper(id=42, target_id=999, message="secret", created_at=1000.0)
    emb = build_inbox_embed(
        whispers=[w], selected=w, mode="sent", now=1000.0 + 60,
    )
    assert emb.description is not None
    assert "→ <@999>" in emb.description


def test_inbox_embed_includes_footer():
    w = _whisper(guesses_left=2)
    emb = build_inbox_embed(
        whispers=[w], selected=w, mode="received", now=w.created_at + 1,
    )
    assert emb.footer.text is not None
    assert "2 guesses" in emb.footer.text


def test_inbox_embed_count_in_title():
    ws = [_whisper(id=i) for i in range(1, 4)]
    emb = build_inbox_embed(whispers=ws, selected=ws[0], mode="received")
    assert emb.title is not None
    assert "(3)" in emb.title


# ── inbox_option_label / description ──────────────────────────────────


def test_inbox_option_label_includes_id_and_status():
    w = _whisper(id=7, state=STATE_SHARED, created_at=1000.0)
    label = inbox_option_label(w, now=1000.0 + 60)
    assert label.startswith("#7")
    assert "Shared" in label
    assert "1m ago" in label


def test_inbox_option_description_returns_preview():
    w = _whisper(message="some content here")
    assert inbox_option_description(w) == "some content here"


def test_inbox_option_description_returns_none_when_empty():
    w = _whisper(message="")
    assert inbox_option_description(w) is None


def test_inbox_option_description_caps_at_100_chars():
    w = _whisper(message="a" * 500)
    out = inbox_option_description(w)
    assert out is not None
    assert len(out) <= 100


# ── build_reply_audit_embed ───────────────────────────────────────────


def test_reply_audit_embed_basic_fields():
    pinned = datetime(2026, 1, 1, tzinfo=timezone.utc)
    emb = build_reply_audit_embed(
        whisper_id=7,
        from_user_id=100,
        to_user_id=200,
        content="hello",
        now=pinned,
    )
    assert emb.title == "Whisper Reply"
    assert emb.description == "hello"
    assert emb.timestamp == pinned
    assert emb.fields[0].name == "From"
    assert emb.fields[0].value is not None
    assert "100" in emb.fields[0].value
    assert emb.fields[1].name == "To"
    assert emb.fields[1].value is not None
    assert "200" in emb.fields[1].value
    assert emb.fields[2].name == "Whisper ID"
    assert emb.fields[2].value == "7"


def test_reply_audit_embed_escapes_codefence():
    emb = build_reply_audit_embed(
        whisper_id=1,
        from_user_id=1,
        to_user_id=2,
        content="evil ``` fence",
    )
    assert emb.description is not None
    assert "```" not in emb.description
    assert "ʼʼʼ" in emb.description


def test_reply_audit_embed_now_defaults_to_utcnow_when_omitted():
    emb = build_reply_audit_embed(
        whisper_id=1, from_user_id=1, to_user_id=2, content="x"
    )
    assert emb.timestamp is not None


# ── build_report_audit_embed ──────────────────────────────────────────


def test_report_audit_embed_basic_fields():
    w = _whisper(id=9, sender_id=10, target_id=20, message="bad msg")
    emb = build_report_audit_embed(whisper=w, reason="spam")
    assert emb.title == "Whisper Reported"
    assert emb.description == "bad msg"
    assert emb.color == discord.Color.red()
    assert emb.fields[0].name == "Sender"
    assert emb.fields[0].value is not None
    assert "10" in emb.fields[0].value
    assert emb.fields[1].name == "Reporter (Target)"
    assert emb.fields[1].value is not None
    assert "20" in emb.fields[1].value
    assert emb.fields[2].value == "spam"
    assert emb.fields[3].value == "9"


def test_report_audit_embed_pins_timestamp():
    pinned = datetime(2026, 3, 1, tzinfo=timezone.utc)
    w = _whisper()
    emb = build_report_audit_embed(whisper=w, reason="r", now=pinned)
    assert emb.timestamp == pinned


def test_report_audit_embed_escapes_codefence_in_body():
    w = _whisper(message="msg ``` fence")
    emb = build_report_audit_embed(whisper=w, reason="r")
    assert emb.description is not None
    assert "ʼʼʼ" in emb.description


# ── build_reply_report_audit_embed ─────────────────────────────────────


def _reply() -> WhisperReply:
    return WhisperReply(
        id=55,
        whisper_id=7,
        from_user_id=100,
        to_user_id=200,
        content="rude reply",
        created_at=1_000.0,
    )


def test_reply_report_audit_embed_basic_fields():
    emb = build_reply_report_audit_embed(
        reply=_reply(), reporter_id=200, reason="harassment",
    )
    assert emb.title == "Whisper Reply Reported"
    assert emb.description == "rude reply"
    assert emb.color == discord.Color.red()
    assert emb.fields[0].name == "Sender (anonymous)"
    assert emb.fields[0].value is not None
    assert "100" in emb.fields[0].value
    assert emb.fields[1].name == "Reporter (recipient)"
    assert emb.fields[1].value is not None
    assert "200" in emb.fields[1].value
    assert emb.fields[2].value == "harassment"
    assert emb.fields[3].name == "Reply ID"
    assert emb.fields[3].value == "55"
    assert emb.fields[4].name == "Whisper ID"
    assert emb.fields[4].value == "7"


def test_reply_report_audit_embed_pins_timestamp():
    pinned = datetime(2026, 4, 1, tzinfo=timezone.utc)
    emb = build_reply_report_audit_embed(
        reply=_reply(), reporter_id=1, reason="r", now=pinned,
    )
    assert emb.timestamp == pinned


# ── check_send_cooldown ───────────────────────────────────────────────


def test_cooldown_none_when_no_prior_send():
    assert check_send_cooldown(None, now=100, cooldown_seconds=30) is None


def test_cooldown_zero_last_send_treated_as_none():
    # The cog used a ``.get(uid, 0)`` default; verify 0 is permissive.
    assert check_send_cooldown(0, now=100, cooldown_seconds=30) is None


def test_cooldown_returns_seconds_remaining():
    out = check_send_cooldown(100, now=110, cooldown_seconds=30)
    assert out == 20


def test_cooldown_exactly_at_boundary_clears():
    # 30s after a 30s cooldown started — boundary is the moment it clears.
    assert check_send_cooldown(100, now=130, cooldown_seconds=30) is None


def test_cooldown_returns_at_least_one_second_to_avoid_zero():
    # If we're 29.5 s in, int(0.5) would be 0; the helper bumps to 1 so the
    # user never sees "wait 0s".
    out = check_send_cooldown(100, now=129.5, cooldown_seconds=30)
    assert out == 1


# ── prune_recent_target_sends ─────────────────────────────────────────


def test_prune_keeps_recent_drops_old():
    # window=3600. now=3800. t=100→3700 ago (drop), t=300→3500 ago (keep),
    # t=3700→100 ago (keep).
    ts = [100.0, 300.0, 3700.0]
    out = prune_recent_target_sends(ts, now=3800.0)
    assert out == [300.0, 3700.0]


def test_prune_empty_input():
    assert prune_recent_target_sends([], now=1000.0) == []


def test_prune_custom_window():
    ts = [100.0, 250.0]
    out = prune_recent_target_sends(ts, now=300.0, window_seconds=60)
    assert out == [250.0]


def test_prune_boundary_dropped():
    # Exactly window_seconds old → dropped (strict <)
    ts = [100.0]
    assert prune_recent_target_sends(ts, now=160.0, window_seconds=60) == []


# ── format_cooldown / hourly_cap messages ─────────────────────────────


def test_cooldown_message_includes_seconds():
    assert "20s" in format_cooldown_message(20)


def test_hourly_cap_message_includes_cap():
    out = format_hourly_cap_message(5)
    assert "5 whispers" in out


# ── format_send_dm_body / build_send_feed_embed ───────────────────────


def test_send_dm_body_includes_guild_name_and_message():
    out = format_send_dm_body(guild_name="My Server", message="hi there")
    assert "**My Server**" in out
    assert "```hi there```" in out
    assert "3 guesses" in out


def test_send_dm_body_strips_and_escapes_codefence():
    out = format_send_dm_body(
        guild_name="S", message="  trim me ``` evil  ",
    )
    assert "```trim me ʼʼʼ evil```" in out


def test_build_send_feed_embed_mentions_target():
    # The embed carries the visible name; the (separate) content ping is
    # spoilered, so showing the mention here is the one un-hidden name.
    emb = build_send_feed_embed(42)
    assert "<@42>" in emb.description
    assert "anonymous" in emb.description
    assert "Whisper" in emb.title


def test_launcher_message_body_constant():
    assert "Whisper" in LAUNCHER_MESSAGE_BODY


# ── inbox_select_placeholder ───────────────────────────────────────────


def test_inbox_placeholder_no_filter_single_page():
    out = inbox_select_placeholder(
        filter_query="", display_count=3, page=0, page_count=1,
    )
    assert out == "Pick a whisper… (3 total)"


def test_inbox_placeholder_no_filter_multi_page_includes_index():
    out = inbox_select_placeholder(
        filter_query="", display_count=50, page=1, page_count=2,
    )
    assert "(2/2)" in out


def test_inbox_placeholder_filter_plural_match():
    out = inbox_select_placeholder(
        filter_query="hi", display_count=3, page=0, page_count=1,
    )
    assert "3 matches" in out
    assert '"hi"' in out


def test_inbox_placeholder_filter_single_match_singular():
    out = inbox_select_placeholder(
        filter_query="hi", display_count=1, page=0, page_count=1,
    )
    assert "1 match" in out and "1 matches" not in out


# ── member_picker_placeholder ──────────────────────────────────────────


def test_member_picker_base_text_used_when_no_filter_single_page():
    out = member_picker_placeholder(
        filter_query="", display_count=5, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert out == "Pick the sender…"


def test_member_picker_no_filter_multi_page_includes_index():
    out = member_picker_placeholder(
        filter_query="", display_count=30, page=0, page_count=2,
        base="Pick recipient…",
    )
    assert out == "Pick recipient… (1/2)"


def test_member_picker_filter_plural():
    out = member_picker_placeholder(
        filter_query="a", display_count=3, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert "3 matches" in out


def test_member_picker_filter_singular():
    out = member_picker_placeholder(
        filter_query="a", display_count=1, page=0, page_count=1,
        base="Pick the sender…",
    )
    assert "1 match" in out and "1 matches" not in out


def test_member_picker_filter_multi_page_includes_index():
    out = member_picker_placeholder(
        filter_query="a", display_count=30, page=1, page_count=2,
        base="Pick recipient…",
    )
    assert "(2/2)" in out


# ── inbox_action_buttons ───────────────────────────────────────────────


def test_inbox_actions_received_fresh_whisper():
    w = _whisper(state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert actions == ["guess", "share", "reply", "report", "delete"]


def test_inbox_actions_received_solved_no_guess_button():
    w = _whisper(solved=True, state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "guess" not in actions
    # still pending, so share is offered
    assert "share" in actions


def test_inbox_actions_received_no_guesses_left():
    w = _whisper(guesses_left=0)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "guess" not in actions


def test_inbox_actions_received_locked_no_guess_button():
    w = _whisper()
    actions = inbox_action_buttons(
        w, mode="received", now=w.created_at + 100 * 86400,
    )
    assert "guess" not in actions


def test_inbox_actions_received_shared_no_share_button():
    w = _whisper(state=STATE_SHARED)
    actions = inbox_action_buttons(w, mode="received", now=w.created_at + 1)
    assert "share" not in actions
    # reply / report / delete remain
    assert {"reply", "report", "delete"}.issubset(set(actions))


def test_inbox_actions_sent_mode_only_reply_and_delete():
    w = _whisper(state=STATE_PENDING)
    actions = inbox_action_buttons(w, mode="sent", now=w.created_at + 1)
    assert actions == ["reply", "delete"]


# ── recompute_inbox_after_delete ───────────────────────────────────────


def test_delete_clears_state_when_inbox_emptied():
    w = _whisper(id=1)
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=[w],
        display_whispers=[w],
        deleted_id=1,
        page=0,
        page_size=25,
    )
    assert new_all == []
    assert new_display == []
    assert page == 0
    assert sel is None


def test_delete_keeps_page_and_picks_next_row():
    ws = [_whisper(id=i) for i in range(1, 4)]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=ws,
        display_whispers=ws,
        deleted_id=2,
        page=0,
        page_size=25,
    )
    assert [w.id for w in new_all] == [1, 3]
    assert page == 0
    # After deleting #2, first row of (clamped) page is #1
    assert sel == 1


def test_delete_clamps_to_previous_page_when_last_row_on_page_gone():
    # Page size 2: page 0 = [1,2], page 1 = [3]. Delete #3 while on page 1.
    ws = [_whisper(id=i) for i in range(1, 4)]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=ws,
        display_whispers=ws,
        deleted_id=3,
        page=1,
        page_size=2,
    )
    assert [w.id for w in new_display] == [1, 2]
    assert page == 0
    assert sel == 1


def test_delete_respects_display_subset_when_filter_active():
    all_ws = [_whisper(id=i) for i in range(1, 4)]
    # Filter shows only #2 and #3
    display = [all_ws[1], all_ws[2]]
    new_all, new_display, page, sel = recompute_inbox_after_delete(
        all_whispers=all_ws,
        display_whispers=display,
        deleted_id=2,
        page=0,
        page_size=25,
    )
    assert [w.id for w in new_all] == [1, 3]
    assert [w.id for w in new_display] == [3]
    assert sel == 3


def test_inbox_actions_delete_always_last():
    w = _whisper(state=STATE_PENDING)
    for mode in ("received", "sent"):
        actions = inbox_action_buttons(w, mode=mode, now=w.created_at + 1)
        assert actions[-1] == "delete"


def test_reply_report_audit_embed_escapes_codefence():
    reply = WhisperReply(
        id=1,
        whisper_id=1,
        from_user_id=1,
        to_user_id=2,
        content="```evil",
        created_at=0,
    )
    emb = build_reply_report_audit_embed(
        reply=reply, reporter_id=2, reason="r",
    )
    assert emb.description is not None
    assert "ʼʼʼ" in emb.description
