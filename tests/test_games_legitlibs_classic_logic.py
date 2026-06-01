"""Tests for the extracted LegitLibs Classic pure-logic module.

Covers ``bot_modules/cogs/games_legitlibs/classic_logic.py`` (payload
constructors, join/start/fill/rescue mutators, rescue helpers, modal
pre-fill helpers, tier-cap clamp). Follows the games_ttl / games_clapback
template: the mode file stays a thin orchestrator; these tests prove the
extracted pieces work without spinning up Discord or DB.
"""

from __future__ import annotations

import pytest

from bot_modules.cogs.games_legitlibs.classic_logic import (
    add_player,
    add_volunteer,
    build_initial_payload,
    claim_start,
    clamp_tier,
    existing_fill_values,
    filter_rescuers,
    freeze_rescue,
    init_rescue,
    my_blank_ids,
    remove_player,
    rescuers_done_count,
    set_rescue_fill_state,
    store_round1_fills,
    store_rescue_fills,
)


# ── Sample template ──────────────────────────────────────────────────


def _sample_template() -> dict:
    return {
        "template_id": "tpl_demo",
        "title": "Demo",
        "body": "Body with {a} and {b}",
        "blanks": [
            {"id": "a", "position": 1, "pos": "noun"},
            {"id": "b", "position": 2, "pos": "verb"},
            {"id": "c", "position": 3, "pos": "adj"},
        ],
        "player_min": 2,
    }


# ── clamp_tier ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requested,max_tier,expected,clamped",
    [
        (1, 4, 1, False),
        (3, 4, 3, False),
        (4, 4, 4, False),
        (5, 4, 4, True),
        (4, 2, 2, True),
        (1, 1, 1, False),
    ],
)
def test_clamp_tier(requested, max_tier, expected, clamped):
    assert clamp_tier(requested, max_tier) == (expected, clamped)


# ── build_initial_payload ────────────────────────────────────────────


def test_build_initial_payload_seeds_host_and_state():
    payload = build_initial_payload(host_id=42, tier=2, template=_sample_template())
    assert payload["mode"] == "classic"
    assert payload["tier"] == 2
    assert payload["template_id"] == "tpl_demo"
    assert payload["template"]["title"] == "Demo"
    assert payload["template"]["body"].startswith("Body with")
    assert payload["template"]["blanks"][0]["id"] == "a"
    assert payload["players"] == [42]
    assert payload["host_id"] == 42
    assert payload["state"] == "joining"
    assert payload["assignments"] == {}
    assert payload["fills"] == {}


def test_build_initial_payload_blanks_copied_from_template():
    tpl = _sample_template()
    payload = build_initial_payload(1, 1, tpl)
    # Same blank dicts (no deep copy assumption) — just confirm the list
    # came from the template.
    assert [b["id"] for b in payload["template"]["blanks"]] == ["a", "b", "c"]


# ── add_player / remove_player ───────────────────────────────────────


def test_add_player_appends_when_new():
    p = {"players": [1]}
    assert add_player(p, 2) is True
    assert p["players"] == [1, 2]


def test_add_player_noop_when_present():
    p = {"players": [1, 2]}
    assert add_player(p, 1) is False
    assert p["players"] == [1, 2]


def test_add_player_initializes_missing_list():
    p: dict = {}
    assert add_player(p, 7) is True
    assert p["players"] == [7]


def test_remove_player_removes_when_present():
    p = {"players": [1, 2, 3]}
    assert remove_player(p, 2) is True
    assert p["players"] == [1, 3]


def test_remove_player_noop_when_missing():
    p = {"players": [1, 2]}
    assert remove_player(p, 9) is False
    assert p["players"] == [1, 2]


def test_remove_player_safe_on_empty_payload():
    p: dict = {}
    assert remove_player(p, 1) is False


# ── claim_start ──────────────────────────────────────────────────────


def test_claim_start_transitions_when_joining():
    p = {"state": "joining", "players": [1, 2]}
    assigns = {"a": 1, "b": 2}
    assert claim_start(p, assigns) is True
    assert p["state"] == "filling"
    assert p["assignments"] == {"a": 1, "b": 2}


def test_claim_start_idempotent_when_already_filling():
    """Two callers racing to start — only the first transitions."""
    p = {"state": "filling", "players": [1, 2], "assignments": {"a": 1}}
    assert claim_start(p, {"a": 2}) is False
    # Existing assignments untouched
    assert p["assignments"] == {"a": 1}


def test_claim_start_rejects_other_states():
    p = {"state": "rescuing_claim"}
    assert claim_start(p, {}) is False


# ── store_round1_fills ───────────────────────────────────────────────


def test_store_round1_fills_writes_when_filling():
    p = {"state": "filling", "fills": {}}
    assert store_round1_fills(p, {"a": "apple"}, by_uid=42) is True
    assert p["fills"] == {"a": {"value": "apple", "by": 42}}


def test_store_round1_fills_merges_with_existing():
    p = {
        "state": "filling",
        "fills": {"a": {"value": "old", "by": 1}},
    }
    store_round1_fills(p, {"b": "new"}, by_uid=2)
    assert p["fills"] == {
        "a": {"value": "old", "by": 1},
        "b": {"value": "new", "by": 2},
    }


def test_store_round1_fills_overwrites_same_blank():
    p = {
        "state": "filling",
        "fills": {"a": {"value": "old", "by": 1}},
    }
    store_round1_fills(p, {"a": "new"}, by_uid=2)
    assert p["fills"]["a"] == {"value": "new", "by": 2}


def test_store_round1_fills_rejects_wrong_state():
    p = {"state": "joining", "fills": {}}
    assert store_round1_fills(p, {"a": "x"}, by_uid=1) is False
    assert p["fills"] == {}


def test_store_round1_fills_initializes_missing_fills():
    p = {"state": "filling"}
    assert store_round1_fills(p, {"a": "x"}, by_uid=1) is True
    assert p["fills"] == {"a": {"value": "x", "by": 1}}


# ── init_rescue / add_volunteer / freeze_rescue ──────────────────────


def test_init_rescue_seeds_rescue_block():
    p = {"state": "filling"}
    init_rescue(p, claim_deadline_ts=1234567890)
    assert p["state"] == "rescuing_claim"
    assert p["rescue"] == {
        "volunteers": [],
        "assignments": {},
        "claim_deadline": 1234567890,
        "fill_deadline": 0,
    }


def test_add_volunteer_added_when_open_and_player():
    p = {"state": "rescuing_claim", "players": [1, 2], "rescue": {"volunteers": []}}
    assert add_volunteer(p, 1) == "added"
    assert p["rescue"]["volunteers"] == [1]


def test_add_volunteer_closed_when_state_changed():
    p = {"state": "rescuing_fill", "players": [1], "rescue": {"volunteers": []}}
    assert add_volunteer(p, 1) == "closed"


def test_add_volunteer_not_player_when_not_in_round():
    p = {"state": "rescuing_claim", "players": [1], "rescue": {"volunteers": []}}
    assert add_volunteer(p, 99) == "not_player"
    assert p["rescue"]["volunteers"] == []


def test_add_volunteer_already_when_double_click():
    p = {"state": "rescuing_claim", "players": [1, 2], "rescue": {"volunteers": [1]}}
    assert add_volunteer(p, 1) == "already"


def test_add_volunteer_initializes_missing_rescue_block():
    """Defensive: the cog always inits rescue before adding, but the
    helper should not crash if rescue is missing."""
    p = {"state": "rescuing_claim", "players": [1]}
    assert add_volunteer(p, 1) == "added"
    assert p["rescue"]["volunteers"] == [1]


def test_freeze_rescue_writes_assignments_and_deadline():
    p = {"rescue": {"volunteers": [1, 2], "assignments": {}, "fill_deadline": 0}}
    freeze_rescue(p, {"a": 1, "b": 2}, fill_deadline_ts=999)
    assert p["rescue"]["assignments"] == {"a": 1, "b": 2}
    assert p["rescue"]["fill_deadline"] == 999
    # Volunteers preserved
    assert p["rescue"]["volunteers"] == [1, 2]


def test_freeze_rescue_creates_rescue_block_if_missing():
    p: dict = {}
    freeze_rescue(p, {"a": 1}, fill_deadline_ts=5)
    assert p["rescue"]["assignments"] == {"a": 1}


# ── set_rescue_fill_state + store_rescue_fills ───────────────────────


def test_set_rescue_fill_state_flips_state():
    p = {"state": "rescuing_claim"}
    set_rescue_fill_state(p)
    assert p["state"] == "rescuing_fill"


def test_store_rescue_fills_writes_when_rescuing_fill():
    p = {"state": "rescuing_fill", "fills": {"a": {"value": "x", "by": 1}}}
    assert store_rescue_fills(p, {"b": "y"}, by_uid=2) is True
    assert p["fills"]["b"] == {"value": "y", "by": 2}
    # Round-1 fill untouched
    assert p["fills"]["a"] == {"value": "x", "by": 1}


def test_store_rescue_fills_rejects_other_states():
    p = {"state": "filling", "fills": {}}
    assert store_rescue_fills(p, {"b": "y"}, by_uid=2) is False
    assert p["fills"] == {}


def test_store_rescue_fills_initializes_missing_fills():
    p = {"state": "rescuing_fill"}
    assert store_rescue_fills(p, {"b": "y"}, by_uid=2) is True
    assert p["fills"] == {"b": {"value": "y", "by": 2}}


# ── rescuers_done_count ──────────────────────────────────────────────


def test_rescuers_done_count_all_done():
    assignments = {"a": 1, "b": 2}
    fills = {"a": {"value": "x", "by": 1}, "b": {"value": "y", "by": 2}}
    assert rescuers_done_count(assignments, fills, [1, 2]) == 2


def test_rescuers_done_count_partial():
    assignments = {"a": 1, "b": 1, "c": 2}
    fills = {"a": {"value": "x", "by": 1}}  # b missing — rescuer 1 not done
    # rescuer 2 has c, also missing
    assert rescuers_done_count(assignments, fills, [1, 2]) == 0


def test_rescuers_done_count_one_done_one_not():
    assignments = {"a": 1, "b": 2}
    fills = {"a": {"value": "x", "by": 1}}
    assert rescuers_done_count(assignments, fills, [1, 2]) == 1


def test_rescuers_done_count_excludes_zero_assignment_rescuers():
    """Unlike round-1 players_done_count, rescue contributors with no
    assignments do NOT count as done — the helper only ever sees real
    rescuers post-filter."""
    assignments = {"a": 1}
    fills = {"a": {"value": "x", "by": 1}}
    # rescuer 2 has nothing assigned — shouldn't be tallied as done
    assert rescuers_done_count(assignments, fills, [1, 2]) == 1


# ── filter_rescuers ──────────────────────────────────────────────────


def test_filter_rescuers_keeps_only_assigned():
    assignments = {"a": 1, "b": 2}
    vols = [1, 2, 3]
    assert filter_rescuers(assignments, vols) == [1, 2]


def test_filter_rescuers_preserves_volunteer_order():
    assignments = {"a": 3, "b": 1}
    vols = [1, 2, 3]
    # 2 not assigned, dropped; 1 and 3 kept in original order
    assert filter_rescuers(assignments, vols) == [1, 3]


def test_filter_rescuers_empty_when_no_assignments():
    assert filter_rescuers({}, [1, 2]) == []


# ── my_blank_ids ─────────────────────────────────────────────────────


def test_my_blank_ids_returns_only_mine():
    assigns = {"a": 1, "b": 2, "c": 1, "d": 3}
    assert my_blank_ids(assigns, 1) == ["a", "c"]
    assert my_blank_ids(assigns, 2) == ["b"]
    assert my_blank_ids(assigns, 3) == ["d"]


def test_my_blank_ids_empty_for_unassigned():
    assigns = {"a": 1, "b": 2}
    assert my_blank_ids(assigns, 99) == []


def test_my_blank_ids_handles_empty_assignments():
    assert my_blank_ids({}, 1) == []


# ── existing_fill_values ─────────────────────────────────────────────


def test_existing_fill_values_returns_prior_for_assigned_blanks():
    blanks = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    fills = {
        "a": {"value": "apple", "by": 1},
        "c": {"value": "cat", "by": 2},
    }
    # Assigned ids include one filled (a) and one unfilled (b)
    out = existing_fill_values(blanks, fills, ["a", "b"])
    assert out == {"a": "apple"}


def test_existing_fill_values_empty_when_no_prior():
    blanks = [{"id": "a"}]
    assert existing_fill_values(blanks, {}, ["a"]) == {}


def test_existing_fill_values_ignores_unassigned_ids():
    blanks = [{"id": "a"}, {"id": "b"}]
    fills = {"a": {"value": "x", "by": 1}, "b": {"value": "y", "by": 2}}
    # caller's blank_ids only includes "a" — "b" prior must not leak
    out = existing_fill_values(blanks, fills, ["a"])
    assert out == {"a": "x"}
