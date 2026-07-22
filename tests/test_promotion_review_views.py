"""Pure-helper tests for promotion_review_views (embed + prune-line rendering).

The interactive button/post flow is Discord glue tested via the service layer;
here we only pin the pure formatting branches.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord

from bot_modules.services import promotion_review_service as svc
from bot_modules.services.promotion_review_views import (
    build_review_embed,
    format_prune_lines,
)


class _FakeGuild:
    def __init__(self, roles):
        self._roles = roles  # {role_id: mention_str}

    def get_role(self, rid):
        m = self._roles.get(rid)
        return SimpleNamespace(mention=m) if m is not None else None


def test_format_prune_lines_known_unknown_and_undated():
    guild = _FakeGuild({900: "@NSFW"})
    lines = format_prune_lines(guild, [(900, 1_700_000_000.0), (901, None)])
    assert lines[0] == "@NSFW — removed <t:1700000000:D>"
    # Unknown role falls back to a code-formatted id, undated says so.
    assert lines[1] == "role `901` — removed (date unknown)"


def test_build_embed_pruned_return_title_and_fields():
    embed = build_review_embed(
        discord.Color.blurple(),
        kind=svc.KIND_PRUNED_RETURN,
        member_mention="<@7>",
        member_display="ghost#1",
        level=5,
        prune_lines=["@NSFW — removed <t:1:D>"],
        action_hint="do the thing",
    )
    assert "returned" in embed.title.lower()
    names = {f.name for f in embed.fields}
    assert {"Member", "Level", "Access a sweep removed"} <= names


def test_build_embed_sleeper_title_no_prune_field_when_empty():
    embed = build_review_embed(
        discord.Color.blurple(),
        kind=svc.KIND_SLEEPER,
        member_mention="<@7>",
        member_display="ghost#1",
        level=0,
        prune_lines=[],
        action_hint="reactivate them",
    )
    assert "sleeper" in embed.title.lower()
    assert "Access a sweep removed" not in {f.name for f in embed.fields}


def test_build_embed_resolved_verbs():
    for resolution, needle in [
        (svc.RESOLUTION_GRANTED, "granted"),
        (svc.RESOLUTION_REACTIVATED, "reactivated"),
        (svc.RESOLUTION_DISMISSED, "dismissed"),
    ]:
        embed = build_review_embed(
            discord.Color.blurple(),
            kind=svc.KIND_PRUNED_RETURN,
            member_mention="<@7>",
            member_display="ghost#1",
            level=1,
            prune_lines=[],
            action_hint="",
            resolved=(resolution, "<@99>"),
        )
        resolved_field = next(f for f in embed.fields if f.name == "Resolved")
        assert needle in resolved_field.value.lower()
        assert "<@99>" in resolved_field.value
