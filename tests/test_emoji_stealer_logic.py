"""Tests for the extracted emoji-stealer pure helpers.

Covers ``bot_modules/emoji_stealer/logic.py`` (URL, name, parsing, prompt
builders) and the GIF-compression entrypoint in ``compress.py``. The cog
keeps the Discord glue; this module proves the helpers behave without a
real interaction.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bot_modules.emoji_stealer.compress import compress_gif_for_emoji
from bot_modules.emoji_stealer.logic import (
    DISCORD_MAX_EMOJI_BYTES,
    build_steal_prompt,
    emoji_cdn_url,
    extract_emojis_from_text,
    extract_emojis_from_reactions,
    format_steal_all_summary,
    is_https_url,
    looks_like_image,
    merge_emoji_hits,
    sanitize_emoji_name,
    validate_emoji_name,
)


# ── emoji_cdn_url ────────────────────────────────────────────────────


def test_emoji_cdn_url_static():
    assert emoji_cdn_url(12345, animated=False) == "https://cdn.discordapp.com/emojis/12345.png"


def test_emoji_cdn_url_animated():
    assert emoji_cdn_url(12345, animated=True) == "https://cdn.discordapp.com/emojis/12345.gif"


# ── sanitize_emoji_name ──────────────────────────────────────────────


def test_sanitize_emoji_name_replaces_punctuation_with_underscores():
    assert sanitize_emoji_name("hello-world!") == "hello_world_"


def test_sanitize_emoji_name_pads_single_char_to_minimum_length():
    """Discord rejects single-char emoji names. Pad with `_e` to satisfy 2-char min."""
    assert sanitize_emoji_name("x") == "x_e"


def test_sanitize_emoji_name_pads_empty_string():
    assert sanitize_emoji_name("") == "_e"


def test_sanitize_emoji_name_caps_at_32_chars():
    long = "abcdefghijklmnopqrstuvwxyz123456789"
    assert len(sanitize_emoji_name(long)) <= 32


def test_sanitize_emoji_name_preserves_underscores_and_alphanumerics():
    assert sanitize_emoji_name("good_name_123") == "good_name_123"


# ── is_https_url ─────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://example.com/foo.png",
    "HTTPS://example.com/foo.png",  # case-insensitive
    "https://",
])
def test_is_https_url_accepts_https(url):
    assert is_https_url(url) is True


@pytest.mark.parametrize("url", [
    "http://example.com/foo.png",
    "ftp://example.com/foo.png",
    "javascript:alert(1)",  # nasty
    "",
    "example.com/foo.png",
])
def test_is_https_url_rejects_non_https(url):
    assert is_https_url(url) is False


# ── looks_like_image ─────────────────────────────────────────────────


@pytest.mark.parametrize("data", [
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,          # PNG
    b"\xff\xd8\xff\xe0" + b"\x00" * 16,            # JPEG
    b"GIF89a" + b"\x00" * 16,                      # GIF
    b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8,     # WEBP
])
def test_looks_like_image_accepts_real_image_magic(data):
    assert looks_like_image(data) is True


@pytest.mark.parametrize("data", [
    b"<!DOCTYPE html><html>...",                   # the bug: an HTML page served as 200
    b"not an image at all, just text bytes here",
    b"",
    b"GI",                                         # too short to classify
    b"RIFF\x00\x00\x00\x00AVI ",                   # RIFF container, but not WEBP
])
def test_looks_like_image_rejects_non_images(data):
    assert looks_like_image(data) is False


# ── validate_emoji_name ──────────────────────────────────────────────


def test_validate_emoji_name_accepts_normal_name():
    ok, clean, err = validate_emoji_name("party_parrot")
    assert ok is True
    assert clean == "party_parrot"
    assert err is None


def test_validate_emoji_name_sanitizes_punctuation():
    """The cleaned name comes back even when the input had punctuation."""
    ok, clean, err = validate_emoji_name("hello world")
    assert ok is True
    assert clean == "hello_world"


def test_validate_emoji_name_sanitizes_punctuation_through_padding():
    """A single punctuation char becomes ``_`` (length 1) then pads to ``__e``
    via the sanitizer — well over the 2-char minimum, so it's accepted."""
    ok, clean, err = validate_emoji_name("!")
    assert ok is True
    assert clean == "__e"
    assert err is None


def test_validate_emoji_name_pads_empty_input_to_valid_length():
    ok, clean, err = validate_emoji_name("")
    assert ok is True
    assert clean == "_e"
    assert err is None


# ── extract_emojis_from_text ─────────────────────────────────────────


def test_extract_emojis_from_text_finds_static_emojis():
    text = "look at <:foo:123> and <:bar:456>"
    emojis = extract_emojis_from_text(text)
    assert emojis == [(False, "foo", 123), (False, "bar", 456)]


def test_extract_emojis_from_text_finds_animated_emojis():
    text = "<a:wave:987>"
    emojis = extract_emojis_from_text(text)
    assert emojis == [(True, "wave", 987)]


def test_extract_emojis_from_text_deduplicates_by_id():
    """A message mentioning the same emoji twice yields one entry."""
    text = "<:foo:123> <:foo:123> <:bar:456>"
    emojis = extract_emojis_from_text(text)
    assert emojis == [(False, "foo", 123), (False, "bar", 456)]


def test_extract_emojis_from_text_preserves_order():
    text = "<:c:300> <:a:100> <:b:200>"
    ids = [e[2] for e in extract_emojis_from_text(text)]
    assert ids == [300, 100, 200]


def test_extract_emojis_from_text_returns_empty_for_no_emojis():
    assert extract_emojis_from_text("just text, no emojis") == []


def test_extract_emojis_from_text_handles_empty_input():
    assert extract_emojis_from_text("") == []


def test_extract_emojis_from_text_ignores_invalid_emoji_syntax():
    """Patterns that don't match the strict ``<a?:name:id>`` shape are ignored."""
    text = "<:no_id:> <something:not:emoji> <:bad>"
    assert extract_emojis_from_text(text) == []


# ── extract_emojis_from_reactions ────────────────────────────────────
#
# Reaction emoji arrive as discord.py objects, not text. Fake them with a tiny
# duck-typed stand-in — the helper reads only .id/.name/.animated, and a
# Unicode reaction is a bare str.


class _FakeReactionEmoji:
    def __init__(self, *, id, name, animated=False):
        self.id = id
        self.name = name
        self.animated = animated


def test_extract_from_reactions_finds_custom_emoji():
    emojis = [
        _FakeReactionEmoji(id=123, name="foo"),
        _FakeReactionEmoji(id=456, name="wave", animated=True),
    ]
    assert extract_emojis_from_reactions(emojis) == [
        (False, "foo", 123),
        (True, "wave", 456),
    ]


def test_extract_from_reactions_skips_unicode_str_reactions():
    """A Unicode reaction is a plain str and isn't stealable."""
    emojis = ["😀", _FakeReactionEmoji(id=123, name="foo"), "🔥"]
    assert extract_emojis_from_reactions(emojis) == [(False, "foo", 123)]


def test_extract_from_reactions_skips_partial_emoji_without_id():
    """A PartialEmoji for a Unicode emoji has id=None — skip it."""
    emojis = [_FakeReactionEmoji(id=None, name="😀"), _FakeReactionEmoji(id=9, name="ok")]
    assert extract_emojis_from_reactions(emojis) == [(False, "ok", 9)]


def test_extract_from_reactions_deduplicates_by_id():
    emojis = [_FakeReactionEmoji(id=1, name="a"), _FakeReactionEmoji(id=1, name="a")]
    assert extract_emojis_from_reactions(emojis) == [(False, "a", 1)]


def test_extract_from_reactions_falls_back_when_name_missing():
    """A nameless custom emoji still steals — sanitize_emoji_name pads it later."""
    assert extract_emojis_from_reactions([_FakeReactionEmoji(id=7, name=None)]) == [
        (False, "emoji", 7)
    ]


def test_extract_from_reactions_handles_empty_and_none():
    assert extract_emojis_from_reactions([]) == []
    assert extract_emojis_from_reactions(None) == []


# ── merge_emoji_hits ─────────────────────────────────────────────────


def test_merge_dedupes_across_lists_keeping_first():
    text_hits = [(False, "foo", 123)]
    reaction_hits = [(False, "foo", 123), (True, "bar", 456)]
    # 123 seen in text first, so the reaction copy is dropped; 456 is kept.
    assert merge_emoji_hits(text_hits, reaction_hits) == [
        (False, "foo", 123),
        (True, "bar", 456),
    ]


def test_merge_preserves_argument_order():
    a = [(False, "a", 1)]
    b = [(False, "b", 2)]
    assert merge_emoji_hits(a, b) == [(False, "a", 1), (False, "b", 2)]
    assert merge_emoji_hits(b, a) == [(False, "b", 2), (False, "a", 1)]


def test_merge_handles_no_lists_and_empty_lists():
    assert merge_emoji_hits() == []
    assert merge_emoji_hits([], []) == []


# ── build_steal_prompt ───────────────────────────────────────────────


def test_build_steal_prompt_multi_emoji_multi_guild():
    msg = build_steal_prompt(
        n_emoji=3, guild_count=2,
        first_emoji_name="a", first_guild_name="MyGuild",
    )
    assert "3" in msg
    assert "and a server" in msg


def test_build_steal_prompt_multi_emoji_single_guild():
    msg = build_steal_prompt(
        n_emoji=3, guild_count=1,
        first_emoji_name="a", first_guild_name="OnlyGuild",
    )
    assert "3" in msg
    assert "OnlyGuild" in msg


def test_build_steal_prompt_single_emoji():
    msg = build_steal_prompt(
        n_emoji=1, guild_count=2,
        first_emoji_name="partyparrot", first_guild_name="MyGuild",
    )
    assert "partyparrot" in msg
    assert "which server" in msg


# ── format_steal_all_summary ─────────────────────────────────────────


def test_format_steal_all_summary_added_only():
    text = format_steal_all_summary(
        added_mentions=["<:a:1>", "<:b:2>", "<:c:3>"],
        guild_name="MyGuild",
        failed=[],
    )
    assert "**3**" in text
    assert "MyGuild" in text
    assert "<:a:1>" in text


def test_format_steal_all_summary_pluralizes_correctly():
    """One added emoji uses singular; many uses plural."""
    one = format_steal_all_summary(
        added_mentions=["<:a:1>"], guild_name="G", failed=[],
    )
    many = format_steal_all_summary(
        added_mentions=["<:a:1>", "<:b:2>"], guild_name="G", failed=[],
    )
    assert "emoji " in one  # "1 emoji to"
    assert "emojis " in many  # "2 emojis to"


def test_format_steal_all_summary_includes_failures():
    text = format_steal_all_summary(
        added_mentions=[],
        guild_name="G",
        failed=[("bad1", "Discord rejected"), ("bad2", "Too big")],
    )
    assert "Failed **2**" in text
    assert "bad1" in text
    assert "Discord rejected" in text


def test_format_steal_all_summary_mixed_added_and_failed():
    text = format_steal_all_summary(
        added_mentions=["<:ok:1>"],
        guild_name="G",
        failed=[("bad", "nope")],
    )
    assert "Added" in text
    assert "Failed" in text


def test_format_steal_all_summary_empty_returns_empty_string():
    assert format_steal_all_summary(
        added_mentions=[], guild_name="G", failed=[],
    ) == ""


def test_format_steal_all_summary_reports_skipped_duplicates():
    text = format_steal_all_summary(
        added_mentions=[],
        guild_name="G",
        failed=[],
        skipped=["dup1", "dup2"],
    )
    assert "Skipped **2** already present" in text
    assert "dup1" in text
    assert "dup2" in text


def test_format_steal_all_summary_added_skipped_and_failed():
    text = format_steal_all_summary(
        added_mentions=["<:ok:1>"],
        guild_name="G",
        failed=[("bad", "nope")],
        skipped=["already"],
    )
    assert "Added" in text
    assert "Skipped **1**" in text
    assert "Failed" in text


# ── compress_gif_for_emoji ───────────────────────────────────────────


def _make_gif(width: int, height: int, frame_count: int = 1) -> bytes:
    """Render a minimal GIF in-memory for compression tests."""
    img = Image.new("RGBA", (width, height), color=(255, 0, 0, 255))
    out = io.BytesIO()
    frames = [img] * max(frame_count, 1)
    frames[0].save(
        out, format="GIF", save_all=True, append_images=frames[1:],
        duration=100, loop=0,
    )
    return out.getvalue()


def test_compress_gif_returns_input_when_under_limit():
    """Small inputs are returned untouched — the function only resizes when over the cap."""
    small = _make_gif(32, 32, frame_count=2)
    assert len(small) <= DISCORD_MAX_EMOJI_BYTES
    assert compress_gif_for_emoji(small) is small


def test_compress_gif_returns_input_when_not_a_gif():
    """Non-GIF input passes through unchanged regardless of size."""
    not_a_gif = b"PNG\x89" + b"\x00" * 300_000
    assert compress_gif_for_emoji(not_a_gif) is not_a_gif


def test_compress_gif_handles_short_input_safely():
    """A pre-magic-bytes-only input must not crash."""
    assert compress_gif_for_emoji(b"") == b""
    assert compress_gif_for_emoji(b"GI") == b"GI"
