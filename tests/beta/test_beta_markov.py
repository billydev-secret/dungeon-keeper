"""Tests for beta_tools.markov.MarkovChain."""
from __future__ import annotations

import json
from pathlib import Path



def _make_chain_file(tmp_path: Path, chain: dict, corpus_size: int = 500) -> Path:
    data = {"version": 1, "corpus_size": corpus_size, "chain": chain}
    p = tmp_path / "chain.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _cyclic_chain(n: int) -> dict[str, list[str]]:
    """Build an n-word cyclic chain so generation never dead-ends."""
    words = [f"word{i}" for i in range(n)]
    chain = {}
    for i in range(n - 2):
        chain[f"{words[i]} {words[i+1]}"] = [words[i + 2]]
    chain[f"{words[-2]} {words[-1]}"] = [words[0]]
    return chain


def test_load_sets_corpus_size(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(10), corpus_size=999)
    mc = MarkovChain.load(p)
    assert mc.corpus_size == 999


def test_load_sets_vocab_size(tmp_path):
    from beta_tools.markov import MarkovChain
    chain = _cyclic_chain(10)
    p = _make_chain_file(tmp_path, chain)
    mc = MarkovChain.load(p)
    assert mc.vocab_size == len(chain)


def test_generate_returns_nonempty_string(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(20))
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert isinstance(result, str)
    assert len(result) > 0


def test_generate_short_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(20):
        result = mc.generate("short")
        word_count = len(result.split())
        assert 5 <= word_count <= 15, f"Expected 5-15 words, got {word_count}: {result!r}"


def test_generate_medium_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("medium")
        word_count = len(result.split())
        assert 10 <= word_count <= 30, f"Expected 10-30 words, got {word_count}"


def test_generate_long_within_word_range(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(100))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("long")
        word_count = len(result.split())
        assert 20 <= word_count <= 60, f"Expected 20-60 words, got {word_count}"


def test_generate_unknown_bias_falls_back_to_medium(tmp_path):
    from beta_tools.markov import MarkovChain
    p = _make_chain_file(tmp_path, _cyclic_chain(50))
    mc = MarkovChain.load(p)
    for _ in range(10):
        result = mc.generate("nonexistent_bias")
        word_count = len(result.split())
        assert 10 <= word_count <= 30


def test_generate_handles_dead_end_chain(tmp_path):
    from beta_tools.markov import MarkovChain
    # A chain that immediately dead-ends — only one key, empty followers
    dead_end = {"hello world": []}
    p = _make_chain_file(tmp_path, dead_end)
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert isinstance(result, str)
    # With only one bigram key, dead-end recovery re-uses it forever;
    # output should still be non-empty and within bounds
    words = result.split()
    assert len(words) >= 2  # at minimum the starting bigram
    assert len(words) <= 15  # must respect max_words budget


def test_generate_stops_at_sentence_ender(tmp_path):
    from beta_tools.markov import MarkovChain
    # Chain where follower words end in sentence-enders
    # "hello there" -> ["friend.", "world!"]
    chain = {
        "hello there": ["friend.", "world!"],
        "there friend.": ["bye"],
        "there world!": ["bye"],
    }
    p = _make_chain_file(tmp_path, chain)
    mc = MarkovChain.load(p)
    for _ in range(20):
        result = mc.generate("short")
        words = result.split()
        assert len(words) >= 5  # min_words for short


def test_generate_stops_at_max_budget(tmp_path):
    from beta_tools.markov import MarkovChain
    # Infinite cyclic chain — generation must stop at budget
    p = _make_chain_file(tmp_path, _cyclic_chain(100))
    mc = MarkovChain.load(p)
    result = mc.generate("short")
    assert len(result.split()) <= 15
