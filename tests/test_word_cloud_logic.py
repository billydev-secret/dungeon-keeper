"""Tests for the word cloud's pure logic: windows, tokenising, counting.

These are the rules that decide whether a cloud is a portrait of the room or a
portrait of the bot, so they are asserted directly rather than through the
Discord surface that displays them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot_modules.word_cloud import presets
from bot_modules.word_cloud.logic import (
    LIVE_FETCH_MAX,
    Doc,
    WindowError,
    apply_cap,
    build_stats,
    clamp_live_window,
    clean_text,
    parse_window,
    tokenize,
)


# --------------------------------------------------------------------------
# Window parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("30 m", timedelta(minutes=30)),
        ("5min", timedelta(minutes=5)),
        ("45 minutes", timedelta(minutes=45)),
        ("6h", timedelta(hours=6)),
        ("6 HOURS", timedelta(hours=6)),
        ("1hr", timedelta(hours=1)),
        ("7d", timedelta(days=7)),
        ("90 days", timedelta(days=90)),
        ("  2 day  ", timedelta(days=2)),
    ],
)
def test_parse_window_accepts_units(text, expected):
    assert parse_window(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "7",          # no unit
        "d",          # no amount
        "7 weeks",    # unit we don't serve
        "-3d",        # the regex refuses the sign outright
        "lots",
        "7d 2h",      # one unit at a time
    ],
)
def test_parse_window_rejects_junk(text):
    with pytest.raises(WindowError):
        parse_window(text)


def test_parse_window_rejects_zero():
    with pytest.raises(WindowError, match="more than zero"):
        parse_window("0h")


def test_parse_window_rejects_absurd_window():
    """A runaway window is the one way this command costs real time."""
    with pytest.raises(WindowError, match="two years"):
        parse_window("5000d")


# --------------------------------------------------------------------------
# The live-fetch ceiling
# --------------------------------------------------------------------------


def test_clamp_live_window_shortens_and_reports():
    clamped, was_clamped = clamp_live_window(timedelta(days=7))
    assert clamped == LIVE_FETCH_MAX
    assert was_clamped is True


def test_clamp_live_window_leaves_short_windows_alone():
    clamped, was_clamped = clamp_live_window(timedelta(minutes=3))
    assert clamped == timedelta(minutes=3)
    assert was_clamped is False


def test_clamp_live_window_is_inclusive_at_the_ceiling():
    clamped, was_clamped = clamp_live_window(LIVE_FETCH_MAX)
    assert clamped == LIVE_FETCH_MAX
    assert was_clamped is False


# --------------------------------------------------------------------------
# Cleaning and tokenising
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "gone"),
    [
        ("look at https://example.com/thing", "example"),
        ("see www.klipy.co/gif for it", "klipy"),
        ("nice <:blobcat:12345> there", "blobcat"),
        ("nice <a:spin:678> there", "spin"),
        ("hey <@1234> and <@!5678>", "1234"),
        ("hey <@&999> team", "999"),
        ("posted in <#4242> earlier", "4242"),
        ("run ```python\nsecret_token\n``` now", "secret_token"),
        ("use `inline_code` here", "inline_code"),
    ],
)
def test_clean_text_strips_markup_and_urls(raw, gone):
    assert gone not in tokenize(raw)


def test_clean_text_keeps_the_surrounding_words():
    """Stripping must not eat the sentence around the thing stripped."""
    assert tokenize("cats https://x.com/a dogs") == ["cats", "dogs"]


def test_code_fence_is_stripped_before_inline_code():
    """A fence contains backticks, so fence-first is the only order that works."""
    assert "hidden" not in tokenize("start ```a `hidden` b``` end")


@pytest.mark.parametrize("apostrophe", ["'", "’", "‘", "ʼ"])
def test_contractions_survive_every_apostrophe(apostrophe):
    """U+2019 outnumbers ASCII in the archive; unfolded it strands "don"."""
    assert tokenize(f"don{apostrophe}t worry cats") == ["worry", "cats"]
    assert tokenize(f"that{apostrophe}s it{apostrophe}s cats") == ["cats"]


def test_typographic_apostrophe_does_not_leak_a_stem():
    assert "don" not in tokenize("I don’t think that’s it")


def test_tokenize_drops_stopwords_and_short_words():
    assert tokenize("the cat is on a mat") == ["cat", "mat"]


def test_tokenize_lowercases():
    assert tokenize("CATS Cats cats") == ["cats", "cats", "cats"]


def test_tokenize_drops_pure_punctuation_and_digits():
    assert tokenize("!!! 12345 --- ???") == []


def test_clean_text_handles_empty_and_none_ish():
    assert clean_text("") == ""
    assert tokenize("") == []


# --------------------------------------------------------------------------
# The message cap
# --------------------------------------------------------------------------


def test_apply_cap_keeps_the_newest_and_reports():
    docs = [Doc(text=str(i)) for i in range(10)]
    kept, was_capped = apply_cap(docs, 4)
    assert [d.text for d in kept] == ["0", "1", "2", "3"]
    assert was_capped is True


def test_apply_cap_below_the_cap_is_untouched():
    docs = [Doc(text="a"), Doc(text="b")]
    kept, was_capped = apply_cap(docs, 50)
    assert kept == docs
    assert was_capped is False


def test_apply_cap_of_zero_is_a_no_op():
    """A dial blanked to 0 must not silently render an empty cloud."""
    docs = [Doc(text="a")]
    kept, was_capped = apply_cap(docs, 0)
    assert kept == docs
    assert was_capped is False


# --------------------------------------------------------------------------
# Counting and sentiment
# --------------------------------------------------------------------------


def test_build_stats_counts_and_orders_by_frequency():
    docs = [Doc(text="cats cats dogs"), Doc(text="cats")]
    stats = build_stats(docs)
    assert [(s.word, s.count) for s in stats] == [("cats", 3), ("dogs", 1)]


def test_build_stats_averages_sentiment_per_occurrence():
    """A word used twice in one message weighs twice, as it does in the count."""
    docs = [Doc(text="cats cats", sentiment=1.0), Doc(text="cats", sentiment=-0.5)]
    stats = {s.word: s for s in build_stats(docs)}
    assert stats["cats"].count == 3
    assert stats["cats"].sentiment == pytest.approx((1.0 + 1.0 - 0.5) / 3)


def test_build_stats_leaves_sentiment_none_when_unscored():
    """The live-fetch path has no scores, so colouring must fall back."""
    stats = build_stats([Doc(text="cats dogs")])
    assert all(s.sentiment is None for s in stats)


def test_build_stats_mixes_scored_and_unscored_docs():
    docs = [Doc(text="cats", sentiment=0.8), Doc(text="cats dogs")]
    stats = {s.word: s for s in build_stats(docs)}
    assert stats["cats"].count == 2
    assert stats["cats"].sentiment == pytest.approx(0.8)
    assert stats["dogs"].sentiment is None


def test_build_stats_honours_min_count():
    docs = [Doc(text="cats cats dogs")]
    assert [s.word for s in build_stats(docs, min_count=2)] == ["cats"]


def test_build_stats_honours_max_words():
    # Distinct and alphabetic — digits are dropped, so "word0".."word49"
    # would all collapse to the single token "word".
    vocab = [f"zz{chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(50)]
    docs = [Doc(text=" ".join(vocab))]
    assert len(build_stats(docs, max_words=10)) == 10


def test_build_stats_on_nothing_is_empty_not_an_error():
    assert build_stats([]) == []
    assert build_stats([Doc(text="the and but")]) == []


# --------------------------------------------------------------------------
# Presets and colour
# --------------------------------------------------------------------------


def test_resolve_preset_finds_each_key():
    for preset in presets.PRESETS:
        assert presets.resolve_preset(preset.key).key == preset.key


@pytest.mark.parametrize("key", [None, "", "  ", "no-such-preset"])
def test_resolve_preset_falls_back_rather_than_raising(key):
    """A dial holding a key a later release renamed must not break the command."""
    assert presets.resolve_preset(key).key == presets.DEFAULT_PRESET


def test_resolve_preset_is_case_insensitive():
    assert presets.resolve_preset("MIDNIGHT").key == "midnight"


def test_sentiment_color_moves_between_the_stops():
    preset = presets.resolve_preset("midnight")
    neg, mid, pos = preset.sentiment_stops
    assert presets.sentiment_color(preset, -1.0) == neg
    assert presets.sentiment_color(preset, 0.0) == mid
    assert presets.sentiment_color(preset, 1.0) == pos


def test_sentiment_color_uses_neutral_when_unscored():
    preset = presets.resolve_preset("midnight")
    assert presets.sentiment_color(preset, None) == preset.sentiment_stops[1]


@pytest.mark.parametrize("score", [-99.0, 99.0])
def test_sentiment_color_clamps_out_of_range_scores(score):
    preset = presets.resolve_preset("parchment")
    neg, _mid, pos = preset.sentiment_stops
    assert presets.sentiment_color(preset, score) in {neg, pos}


def test_rank_color_cycles_the_palette():
    preset = presets.resolve_preset("neon")
    size = len(preset.palette)
    assert presets.rank_color(preset, 0) == presets.rank_color(preset, size)


def test_every_preset_names_a_font_that_exists_in_the_repo():
    """Guards a typo in a preset, which would otherwise only fail at render."""
    for preset in presets.PRESETS:
        assert preset.font_path.suffix == ".ttf"
        assert preset.font_path.parent.name == "fonts"
