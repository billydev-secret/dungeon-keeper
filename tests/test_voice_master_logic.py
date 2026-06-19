"""Tests for the extracted Voice Master pure-logic modules.

Covers ``bot_modules/voice_master/logic.py`` (validation, decisions,
formatting, planning) and ``bot_modules/voice_master/embeds.py`` (embed
builders). Same pressure-cooker pattern as the starboard/dm_perms
refactors: the cog file keeps Discord glue; this file proves the
helpers behave without any real Discord client.
"""

from __future__ import annotations

import discord

from bot_modules.voice_master.embeds import (
    build_admin_audit_mirror_embed,
    build_inline_panel_embed,
    build_knock_request_embed,
    build_panel_embed,
    build_profile_show_embed,
)
from bot_modules.voice_master.logic import (
    PANEL_BUTTON_ORDER,
    PANEL_GROUP_ORDER,
    PROFILE_RESET_FIELDS,
    ClaimDecision,
    MemberInfo,
    OverwritePlan,
    OverwritePlanEntry,
    PanelButtonMeta,
    RenameValidation,
    UserPickerLabels,
    all_panel_button_metas,
    build_force_clear_profile_summary,
    build_force_delete_summary,
    build_force_transfer_summary,
    build_hub_join_notes,
    build_join_url,
    build_skipped_payload,
    build_transfer_picker_plan,
    classify_claim_attempt,
    format_block_add_result,
    format_blocked_list,
    format_edit_rate_limit_error,
    format_hide_result,
    format_invite_dm,
    format_invite_result,
    format_kick_result,
    format_knock_accepted_dm,
    format_limit_result,
    format_lock_result,
    format_rename_result,
    format_reset_result,
    format_transfer_result,
    format_trust_add_result,
    format_trusted_list,
    hub_create_blocked_by_cooldown,
    panel_button_meta,
    panel_group_placeholder,
    panel_metas_for_group,
    parse_limit_input,
    plan_initial_overwrites,
    profile_reset_summary,
    select_effective_bitrate,
    select_effective_limit,
    should_save_profile_field,
    user_picker_labels,
    validate_block_add,
    validate_invite_target,
    validate_kick_target,
    validate_limit_value,
    validate_rename_input,
    validate_transfer_target,
    validate_trust_add,
)


# ── classify_claim_attempt ───────────────────────────────────────────


def test_claim_caller_already_owner_short_circuits():
    out = classify_claim_attempt(
        owner_present=True,
        owner_left_at=None,
        now=100.0,
        owner_grace_s=300,
        caller_is_owner=True,
    )
    assert out.eligible is False
    assert out.retry_seconds is None
    assert out.error_message is not None
    assert "already own" in out.error_message.lower()


def test_claim_owner_left_server_makes_eligible():
    out = classify_claim_attempt(
        owner_present=False,
        owner_left_at=None,
        now=100.0,
        owner_grace_s=300,
        caller_is_owner=False,
    )
    assert out.eligible is True
    assert out.retry_seconds is None
    assert out.error_message is None


def test_claim_owner_in_channel_blocks():
    """Owner present and never left the channel → reject without retry."""
    out = classify_claim_attempt(
        owner_present=True,
        owner_left_at=None,
        now=100.0,
        owner_grace_s=300,
        caller_is_owner=False,
    )
    assert out.eligible is False
    assert out.retry_seconds is None
    assert out.error_message is not None
    assert "still active" in out.error_message.lower()


def test_claim_owner_left_past_grace_makes_eligible():
    out = classify_claim_attempt(
        owner_present=True,
        owner_left_at=100.0,
        now=500.0,
        owner_grace_s=300,
        caller_is_owner=False,
    )
    assert out.eligible is True
    assert out.error_message is None


def test_claim_within_grace_window_blocks_with_retry():
    out = classify_claim_attempt(
        owner_present=True,
        owner_left_at=100.0,
        now=200.0,  # 100s after leaving, grace is 300s → 200s remaining
        owner_grace_s=300,
        caller_is_owner=False,
    )
    assert out.eligible is False
    assert out.retry_seconds == 200
    assert out.error_message is not None
    assert "100s" in out.error_message
    assert "200s" in out.error_message


def test_claim_at_exact_grace_threshold_is_eligible():
    out = classify_claim_attempt(
        owner_present=True,
        owner_left_at=0.0,
        now=300.0,
        owner_grace_s=300,
        caller_is_owner=False,
    )
    assert out.eligible is True


def test_claim_decision_is_a_dataclass_with_fields():
    d = ClaimDecision(eligible=True, retry_seconds=None, error_message=None)
    assert d.eligible is True
    assert d.retry_seconds is None


# ── validate_trust_add / validate_block_add ──────────────────────────


def test_validate_trust_add_passes_with_normal_target():
    assert (
        validate_trust_add(
            target_is_bot=False,
            target_is_self=False,
            disable_saves=False,
            saveable_fields={"trusted", "blocked"},
        )
        is None
    )


def test_validate_trust_add_rejects_bot():
    out = validate_trust_add(
        target_is_bot=True,
        target_is_self=False,
        disable_saves=False,
        saveable_fields={"trusted"},
    )
    assert out is not None
    assert "bot" in out.lower()


def test_validate_trust_add_rejects_self():
    out = validate_trust_add(
        target_is_bot=False,
        target_is_self=True,
        disable_saves=False,
        saveable_fields={"trusted"},
    )
    assert out is not None
    assert "yourself" in out.lower()


def test_validate_trust_add_rejects_when_saves_disabled():
    out = validate_trust_add(
        target_is_bot=False,
        target_is_self=False,
        disable_saves=True,
        saveable_fields={"trusted"},
    )
    assert out is not None
    assert "disabled" in out.lower()


def test_validate_trust_add_rejects_when_field_not_saveable():
    out = validate_trust_add(
        target_is_bot=False,
        target_is_self=False,
        disable_saves=False,
        saveable_fields={"name", "limit"},  # no "trusted"
    )
    assert out is not None
    assert "disabled" in out.lower()


def test_validate_trust_add_bot_check_wins_over_disable_saves():
    """Bot/self errors are shown even when saves are disabled — they're
    definite even in the alternate world where saves are re-enabled."""
    out = validate_trust_add(
        target_is_bot=True,
        target_is_self=False,
        disable_saves=True,
        saveable_fields=set(),
    )
    assert out is not None
    assert "bot" in out.lower()


def test_validate_block_add_passes_with_normal_target():
    assert (
        validate_block_add(
            target_is_bot=False,
            target_is_self=False,
            disable_saves=False,
            saveable_fields={"blocked"},
        )
        is None
    )


def test_validate_block_add_rejects_bot():
    out = validate_block_add(
        target_is_bot=True,
        target_is_self=False,
        disable_saves=False,
        saveable_fields={"blocked"},
    )
    assert out is not None
    assert "bot" in out.lower()


def test_validate_block_add_rejects_self_with_block_specific_wording():
    out = validate_block_add(
        target_is_bot=False,
        target_is_self=True,
        disable_saves=False,
        saveable_fields={"blocked"},
    )
    assert out is not None
    # "Can't block yourself" is different from trust's "trusted by yourself".
    assert "yourself" in out.lower()


def test_validate_block_add_rejects_when_saves_disabled():
    out = validate_block_add(
        target_is_bot=False,
        target_is_self=False,
        disable_saves=True,
        saveable_fields={"blocked"},
    )
    assert out is not None


def test_validate_block_add_rejects_when_field_not_saveable():
    out = validate_block_add(
        target_is_bot=False,
        target_is_self=False,
        disable_saves=False,
        saveable_fields={"name"},
    )
    assert out is not None


# ── format_*_add_result ──────────────────────────────────────────────


def test_format_trust_add_result_idempotent():
    out = format_trust_add_result(
        target_mention="<@1>", added=False, evicted_id=None
    )
    assert "already" in out
    assert "<@1>" in out


def test_format_trust_add_result_clean_add():
    out = format_trust_add_result(
        target_mention="<@1>", added=True, evicted_id=None
    )
    assert "Added" in out
    assert "<@1>" in out
    assert "Cap" not in out


def test_format_trust_add_result_eviction():
    out = format_trust_add_result(
        target_mention="<@1>", added=True, evicted_id=99
    )
    assert "<@1>" in out
    assert "<@99>" in out
    assert "Cap reached" in out


def test_format_block_add_result_idempotent_uses_blocklist_wording():
    out = format_block_add_result(
        target_mention="<@1>", added=False, evicted_id=None
    )
    assert "blocklist" in out.lower()


def test_format_block_add_result_eviction_mentions_evicted():
    out = format_block_add_result(
        target_mention="<@1>", added=True, evicted_id=99
    )
    assert "<@99>" in out
    assert "Cap reached" in out


# ── format_*_list ────────────────────────────────────────────────────


def test_format_trusted_list_empty():
    assert format_trusted_list([]) == "Your trust list is empty."


def test_format_trusted_list_renders_mentions_with_count():
    out = format_trusted_list([1, 2, 3])
    assert "Trusted (3)" in out
    assert "<@1>" in out
    assert "<@2>" in out
    assert "<@3>" in out


def test_format_blocked_list_empty():
    assert format_blocked_list([]) == "Your blocklist is empty."


def test_format_blocked_list_renders_mentions_with_count():
    out = format_blocked_list([42, 7])
    assert "Blocked (2)" in out
    assert "<@42>" in out
    assert "<@7>" in out


# ── plan_initial_overwrites ──────────────────────────────────────────


def test_plan_initial_overwrites_default_profile_no_trust_no_block():
    """Smallest plan: just @everyone (unchanged) and the owner."""
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=False,
        profile_hidden=False,
        trusted_ids=[],
        blocked_ids=[],
        present_member_ids=set(),
    )
    assert isinstance(plan, OverwritePlan)
    assert len(plan.entries) == 2
    assert plan.entries[0].target_kind == "everyone"
    assert plan.entries[0].target_id == 99
    assert plan.entries[0].view_channel is None
    assert plan.entries[0].connect is None
    assert plan.entries[1].target_kind == "owner"
    assert plan.entries[1].target_id == 10
    assert plan.entries[1].view_channel is True
    assert plan.entries[1].connect is True
    assert plan.missing_target_ids == []


def test_plan_initial_overwrites_locked_denies_everyone_connect():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=True,
        profile_hidden=False,
        trusted_ids=[],
        blocked_ids=[],
        present_member_ids=set(),
    )
    everyone = plan.entries[0]
    assert everyone.connect is False
    assert everyone.view_channel is None


def test_plan_initial_overwrites_hidden_denies_everyone_view():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=False,
        profile_hidden=True,
        trusted_ids=[],
        blocked_ids=[],
        present_member_ids=set(),
    )
    everyone = plan.entries[0]
    assert everyone.view_channel is False
    assert everyone.connect is None


def test_plan_initial_overwrites_locked_and_hidden_combined():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=True,
        profile_hidden=True,
        trusted_ids=[],
        blocked_ids=[],
        present_member_ids=set(),
    )
    everyone = plan.entries[0]
    assert everyone.connect is False
    assert everyone.view_channel is False


def test_plan_initial_overwrites_trusted_resolved_when_present():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=False,
        profile_hidden=False,
        trusted_ids=[2, 3],
        blocked_ids=[],
        present_member_ids={2, 3, 10},
    )
    trust_entries = [e for e in plan.entries if e.target_kind == "trusted"]
    assert len(trust_entries) == 2
    assert {e.target_id for e in trust_entries} == {2, 3}
    for e in trust_entries:
        assert e.view_channel is True
        assert e.connect is True
    assert plan.missing_target_ids == []


def test_plan_initial_overwrites_blocked_resolved_when_present():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=False,
        profile_hidden=False,
        trusted_ids=[],
        blocked_ids=[4],
        present_member_ids={4, 10},
    )
    block_entries = [e for e in plan.entries if e.target_kind == "blocked"]
    assert len(block_entries) == 1
    assert block_entries[0].target_id == 4
    assert block_entries[0].connect is False
    # view_channel left to inherit so hidden+blocked still hides them, etc.
    assert block_entries[0].view_channel is None


def test_plan_initial_overwrites_missing_targets_are_dropped():
    plan = plan_initial_overwrites(
        owner_id=10,
        everyone_role_id=99,
        profile_locked=False,
        profile_hidden=False,
        trusted_ids=[2, 3],
        blocked_ids=[4],
        present_member_ids={2},  # 3 and 4 left the guild
    )
    trust_ids = [e.target_id for e in plan.entries if e.target_kind == "trusted"]
    block_ids = [e.target_id for e in plan.entries if e.target_kind == "blocked"]
    assert trust_ids == [2]
    assert block_ids == []
    # missing list preserves order: trusted-missing first, then blocked-missing.
    assert plan.missing_target_ids == [3, 4]


def test_overwrite_plan_entry_is_a_dataclass():
    e = OverwritePlanEntry(
        target_id=1, target_kind="owner", view_channel=True, connect=True
    )
    assert e.target_id == 1


# ── select_effective_limit / bitrate ─────────────────────────────────


def test_select_effective_limit_saved_overrides_default():
    assert select_effective_limit(saved_limit=5, default_user_limit=10) == 5


def test_select_effective_limit_falls_back_to_default():
    assert select_effective_limit(saved_limit=0, default_user_limit=10) == 10


def test_select_effective_limit_both_zero_returns_zero():
    assert select_effective_limit(saved_limit=0, default_user_limit=0) == 0


def test_select_effective_bitrate_saved_wins():
    assert (
        select_effective_bitrate(
            saved_bitrate=64000, default_bitrate=32000, guild_max_bitrate=96000
        )
        == 64000
    )


def test_select_effective_bitrate_none_falls_back():
    assert (
        select_effective_bitrate(
            saved_bitrate=None, default_bitrate=32000, guild_max_bitrate=96000
        )
        == 32000
    )


def test_select_effective_bitrate_zero_falls_back():
    """0 is treated as 'use default' the same as None."""
    assert (
        select_effective_bitrate(
            saved_bitrate=0, default_bitrate=32000, guild_max_bitrate=96000
        )
        == 32000
    )


def test_select_effective_bitrate_falls_back_to_guild_max():
    """No saved and no default → use the highest bitrate the guild allows."""
    assert (
        select_effective_bitrate(
            saved_bitrate=0, default_bitrate=0, guild_max_bitrate=384000
        )
        == 384000
    )


def test_select_effective_bitrate_clamps_to_guild_max():
    """A value saved under a higher boost tier is clamped to the current max."""
    assert (
        select_effective_bitrate(
            saved_bitrate=384000, default_bitrate=0, guild_max_bitrate=96000
        )
        == 96000
    )


# ── build_skipped_payload / build_hub_join_notes ─────────────────────


def test_build_skipped_payload_empty():
    assert build_skipped_payload(name_fell_back=False, missing_target_count=0) == []


def test_build_skipped_payload_only_name():
    assert build_skipped_payload(name_fell_back=True, missing_target_count=0) == [
        "name",
    ]


def test_build_skipped_payload_only_missing():
    assert build_skipped_payload(name_fell_back=False, missing_target_count=3) == [
        "missing_members",
    ]


def test_build_skipped_payload_both_in_stable_order():
    out = build_skipped_payload(name_fell_back=True, missing_target_count=2)
    assert out == ["name", "missing_members"]


def test_build_hub_join_notes_returns_none_when_nothing_to_say():
    assert (
        build_hub_join_notes(
            name_fell_back=False,
            fallback_name="anything",
            missing_target_count=0,
        )
        is None
    )


def test_build_hub_join_notes_name_only():
    out = build_hub_join_notes(
        name_fell_back=True,
        fallback_name="Alice's Room",
        missing_target_count=0,
    )
    assert out is not None
    assert "Alice's Room" in out
    assert "blocked" in out.lower()


def test_build_hub_join_notes_missing_only_mentions_count():
    out = build_hub_join_notes(
        name_fell_back=False,
        fallback_name="ignored",
        missing_target_count=3,
    )
    assert out is not None
    assert "3 member" in out


def test_build_hub_join_notes_both_joined_by_newline():
    out = build_hub_join_notes(
        name_fell_back=True,
        fallback_name="Bob's Room",
        missing_target_count=2,
    )
    assert out is not None
    assert "\n" in out
    assert "Bob's Room" in out
    assert "2 member" in out


# ── hub_create_blocked_by_cooldown ───────────────────────────────────


def test_hub_create_blocked_by_cooldown_disabled_when_zero():
    assert (
        hub_create_blocked_by_cooldown(now=10.0, last_create_at=10.0, cooldown_s=0)
        is False
    )


def test_hub_create_blocked_by_cooldown_blocks_within_window():
    assert (
        hub_create_blocked_by_cooldown(
            now=20.0, last_create_at=10.0, cooldown_s=30
        )
        is True
    )


def test_hub_create_blocked_by_cooldown_passes_after_window():
    assert (
        hub_create_blocked_by_cooldown(
            now=50.0, last_create_at=10.0, cooldown_s=30
        )
        is False
    )


def test_hub_create_blocked_by_cooldown_passes_at_exact_boundary():
    # `(now - last) < cooldown_s` — at exactly cooldown_s the gate opens.
    assert (
        hub_create_blocked_by_cooldown(
            now=40.0, last_create_at=10.0, cooldown_s=30
        )
        is False
    )


def test_hub_create_blocked_by_cooldown_handles_default_last():
    """First-ever join uses last_create_at=0.0; should not block."""
    assert (
        hub_create_blocked_by_cooldown(
            now=10_000.0, last_create_at=0.0, cooldown_s=30
        )
        is False
    )


# ── profile_reset_summary / PROFILE_RESET_FIELDS ─────────────────────


def test_profile_reset_summary_all():
    out = profile_reset_summary("all")
    assert "profile" in out.lower()
    assert "trust" in out.lower()
    assert "blocklist" in out.lower()


def test_profile_reset_summary_trusted():
    assert "Trust list" in profile_reset_summary("trusted")


def test_profile_reset_summary_blocked():
    assert "Blocklist" in profile_reset_summary("blocked")


def test_profile_reset_summary_named_field():
    for field in ("name", "limit", "locked", "hidden"):
        out = profile_reset_summary(field)
        assert f"`{field}`" in out


def test_profile_reset_summary_unknown_field_falls_back_safely():
    """Defensive: an unknown field doesn't raise; just renders a generic line."""
    out = profile_reset_summary("nonsense")
    assert "nonsense" in out


def test_profile_reset_fields_contains_all_choice_values():
    expected = {"all", "name", "limit", "locked", "hidden", "trusted", "blocked"}
    assert PROFILE_RESET_FIELDS == expected


# ── force-* admin summaries ──────────────────────────────────────────


def test_build_force_delete_summary_renders_inline_id_and_mention():
    out = build_force_delete_summary(
        channel_name="alice's room", channel_id=123, owner_id=999
    )
    assert "`alice's room`" in out
    assert "`123`" in out
    assert "<@999>" in out


def test_build_force_transfer_summary_shows_arrow_between_owners():
    out = build_force_transfer_summary(
        channel_name="room",
        channel_id=123,
        old_owner_id=1,
        new_owner_mention="<@2>",
    )
    assert "<@1>" in out
    assert "<@2>" in out
    assert "→" in out


def test_build_force_clear_profile_summary_mentions_target():
    out = build_force_clear_profile_summary(target_mention="<@42>")
    assert "<@42>" in out
    assert "Cleared" in out


# ── embeds.build_profile_show_embed ──────────────────────────────────


def test_build_profile_show_embed_uses_template_default_for_empty_name():
    embed = build_profile_show_embed(
        saved_name=None,
        saved_limit=0,
        locked=False,
        hidden=False,
        trusted_count=0,
        blocked_count=0,
    )
    assert isinstance(embed, discord.Embed)
    fields = {f.name: f.value or "" for f in embed.fields}
    assert "*(template default)*" in fields["Saved name"]
    assert fields["User limit"] == "no cap"
    assert fields["Locked"] == "no"
    assert fields["Hidden"] == "no"
    assert fields["Trusted (count)"] == "0"
    assert fields["Blocked (count)"] == "0"


def test_build_profile_show_embed_renders_saved_name():
    embed = build_profile_show_embed(
        saved_name="my room",
        saved_limit=5,
        locked=True,
        hidden=True,
        trusted_count=3,
        blocked_count=1,
    )
    fields = {f.name: f.value or "" for f in embed.fields}
    assert fields["Saved name"] == "my room"
    assert fields["User limit"] == "5"
    assert fields["Locked"] == "yes"
    assert fields["Hidden"] == "yes"
    assert fields["Trusted (count)"] == "3"
    assert fields["Blocked (count)"] == "1"


def test_build_profile_show_embed_has_blurple_color():
    embed = build_profile_show_embed(
        saved_name=None,
        saved_limit=0,
        locked=False,
        hidden=False,
        trusted_count=0,
        blocked_count=0,
    )
    assert embed.color == discord.Color.blurple()


# ── embeds.build_admin_audit_mirror_embed ────────────────────────────


def test_build_admin_audit_mirror_embed_prefixes_action_in_title():
    embed = build_admin_audit_mirror_embed(
        action="force-delete",
        summary="Deleted X.",
        actor_name="mod#1234",
        actor_id=42,
    )
    assert embed.title is not None
    assert "Voice Master" in embed.title
    assert "force-delete" in embed.title


def test_build_admin_audit_mirror_embed_uses_summary_as_description():
    embed = build_admin_audit_mirror_embed(
        action="force-transfer",
        summary="<@1> → <@2>",
        actor_name="mod",
        actor_id=42,
    )
    assert embed.description == "<@1> → <@2>"


def test_build_admin_audit_mirror_embed_sets_actor_footer():
    embed = build_admin_audit_mirror_embed(
        action="force-clear-profile",
        summary="Cleared.",
        actor_name="mod#1234",
        actor_id=42,
    )
    assert embed.footer.text is not None
    assert "mod#1234" in embed.footer.text
    assert "42" in embed.footer.text


def test_build_admin_audit_mirror_embed_color_is_orange():
    embed = build_admin_audit_mirror_embed(
        action="force-delete",
        summary="x",
        actor_name="mod",
        actor_id=1,
    )
    assert embed.color == discord.Color.orange()


# ── validate_rename_input ────────────────────────────────────────────


def test_validate_rename_input_accepts_normal_name():
    out = validate_rename_input(
        "Game Night", max_len=100, blocklist_patterns=[]
    )
    assert isinstance(out, RenameValidation)
    assert out.error_message is None
    assert out.cleaned == "Game Night"


def test_validate_rename_input_strips_whitespace():
    out = validate_rename_input(
        "   Hello   ", max_len=100, blocklist_patterns=[]
    )
    assert out.error_message is None
    assert out.cleaned == "Hello"


def test_validate_rename_input_rejects_empty_after_strip():
    out = validate_rename_input("    ", max_len=100, blocklist_patterns=[])
    assert out.error_message is not None
    assert "empty" in out.error_message.lower()


def test_validate_rename_input_rejects_empty_string():
    out = validate_rename_input("", max_len=100, blocklist_patterns=[])
    assert out.error_message is not None


def test_validate_rename_input_truncates_long_names():
    long_name = "x" * 200
    out = validate_rename_input(long_name, max_len=100, blocklist_patterns=[])
    assert out.error_message is None
    assert len(out.cleaned) == 100


def test_validate_rename_input_rejects_blocked_pattern_case_insensitive():
    out = validate_rename_input(
        "BAD Channel", max_len=100, blocklist_patterns=["bad"]
    )
    assert out.error_message is not None
    assert "filter" in out.error_message.lower()


def test_validate_rename_input_ignores_empty_pattern_in_blocklist():
    """An empty string pattern shouldn't match every name."""
    out = validate_rename_input(
        "Safe", max_len=100, blocklist_patterns=["", "bad"]
    )
    assert out.error_message is None


# ── validate_limit_value / parse_limit_input ─────────────────────────


def test_validate_limit_value_accepts_zero():
    assert validate_limit_value(0) is None


def test_validate_limit_value_accepts_99():
    assert validate_limit_value(99) is None


def test_validate_limit_value_rejects_negative():
    out = validate_limit_value(-1)
    assert out is not None
    assert "0" in out


def test_validate_limit_value_rejects_over_99():
    out = validate_limit_value(100)
    assert out is not None
    assert "99" in out


def test_parse_limit_input_clean_int():
    value, err = parse_limit_input("5")
    assert err is None
    assert value == 5


def test_parse_limit_input_strips_whitespace():
    value, err = parse_limit_input("  5  ")
    assert err is None
    assert value == 5


def test_parse_limit_input_rejects_non_integer():
    value, err = parse_limit_input("abc")
    assert value is None
    assert err is not None
    assert "whole number" in err


def test_parse_limit_input_rejects_float():
    value, err = parse_limit_input("3.14")
    assert value is None
    assert err is not None


# ── validate_transfer_target / invite / kick ─────────────────────────


def test_validate_transfer_target_accepts_normal():
    assert (
        validate_transfer_target(
            target_is_bot=False,
            target_is_current_owner=False,
            target_in_channel=True,
        )
        is None
    )


def test_validate_transfer_target_rejects_bot():
    out = validate_transfer_target(
        target_is_bot=True,
        target_is_current_owner=False,
        target_in_channel=True,
    )
    assert out is not None
    assert "bot" in out.lower()


def test_validate_transfer_target_rejects_current_owner():
    out = validate_transfer_target(
        target_is_bot=False,
        target_is_current_owner=True,
        target_in_channel=True,
    )
    assert out is not None
    assert "already" in out.lower()


def test_validate_transfer_target_rejects_not_in_channel():
    out = validate_transfer_target(
        target_is_bot=False,
        target_is_current_owner=False,
        target_in_channel=False,
    )
    assert out is not None
    assert "must" in out.lower()


def test_validate_invite_target_accepts_normal():
    assert (
        validate_invite_target(target_is_bot=False, target_is_owner=False) is None
    )


def test_validate_invite_target_rejects_bot():
    out = validate_invite_target(target_is_bot=True, target_is_owner=False)
    assert out is not None
    assert "bot" in out.lower()


def test_validate_invite_target_rejects_owner_self():
    out = validate_invite_target(target_is_bot=False, target_is_owner=True)
    assert out is not None
    assert "already" in out.lower()


def test_validate_kick_target_accepts_normal():
    assert (
        validate_kick_target(
            target_is_bot=False, target_is_self_owner=False
        )
        is None
    )


def test_validate_kick_target_rejects_bot():
    out = validate_kick_target(target_is_bot=True, target_is_self_owner=False)
    assert out is not None
    assert "bot" in out.lower()


def test_validate_kick_target_rejects_self_owner():
    out = validate_kick_target(target_is_bot=False, target_is_self_owner=True)
    assert out is not None
    assert "yourself" in out.lower()


# ── should_save_profile_field ────────────────────────────────────────


def test_should_save_profile_field_true_when_listed_and_saves_enabled():
    assert (
        should_save_profile_field(
            saveable_key="name",
            disable_saves=False,
            saveable_fields={"name", "limit"},
        )
        is True
    )


def test_should_save_profile_field_false_when_saves_disabled():
    assert (
        should_save_profile_field(
            saveable_key="name",
            disable_saves=True,
            saveable_fields={"name", "limit"},
        )
        is False
    )


def test_should_save_profile_field_false_when_not_in_list():
    assert (
        should_save_profile_field(
            saveable_key="name",
            disable_saves=False,
            saveable_fields={"limit"},
        )
        is False
    )


# ── Result formatters ────────────────────────────────────────────────


def test_format_edit_rate_limit_error_renders_minutes_and_seconds():
    out = format_edit_rate_limit_error(retry_seconds=42.0, window_s=600.0)
    assert "10 minutes" in out
    assert "42s" in out


def test_format_edit_rate_limit_error_floors_retry_seconds():
    out = format_edit_rate_limit_error(retry_seconds=42.9, window_s=600.0)
    assert "42s" in out


def test_format_lock_result_locked():
    assert "locked" in format_lock_result(locked=True)


def test_format_lock_result_unlocked():
    assert "unlocked" in format_lock_result(locked=False)


def test_format_hide_result_hidden():
    assert "hidden" in format_hide_result(hidden=True)


def test_format_hide_result_visible():
    assert "visible" in format_hide_result(hidden=False)


def test_format_rename_result_includes_name():
    out = format_rename_result(new_name="Game Night")
    assert "Game Night" in out
    assert "Renamed" in out


def test_format_limit_result_zero_shows_no_cap():
    out = format_limit_result(new_limit=0)
    assert "no cap" in out


def test_format_limit_result_positive_shows_number():
    out = format_limit_result(new_limit=5)
    assert "5" in out
    assert "no cap" not in out


def test_format_reset_result_channel_only():
    out = format_reset_result(also_profile=False)
    assert "unchanged" in out
    assert "profile" in out.lower()


def test_format_reset_result_includes_profile():
    out = format_reset_result(also_profile=True)
    assert "profile" in out.lower()
    assert "reset" in out.lower()


def test_format_transfer_result_mentions_new_owner():
    out = format_transfer_result(new_owner_mention="<@42>")
    assert "<@42>" in out
    assert "transferred" in out.lower()


def test_format_invite_result_remember_uses_remembered_wording():
    out = format_invite_result(
        target_mention="<@1>", remember=True, cap_evicted_id=None
    )
    assert "<@1>" in out
    assert "remembered" in out
    assert "Trust" not in out  # no eviction note


def test_format_invite_result_without_remember_uses_invited_wording():
    out = format_invite_result(
        target_mention="<@1>", remember=False, cap_evicted_id=None
    )
    assert "invited" in out
    assert "remembered" not in out


def test_format_invite_result_cap_eviction_mentions_evicted_id():
    out = format_invite_result(
        target_mention="<@1>", remember=True, cap_evicted_id=99
    )
    assert "<@99>" in out
    assert "Trust list cap" in out


def test_format_kick_result_remember_uses_blocked_wording():
    out = format_kick_result(
        target_mention="<@1>", remember=True, cap_evicted_id=None
    )
    assert "blocked permanently" in out


def test_format_kick_result_without_remember_uses_kicked_wording():
    out = format_kick_result(
        target_mention="<@1>", remember=False, cap_evicted_id=None
    )
    assert "kicked" in out
    assert "blocked" not in out.lower()


def test_format_kick_result_cap_eviction_mentions_evicted_id():
    out = format_kick_result(
        target_mention="<@1>", remember=True, cap_evicted_id=99
    )
    assert "<@99>" in out
    assert "Block list cap" in out


# ── DM / URL builders ────────────────────────────────────────────────


def test_build_join_url_format():
    assert (
        build_join_url(guild_id=111, channel_id=222)
        == "https://discord.com/channels/111/222"
    )


def test_format_invite_dm_includes_all_pieces():
    out = format_invite_dm(
        channel_name="Game Night",
        inviter_mention="<@1>",
        guild_name="My Server",
        join_url="https://example/x/y",
    )
    assert "Game Night" in out
    assert "<@1>" in out
    assert "My Server" in out
    assert "https://example/x/y" in out


def test_format_knock_accepted_dm_includes_url_and_channel():
    out = format_knock_accepted_dm(
        channel_name="Game Night",
        join_url="https://example/x/y",
    )
    assert "Game Night" in out
    assert "https://example/x/y" in out
    assert "accepted" in out


# ── Transfer picker plan ─────────────────────────────────────────────


def test_build_transfer_picker_plan_filters_bots_and_owner():
    members = [
        MemberInfo(id=1, display_name="Alice", name="alice", is_bot=False),
        MemberInfo(id=2, display_name="Bob",   name="bob",   is_bot=True),
        MemberInfo(id=3, display_name="Owner", name="owner", is_bot=False),
    ]
    plan = build_transfer_picker_plan(members, owner_id=3)
    assert plan.has_options is True
    assert len(plan.options) == 1
    assert plan.options[0].value == "1"
    assert plan.options[0].label == "Alice"
    assert plan.options[0].description == "@alice"


def test_build_transfer_picker_plan_empty_when_only_owner_and_bots():
    members = [
        MemberInfo(id=2, display_name="Bob", name="bob", is_bot=True),
        MemberInfo(id=3, display_name="Me",  name="me",  is_bot=False),
    ]
    plan = build_transfer_picker_plan(members, owner_id=3)
    assert plan.has_options is False
    assert plan.options == []


def test_build_transfer_picker_plan_truncates_at_max_options():
    members = [
        MemberInfo(id=i, display_name=f"u{i}", name=f"u{i}", is_bot=False)
        for i in range(1, 40)
    ]
    plan = build_transfer_picker_plan(members, owner_id=999, max_options=25)
    assert len(plan.options) == 25
    assert plan.options[0].value == "1"
    assert plan.options[-1].value == "25"


# ── User picker labels ───────────────────────────────────────────────


def test_user_picker_labels_invite_mode():
    labels = user_picker_labels("invite")
    assert isinstance(labels, UserPickerLabels)
    assert "invite" in labels.placeholder.lower()
    assert labels.action_one == "Invite"
    assert "Trusted" in labels.action_two


def test_user_picker_labels_kick_mode():
    labels = user_picker_labels("kick")
    assert "kick" in labels.placeholder.lower()
    assert labels.action_one == "Kick"
    assert "block" in labels.action_two.lower()


def test_user_picker_labels_unknown_falls_back_to_invite():
    """A typo shouldn't crash the cog — fall back to invite-mode wording."""
    labels = user_picker_labels("nonsense")
    assert labels.action_one == "Invite"


# ── Panel button registry ────────────────────────────────────────────


def test_panel_button_order_lists_all_actions():
    assert "lock" in PANEL_BUTTON_ORDER
    assert "transfer" in PANEL_BUTTON_ORDER
    assert "reset" in PANEL_BUTTON_ORDER
    assert len(PANEL_BUTTON_ORDER) == 10


def test_panel_button_meta_known_action():
    meta = panel_button_meta("lock")
    assert isinstance(meta, PanelButtonMeta)
    assert meta.label == "Lock"
    assert meta.emoji == "🔒"


def test_panel_button_meta_unknown_action_returns_none():
    assert panel_button_meta("nonsense") is None


def test_all_panel_button_metas_returns_canonical_order():
    metas = all_panel_button_metas()
    assert [m.action for m in metas] == list(PANEL_BUTTON_ORDER)


# ── Panel select groups ──────────────────────────────────────────────


def test_panel_groups_partition_all_actions():
    """The dropdown groups must cover every action exactly once — no action
    silently dropped from the panel, none duplicated across menus."""
    grouped: list[str] = []
    for group in PANEL_GROUP_ORDER:
        grouped.extend(m.action for m in panel_metas_for_group(group))
    # Exact partition of PANEL_BUTTON_ORDER.
    assert set(grouped) == set(PANEL_BUTTON_ORDER)
    assert len(grouped) == len(PANEL_BUTTON_ORDER)  # no action in two groups


def test_panel_metas_for_group_preserve_display_order():
    settings = [m.action for m in panel_metas_for_group("settings")]
    assert settings == ["rename", "limit", "hide", "unhide", "reset"]
    perms = [m.action for m in panel_metas_for_group("permissions")]
    assert perms == ["lock", "unlock", "invite", "kick", "transfer"]


def test_panel_group_placeholder_text():
    assert panel_group_placeholder("settings") == "Change channel settings"
    assert panel_group_placeholder("permissions") == "Change channel permissions"


# ── Panel embeds ─────────────────────────────────────────────────────


def test_build_panel_embed_title_and_color():
    embed = build_panel_embed()
    assert isinstance(embed, discord.Embed)
    assert embed.title is not None
    assert "Voice Master" in embed.title
    assert embed.color == discord.Color.blurple()


def test_build_panel_embed_has_footer():
    embed = build_panel_embed()
    assert embed.footer.text is not None
    assert "Hub" in embed.footer.text


def test_build_inline_panel_embed_greets_owner():
    embed = build_inline_panel_embed(owner_mention="<@42>")
    assert isinstance(embed, discord.Embed)
    assert embed.description is not None
    assert "<@42>" in embed.description
    assert embed.color == discord.Color.blurple()


def test_build_knock_request_embed_includes_requester_owner_channel():
    embed = build_knock_request_embed(
        requester_mention="<@1>",
        owner_mention="<@2>",
        channel_name="Game Night",
    )
    assert embed.title is not None
    assert "knock" in embed.title.lower()
    assert embed.description is not None
    assert "<@1>" in embed.description
    assert "<@2>" in embed.description
    assert "Game Night" in embed.description
    assert embed.color == discord.Color.gold()
