"""Tile → text rendering: custom emoji with an always-on text-chip fallback.

Stage 5 of docs/plans/meadow-mahjong.md (spec §7). The id map
(``tile_emoji.json``, next to this module) is written by
``scripts/register_tile_emoji.py`` after the one-command prod registration —
it ships **empty**, so chips are the launch state and racks can never render
blank (§7.2). Every renderer goes through :func:`tile_str`; nothing else in
the cog may format a tile.

Chips are two-to-three characters, monospace-friendly: ``2B`` ``6D`` ``9C``,
winds ``N/E/W/S``, dragons ``R/G`` and ``▢`` (the soap frame), ``🌸`` flower,
``JKR`` joker, ``▨`` face-down. Embeds that want alignment wrap them in
code spans themselves.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from bot_modules.games.mahjong.tiles import Tile

log = logging.getLogger(__name__)

EMOJI_MAP_PATH = Path(__file__).parent / "tile_emoji.json"

_CHIPS = {
    "wn": "N", "we": "E", "ww": "W", "ws": "S",
    "dr": "R", "dg": "G", "soap": "▢",
    "flower": "🌸", "joker": "JKR",
}
BACK_CHIP = "▨"

_map_cache: dict[str, int] | None = None


def load_emoji_map(*, refresh: bool = False) -> dict[str, int]:
    """The registered emoji ids by tile code (plus ``back``); {} until the
    prod registration step has run. Cached; ``refresh=True`` re-reads."""
    global _map_cache
    if _map_cache is None or refresh:
        try:
            raw = json.loads(EMOJI_MAP_PATH.read_text(encoding="utf-8"))
            _map_cache = {str(k): int(v) for k, v in raw.items()}
        except FileNotFoundError:
            _map_cache = {}
        except (ValueError, OSError) as e:
            log.warning("tile_emoji.json unreadable (%s) — text chips only", e)
            _map_cache = {}
    return _map_cache


def chip(tile: Tile) -> str:
    """The text-chip fallback for one tile — never fails."""
    if tile.is_suited:
        return f"{tile.rank}{(tile.suit or '').upper()}"
    return _CHIPS[tile.code]


def tile_str(tile: Tile, *, emoji_map: dict[str, int] | None = None) -> str:
    """Custom-emoji markup when the id resolves, else the chip (§7.2)."""
    mapping = load_emoji_map() if emoji_map is None else emoji_map
    emoji_id = mapping.get(tile.code)
    if emoji_id:
        return f"<:mm_{tile.code}:{emoji_id}>"
    return chip(tile)


def back_str(*, emoji_map: dict[str, int] | None = None) -> str:
    """The face-down tile (opponents' racks, wall art)."""
    mapping = load_emoji_map() if emoji_map is None else emoji_map
    emoji_id = mapping.get("back")
    if emoji_id:
        return f"<:mm_back:{emoji_id}>"
    return BACK_CHIP


def rack_str(tiles: list[Tile], *, emoji_map: dict[str, int] | None = None) -> str:
    """A whole rack, display-sorted upstream; single space between tiles."""
    return " ".join(tile_str(t, emoji_map=emoji_map) for t in tiles)
