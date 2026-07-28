"""Tests for the extracted DM-permission pure-logic modules.

Covers ``bot_modules/dm_perms/logic.py`` (validation, formatting, decision)
and ``bot_modules/dm_perms/embeds.py`` (embed builders). Same pressure-
cooker pattern as the starboard/emoji-stealer refactors: the cog file
keeps the Discord glue, this file proves the helpers behave without any
real Discord client.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.dm_perms.embeds import (
    build_acceptance_embed,
    build_denial_embed_for_requester,
    build_denial_embed_for_view,
    build_dm_settings_embed,
    build_expired_embed,
    build_guild_unavailable_embed,
    build_request_dm_embed,
    build_request_sent_embed,
    build_revoked_embed,
    build_stale_request_embed,
)
from bot_modules.dm_perms.logic import (
    audit_line_accepted,
    audit_line_asked,
    audit_line_denied,
    audit_line_expired,
    audit_line_revoked,
    clamp_reason,
    classify_dm_request,
    discard_consent_pair,
    display_name_for,
    dm_status_text,
    pick_dm_roles_to_remove,
    safe_field_text,
)


# ── safe_field_text ──────────────────────────────────────────────────


def test_safe_field_text_returns_dash_for_empty_string():
    assert safe_field_text("") == "—"


def test_safe_field_text_returns_dash_for_none():
    assert safe_field_text(None) == "—"


def test_safe_field_text_escapes_markdown():
    # ``escape_markdown`` backslash-escapes special characters so a
    # crafted reason can't impersonate bot formatting.
    out = safe_field_text("**bold**")
    assert "**" not in out or "\\*" in out


def test_safe_field_text_passes_plain_text_through():
    assert safe_field_text("hello world") == "hello world"


# ── clamp_reason ─────────────────────────────────────────────────────


def test_clamp_reason_passes_short_text_through():
    assert clamp_reason("hi", max_len=10) == "hi"


def test_clamp_reason_passes_text_at_exact_limit_unchanged():
    text = "x" * 10
    assert clamp_reason(text, max_len=10) == text


def test_clamp_reason_truncates_with_ellipsis():
    text = "x" * 20
    out = clamp_reason(text, max_len=10)
    assert len(out) == 10
    assert out.endswith("…")
    # max_len - 1 chars of original + ellipsis
    assert out == "x" * 9 + "…"


def test_clamp_reason_handles_empty_string():
    assert clamp_reason("", max_len=5) == ""


# ── classify_dm_request ──────────────────────────────────────────────


def _classify_kwargs(**overrides: Any) -> dict[str, Any]:
    """Build classify_dm_request kwargs with passing defaults (returns None)."""
    base: dict[str, Any] = dict(
        target_in_guild=True,
        is_self=False,
        target_is_bot=False,
        target_mode="ask",
        is_mutual=False,
        has_pending=False,
        target_display_name="Bob",
    )
    base.update(overrides)
    return base


def test_classify_dm_request_returns_none_when_all_checks_pass():
    assert classify_dm_request(**_classify_kwargs()) is None


def test_classify_dm_request_rejects_user_not_in_guild():
    out = classify_dm_request(**_classify_kwargs(target_in_guild=False))
    assert out is not None
    assert "couldn't check" in out.lower() or "may not be in this server" in out


def test_classify_dm_request_rejects_self_request():
    out = classify_dm_request(**_classify_kwargs(is_self=True))
    assert out is not None
    assert "yourself" in out.lower()


def test_classify_dm_request_rejects_bot_targets():
    out = classify_dm_request(**_classify_kwargs(target_is_bot=True))
    assert out is not None
    assert "bot" in out.lower()


def test_classify_dm_request_rejects_closed_target():
    out = classify_dm_request(**_classify_kwargs(target_mode="closed"))
    assert out is not None
    assert "Bob" in out
    assert "isn't accepting" in out.lower() or "not accepting" in out.lower()


def test_classify_dm_request_short_circuits_open_target_without_request():
    out = classify_dm_request(**_classify_kwargs(target_mode="open"))
    assert out is not None
    assert "open" in out.lower()
    assert "no request needed" in out.lower() or "just message" in out.lower()


def test_classify_dm_request_blocks_when_already_mutual():
    out = classify_dm_request(**_classify_kwargs(is_mutual=True))
    assert out is not None
    assert "already" in out.lower() or "connection" in out.lower()


def test_classify_dm_request_blocks_when_pending_request_exists():
    out = classify_dm_request(**_classify_kwargs(has_pending=True))
    assert out is not None
    assert "pending" in out.lower()


def test_classify_dm_request_self_request_takes_priority_over_bot_target():
    """Priority: not-in-guild > self > bot > mode > mutual > pending."""
    # If both is_self and target_is_bot are set, is_self wins.
    out = classify_dm_request(**_classify_kwargs(is_self=True, target_is_bot=True))
    assert out is not None
    assert "yourself" in out.lower()


def test_classify_dm_request_not_in_guild_takes_priority_over_self():
    out = classify_dm_request(**_classify_kwargs(target_in_guild=False, is_self=True))
    assert out is not None
    assert "may not be in this server" in out.lower()


# ── dm_status_text ───────────────────────────────────────────────────


def test_dm_status_text_when_mutual():
    out = dm_status_text(True)
    assert "✅" in out
    assert "connected" in out.lower()


def test_dm_status_text_when_not_mutual():
    out = dm_status_text(False)
    assert "❌" in out
    assert "no connection" in out.lower()


# ── pick_dm_roles_to_remove ──────────────────────────────────────────


def _role(name: str, position: int):
    r = MagicMock(spec=discord.Role)
    r.name = name
    r.position = position
    return r


def test_pick_dm_roles_to_remove_returns_empty_when_no_roles():
    assert pick_dm_roles_to_remove([]) == []


def test_pick_dm_roles_to_remove_returns_empty_when_single_role():
    assert pick_dm_roles_to_remove([_role("DMs: Ask", 1)]) == []


def test_pick_dm_roles_to_remove_keeps_highest_position():
    """When multiple DM-mode roles exist, the one with the highest
    ``position`` wins (rationale: the cog grants the new role first then
    cleans up — the new role is bumped above the old)."""
    low = _role("DMs: Ask", 1)
    mid = _role("DMs: Closed", 5)
    high = _role("DMs: Open", 10)
    out = pick_dm_roles_to_remove([low, mid, high])
    # high must NOT be in to-remove
    assert high not in out
    # low and mid must be removed
    assert low in out
    assert mid in out
    assert len(out) == 2


def test_pick_dm_roles_to_remove_handles_two_role_case():
    a = _role("DMs: Ask", 1)
    b = _role("DMs: Open", 2)
    out = pick_dm_roles_to_remove([a, b])
    assert out == [a]


def test_pick_dm_roles_to_remove_accepts_iterable():
    a = _role("DMs: Ask", 1)
    b = _role("DMs: Open", 2)
    # tuple input — function should not assume list semantics
    out = pick_dm_roles_to_remove((a, b))
    assert out == [a]


# ── audit-line builders ──────────────────────────────────────────────


def test_audit_line_asked_uses_arrow_and_type():
    line = audit_line_asked("Alice", "Bob", "Direct Message")
    assert "Alice" in line
    assert "Bob" in line
    assert "➝" in line
    assert "Direct Message" in line


def test_audit_line_accepted_uses_double_arrow():
    """Accepted is a two-way relationship — uses ↔ not ➝."""
    line = audit_line_accepted("Alice", "Bob", "Friend Request")
    assert "↔" in line
    assert "Friend Request" in line
    assert "accepted" in line.lower()


def test_audit_line_denied_uses_one_way_arrow():
    line = audit_line_denied("Alice", "Bob", "Direct Message")
    assert "➝" in line
    assert "denied" in line.lower()


def test_audit_line_expired_uses_one_way_arrow():
    line = audit_line_expired("Alice", "Bob", "Direct Message")
    assert "➝" in line
    assert "expired" in line.lower()


def test_audit_line_revoked_includes_actor():
    line = audit_line_revoked("Alice", "Bob", "Carol")
    assert "↔" in line
    assert "Alice" in line
    assert "Bob" in line
    assert "Carol" in line
    assert "revoked" in line.lower()


def test_audit_line_revoked_when_actor_is_one_of_the_pair():
    """Self-revoke (common case) still surfaces the actor name explicitly."""
    line = audit_line_revoked("Alice", "Bob", "Alice")
    # Alice appears at least once — actor disambiguation may dedupe or not,
    # but at minimum the line must mention "(by Alice)".
    assert "by Alice" in line


# ── display_name_for ─────────────────────────────────────────────────


def test_display_name_for_uses_member_display_name():
    member = MagicMock(spec=discord.Member)
    member.display_name = "Cool Name"
    assert display_name_for(member, 42) == "Cool Name"


def test_display_name_for_falls_back_to_id_when_member_is_none():
    assert display_name_for(None, 42) == "42"


# ── build_stale_request_embed ────────────────────────────────────────


def test_build_stale_request_embed_uses_hourglass_title():
    embed = build_stale_request_embed()
    title = embed.title or ""
    assert "⌛" in title
    assert "no longer active" in title.lower()


# ── build_guild_unavailable_embed ────────────────────────────────────


def test_build_guild_unavailable_embed_uses_server_unavailable_title():
    embed = build_guild_unavailable_embed()
    title = embed.title or ""
    assert "❌" in title
    assert "server unavailable" in title.lower()


# ── build_acceptance_embed ───────────────────────────────────────────


def test_build_acceptance_embed_carries_both_users_and_label():
    embed = build_acceptance_embed(
        requester_display_name="Alice",
        target_display_name="Bob",
        requester_mention="<@1>",
        target_mention="<@2>",
        type_label="Direct Message",
        reason="just want to chat",
    )
    desc = embed.description or ""
    assert "Alice" in desc
    assert "Bob" in desc
    assert "<@1>" in desc
    assert "<@2>" in desc
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["Request Type"] == "Direct Message"
    assert "chat" in by_name["Reason"]


def test_build_acceptance_embed_uses_dash_for_empty_reason():
    embed = build_acceptance_embed(
        requester_display_name="A",
        target_display_name="B",
        requester_mention="<@1>",
        target_mention="<@2>",
        type_label="Direct Message",
        reason="",
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Reason"] == "—"


# ── build_denial_embed_for_view ──────────────────────────────────────


def test_build_denial_embed_for_view_uses_no_worries_copy():
    embed = build_denial_embed_for_view(
        type_label="Direct Message", reason="not now",
    )
    title = embed.title or ""
    desc = embed.description or ""
    assert "❌" in title
    assert "no worries" in desc.lower()
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["Request Type"] == "Direct Message"
    assert "not now" in by_name["Reason"]
    # No reply given → no reply field.
    assert "Your Reply" not in by_name


def test_build_denial_embed_for_view_shows_reply_when_given():
    embed = build_denial_embed_for_view(
        type_label="Direct Message", reason="not now", reply="maybe later, ok?",
    )
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "maybe later, ok?" in by_name["Your Reply"]


# ── build_denial_embed_for_requester ─────────────────────────────────


def test_build_denial_embed_for_requester_lowercases_label_in_sentence():
    embed = build_denial_embed_for_requester(
        target_display_name="Bob",
        guild_name="My Guild",
        type_label="Friend Request",
        reason="busy",
    )
    desc = embed.description or ""
    # Description natural-language form lowercases the label.
    assert "friend request" in desc.lower()
    assert "Bob" in desc
    assert "My Guild" in desc
    # The field value preserves the original cased label.
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Request Type"] == "Friend Request"
    # No reply given → no "Reply from …" field.
    assert not any((f.name or "").startswith("Reply from") for f in embed.fields)


def test_build_denial_embed_for_requester_shows_reply_from_target():
    embed = build_denial_embed_for_requester(
        target_display_name="Bob",
        guild_name="My Guild",
        type_label="Friend Request",
        reason="busy",
        reply="please don't message me again",
    )
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["Reply from Bob"] == "please don't message me again"


# ── build_request_dm_embed ───────────────────────────────────────────


def test_build_request_dm_embed_sets_author_and_footer():
    embed = build_request_dm_embed(
        guild_name="Cool Guild",
        requester_display_name="Alice",
        requester_avatar_url="https://cdn.example/a.png",
        request_timeout_label="24 hours",
        type_label="Direct Message",
        reason="hi",
    )
    desc = embed.description or ""
    footer_text = embed.footer.text or ""
    assert embed.author.name == "Alice"
    assert embed.author.icon_url == "https://cdn.example/a.png"
    assert "24 hours" in desc
    # Points at the panel, not the retired /dm_revoke command. This DM is read
    # outside the server, where there is no panel button in front of the reader,
    # so the footer has to name where to find it.
    assert "My DM Settings" in footer_text
    assert "/dm_" not in footer_text


# ── build_request_sent_embed ─────────────────────────────────────────


def test_build_request_sent_embed_mentions_timeout_and_target():
    embed = build_request_sent_embed(
        target_display_name="Bob",
        guild_name="Cool Guild",
        request_timeout_label="24 hours",
        type_label="Direct Message",
        reason="hi",
    )
    desc = embed.description or ""
    assert "Bob" in desc
    assert "Cool Guild" in desc
    assert "24 hours" in desc
    assert "delivered" in desc.lower()


# ── build_expired_embed ──────────────────────────────────────────────


def test_build_expired_embed_describes_age_out():
    embed = build_expired_embed(
        target_display_name="Bob",
        guild_name="Cool Guild",
        type_label="Direct Message",
        request_timeout_label="24 hours",
    )
    title = embed.title or ""
    desc = embed.description or ""
    assert "⌛" in title
    assert "Bob" in desc
    assert "Cool Guild" in desc
    assert "24 hours" in desc
    assert "expired" in desc.lower()


# ── build_revoked_embed ──────────────────────────────────────────────


def test_build_revoked_embed_includes_both_users_and_meta():
    embed = build_revoked_embed(
        requester_display_name="Alice",
        target_display_name="Bob",
        type_label="Direct Message",
        reason="needed space",
    )
    title = embed.title or ""
    desc = embed.description or ""
    assert "🚫" in title
    assert "Alice" in desc
    assert "Bob" in desc
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert by_name["Request Type"] == "Direct Message"
    assert "needed space" in by_name["Reason"]


def test_build_revoked_embed_handles_missing_reason():
    """Older consent pairs have ``reason=None`` — must not crash and must
    show the em-dash placeholder."""
    embed = build_revoked_embed(
        requester_display_name="Alice",
        target_display_name="Bob",
        type_label="Direct Message",
        reason=None,
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Reason"] == "—"


# ── build_dm_settings_embed ──────────────────────────────────────────


@pytest.mark.parametrize("mode", ["open", "ask", "closed"])
def test_build_dm_settings_embed_marks_the_current_mode(mode):
    """The panel states the active mode, so nobody needs a status command."""
    embed = build_dm_settings_embed(mode, None)
    modes_field = next(f for f in embed.fields if f.name == "Your DM Modes")
    value = modes_field.value or ""
    marked = [ln for ln in value.splitlines() if "← current" in ln]
    assert len(marked) == 1
    assert mode.upper() in marked[0]


def test_build_dm_settings_embed_marks_nothing_for_unknown_mode():
    """A mode we don't recognise must not silently mark the wrong row."""
    embed = build_dm_settings_embed("bogus", None)
    modes_field = next(f for f in embed.fields if f.name == "Your DM Modes")
    assert "← current" not in (modes_field.value or "")


def test_build_dm_settings_embed_has_modes_and_connections_sections():
    embed = build_dm_settings_embed("ask", None)
    field_names = {f.name for f in embed.fields}
    assert field_names == {"Your DM Modes", "Connections"}


def test_build_dm_settings_embed_sets_thumbnail_when_icon_provided():
    embed = build_dm_settings_embed("ask", "https://cdn.example/icon.png")
    assert embed.thumbnail.url == "https://cdn.example/icon.png"


def test_build_dm_settings_embed_skips_thumbnail_for_iconless_guild():
    embed = build_dm_settings_embed("ask", None)
    assert embed.thumbnail.url is None



# ── discard_consent_pair ─────────────────────────────────────────────
#
# A connection is cached under whichever ordering created it, so revoke has to
# try both. Before this was extracted, the cog open-coded the two discards and
# the boolean; the panel's revoke button and any future caller now share it.


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param((1, 2), id="stored-as-requester-target"),
        pytest.param((2, 1), id="stored-as-target-requester"),
    ],
)
def test_discard_consent_pair_removes_either_ordering(stored):
    pairs = {stored}
    assert discard_consent_pair(pairs, 1, 2) is True
    assert pairs == set()


def test_discard_consent_pair_removes_both_orderings_when_duplicated():
    """A pair written under both orderings must not survive a revoke."""
    pairs = {(1, 2), (2, 1)}
    assert discard_consent_pair(pairs, 1, 2) is True
    assert pairs == set()


def test_discard_consent_pair_reports_absent_pair():
    """False is what makes the caller say "no connection" instead of "done"."""
    pairs = {(3, 4)}
    assert discard_consent_pair(pairs, 1, 2) is False
    assert pairs == {(3, 4)}


def test_discard_consent_pair_leaves_other_pairs_alone():
    pairs = {(1, 2), (3, 4), (5, 6)}
    assert discard_consent_pair(pairs, 3, 4) is True
    assert pairs == {(1, 2), (5, 6)}


def test_discard_consent_pair_handles_empty_cache():
    pairs: set[tuple[int, int]] = set()
    assert discard_consent_pair(pairs, 1, 2) is False
