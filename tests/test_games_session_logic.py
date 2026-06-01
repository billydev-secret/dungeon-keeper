"""Tests for the extracted Session Recap pure-logic modules.

Covers ``bot_modules/games_session/logic.py`` (duration formatting,
per-game highlight rules) and ``bot_modules/games_session/embeds.py``
(recap embed shape). Mirrors the traditional pattern: the cog file
stays thin; this module proves the extracted pieces work without
spinning up Discord or a guild.
"""

from __future__ import annotations

from bot_modules.games_session.embeds import build_session_recap_embed
from bot_modules.games_session.logic import (
    build_game_highlight,
    build_highlights,
    format_duration,
)


# ── format_duration ──────────────────────────────────────────────────


def test_format_duration_basic_minutes_only():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T12:15:00") == "15m"


def test_format_duration_with_hours_includes_both_components():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T14:15:00") == "2h 15m"


def test_format_duration_zero_when_same_timestamp():
    assert format_duration("2026-01-01T12:00:00", "2026-01-01T12:00:00") == "0m"


def test_format_duration_drops_hours_when_under_an_hour():
    """A 12m session shouldn't render as ``0h 12m``."""
    result = format_duration("2026-01-01T00:00:00", "2026-01-01T00:12:00")
    assert "h" not in result


def test_format_duration_unknown_on_invalid_iso():
    assert format_duration("not a date", "2026-01-01T12:00:00") == "unknown"


def test_format_duration_unknown_on_none_inputs():
    # type-ignore: deliberately wrong input to exercise the fallback
    assert format_duration(None, None) == "unknown"  # type: ignore[arg-type]


def test_format_duration_unknown_on_negative_span():
    """An end-before-start row is malformed — render ``"unknown"`` instead
    of a confusing negative duration."""
    assert format_duration("2026-01-01T12:30:00", "2026-01-01T12:00:00") == "unknown"


def test_format_duration_handles_multi_hour_session():
    assert format_duration("2026-01-01T10:00:00", "2026-01-01T14:30:00") == "4h 30m"


# ── build_game_highlight ─────────────────────────────────────────────


def test_build_game_highlight_unknown_type_uses_raw_key():
    out = build_game_highlight("unknown_game", {})
    # No icon configured for unknown_game — but the key still surfaces.
    assert "unknown_game" in out


def test_build_game_highlight_known_game_uses_friendly_name():
    out = build_game_highlight("wyr", {})
    assert "Would You Rather" in out


def test_build_game_highlight_wyr_picks_most_divisive():
    payload = {
        "rounds": {
            "1": {"q": "Coffee or tea?", "a": [1, 2, 3], "b": [4]},
            "2": {"q": "Cats or dogs?", "a": [1, 2], "b": [3, 4]},
            "3": {"q": "Pizza or burger?", "a": [1, 2, 3, 4], "b": []},
        }
    }
    out = build_game_highlight("wyr", payload)
    # Round 2 is the most divisive — equal split of 2/2
    assert "Cats or dogs?" in out
    assert "Most divisive" in out


def test_build_game_highlight_wyr_truncates_long_question():
    long_q = "x" * 100
    payload = {"rounds": {"1": {"q": long_q, "a": [1], "b": [2]}}}
    out = build_game_highlight("wyr", payload)
    # 50-char truncation
    assert "x" * 50 in out
    assert "x" * 51 not in out


def test_build_game_highlight_wyr_no_rounds_returns_bare_header():
    out = build_game_highlight("wyr", {})
    assert "Would You Rather" in out
    assert "Most divisive" not in out


def test_build_game_highlight_nhie_picks_guiltiest():
    payload = {"guilt_scores": {"100": 2, "200": 7, "300": 5}}
    name_lookup = {"100": "Alice", "200": "Bob", "300": "Carol"}
    out = build_game_highlight("nhie", payload, name_lookup)
    assert "Bob" in out
    assert "7 guilty" in out


def test_build_game_highlight_nhie_falls_back_to_raw_id_without_lookup():
    """No name_lookup means the raw id renders — never crashes."""
    payload = {"guilt_scores": {"200": 7}}
    out = build_game_highlight("nhie", payload)
    assert "200" in out


def test_build_game_highlight_nhie_empty_scores_returns_bare_header():
    out = build_game_highlight("nhie", {"guilt_scores": {}})
    assert "Never Have I Ever" in out
    assert "Guiltiest" not in out


def test_build_game_highlight_ttl_picks_best_liar():
    payload = {
        "scores": {
            "100": {"fooled": 2},
            "200": {"fooled": 5},
            "300": {"fooled": 1},
        }
    }
    out = build_game_highlight("ttl", payload, {"200": "Bob"})
    assert "Bob" in out
    assert "Best Liar" in out


def test_build_game_highlight_ttl_falls_back_to_raw_id_without_lookup():
    payload = {"scores": {"42": {"fooled": 5}}}
    out = build_game_highlight("ttl", payload)
    assert "42" in out
    assert "Best Liar" in out


def test_build_game_highlight_ttl_empty_scores_returns_bare_header():
    out = build_game_highlight("ttl", {"scores": {}})
    assert "Best Liar" not in out


def test_build_game_highlight_hottakes_picks_highest_avg():
    payload = {
        "results": [
            {"text": "lukewarm take", "avg": 2.1},
            {"text": "very hot take", "avg": 3.8},
            {"text": "cold take", "avg": 1.0},
        ]
    }
    out = build_game_highlight("hottakes", payload)
    assert "very hot take" in out
    assert "3.8/4" in out


def test_build_game_highlight_hottakes_truncates_long_text():
    payload = {"results": [{"text": "y" * 100, "avg": 4.0}]}
    out = build_game_highlight("hottakes", payload)
    assert "y" * 40 in out
    assert "y" * 41 not in out


def test_build_game_highlight_hottakes_empty_returns_bare_header():
    out = build_game_highlight("hottakes", {"results": []})
    assert "Hot Takes" in out
    assert "Hottest" not in out


def test_build_game_highlight_other_game_returns_bare_header():
    """A game type with no special highlight rule (e.g. ffa) still
    renders cleanly."""
    out = build_game_highlight("ffa", {"question": "Q?"})
    assert "Free For All" in out


# ── build_highlights ─────────────────────────────────────────────────


def test_build_highlights_runs_per_game():
    histories = [
        {"game_type": "wyr", "payload": {"rounds": {"1": {"q": "Q?", "a": [1], "b": [2]}}}},
        {"game_type": "ffa", "payload": {}},
    ]
    out = build_highlights(histories)
    assert len(out) == 2
    assert "Would You Rather" in out[0]
    assert "Free For All" in out[1]


def test_build_highlights_passes_name_lookup_through():
    histories = [{"game_type": "ttl", "payload": {"scores": {"7": {"fooled": 3}}}}]
    out = build_highlights(histories, {"7": "Zara"})
    assert "Zara" in out[0]


def test_build_highlights_empty_returns_empty_list():
    assert build_highlights([]) == []


# ── build_session_recap_embed ────────────────────────────────────────


def test_build_session_recap_embed_shows_core_stats():
    embed = build_session_recap_embed(
        game_count=3,
        player_ids=[1, 2, 3, 4],
        duration_str="1h 5m",
        highlights=[],
    )
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["🎮 Games Played"] == "3"
    assert by_name["👥 Unique Players"] == "4"
    assert by_name["⏱️ Total Duration"] == "1h 5m"


def test_build_session_recap_embed_omits_players_field_when_empty():
    embed = build_session_recap_embed(0, [], "0m", [])
    by_name = {f.name: f.value for f in embed.fields}
    assert "🏆 Players" not in by_name


def test_build_session_recap_embed_renders_mentions_for_players():
    embed = build_session_recap_embed(1, [42, 99], "5m", [])
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "<@42>" in by_name["🏆 Players"]
    assert "<@99>" in by_name["🏆 Players"]


def test_build_session_recap_embed_truncates_player_list_to_ten():
    ids = list(range(1, 20))  # 19 players
    embed = build_session_recap_embed(5, ids, "30m", [])
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "<@10>" in by_name["🏆 Players"]
    # The 11th and beyond shouldn't appear
    assert "<@11>" not in by_name["🏆 Players"]
    # But unique count still reflects the full list
    assert by_name["👥 Unique Players"] == "19"


def test_build_session_recap_embed_renders_highlights_block():
    embed = build_session_recap_embed(
        1, [1], "5m", ["**🤔 Would You Rather**: round 1"]
    )
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "Game Highlights" in by_name
    assert "round 1" in by_name["Game Highlights"]
    assert by_name["Game Highlights"].startswith("• ")


def test_build_session_recap_embed_omits_highlights_field_when_empty():
    embed = build_session_recap_embed(0, [1], "0m", [])
    by_name = {f.name: f.value or "" for f in embed.fields}
    assert "Game Highlights" not in by_name


def test_build_session_recap_embed_truncates_highlights_to_eight():
    highlights = [f"**game {i}**" for i in range(15)]
    embed = build_session_recap_embed(15, [1], "1h", highlights)
    by_name = {f.name: f.value or "" for f in embed.fields}
    # Eighth highlight should be present, ninth should not
    assert "game 7" in by_name["Game Highlights"]
    assert "game 8" not in by_name["Game Highlights"]


def test_build_session_recap_embed_has_footer_and_title():
    embed = build_session_recap_embed(0, [], "0m", [])
    assert embed.title is not None
    assert "SESSION RECAP" in embed.title
    assert embed.footer.text is not None
    assert "Session Recap" in embed.footer.text
