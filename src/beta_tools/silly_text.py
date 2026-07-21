"""Silly off-the-shelf filler text for ambient sim messages.

A drop-in replacement for ``MarkovChain``: same ``generate(length_bias)`` /
``corpus_size`` / ``vocab_size`` surface, but instead of a word-transition
chain trained on real server messages it assembles posts from a baked-in
library of classic joke-"ipsum" corpora (bacon, corporate, hipster, cupcake,
pirate). No database, no fixtures, no user data — it works out of the box and
reads as obvious nonsense, which is exactly what we want for beta traffic.
"""
from __future__ import annotations

import random

_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "short":  (5, 15),
    "medium": (10, 30),
    "long":   (20, 60),
}

# Off-the-shelf "silly ipsum" corpora. Each entry is one filler sentence;
# generate() strings these together to hit the requested length.
_CORPUS: tuple[str, ...] = (
    # ── bacon ipsum ──
    "Bacon ipsum dolor amet pork belly short ribs turkey ham hock.",
    "Spare ribs jerky tri-tip, pork chop meatball bresaola shankle.",
    "Pancetta beef ribs pig brisket, tongue tenderloin chuck sausage.",
    "Drumstick capicola ground round, biltong pastrami chislic kielbasa.",
    "Cupim flank shankle, ribeye porchetta swine doner buffalo strip steak.",
    "Andouille meatloaf landjaeger frankfurter, tail short loin venison.",
    "Hamburger boudin salami, prosciutto picanha leberkas filet mignon.",
    # ── corporate ipsum ──
    "Let's circle back and leverage our core competencies going forward.",
    "We need to move the needle and drill down on actionable deliverables.",
    "Going forward, let's take this offline and touch base after the sync.",
    "Synergize the low-hanging fruit before we boil the ocean on this one.",
    "I'll ping the team so we can ideate around a holistic value-add.",
    "Let's not reinvent the wheel — just run it up the flagpole real quick.",
    "Per my last email, we should table this and revisit at a higher altitude.",
    # ── hipster ipsum ──
    "Honestly I liked the channel before it was cool, very small-batch energy.",
    "Brought my own oat milk to the raid, sustainably sourced obviously.",
    "This meme is basically artisanal, you wouldn't have heard of it.",
    "Vinyl, kombucha, and a hand-thrown mug — peak Tuesday vibes here.",
    "I only post ironically, it's a whole curated authentic aesthetic.",
    "Fixie-gang assemble, we are biking to the lo-fi listening party.",
    # ── cupcake ipsum ──
    "Cupcake gummi bears jelly beans, lollipop marshmallow tiramisu.",
    "Chocolate cake danish, sweet roll candy canes liquorice topping.",
    "Toffee bonbon, sugar plum gingerbread macaroon brownie soufflé.",
    "Jujubes carrot cake, croissant powder donut wafer with sprinkles.",
    "Sweet tart pudding, caramels icing dragée cheesecake fruitcake.",
    # ── pirate ipsum ──
    "Arr, hoist the colors ye scurvy dog, the kraken be hungry tonight.",
    "Shiver me timbers, who left the grog barrel open on the poop deck?",
    "Avast! There be treasure in this channel, or me name ain't Long John.",
    "Yo ho ho, splice the mainbrace and feed the parrot some crackers.",
    "Batten down the hatches, a storm o' bad takes be rollin' in.",
    # ── generic small talk so it isn't ALL ipsum ──
    "lol no way that actually happened",
    "ok this is genuinely sending me",
    "wait who scheduled this for so early",
    "brb grabbing snacks, carry on without me",
    "honestly same, every single time",
    "that's so real it hurts a little",
    "anyway, how's everyone doing today?",
    "ngl that's a top tier take",
)

_SENTENCE_ENDERS = {".", "!", "?"}


class SillyTextSource:
    """Assembles filler messages from a fixed library of silly canned text."""

    def __init__(self, lines: tuple[str, ...] | None = None) -> None:
        self._lines = lines if lines is not None else _CORPUS
        self._vocab = {w for line in self._lines for w in line.split()}

    @property
    def corpus_size(self) -> int:
        """Number of canned source lines."""
        return len(self._lines)

    @property
    def vocab_size(self) -> int:
        """Number of distinct words across the corpus."""
        return len(self._vocab)

    def generate(self, length_bias: str = "medium") -> str:
        if length_bias not in _LENGTH_RANGES:
            length_bias = "medium"
        min_words, max_words = _LENGTH_RANGES[length_bias]

        if not self._lines:
            return ""

        chosen: list[str] = []
        word_count = 0
        while word_count < min_words:
            line = random.choice(self._lines)
            chosen.append(line)
            word_count += len(line.split())

        words = " ".join(chosen).split()
        if len(words) > max_words:
            words = words[:max_words]
            # Trimming may have cut mid-sentence; tidy the final punctuation.
            if words[-1][-1] not in _SENTENCE_ENDERS:
                words[-1] = words[-1].rstrip(",;:") + "."
        return " ".join(words)
