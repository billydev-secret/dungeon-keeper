"""Bigram Markov chain for ambient sim message generation."""
from __future__ import annotations

import json
import random
from pathlib import Path

_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "short":  (5, 15),
    "medium": (10, 30),
    "long":   (20, 60),
}

_SENTENCE_ENDERS = {".", "!", "?"}


class MarkovChain:
    def __init__(self, chain: dict[str, list[str]], corpus_size: int) -> None:
        self._chain = chain
        self._corpus_size = corpus_size
        self._keys = list(chain.keys())

    @classmethod
    def load(cls, path: str | Path) -> "MarkovChain":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(chain=data["chain"], corpus_size=data["corpus_size"])

    @property
    def corpus_size(self) -> int:
        return self._corpus_size

    @property
    def vocab_size(self) -> int:
        return len(self._keys)

    def generate(self, length_bias: str = "medium") -> str:
        if length_bias not in _LENGTH_RANGES:
            length_bias = "medium"
        min_words, max_words = _LENGTH_RANGES[length_bias]

        if not self._keys:
            return ""

        state = random.choice(self._keys)
        words: list[str] = list(state.split())

        for _ in range(max_words - len(words)):
            followers = self._chain.get(state) or []
            if not followers:
                if len(words) >= min_words:
                    break
                state = random.choice(self._keys)
                words.extend(state.split())
                continue

            next_word = random.choice(followers)
            words.append(next_word)

            if len(words) >= min_words and next_word and next_word[-1] in _SENTENCE_ENDERS:
                break

            state = f"{words[-2]} {words[-1]}"

        return " ".join(words[:max_words])
