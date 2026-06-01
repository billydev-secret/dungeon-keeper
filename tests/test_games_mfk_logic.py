"""Tests for the extracted Marry/Fornicate/Kiss pure-logic modules.

Covers ``bot_modules/games_mfk/logic.py`` (participant toggle, label
parsing, target assignment, payload serialisation) and
``bot_modules/games_mfk/embeds.py`` (lobby and assignments embeds, slot
formatter). Mirrors the games_traditional template: the cog stays thin;
this module proves the extracted pieces work without spinning up
Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_mfk.embeds import (
    build_assignments_embed,
    build_lobby_embed,
    format_assignment_value,
)
from bot_modules.games_mfk.logic import (
    DEFAULT_LABELS,
    MIN_PARTICIPANTS,
    TARGETS_PER_PLAYER,
    assign_targets,
    parse_labels,
    serialize_assignments,
    toggle_participant,
)


# ── toggle_participant ───────────────────────────────────────────────


def test_toggle_participant_adds_new_user_and_returns_joined():
    payload: dict = {}
    action = toggle_participant(payload, user_id=42)
    assert action == "joined"
    assert payload["participants"] == [42]


def test_toggle_participant_removes_existing_user_and_returns_left():
    payload = {"participants": [42]}
    action = toggle_participant(payload, user_id=42)
    assert action == "left"
    assert payload["participants"] == []


def test_toggle_participant_creates_list_when_missing():
    payload: dict = {}
    toggle_participant(payload, 1)
    assert "participants" in payload


def test_toggle_participant_preserves_other_users():
    payload = {"participants": [1, 2, 3]}
    toggle_participant(payload, 2)
    assert payload["participants"] == [1, 3]


def test_toggle_participant_appends_to_existing_list():
    payload = {"participants": [1]}
    action = toggle_participant(payload, 2)
    assert action == "joined"
    assert payload["participants"] == [1, 2]


# ── parse_labels ─────────────────────────────────────────────────────


def test_parse_labels_none_input_returns_none_labels_no_error():
    """Empty input means caller falls back to DEFAULT_LABELS."""
    labels, error = parse_labels(None)
    assert labels is None
    assert error is None


def test_parse_labels_empty_string_returns_none_no_error():
    labels, error = parse_labels("")
    assert labels is None
    assert error is None


def test_parse_labels_valid_three_labels():
    labels, error = parse_labels("Cruise, Wedding, Vacation")
    assert error is None
    assert labels == ["Cruise", "Wedding", "Vacation"]


def test_parse_labels_strips_whitespace_around_each_entry():
    labels, error = parse_labels("  A  ,  B  ,  C  ")
    assert error is None
    assert labels == ["A", "B", "C"]


def test_parse_labels_ignores_empty_segments():
    """Trailing commas / double-commas shouldn't count toward the total."""
    labels, error = parse_labels("A,,B,C")
    assert error is None
    assert labels == ["A", "B", "C"]


def test_parse_labels_too_few_returns_error():
    labels, error = parse_labels("A, B")
    assert labels is None
    assert error is not None
    assert "got 2" in error


def test_parse_labels_too_many_returns_error():
    labels, error = parse_labels("A, B, C, D")
    assert labels is None
    assert error is not None
    assert "got 4" in error


def test_parse_labels_error_message_mentions_example():
    _, error = parse_labels("only one")
    assert error is not None
    assert "Cruise" in error  # Example in the message


# ── assign_targets ───────────────────────────────────────────────────


def test_assign_targets_below_min_raises():
    with pytest.raises(ValueError):
        assign_targets([1, 2, 3])  # MIN is 4


def test_assign_targets_exactly_min_works():
    rng = random.Random(0)
    out = assign_targets([1, 2, 3, 4], rng=rng)
    assert set(out.keys()) == {1, 2, 3, 4}
    for player_id, targets in out.items():
        assert len(targets) == TARGETS_PER_PLAYER
        assert player_id not in targets


def test_assign_targets_no_self_targeting_in_larger_pool():
    rng = random.Random(0)
    pool = list(range(1, 11))
    out = assign_targets(pool, rng=rng)
    for player_id, targets in out.items():
        assert player_id not in targets


def test_assign_targets_three_distinct_targets_per_player():
    rng = random.Random(7)
    pool = list(range(1, 8))
    out = assign_targets(pool, rng=rng)
    for targets in out.values():
        assert len(set(targets)) == TARGETS_PER_PLAYER


def test_assign_targets_uses_module_random_when_rng_omitted():
    """No rng path: still returns a valid assignment for every player."""
    random.seed(0)
    out = assign_targets([1, 2, 3, 4, 5])
    assert set(out.keys()) == {1, 2, 3, 4, 5}
    for player_id, targets in out.items():
        assert player_id not in targets
        assert len(targets) == 3


def test_assign_targets_deterministic_with_seeded_rng():
    """Same seed → same assignment shape."""
    pool = [1, 2, 3, 4, 5]
    out1 = assign_targets(pool, rng=random.Random(42))
    out2 = assign_targets(pool, rng=random.Random(42))
    assert out1 == out2


# ── serialize_assignments ────────────────────────────────────────────


def test_serialize_assignments_stringifies_player_keys():
    assert serialize_assignments({1: [2, 3, 4]}) == {"1": [2, 3, 4]}


def test_serialize_assignments_preserves_target_lists():
    """Target ids stay as ints in the serialized output."""
    result = serialize_assignments({100: [200, 300, 400]})
    assert result["100"] == [200, 300, 400]
    for val in result["100"]:
        assert isinstance(val, int)


def test_serialize_assignments_empty():
    assert serialize_assignments({}) == {}


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_default_labels_in_title():
    embed = build_lobby_embed("Alice", [])
    assert embed.title is not None
    # Title is the upper-case version of "Marry, Fornicate, Kiss"
    assert "MARRY" in embed.title
    assert "FORNICATE" in embed.title
    assert "KISS" in embed.title


def test_build_lobby_embed_custom_labels_in_title():
    embed = build_lobby_embed("Alice", [], labels=["Cruise", "Wedding", "Vacation"])
    assert embed.title is not None
    assert "CRUISE" in embed.title
    assert "WEDDING" in embed.title
    assert "VACATION" in embed.title


def test_build_lobby_embed_empty_pool_shows_dash():
    embed = build_lobby_embed("Alice", [])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"
    assert by_name["Pool (0)"] == "—"


def test_build_lobby_embed_lists_participants():
    embed = build_lobby_embed("Alice", ["Bob", "Carol", "Dan"])
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Pool (3)"] == "Bob, Carol, Dan"


def test_build_lobby_embed_categories_field_bolds_each_label():
    embed = build_lobby_embed("Alice", [], labels=["A", "B", "C"])
    by_name = {f.name: f.value for f in embed.fields}
    cats = by_name["Categories"] or ""
    assert "**A**" in cats
    assert "**B**" in cats
    assert "**C**" in cats


def test_build_lobby_embed_footer_includes_label_string():
    embed = build_lobby_embed("Alice", [], labels=["A", "B", "C"])
    assert embed.footer.text is not None
    assert "A, B, C" in embed.footer.text


# ── format_assignment_value ──────────────────────────────────────────


def test_format_assignment_value_bolds_each_name():
    result = format_assignment_value(["X", "Y", "Z"])
    assert "**X**" in result
    assert "**Y**" in result
    assert "**Z**" in result


def test_format_assignment_value_separates_with_middle_dot():
    result = format_assignment_value(["X", "Y", "Z"])
    assert " · " in result


# ── build_assignments_embed ──────────────────────────────────────────


def test_build_assignments_embed_one_field_per_player():
    assignments = [
        ("<@1>", ["Bob", "Carol", "Dan"]),
        ("<@2>", ["Alice", "Carol", "Dan"]),
    ]
    embed = build_assignments_embed(assignments)
    assert len(embed.fields) == 2


def test_build_assignments_embed_title_mentions_three_names():
    embed = build_assignments_embed([], labels=DEFAULT_LABELS)
    assert embed.title is not None
    assert "YOUR THREE NAMES" in embed.title


def test_build_assignments_embed_description_lists_categories():
    """The CTA in the description mentions the actual categories."""
    embed = build_assignments_embed([], labels=["Cruise", "Wedding", "Vacation"])
    assert embed.description is not None
    assert "Cruise" in embed.description
    assert "Wedding" in embed.description
    assert "Vacation" in embed.description


def test_build_assignments_embed_custom_labels_in_title():
    embed = build_assignments_embed([], labels=["A", "B", "C"])
    assert embed.title is not None
    assert "A, B, C" in embed.title


def test_build_assignments_embed_field_value_includes_bold_names():
    embed = build_assignments_embed([("<@1>", ["Bob", "Carol", "Dan"])])
    field = embed.fields[0]
    assert field.value is not None
    assert "**Bob**" in field.value
    assert "**Carol**" in field.value
    assert "**Dan**" in field.value


def test_build_assignments_embed_field_name_is_player_mention():
    embed = build_assignments_embed([("<@99>", ["X", "Y", "Z"])])
    field = embed.fields[0]
    assert field.name == "<@99>"


def test_build_assignments_embed_has_footer():
    embed = build_assignments_embed([])
    assert embed.footer.text is not None


# ── DEFAULT_LABELS / constants sanity ────────────────────────────────


def test_default_labels_has_three_entries():
    assert len(DEFAULT_LABELS) == TARGETS_PER_PLAYER


def test_min_participants_constant():
    assert MIN_PARTICIPANTS == 4


# ── integration ──────────────────────────────────────────────────────


def test_full_lobby_flow_four_players():
    """Four players join, then assignments are produced."""
    payload: dict = {}
    for uid in [1, 2, 3, 4]:
        toggle_participant(payload, uid)
    assert payload["participants"] == [1, 2, 3, 4]
    out = assign_targets(payload["participants"], rng=random.Random(0))
    for uid, targets in out.items():
        assert uid not in targets
        assert len(targets) == 3
    # Serialize for end-game payload
    serialized = serialize_assignments(out)
    assert set(serialized.keys()) == {"1", "2", "3", "4"}
