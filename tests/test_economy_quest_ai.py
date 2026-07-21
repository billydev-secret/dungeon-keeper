"""Tests for bot_modules/economy/quest_ai.py — pure prompt building and the
tolerant response parser.

The parser is the risk surface: a cloud model hands back JSON often enough to
parse but malformed often enough that a naive ``json.loads`` would strand the
manager with an empty results panel. These tests pin the happy path, every
degradation step (fenced JSON, leading prose, title-only salvage), and the
type-coercion of reward/target so a stringy or missing field never crashes.
"""

from __future__ import annotations

import json

import pytest

from bot_modules.economy.quest_ai import (
    DEFAULT_COUNT,
    MAX_COUNT,
    QuestIdea,
    build_system_prompt,
    build_user_prompt,
    parse_quest_ideas,
)


# ── prompt building ───────────────────────────────────────────────────


def test_system_prompt_names_currency():
    assert '"Gold"' in build_system_prompt("Gold")


def test_user_prompt_bakes_type_count_and_band():
    prompt = build_user_prompt("daily", 3)
    assert "3 distinct daily" in prompt
    assert "10–20" in prompt  # daily reward band steer
    assert "JSON array" in prompt


def test_user_prompt_community_requests_target_field():
    prompt = build_user_prompt("community", 2)
    assert "community_target" in prompt


def test_user_prompt_non_community_omits_target_field():
    assert "community_target" not in build_user_prompt("weekly", 2)


def test_user_prompt_theme_included_when_given():
    assert "voice chat" in build_user_prompt("daily", 2, theme="  voice chat  ")
    assert "Theme" not in build_user_prompt("daily", 2, theme="   ")


def test_user_prompt_rejects_unknown_type():
    with pytest.raises(ValueError):
        build_user_prompt("monthly", 2)


# ── parsing: happy path ───────────────────────────────────────────────


def test_parse_clean_json_array():
    text = json.dumps(
        [
            {"title": "Say hi", "description": "Greet someone", "criteria": "Post a greeting", "reward": 15},
            {"title": "React", "description": "Add a reaction", "criteria": "React once", "reward": 12},
        ]
    )
    ideas = parse_quest_ideas(text, "daily")
    assert [i.title for i in ideas] == ["Say hi", "React"]
    assert ideas[0].reward == 15
    assert ideas[0].community_target is None


def test_parse_respects_limit():
    text = json.dumps([{"title": f"q{n}", "reward": 10} for n in range(20)])
    assert len(parse_quest_ideas(text, "daily", limit=3)) == 3


def test_parse_community_keeps_target():
    text = json.dumps([{"title": "Big goal", "reward": 0, "community_target": 250}])
    ideas = parse_quest_ideas(text, "community")
    assert ideas[0].community_target == 250


def test_parse_ignores_target_for_non_community():
    text = json.dumps([{"title": "Weekly", "reward": 40, "community_target": 999}])
    assert parse_quest_ideas(text, "weekly")[0].community_target is None


# ── parsing: degradation ──────────────────────────────────────────────


def test_parse_strips_markdown_fence():
    text = '```json\n[{"title": "Fenced", "reward": 11}]\n```'
    assert parse_quest_ideas(text, "daily")[0].title == "Fenced"


def test_parse_extracts_array_from_leading_prose():
    text = 'Here are your ideas!\n[{"title": "Buried", "reward": 13}]\nHope these help.'
    assert parse_quest_ideas(text, "daily")[0].title == "Buried"


def test_parse_skips_non_dict_and_titleless_elements():
    text = json.dumps(["junk", {"reward": 10}, {"title": "Keeper", "reward": 10}])
    ideas = parse_quest_ideas(text, "daily")
    assert [i.title for i in ideas] == ["Keeper"]


def test_parse_title_only_fallback_on_broken_json():
    text = "Do a daily check-in\nHost a movie night\nStart a thread"
    ideas = parse_quest_ideas(text, "weekly")
    assert [i.title for i in ideas] == [
        "Do a daily check-in",
        "Host a movie night",
        "Start a thread",
    ]
    # Fallback rewards default to the band low (weekly → 25).
    assert all(i.reward == 25 for i in ideas)


def test_parse_title_only_strips_list_markers():
    text = "1. First quest\n- Second quest\n* Third quest"
    titles = [i.title for i in parse_quest_ideas(text, "daily")]
    assert titles == ["First quest", "Second quest", "Third quest"]


def test_parse_empty_text_returns_empty():
    assert parse_quest_ideas("", "daily") == []
    assert parse_quest_ideas("   \n  ", "daily") == []


# ── coercion ──────────────────────────────────────────────────────────


def test_reward_coerces_stringy_and_defaults_to_band_low():
    text = json.dumps(
        [
            {"title": "Stringy", "reward": "17"},
            {"title": "Missing"},
            {"title": "Negative", "reward": -5},
        ]
    )
    ideas = parse_quest_ideas(text, "daily")
    assert ideas[0].reward == 17
    assert ideas[1].reward == 10  # daily band low
    assert ideas[2].reward == 0   # negatives clamped


def test_idea_as_dict_roundtrips_fields():
    idea = QuestIdea("t", "d", "c", 5, community_target=7)
    assert idea.as_dict() == {
        "title": "t",
        "description": "d",
        "criteria": "c",
        "reward": 5,
        "community_target": 7,
    }


def test_module_count_constants_sane():
    assert 1 <= DEFAULT_COUNT <= MAX_COUNT
