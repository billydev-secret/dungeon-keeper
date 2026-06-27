"""Tests for beta_tools.silly_text.SillyTextSource."""
from __future__ import annotations


def test_corpus_size_counts_lines():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource(lines=("one two three.", "four five six!"))
    assert src.corpus_size == 2


def test_vocab_size_counts_unique_words():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource(lines=("alpha beta", "beta gamma"))
    assert src.vocab_size == 3  # alpha, beta, gamma


def test_default_corpus_is_nonempty():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    assert src.corpus_size > 0
    assert src.vocab_size > 0


def test_generate_returns_nonempty_string():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    result = src.generate("short")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_short_within_word_range():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    for _ in range(50):
        result = src.generate("short")
        word_count = len(result.split())
        assert 5 <= word_count <= 15, f"Expected 5-15 words, got {word_count}: {result!r}"


def test_generate_medium_within_word_range():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    for _ in range(50):
        result = src.generate("medium")
        word_count = len(result.split())
        assert 10 <= word_count <= 30, f"Expected 10-30 words, got {word_count}: {result!r}"


def test_generate_long_within_word_range():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    for _ in range(50):
        result = src.generate("long")
        word_count = len(result.split())
        assert 20 <= word_count <= 60, f"Expected 20-60 words, got {word_count}: {result!r}"


def test_generate_unknown_bias_falls_back_to_medium():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource()
    for _ in range(20):
        word_count = len(src.generate("nonexistent_bias").split())
        assert 10 <= word_count <= 30


def test_generate_empty_corpus_returns_empty_string():
    from beta_tools.silly_text import SillyTextSource
    src = SillyTextSource(lines=())
    assert src.generate("short") == ""


def test_generate_trims_and_tidies_punctuation():
    from beta_tools.silly_text import SillyTextSource
    # A single long line forces a mid-sentence trim on "short" (max 15 words).
    long_line = " ".join(f"word{i}" for i in range(40))
    src = SillyTextSource(lines=(long_line,))
    result = src.generate("short")
    words = result.split()
    assert len(words) == 15
    assert result[-1] in ".!?"
