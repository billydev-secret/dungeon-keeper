"""Pure-helper tests for intake_views (embed + checklist rendering).

The interactive button/post flow is Discord glue tested via the service layer
(test_intake_logic.py); here we only pin the pure formatting branches.
"""

from __future__ import annotations

import discord

from bot_modules.services import intake_service as svc
from bot_modules.services.intake_views import build_intake_embed, format_step_lines


def _step(key, label, auto_kind="", done_at=None, done_by=None, skipped=0):
    return {
        "step_key": key,
        "label": label,
        "auto_kind": auto_kind,
        "done_at": done_at,
        "done_by": done_by,
        "skipped": skipped,
    }


STEPS = [
    _step("greeted", "Greeted", "greeted", done_at=1_700_000_000.0, done_by=99),
    _step("verified", "Verified", "verified", done_at=1_700_000_100.0, done_by=0),
    _step("sfw_questions", "SFW questions asked"),
    _step("nsfw_questions", "NSFW questions asked", skipped=1),
]


def test_format_step_lines_states():
    lines = format_step_lines(STEPS)
    assert lines[0] == "✅ Greeted — <@99> <t:1700000000:R>"
    # done_by 0 renders as the bot's auto-tick, not a broken mention.
    assert lines[1] == "✅ Verified — auto <t:1700000100:R>"
    assert lines[2] == "⬜ SFW questions asked"
    assert lines[3] == "⏭️ ~~NSFW questions asked~~ — skipped"


def test_build_embed_progress_and_fields():
    embed = build_intake_embed(
        discord.Color.blurple(),
        member_mention="<@7>",
        member_display="newbie#1",
        account_created_ts=1_600_000_000.0,
        inviter_mention="<@55>",
        steps=STEPS,
    )
    # Skipped steps never count toward the bar: 2 of 4 here.
    assert "2/4" in (embed.description or "")
    assert "▰" in (embed.description or "")
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Invited by"] == "<@55>"
    assert "<t:1600000000:R>" in fields["Account created"]
    assert "⬜ SFW questions asked" in fields["Checklist"]
    assert "Resolved" not in fields


def test_build_embed_unknown_inviter_and_missing_account_ts():
    embed = build_intake_embed(
        discord.Color.blurple(),
        member_mention="<@7>",
        member_display="7",
        account_created_ts=None,
        inviter_mention=None,
        steps=STEPS,
    )
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Invited by"] == "unknown"
    assert "Account created" not in fields


def test_build_embed_resolved_headlines():
    for resolution, needle in [
        (svc.RESOLUTION_COMPLETED, "welcomed by <@99>"),
        (svc.RESOLUTION_DISMISSED, "Dismissed by <@99>"),
        (svc.RESOLUTION_LEFT, "left the server"),
        (svc.RESOLUTION_BANNED, "banned"),
    ]:
        embed = build_intake_embed(
            discord.Color.blurple(),
            member_mention="<@7>",
            member_display="newbie#1",
            account_created_ts=None,
            inviter_mention=None,
            steps=STEPS,
            resolved=(resolution, "<@99>"),
        )
        resolved = next(f for f in embed.fields if f.name == "Resolved")
        assert needle in resolved.value
