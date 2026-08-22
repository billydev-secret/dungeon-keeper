"""Tests for tile rendering — stage 5 of docs/plans/meadow-mahjong.md.

The chip fallback is the launch state (the id map ships empty), so chips are
tested exhaustively; emoji resolution is tested against an injected map. Art
checks skip when assets/ is absent — the remote gate runner syncs only src/,
tests/, scripts/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot_modules.games.mahjong import tile_render
from bot_modules.games.mahjong.tile_render import (
    BACK_CHIP,
    back_str,
    chip,
    load_emoji_map,
    rack_str,
    tile_str,
)
from bot_modules.games.mahjong.tiles import Tile


def test_every_tile_has_a_nonempty_chip():
    seen = set()
    for tile in Tile:
        c = chip(tile)
        assert c and len(c) <= 3
        seen.add(c)
    assert len(seen) == 36  # chips are unique per kind
    assert chip(Tile.BAM2) == "2B"
    assert chip(Tile.DOT6) == "6D"
    assert chip(Tile.JOKER) == "JKR"
    assert chip(Tile.FLOWER) == "🌸"
    assert chip(Tile.SOAP) == "▢"
    assert BACK_CHIP


def test_tile_str_prefers_emoji_and_falls_back():
    mapping = {"5b": 123456789012345678}
    assert tile_str(Tile.BAM5, emoji_map=mapping) == "<:mm_5b:123456789012345678>"
    assert tile_str(Tile.DOT1, emoji_map=mapping) == "1D"  # unmapped → chip
    assert back_str(emoji_map={"back": 42}) == "<:mm_back:42>"
    assert back_str(emoji_map={}) == BACK_CHIP


def test_rack_str_joins_in_order():
    mapping = {}
    assert rack_str([Tile.DOT1, Tile.JOKER], emoji_map=mapping) == "1D JKR"


def test_shipped_map_loads_and_is_id_shaped(monkeypatch, tmp_path):
    # the real file (empty at launch) parses
    shipped = load_emoji_map(refresh=True)
    assert isinstance(shipped, dict)
    # a corrupt file degrades to chips, never raises
    bad = tmp_path / "tile_emoji.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(tile_render, "EMOJI_MAP_PATH", bad)
    assert load_emoji_map(refresh=True) == {}
    missing = tmp_path / "gone.json"
    monkeypatch.setattr(tile_render, "EMOJI_MAP_PATH", missing)
    assert load_emoji_map(refresh=True) == {}
    # restore the module cache for other tests
    monkeypatch.setattr(tile_render, "EMOJI_MAP_PATH", tile_render.EMOJI_MAP_PATH)
    load_emoji_map(refresh=True)


ASSET_DIR = Path(__file__).resolve().parent.parent / "assets" / "tile_emoji"


def test_generated_art_covers_every_tile():
    if not ASSET_DIR.is_dir():
        pytest.skip("assets/ not present on this runner")
    names = {p.name for p in ASSET_DIR.glob("mm_*.png")}
    expected = {f"mm_{t.code}.png" for t in Tile} | {"mm_back.png"}
    assert expected <= names
    assert len(expected) == 37


def test_registration_script_names_match_the_renderer():
    script = Path(__file__).resolve().parent.parent / "scripts" / "register_tile_emoji.py"
    if not script.exists():
        pytest.skip("scripts/ not present")
    text = script.read_text(encoding="utf-8")
    # the script derives names from Tile codes + back — the same contract
    # tile_render resolves through; a drift here would strand the id map
    assert 'f"mm_{t.code}"' in text and '"mm_back"' in text
    assert "tile_emoji.json" in Path(
        Path(__file__).resolve().parent.parent
        / "src/bot_modules/games/mahjong/tile_render.py"
    ).read_text(encoding="utf-8")
