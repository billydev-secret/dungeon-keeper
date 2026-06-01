"""Tests for the embed builders in ``bot_modules.jail.embeds``.

Each builder takes plain dicts / lists / strings and returns a
``discord.Embed``; tests assert on the embed's fields, color, title, and
footer rather than serializing — fast, deterministic, and gives a useful
backtrace when the cog's expected format drifts.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bot_modules.jail.embeds import (
    DEFAULT_MAX_ELIGIBLE_MENTIONS,
    DEFAULT_POLICIES_PAGE_SIZE,
    DEFAULT_WARNINGS_PAGE_SIZE,
    build_adopted_policies_embed,
    build_jail_audit_embed,
    build_modinfo_embed,
    build_policy_close_embed,
    build_policy_list_embed,
    build_policy_proposal_embed,
    build_policy_vote_initial_embed,
    build_policy_vote_update_embed,
    build_setup_complete_embed,
    build_setup_step_embed,
    build_ticket_open_embed,
    build_ticket_panel_embed,
    build_warning_audit_embed,
    build_warning_revoke_audit_embed,
    build_warning_threshold_embed,
    build_warnings_list_embed,
)
from bot_modules.services.embeds import (
    MOD_INFO,
    MOD_JAIL,
    MOD_POLICY,
    MOD_SUCCESS,
    MOD_TICKET,
    MOD_WARNING,
)


def _field_by_name(embed, name):
    """Return the embed field whose name equals ``name`` (or None)."""
    for f in embed.fields:
        if f.name == name:
            return f
    return None


# ── build_policy_vote_initial_embed ───────────────────────────────────


def test_initial_vote_embed_zero_voters():
    embed = build_policy_vote_initial_embed(
        channel_name="policy-test", vote_text="No bots in #general", eligible_ids=[],
    )
    assert embed.title == "Policy Vote: policy-test"
    assert embed.color is not None and embed.color.value == MOD_POLICY
    assert _field_by_name(embed, "Votes Cast").value == "0/0"
    assert _field_by_name(embed, "Status").value == "🗳️ Voting"
    assert _field_by_name(embed, "⏳ Awaiting").value == "—"


def test_initial_vote_embed_with_small_eligible_roster():
    embed = build_policy_vote_initial_embed(
        channel_name="p1", vote_text="text", eligible_ids=[10, 20, 30],
    )
    assert _field_by_name(embed, "Votes Cast").value == "0/3"
    awaiting = _field_by_name(embed, "⏳ Awaiting").value
    assert "<@10>" in awaiting and "<@20>" in awaiting and "<@30>" in awaiting
    assert "more" not in awaiting  # no overflow


def test_initial_vote_embed_caps_mentions_with_overflow_note():
    """When the eligible roster exceeds the default cap, the field gets a
    ``"+N more"`` suffix and the awaiting list itself is truncated."""
    eligible = list(range(40))
    embed = build_policy_vote_initial_embed(
        channel_name="p1", vote_text="text", eligible_ids=eligible,
    )
    awaiting = _field_by_name(embed, "⏳ Awaiting").value
    assert "<@0>" in awaiting  # first ID shown
    assert "<@39>" not in awaiting  # 40th ID truncated (over 25-cap)
    assert f"+{40 - DEFAULT_MAX_ELIGIBLE_MENTIONS} more" in awaiting


def test_initial_vote_embed_respects_custom_cap():
    embed = build_policy_vote_initial_embed(
        channel_name="p1", vote_text="t", eligible_ids=[1, 2, 3, 4, 5], max_mentions=2,
    )
    awaiting = _field_by_name(embed, "⏳ Awaiting").value
    assert "+3 more" in awaiting


def test_initial_vote_embed_field_order():
    """The update path uses ``set_field_at`` with hard-coded indices — the
    initial embed must keep the same order so updates land in the right slot."""
    embed = build_policy_vote_initial_embed(
        channel_name="p", vote_text="t", eligible_ids=[],
    )
    names = [f.name for f in embed.fields]
    assert names == [
        "📜 Policy Text", "Votes Cast", "Status",
        "✅ Yes", "❌ No", "➖ Abstain", "⏳ Awaiting",
    ]


# ── build_policy_vote_update_embed ────────────────────────────────────


def test_update_embed_running_tally():
    embed = build_policy_vote_update_embed(
        policy_title="Title", vote_text="Text",
        yes_ids=[10], no_ids=[], abstain_ids=[20], awaiting_ids=[30, 40],
    )
    assert embed.title == "Policy Vote: Title"
    assert embed.color is not None and embed.color.value == MOD_POLICY
    assert _field_by_name(embed, "Status").value == "🗳️ Voting"
    assert _field_by_name(embed, "Votes Cast").value == "2/4"  # 1 yes + 1 abstain
    assert _field_by_name(embed, "✅ Yes").value == "<@10>"
    assert _field_by_name(embed, "❌ No").value == "—"
    assert _field_by_name(embed, "➖ Abstain").value == "<@20>"


def test_update_embed_adopted():
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="t",
        yes_ids=[1, 2], no_ids=[], abstain_ids=[3], awaiting_ids=[],
        outcome="adopted",
    )
    assert embed.color is not None and embed.color.value == MOD_SUCCESS
    assert _field_by_name(embed, "Status").value == "✅ Adopted"
    assert _field_by_name(embed, "Votes Cast").value == "3/3"


def test_update_embed_rejected():
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="t",
        yes_ids=[1], no_ids=[2], abstain_ids=[], awaiting_ids=[],
        outcome="rejected",
    )
    assert _field_by_name(embed, "Status").value == "❌ Rejected"


def test_update_embed_handles_empty_vote_text():
    """When the policy has no vote_text, the field still renders something."""
    embed = build_policy_vote_update_embed(
        policy_title="t", vote_text="",
        yes_ids=[], no_ids=[], abstain_ids=[], awaiting_ids=[1],
    )
    assert _field_by_name(embed, "📜 Policy Text").value == "(no text)"


# ── build_policy_list_embed ───────────────────────────────────────────


def _policy_row(pid, title, description, passed_at=1700000000):
    return {
        "id": pid, "title": title, "description": description, "passed_at": passed_at,
    }


def test_policy_list_embed_minimal():
    policies = [_policy_row(1, "Be kind", "Short description")]
    embed = build_policy_list_embed(policies)
    assert embed.title == "📋 Passed Policies"
    field = _field_by_name(embed, "#1 — Be kind")
    assert field is not None
    assert "Short description" in field.value
    assert "<t:1700000000:d>" in field.value
    # Not truncated → no footer
    assert embed.footer.text is None or embed.footer.text == ""


def test_policy_list_embed_truncates_long_description():
    long_desc = "x" * 200
    policies = [_policy_row(1, "T", long_desc)]
    embed = build_policy_list_embed(policies)
    field = _field_by_name(embed, "#1 — T")
    assert "…" in field.value
    # The preview is bounded by the default preview length (100).
    assert field.value.count("x") == 100


def test_policy_list_embed_paginates_with_footer():
    policies = [_policy_row(i, f"P{i}", "desc") for i in range(30)]
    embed = build_policy_list_embed(policies)
    # Only DEFAULT_POLICIES_PAGE_SIZE rows shown
    assert len(embed.fields) == DEFAULT_POLICIES_PAGE_SIZE
    assert embed.footer.text == (
        f"Showing {DEFAULT_POLICIES_PAGE_SIZE} of {len(policies)} policies."
    )


def test_policy_list_embed_no_pagination_when_zero_size():
    """``page_size=0`` shows all policies without a footer (useful for testing)."""
    policies = [_policy_row(i, f"P{i}", "x") for i in range(5)]
    embed = build_policy_list_embed(policies, page_size=0)
    assert len(embed.fields) == 5
    assert embed.footer.text is None or embed.footer.text == ""


def test_policy_list_embed_empty():
    embed = build_policy_list_embed([])
    assert len(embed.fields) == 0


# ── build_warnings_list_embed ─────────────────────────────────────────


def _warn(
    wid, *, revoked=False, reason="bad", revoke_reason="", moderator_id=99, ts=1000,
):
    return {
        "id": wid,
        "revoked": revoked,
        "reason": reason,
        "revoke_reason": revoke_reason,
        "moderator_id": moderator_id,
        "created_at": ts,
    }


def test_warnings_embed_active_only():
    embed = build_warnings_list_embed(
        "user#1234", [_warn(1, reason="spam")], ts_formatter=lambda ts: f"t{ts}",
    )
    assert embed.title == "Warnings for user#1234"
    assert "**Active**" in embed.description
    assert "spam" in embed.description
    assert "t1000" in embed.description
    assert embed.footer.text == "1 active / 1 total"


def test_warnings_embed_with_revoked_includes_revoke_reason():
    embed = build_warnings_list_embed(
        "u",
        [_warn(1, revoked=True, revoke_reason="appealed")],
        ts_formatter=lambda ts: f"t{ts}",
    )
    assert "~~Revoked~~" in embed.description
    assert "Revoke reason: appealed" in embed.description
    assert embed.footer.text == "0 active / 1 total"


def test_warnings_embed_truncates_long_lists():
    warns = [_warn(i) for i in range(DEFAULT_WARNINGS_PAGE_SIZE + 5)]
    embed = build_warnings_list_embed("u", warns, ts_formatter=lambda ts: "t")
    assert "and 5 more (older)" in embed.description


def test_warnings_embed_empty_warning_with_no_reason():
    embed = build_warnings_list_embed(
        "u", [_warn(1, reason="")], ts_formatter=lambda ts: "t",
    )
    # Empty reason → no "Reason: " line, but the warning row still appears
    assert "Reason:" not in embed.description
    assert "#1" in embed.description


def test_warnings_embed_color_and_footer():
    embed = build_warnings_list_embed("u", [], ts_formatter=lambda ts: "t")
    assert embed.color is not None and embed.color.value == MOD_WARNING
    assert embed.footer.text == "0 active / 0 total"


def test_warnings_embed_default_ts_formatter_when_none():
    """No formatter → uses the discord ``<t:…:f>`` format."""
    embed = build_warnings_list_embed("u", [_warn(1, ts=1700)])
    assert "<t:1700:f>" in embed.description


# ── build_ticket_panel_embed ──────────────────────────────────────────


def test_ticket_panel_embed_static_content():
    embed = build_ticket_panel_embed()
    assert embed.title == "📩 Support Tickets"
    assert "private ticket" in embed.description
    assert embed.color is not None and embed.color.value == MOD_TICKET


# ── build_ticket_open_embed ───────────────────────────────────────────


def test_ticket_open_embed_basic():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    embed = build_ticket_open_embed(
        ticket_id=42, description="My issue", opener_mention="<@1>", now=now,
    )
    assert embed.title == "Ticket #42"
    assert embed.description == "My issue"
    assert embed.timestamp == now
    assert _field_by_name(embed, "Opened by").value == "<@1>"
    assert _field_by_name(embed, "Status").value == "🟢 Open"
    assert "archived" in embed.footer.text


def test_ticket_open_embed_defaults_timestamp_to_now():
    embed = build_ticket_open_embed(
        ticket_id=1, description="d", opener_mention="<@1>",
    )
    assert embed.timestamp is not None


# ── build_setup_step_embed / build_setup_complete_embed ───────────────


def test_setup_step_embed_takes_meta_dict():
    """The cog passes the dict from ``setup_step_meta`` straight in."""
    meta = {
        "title": "Setup — Step 1/6",
        "description": "Which roles?",
        "config_key": "mod_role_ids",
        "select_kind": "role",
        "placeholder": "Select…",
    }
    embed = build_setup_step_embed(meta)
    assert embed.title == "Setup — Step 1/6"
    assert embed.description == "Which roles?"
    assert embed.color is not None and embed.color.value == MOD_TICKET


def test_setup_complete_embed():
    embed = build_setup_complete_embed()
    assert embed.title == "Setup Complete"
    assert embed.color is not None and embed.color.value == MOD_SUCCESS


# ── build_modinfo_embed ───────────────────────────────────────────────


def test_modinfo_embed_minimal_no_warnings_no_tickets():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    embed = build_modinfo_embed(
        user_label="u#1",
        user_avatar_url=None,
        account_created=created,
        account_age_days=400,
        joined_at=None,
        xp_row=None,
        watcher_count=0,
        active_jail=None,
        jail_history=[],
        warns=[],
        tickets=[],
        last_seen_ts=None,
        top_channels=[],
        msgs_30d_total=0,
        ts_formatter=lambda ts: f"t{ts}" if ts else "N/A",
    )
    assert embed.title == "Mod Info — u#1"
    assert embed.color is not None and embed.color.value == MOD_INFO
    # Avatar None → no thumbnail
    assert embed.thumbnail.url is None or embed.thumbnail.url == ""
    assert _field_by_name(embed, "⭐ Level").value == "No XP recorded"
    assert _field_by_name(embed, "🔍 Watch List").value == "Not watched"
    assert _field_by_name(embed, "🔒 Jail").value == "Not currently jailed"
    assert "Last seen: Never" in _field_by_name(
        embed, "💬 Activity — 0 msgs (30d)"
    ).value


def test_modinfo_embed_full_kitchen_sink():
    created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    joined = datetime(2025, 6, 1, tzinfo=timezone.utc)
    embed = build_modinfo_embed(
        user_label="u#1",
        user_avatar_url="https://example.com/avatar.png",
        account_created=created,
        account_age_days=500,
        joined_at=joined,
        xp_row={"level": 7, "total_xp": 12345.0},
        watcher_count=2,
        active_jail={
            "created_at": 1000, "expires_at": 2000, "reason": "spam",
        },
        jail_history=[
            {"status": "active", "created_at": 1000},
            {"status": "released", "created_at": 500, "release_reason": "served"},
        ],
        warns=[
            {"id": 1, "revoked": False, "reason": "bad", "created_at": 100},
            {"id": 2, "revoked": True, "reason": "x", "created_at": 200},
        ],
        tickets=[
            {"id": 10, "status": "open", "created_at": 1500},
            {"id": 9, "status": "closed", "created_at": 1400},
            {"id": 8, "status": "deleted", "created_at": 1300},
        ],
        last_seen_ts=1900,
        top_channels=[
            {"channel_id": 111, "cnt": 50},
            {"channel_id": 222, "cnt": 30},
        ],
        msgs_30d_total=80,
        ts_formatter=lambda ts: f"t{ts}" if ts else "N/A",
    )
    assert embed.thumbnail.url == "https://example.com/avatar.png"
    assert "Level **7**" in _field_by_name(embed, "⭐ Level").value
    assert "12,345 XP" in _field_by_name(embed, "⭐ Level").value
    assert "**2 mods watching**" in _field_by_name(embed, "🔍 Watch List").value

    jail_field = _field_by_name(embed, "🔒 Jail").value
    assert "Currently jailed" in jail_field
    assert "Expires:" in jail_field
    assert "spam" in jail_field
    assert "**Past jails:** 1" in jail_field

    warn_field = _field_by_name(embed, "⚠️ Warnings").value
    assert "**Active:** 1 / **Total:** 2" in warn_field

    ticket_field = _field_by_name(embed, "📩 Tickets").value
    assert "**Open:** 1 / **Closed:** 2" in ticket_field
    assert "#10" in ticket_field  # most recent

    assert embed.image.url == "attachment://modinfo_activity.png"


def test_modinfo_embed_singular_watcher_label():
    """One watcher → "1 mod watching", not "1 mods watching"."""
    embed = build_modinfo_embed(
        user_label="u", user_avatar_url=None,
        account_created=datetime(2024, 1, 1, tzinfo=timezone.utc), account_age_days=1,
        joined_at=None, xp_row=None, watcher_count=1,
        active_jail=None, jail_history=[],
        warns=[], tickets=[], last_seen_ts=None,
        top_channels=[], msgs_30d_total=0,
        ts_formatter=lambda ts: "t",
    )
    assert "1 mod watching" in _field_by_name(embed, "🔍 Watch List").value


# ── Audit embeds ──────────────────────────────────────────────────────


def test_jail_audit_embed_with_reason():
    embed = build_jail_audit_embed(
        target_mention="<@1>", moderator_mention="<@2>",
        duration_text="2h", reason="spam",
    )
    assert embed.title == "🔒 Member Jailed"
    assert embed.color is not None and embed.color.value == MOD_JAIL
    assert "<@1>" in embed.description and "<@2>" in embed.description
    assert "**Duration:** 2h" in embed.description
    assert "**Reason:** spam" in embed.description


def test_jail_audit_embed_no_reason():
    embed = build_jail_audit_embed(
        target_mention="<@1>", moderator_mention="<@2>", duration_text="Indefinite",
    )
    assert "Reason:" not in embed.description


def test_warning_audit_embed_slash_command_path():
    """The /warn command supplies just reason; no notes or source link."""
    embed = build_warning_audit_embed(
        target_mention="<@1>", moderator_mention="<@2>",
        active_count=3, reason="rule 1",
    )
    assert embed.title == "⚠️ Warning Issued"
    assert "**Reason:** rule 1" in embed.description
    assert "**Active warnings:** 3" in embed.description
    assert "Jump to source" not in embed.description
    assert embed.color is not None and embed.color.value == MOD_WARNING


def test_warning_audit_embed_context_menu_path_includes_notes_and_link():
    embed = build_warning_audit_embed(
        target_mention="<@1>", moderator_mention="<@2>",
        active_count=2, reason="bad msg", notes="repeat offender",
        source_jump_url="https://discord.com/.../msg",
    )
    assert "**Notes:** repeat offender" in embed.description
    assert "Jump to source message" in embed.description


def test_warning_threshold_embed_with_admin_pings():
    embed = build_warning_threshold_embed(
        target_mention="<@1>", active_count=5, admin_role_ids=[100, 200],
    )
    assert embed.title == "🚨 Warning Threshold Reached"
    assert "<@&100>" in embed.description and "<@&200>" in embed.description


def test_warning_threshold_embed_without_admin_pings():
    embed = build_warning_threshold_embed(
        target_mention="<@1>", active_count=5, admin_role_ids=[],
    )
    assert "<@&" not in embed.description  # no ping line


def test_warning_revoke_audit_embed():
    embed = build_warning_revoke_audit_embed(
        warning_id=42, target_mention="<@1>", moderator_mention="<@2>",
        active_count=1, reason="appealed",
    )
    assert embed.title == "✅ Warning Revoked"
    assert "#42" in embed.description
    assert "**Reason:** appealed" in embed.description
    assert embed.color is not None and embed.color.value == MOD_SUCCESS


def test_warning_revoke_audit_embed_without_reason():
    embed = build_warning_revoke_audit_embed(
        warning_id=42, target_mention="<@1>", moderator_mention="<@2>",
        active_count=1,
    )
    assert "**Reason:**" not in embed.description


# ── Policy proposal embeds ────────────────────────────────────────────


def test_policy_proposal_embed_basic():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar",
        proposer_mention="<@1>", now=now,
    )
    assert embed.title == "📋 Policy Proposal #1: Foo"
    assert embed.description == "Bar"
    assert embed.timestamp == now
    assert _field_by_name(embed, "Status").value == "💬 Open for Discussion"
    assert "Use /policy vote" in embed.footer.text


def test_policy_proposal_embed_defaults_timestamp():
    embed = build_policy_proposal_embed(
        policy_id=1, title="Foo", description="Bar", proposer_mention="<@1>",
    )
    assert embed.timestamp is not None


def test_policy_close_embed_with_reason():
    embed = build_policy_close_embed(
        title="Foo", moderator_mention="<@1>", reason="rescinded",
    )
    assert embed.title == "📋 Policy Proposal Closed"
    assert "**Foo**" in embed.description
    assert _field_by_name(embed, "Reason").value == "rescinded"


def test_policy_close_embed_no_reason_no_field():
    embed = build_policy_close_embed(title="Foo", moderator_mention="<@1>")
    assert _field_by_name(embed, "Reason") is None


def test_adopted_policies_embed_listing():
    embed = build_adopted_policies_embed(
        [
            {"title": "P1", "description": "d1"},
            {"title": "P2", "description": "d2"},
        ]
    )
    assert embed.title == "Adopted Policies from This Proposal"
    assert _field_by_name(embed, "P1").value == "d1"
    assert _field_by_name(embed, "P2").value == "d2"
    assert embed.color is not None and embed.color.value == MOD_SUCCESS


def test_adopted_policies_embed_truncates_long_descriptions():
    """Discord field values cap at 1024 chars — the builder must truncate."""
    long_desc = "x" * 2000
    embed = build_adopted_policies_embed([{"title": "P", "description": long_desc}])
    assert len(_field_by_name(embed, "P").value) == 1024
