"""The 152-tile deck (spec §2.1) and its CSPRNG shuffle.

Stage 1 of docs/plans/meadow-mahjong.md. A :class:`Tile` is one of 36 *kinds*;
physical copies (4 of each suited/wind/dragon tile, 8 flowers, 8 jokers) exist
only in the wall list — tiles themselves are value objects. The enum value is
the short code shared by the serialized engine state and the emoji id map
(``mm_<code>``, spec §7.1), so ``Tile("5b")`` parses a stored code back.

Definition order is display order — dots, bams, craks 1–9, winds N/E/W/S,
dragons R/G/soap, flower, joker — and :func:`sort_rack` sorts by it. Soap's
double life as rank 0 (§2.1) is a *matching* rule, not a tile property; it
lives in card_logic/match_logic.
"""

from __future__ import annotations

import random
import secrets
from enum import Enum

#: Suit letters in display order: Dots, Bams, Craks.
SUITS = ("d", "b", "c")
_SUIT_WORDS = {"d": "Dot", "b": "Bam", "c": "Crak"}


class Tile(Enum):
    """One tile kind; ``.value`` is its serialized/emoji code."""

    DOT1 = "1d"
    DOT2 = "2d"
    DOT3 = "3d"
    DOT4 = "4d"
    DOT5 = "5d"
    DOT6 = "6d"
    DOT7 = "7d"
    DOT8 = "8d"
    DOT9 = "9d"
    BAM1 = "1b"
    BAM2 = "2b"
    BAM3 = "3b"
    BAM4 = "4b"
    BAM5 = "5b"
    BAM6 = "6b"
    BAM7 = "7b"
    BAM8 = "8b"
    BAM9 = "9b"
    CRAK1 = "1c"
    CRAK2 = "2c"
    CRAK3 = "3c"
    CRAK4 = "4c"
    CRAK5 = "5c"
    CRAK6 = "6c"
    CRAK7 = "7c"
    CRAK8 = "8c"
    CRAK9 = "9c"
    NORTH = "wn"
    EAST = "we"
    WEST = "ww"
    SOUTH = "ws"
    RED = "dr"
    GREEN = "dg"
    SOAP = "soap"
    FLOWER = "flower"
    JOKER = "joker"

    @property
    def code(self) -> str:
        """Serialized short code (also the emoji key, minus the ``mm_``)."""
        return self.value

    @property
    def is_suited(self) -> bool:
        return self.value[0].isdigit()

    @property
    def suit(self) -> str | None:
        """Suit letter ``"d"``/``"b"``/``"c"`` for suited tiles, else None."""
        return self.value[1] if self.is_suited else None

    @property
    def rank(self) -> int | None:
        """Rank 1–9 for suited tiles, else None (soap-as-zero is a card rule)."""
        return int(self.value[0]) if self.is_suited else None

    @property
    def is_wind(self) -> bool:
        return self.value in ("wn", "we", "ww", "ws")

    @property
    def is_dragon(self) -> bool:
        return self.value in ("dr", "dg", "soap")

    @property
    def is_honor(self) -> bool:
        return self.is_wind or self.is_dragon

    @property
    def is_flower(self) -> bool:
        return self is Tile.FLOWER

    @property
    def is_joker(self) -> bool:
        return self is Tile.JOKER

    @property
    def label(self) -> str:
        """Human-readable name for error copy and logs."""
        if self.is_suited:
            return f"{self.rank} {_SUIT_WORDS[self.value[1]]}"
        return _LABELS[self.value]

    def __repr__(self) -> str:  # "Tile<5b>" beats "<Tile.BAM5: '5b'>" in diffs
        return f"Tile<{self.value}>"

    #: Enum's own __hash__ hashes the member *name*; members are singletons
    #: and compare by identity, so an identity hash is equivalent and much
    #: cheaper. The matcher hashes tiles into Counters and dicts millions of
    #: times per simulated game (tens of thousands per real turn), and this
    #: one line took ~15% off a headless sim run.
    __hash__ = object.__hash__


_LABELS = {
    "wn": "North", "we": "East", "ww": "West", "ws": "South",
    "dr": "Red Dragon", "dg": "Green Dragon", "soap": "Soap",
    "flower": "Flower", "joker": "Joker",
}

#: Definition order = display order; sort_rack and the renderers key off it.
#: Definition order = display order; sort_rack, the renderers, and the
#: assist engine's needed/dead-weight ordering all key off this one dict.
TILE_ORDER = {tile: i for i, tile in enumerate(Tile)}

#: Physical copies of each kind in the wall (§2.1): 152 total.
def copies(tile: Tile) -> int:
    return 8 if tile in (Tile.FLOWER, Tile.JOKER) else 4


DECK_SIZE = 152


#: Jokers in a standard wall. A guild may deal MORE (house dial
#: ``mahjong_wall_jokers``): jokers are the only tile that closes several
#: gaps at once, so their count is the sharpest lever on how often a hand
#: actually finishes — measured 2026-08-23, a 4-seat game goes from 52% to
#: 80% decisive between 8 and 14. Extra jokers ADD to the wall (the deck
#: grows past 152) rather than displacing naturals, which would make hands
#: harder in the same breath.
STANDARD_JOKERS = 8


def build_deck(*, jokers: int = STANDARD_JOKERS) -> list[Tile]:
    """The 152-tile deck in definition order (unshuffled), or a deck with
    ``jokers`` jokers instead of the standard eight."""
    return [
        tile
        for tile in Tile
        for _ in range(jokers if tile is Tile.JOKER else copies(tile))
    ]


def shuffled_wall(
    rng: random.Random | None = None, *, jokers: int = STANDARD_JOKERS
) -> list[Tile]:
    """A freshly shuffled wall. Shuffle is CSPRNG (§2.2) unless a test
    injects its own ``rng`` for determinism."""
    wall = build_deck(jokers=jokers)
    (rng or secrets.SystemRandom()).shuffle(wall)
    return wall


def sort_rack(tiles: list[Tile]) -> list[Tile]:
    """Sort a rack into display order (suits 1–9, winds, dragons, F, J)."""
    return sorted(tiles, key=lambda t: TILE_ORDER[t])
