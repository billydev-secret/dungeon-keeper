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
    refresh_spicy_field,
)


def _level_5_embed(spicy_value: str | None) -> discord.Embed:
    """A Level 5 card as xp_service.maybe_log_level_5 renders it."""
    embed = discord.Embed(title="🎉 Level 5 reached", description="<@7> just reached level 5.")
    embed.add_field(name="Total XP", value="326.56", inline=True)
    if spicy_value is not None:
        embed.add_field(name=svc.SPICY_FIELD_NAME, value=spicy_value, inline=True)
    embed.add_field(name="Joined", value="<t:1784741107:F>", inline=False)
    return embed


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


def test_refresh_spicy_field_flips_a_stale_card_to_granted():
    """The reported bug: card says ❌, access was granted afterwards."""
    embed = _level_5_embed(svc.SPICY_NOT_GRANTED)
    updated = refresh_spicy_field(embed, True)
    assert updated is not None
    spicy = next(f for f in updated.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_GRANTED
    # Only that field moves: the rest of the card is an at-promotion snapshot.
    assert [f.name for f in updated.fields] == [f.name for f in embed.fields]
    assert next(f for f in updated.fields if f.name == "Total XP").value == "326.56"
    assert updated.title == embed.title


def test_refresh_spicy_field_does_not_mutate_the_original():
    """Embed.copy() is shallow and shares field dicts — we must deep-copy."""
    embed = _level_5_embed(svc.SPICY_NOT_GRANTED)
    refresh_spicy_field(embed, True)
    spicy = next(f for f in embed.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_NOT_GRANTED


def test_refresh_spicy_field_flips_back_when_access_is_revoked():
    embed = _level_5_embed(svc.SPICY_GRANTED)
    updated = refresh_spicy_field(embed, False)
    assert updated is not None
    spicy = next(f for f in updated.fields if f.name == svc.SPICY_FIELD_NAME)
    assert spicy.value == svc.SPICY_NOT_GRANTED


def test_refresh_spicy_field_noop_when_already_correct():
    """No edit call for a card that already reads right."""
    assert refresh_spicy_field(_level_5_embed(svc.SPICY_GRANTED), True) is None
    assert refresh_spicy_field(_level_5_embed(svc.SPICY_NOT_GRANTED), False) is None


def test_refresh_spicy_field_ignores_embeds_without_the_field():
    # A card posted while nsfw_role_id was unset has no Spicy field at all, and
    # ordinary level-up posts share this channel — neither should be touched.
    assert refresh_spicy_field(_level_5_embed(None), True) is None
    assert refresh_spicy_field(discord.Embed(title="something else"), True) is None


def test_refresh_spicy_field_preserves_field_inline_layout():
    embed = _level_5_embed(svc.SPICY_NOT_GRANTED)
    updated = refresh_spicy_field(embed, True)
    assert updated is not None
    assert [f.inline for f in updated.fields] == [f.inline for f in embed.fields]


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
