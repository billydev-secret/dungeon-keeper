"""Tests for the extracted starboard pure-logic modules.

These cover ``bot_modules/starboard/filters.py`` (decision logic + validation)
and ``bot_modules/starboard/embeds.py`` (embed builders). Mirrors the
pressure_cooker pattern: the cog file stays thin; this module proves the
extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import discord
import pytest

from bot_modules.starboard.embeds import (
    build_starboard_embed,
    build_status_embed,
    updated_starboard_embed,
)
from bot_modules.starboard.filters import (
    merge_default_config,
    nsfw_leak_blocked,
    should_process_reaction,
    validate_emoji,
)


# ── should_process_reaction ──────────────────────────────────────────


def _kwargs(**overrides):
    """Build a kwargs dict for should_process_reaction with sensible defaults."""
    base = dict(
        cfg_enabled=True,
        cfg_channel_id=999,
        cfg_emoji="⭐",
        payload_emoji="⭐",
        payload_channel_id=100,
        excluded_channel_ids=set(),
    )
    base.update(overrides)
    return base


def test_should_process_reaction_returns_true_when_all_checks_pass():
    assert should_process_reaction(**_kwargs()) is True


def test_should_process_reaction_returns_false_when_starboard_disabled():
    assert should_process_reaction(**_kwargs(cfg_enabled=False)) is False


def test_should_process_reaction_returns_false_when_no_channel_configured():
    assert should_process_reaction(**_kwargs(cfg_channel_id=0)) is False


def test_should_process_reaction_returns_false_when_emoji_mismatch():
    assert should_process_reaction(**_kwargs(payload_emoji="🔥")) is False


def test_should_process_reaction_ignores_reactions_on_the_starboard_itself():
    """A star on a starboard post must NOT compound onto another post —
    the reaction's channel matches the starboard channel."""
    assert (
        should_process_reaction(**_kwargs(payload_channel_id=999, cfg_channel_id=999))
        is False
    )


def test_should_process_reaction_skips_excluded_source_channels():
    assert (
        should_process_reaction(
            **_kwargs(payload_channel_id=42, excluded_channel_ids={42, 43})
        )
        is False
    )


def test_should_process_reaction_treats_iterable_input():
    """Excluded list can be a frozenset, list, or anything iterable."""
    assert (
        should_process_reaction(
            **_kwargs(payload_channel_id=42, excluded_channel_ids=frozenset({42}))
        )
        is False
    )
    assert (
        should_process_reaction(
            **_kwargs(payload_channel_id=42, excluded_channel_ids=[1, 2, 42])
        )
        is False
    )


# ── nsfw_leak_blocked ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source,starboard,expected",
    [
        (True, False, True),    # NSFW source → SFW starboard: BLOCK
        (True, True, False),    # NSFW source → NSFW starboard: OK
        (False, True, False),   # SFW source → NSFW starboard: OK
        (False, False, False),  # SFW source → SFW starboard: OK
    ],
)
def test_nsfw_leak_blocked_only_blocks_nsfw_to_sfw(source, starboard, expected):
    assert (
        nsfw_leak_blocked(source_nsfw=source, starboard_nsfw=starboard) == expected
    )


# ── validate_emoji ───────────────────────────────────────────────────


def test_validate_emoji_rejects_empty_string():
    ok, msg = validate_emoji("")
    assert ok is False
    assert "empty" in msg.lower()


def test_validate_emoji_rejects_whitespace_only():
    ok, msg = validate_emoji("   ")
    assert ok is False
    assert msg is not None


def test_validate_emoji_accepts_unicode_emoji():
    ok, msg = validate_emoji("⭐")
    assert ok is True
    assert msg is None


def test_validate_emoji_accepts_custom_server_emoji():
    ok, msg = validate_emoji("<:custom_name:123456789>")
    assert ok is True
    assert msg is None


def test_validate_emoji_accepts_animated_custom_emoji():
    ok, msg = validate_emoji("<a:wave:987654321>")
    assert ok is True
    assert msg is None


def test_validate_emoji_strips_surrounding_whitespace_first():
    """Leading/trailing whitespace must not falsely flag a valid emoji."""
    ok, msg = validate_emoji("  ⭐  ")
    assert ok is True
    assert msg is None


# ── merge_default_config ─────────────────────────────────────────────


def test_merge_default_config_uses_defaults_when_row_is_none():
    cfg = merge_default_config(None)
    assert cfg == {"channel_id": 0, "threshold": 3, "emoji": "⭐", "enabled": 1}


def test_merge_default_config_preserves_stored_row_values():
    row = {"channel_id": 5001, "threshold": 7, "emoji": "🔥", "enabled": 0}
    cfg = merge_default_config(row)
    assert cfg == row


# ── build_starboard_embed ────────────────────────────────────────────


def _make_message(
    *, content: str = "Hello world", channel_name: str = "general",
    author_name: str = "alice", attachments: list | None = None,
) -> MagicMock:
    msg = MagicMock(spec=discord.Message)
    msg.content = content
    # discord.Embed.timestamp setter type-checks for real datetime — a
    # plain MagicMock here raises TypeError on construction.
    msg.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    channel = MagicMock()
    channel.name = channel_name
    channel.id = 100
    msg.channel = channel
    author = MagicMock()
    author.display_name = author_name
    author.display_avatar = MagicMock()
    author.display_avatar.url = "https://cdn.example/avatar.png"
    msg.author = author
    msg.jump_url = "https://discord.com/channels/1/100/200"
    msg.attachments = attachments or []
    return msg


def test_build_starboard_embed_carries_content_and_count():
    msg = _make_message(content="It's a star!", channel_name="general")
    embed = build_starboard_embed(msg, star_count=5, emoji="⭐")
    assert embed.description == "It's a star!"
    assert embed.footer.text == "⭐ 5"
    assert "general" in embed.author.name
    assert "alice" in embed.author.name


def test_build_starboard_embed_truncates_long_content_to_2000_chars():
    msg = _make_message(content="x" * 5000)
    embed = build_starboard_embed(msg, star_count=3, emoji="⭐")
    assert len(embed.description) == 2000


def test_build_starboard_embed_handles_empty_content():
    msg = _make_message(content="")
    embed = build_starboard_embed(msg, star_count=3, emoji="⭐")
    assert embed.description is None  # empty → no description


def test_build_starboard_embed_surfaces_first_image_attachment():
    img = MagicMock()
    img.content_type = "image/png"
    img.url = "https://cdn.example/pic.png"
    text = MagicMock()
    text.content_type = "text/plain"
    text.url = "https://cdn.example/file.txt"

    msg = _make_message(attachments=[text, img, img])
    embed = build_starboard_embed(msg, star_count=3, emoji="⭐")
    assert embed.image.url == "https://cdn.example/pic.png"


def test_build_starboard_embed_skips_non_image_attachments():
    text = MagicMock()
    text.content_type = "text/plain"
    text.url = "https://cdn.example/file.txt"
    msg = _make_message(attachments=[text])
    embed = build_starboard_embed(msg, star_count=3, emoji="⭐")
    # No image set when no image attachments exist
    assert embed.image.url is None


def test_build_starboard_embed_includes_jump_link():
    msg = _make_message()
    embed = build_starboard_embed(msg, star_count=3, emoji="⭐")
    jump_field = next((f for f in embed.fields if f.name == "Original"), None)
    assert jump_field is not None
    assert "Jump" in jump_field.value
    assert msg.jump_url in jump_field.value


# ── updated_starboard_embed ──────────────────────────────────────────


def test_updated_starboard_embed_only_refreshes_footer():
    """Author, description, fields, image — everything but the footer stays."""
    msg = _make_message(content="original")
    original = build_starboard_embed(msg, star_count=3, emoji="⭐")

    updated = updated_starboard_embed(original, star_count=99, emoji="⭐")
    assert updated.footer.text == "⭐ 99"
    assert updated.description == original.description
    assert updated.author.name == original.author.name
    # Same number of fields
    assert len(updated.fields) == len(original.fields)


def test_updated_starboard_embed_returns_a_copy_not_a_mutation():
    msg = _make_message()
    original = build_starboard_embed(msg, star_count=3, emoji="⭐")
    original_footer = original.footer.text

    updated_starboard_embed(original, star_count=10, emoji="⭐")
    # The original embed's footer must NOT have changed (the function copies).
    assert original.footer.text == original_footer


def test_updated_starboard_embed_supports_emoji_swap():
    """Mods can change the starboard emoji at runtime; the new emoji should
    appear on the next refresh of any existing post."""
    msg = _make_message()
    original = build_starboard_embed(msg, star_count=3, emoji="⭐")
    updated = updated_starboard_embed(original, star_count=3, emoji="🔥")
    assert updated.footer.text == "🔥 3"


# ── build_status_embed ───────────────────────────────────────────────


def test_build_status_embed_shows_not_set_when_no_channel_configured():
    cfg = {"channel_id": 0, "threshold": 3, "emoji": "⭐", "enabled": 0}
    embed = build_status_embed(cfg, excluded_ids=[])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Channel"] == "*not set*"
    assert by_name["Status"] == "disabled"
    assert by_name["Excluded channels"] == "*none*"


def test_build_status_embed_renders_channel_mentions():
    cfg = {"channel_id": 7777, "threshold": 5, "emoji": "🔥", "enabled": 1}
    embed = build_status_embed(cfg, excluded_ids=[111, 222])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Channel"] == "<#7777>"
    assert by_name["Status"] == "enabled"
    assert by_name["Threshold"] == "5"
    assert by_name["Emoji"] == "🔥"
    assert "<#111>" in by_name["Excluded channels"]
    assert "<#222>" in by_name["Excluded channels"]


def test_build_status_embed_sorts_excluded_channel_mentions():
    """Stable output regardless of input ordering — easier to read at a glance."""
    cfg = {"channel_id": 1, "threshold": 3, "emoji": "⭐", "enabled": 1}
    embed = build_status_embed(cfg, excluded_ids=[300, 100, 200])
    by_name = {f.name: f.value for f in embed.fields}
    # Channels appear in sorted order
    assert by_name["Excluded channels"].index("100") < by_name["Excluded channels"].index("200")
    assert by_name["Excluded channels"].index("200") < by_name["Excluded channels"].index("300")
