"""Tests for the extracted games-config pure-logic modules.

Covers ``bot_modules/games_config/logic.py`` (row → string transforms
and permission predicates) and ``bot_modules/games_config/embeds.py``
(embed builders). The cog itself stays Discord-glue; this proves the
extracted helpers work without spinning up a bot.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from bot_modules.games.constants import (
    ERROR_COLOR,
    BRAND_COLOR,
    SUCCESS_COLOR,
)
from bot_modules.games_config.embeds import (
    build_audit_channel_embed,
    build_channel_allowed_embed,
    build_channel_disallowed_embed,
    build_channel_list_embed,
    build_force_end_embed,
    build_game_status_embed,
)
from bot_modules.games_config.logic import (
    audit_channel_change,
    channel_ids_from_rows,
    describe_active_game,
    describe_force_end,
    format_allowed_channels,
    has_admin_permissions,
    has_mod_or_admin_permissions,
)


# ── format_allowed_channels ──────────────────────────────────────────


def test_format_allowed_channels_empty_returns_hint():
    text = format_allowed_channels([], resolver=lambda _id: None)
    assert "No game channels" in text
    assert "/games allow-channel" in text


def test_format_allowed_channels_uses_mention_when_resolver_returns_channel():
    """When the guild still has the channel, prefer its rendered mention."""
    rows = [(111,), (222,)]
    channels = {
        111: SimpleNamespace(mention="<#111-fancy>"),
        222: SimpleNamespace(mention="<#222-fancy>"),
    }
    text = format_allowed_channels(rows, resolver=channels.get)
    assert "<#111-fancy>" in text
    assert "<#222-fancy>" in text


def test_format_allowed_channels_falls_back_for_deleted_channels():
    """A None from the resolver (deleted/no-longer-visible) still renders."""
    rows = [(111,), (222,)]
    text = format_allowed_channels(rows, resolver=lambda _id: None)
    assert "<#111>" in text
    assert "<#222>" in text


def test_format_allowed_channels_mixes_resolved_and_unresolved():
    rows = [(100,), (200,)]
    resolver = {100: SimpleNamespace(mention="<#living>")}.get
    text = format_allowed_channels(rows, resolver=resolver)
    assert "<#living>" in text
    assert "<#200>" in text


def test_format_allowed_channels_preserves_row_order():
    rows = [(3,), (1,), (2,)]
    text = format_allowed_channels(rows, resolver=lambda _id: None)
    assert text == "<#3>\n<#1>\n<#2>"


# ── describe_active_game ─────────────────────────────────────────────


def test_describe_active_game_none_returns_no_active_message():
    title, body = describe_active_game(None)
    assert title == "No Active Game"
    assert "no game running" in body.lower()


def test_describe_active_game_with_row_includes_all_fields():
    row = {
        "game_type": "traditional",
        "state": "open",
        "host_id": 12345,
        "game_id": "abc-def",
    }
    title, body = describe_active_game(row)
    assert title == "Active Game"
    assert "traditional" in body
    assert "open" in body
    assert "<@12345>" in body
    assert "abc-def" in body


# ── describe_force_end ───────────────────────────────────────────────


def test_describe_force_end_mentions_game_type():
    assert "traditional" in describe_force_end("traditional")
    assert "ama" in describe_force_end("ama")


def test_describe_force_end_mentions_admin_or_mod():
    body = describe_force_end("ttl")
    assert "admin" in body.lower() or "mod" in body.lower()


# ── audit_channel_change ─────────────────────────────────────────────


def test_audit_channel_change_set_includes_channel_mention():
    title, body = audit_channel_change(7777)
    assert "Set" in title
    assert "<#7777>" in body


def test_audit_channel_change_clear_uses_disabled_language():
    title, body = audit_channel_change(None)
    assert "Cleared" in title
    assert "disabled" in body.lower()


# ── permission predicates ────────────────────────────────────────────


def test_has_admin_permissions_true_for_administrator():
    perms = SimpleNamespace(administrator=True)
    assert has_admin_permissions(perms) is True


def test_has_admin_permissions_false_for_non_admin():
    perms = SimpleNamespace(administrator=False)
    assert has_admin_permissions(perms) is False


def test_has_admin_permissions_false_when_attribute_missing():
    """Defensive: a stripped-down stub mustn't crash the predicate."""
    perms = SimpleNamespace()
    assert has_admin_permissions(perms) is False


@pytest.mark.parametrize(
    "perms,expected",
    [
        (SimpleNamespace(administrator=True, manage_guild=False, manage_channels=False), True),
        (SimpleNamespace(administrator=False, manage_guild=True, manage_channels=False), True),
        (SimpleNamespace(administrator=False, manage_guild=False, manage_channels=True), True),
        (SimpleNamespace(administrator=False, manage_guild=False, manage_channels=False), False),
    ],
)
def test_has_mod_or_admin_permissions_accepts_any_elevated_perm(perms, expected):
    assert has_mod_or_admin_permissions(perms) is expected


def test_has_mod_or_admin_permissions_false_when_perms_is_none():
    assert has_mod_or_admin_permissions(None) is False


# ── channel_ids_from_rows ────────────────────────────────────────────


def test_channel_ids_from_rows_empty():
    assert channel_ids_from_rows([]) == []


def test_channel_ids_from_rows_projects_first_column():
    rows = [(101,), (202,), (303,)]
    assert channel_ids_from_rows(rows) == [101, 202, 303]


def test_channel_ids_from_rows_coerces_to_int():
    rows = [("404",), ("505",)]
    assert channel_ids_from_rows(rows) == [404, 505]


# ── embed builders ───────────────────────────────────────────────────


def test_build_channel_allowed_embed_carries_mention_and_success_color():
    embed = build_channel_allowed_embed("<#1>")
    assert "<#1>" in embed.description
    assert "game channel" in embed.description
    assert embed.color.value == SUCCESS_COLOR


def test_build_channel_disallowed_embed_carries_mention():
    embed = build_channel_disallowed_embed("<#999>")
    assert "<#999>" in embed.description
    assert "no longer" in embed.description.lower()
    assert embed.color.value == SUCCESS_COLOR


def test_build_channel_list_embed_renders_description_via_logic():
    """The embed delegates body text to ``format_allowed_channels``."""
    rows = [(111,)]
    embed = build_channel_list_embed(rows, resolver=lambda _id: None)
    assert "<#111>" in embed.description
    assert embed.color.value == BRAND_COLOR
    assert embed.title == "Game Channels"


def test_build_channel_list_embed_empty_shows_hint():
    embed = build_channel_list_embed([], resolver=lambda _id: None)
    assert "No game channels" in embed.description


def test_build_game_status_embed_no_active_game():
    embed = build_game_status_embed(None)
    assert embed.title == "No Active Game"
    assert embed.color.value == BRAND_COLOR


def test_build_game_status_embed_renders_row_fields():
    row = {
        "game_type": "wyr",
        "state": "playing",
        "host_id": 42,
        "game_id": "xx-yy",
    }
    embed = build_game_status_embed(row)
    assert embed.title == "Active Game"
    assert "wyr" in embed.description
    assert "playing" in embed.description
    assert "<@42>" in embed.description


def test_build_force_end_embed_uses_error_color():
    embed = build_force_end_embed("traditional")
    assert "traditional" in embed.description
    assert embed.color.value == ERROR_COLOR
    assert "Force-Closed" in embed.title


def test_build_audit_channel_embed_set_includes_mention():
    embed = build_audit_channel_embed(5555)
    assert "<#5555>" in embed.description
    assert "Set" in embed.title
    assert embed.color.value == SUCCESS_COLOR


def test_build_audit_channel_embed_clear_uses_disabled_language():
    embed = build_audit_channel_embed(None)
    assert "Cleared" in embed.title
    assert "disabled" in embed.description.lower()


# ── sanity: builders return real discord.Embed objects ────────────────


def test_all_builders_return_discord_embed():
    """Smoke-test: every builder produces a real Embed (not a MagicMock leak)."""
    builders = [
        build_channel_allowed_embed("<#1>"),
        build_channel_disallowed_embed("<#1>"),
        build_channel_list_embed([], resolver=lambda _id: None),
        build_game_status_embed(None),
        build_force_end_embed("x"),
        build_audit_channel_embed(None),
    ]
    for embed in builders:
        assert isinstance(embed, discord.Embed)
