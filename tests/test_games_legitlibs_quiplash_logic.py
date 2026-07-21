"""Tests for the extracted LegitLibs Quiplash pure-logic module.

Covers ``bot_modules/cogs/games_legitlibs/quiplash_logic.py``
(payload constructors, join/start mutators, submission storage and
queries, reveal-order shuffle). Mirrors the games_ttl / classic_logic
pattern: the mode file stays a thin orchestrator; these tests prove
the extracted pieces work without spinning up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.cogs.games_legitlibs.quiplash_logic import (
    add_player,
    build_initial_payload,
    claim_start,
    clamp_tier,
    collect_complete_submissions,
    get_prior_submission,
    shuffle_reveal_order,
    store_submission,
    submitted_count,
)


def _sample_template() -> dict:
    return {
        "template_id": "tpl_q",
        "title": "Q-Demo",
        "body": "Q with {a} and {b}",
        "blanks": [
            {"id": "a", "position": 1, "pos": "noun"},
            {"id": "b", "position": 2, "pos": "verb"},
        ],
        "player_min": 2,
    }


# ── clamp_tier re-export ─────────────────────────────────────────────


def test_clamp_tier_reexported_from_classic():
    assert clamp_tier(5, 3) == (3, True)
    assert clamp_tier(1, 4) == (1, False)


# ── build_initial_payload ────────────────────────────────────────────


def test_build_initial_payload_basics():
    payload = build_initial_payload(host_id=7, tier=3, template=_sample_template())
    assert payload["mode"] == "quiplash"
    assert payload["tier"] == 3
    assert payload["template_id"] == "tpl_q"
    assert payload["template"]["title"] == "Q-Demo"
    assert payload["players"] == [7]
    assert payload["host_id"] == 7
    assert payload["state"] == "joining"
    assert payload["submissions"] == {}


def test_build_initial_payload_blanks_carried_over():
    payload = build_initial_payload(1, 1, _sample_template())
    assert [b["id"] for b in payload["template"]["blanks"]] == ["a", "b"]


# ── add_player ───────────────────────────────────────────────────────


def test_add_player_appends_when_new():
    p = {"players": [1]}
    assert add_player(p, 2) is True
    assert p["players"] == [1, 2]


def test_add_player_noop_when_present():
    p = {"players": [5]}
    assert add_player(p, 5) is False
    assert p["players"] == [5]


def test_add_player_initializes_missing_list():
    p: dict = {}
    assert add_player(p, 7) is True
    assert p["players"] == [7]


# ── claim_start ──────────────────────────────────────────────────────


def test_claim_start_transitions_from_joining():
    p = {"state": "joining"}
    assert claim_start(p) is True
    assert p["state"] == "filling"


def test_claim_start_idempotent_for_other_states():
    p = {"state": "filling"}
    assert claim_start(p) is False
    assert p["state"] == "filling"


@pytest.mark.parametrize("state", ["revealing", "ended", None])
def test_claim_start_rejects_non_joining_states(state):
    p = {"state": state} if state is not None else {}
    assert claim_start(p) is False


# ── store_submission ─────────────────────────────────────────────────


def test_store_submission_writes_when_filling():
    p = {"state": "filling", "submissions": {}}
    assert store_submission(p, 42, {"a": "apple", "b": "boat"}, partial=False) is True
    assert p["submissions"] == {"42": {"fills": {"a": "apple", "b": "boat"}, "partial": False}}


def test_store_submission_uses_string_uid_key():
    """Submissions are JSON-roundtripped, so keys are strings — confirm
    even an int uid gets stringified."""
    p = {"state": "filling", "submissions": {}}
    store_submission(p, 100, {"a": "x"}, partial=True)
    assert "100" in p["submissions"]
    assert 100 not in p["submissions"]


def test_store_submission_overwrites_prior():
    p = {"state": "filling", "submissions": {"1": {"fills": {"a": "old"}, "partial": True}}}
    store_submission(p, 1, {"a": "new"}, partial=False)
    assert p["submissions"]["1"] == {"fills": {"a": "new"}, "partial": False}


def test_store_submission_rejects_wrong_state():
    p = {"state": "joining", "submissions": {}}
    assert store_submission(p, 1, {"a": "x"}, partial=False) is False
    assert p["submissions"] == {}


def test_store_submission_initializes_missing_dict():
    p: dict = {"state": "filling"}
    store_submission(p, 1, {"a": "x"}, partial=False)
    assert p["submissions"]["1"]["fills"] == {"a": "x"}


# ── submitted_count ──────────────────────────────────────────────────


def test_submitted_count_counts_complete_only():
    p = {
        "submissions": {
            "1": {"fills": {"a": "x"}, "partial": False},
            "2": {"fills": {"a": "y"}, "partial": True},
            "3": {"fills": {"a": "z"}, "partial": False},
        },
    }
    assert submitted_count(p, [1, 2, 3]) == 2


def test_submitted_count_ignores_partial_missing_flag_as_complete():
    """Missing 'partial' key defaults to False (i.e. complete)."""
    p = {"submissions": {"1": {"fills": {"a": "x"}}}}
    assert submitted_count(p, [1]) == 1


def test_submitted_count_ignores_players_with_no_submission():
    p = {"submissions": {"1": {"fills": {"a": "x"}, "partial": False}}}
    assert submitted_count(p, [1, 2, 3]) == 1


def test_submitted_count_empty_when_no_submissions():
    assert submitted_count({"submissions": {}}, [1, 2]) == 0
    assert submitted_count({}, [1, 2]) == 0


def test_submitted_count_ignores_extra_submissions_not_in_player_list():
    """Defensive: if somehow a non-player has a submission stored, the
    count must still reflect the player_ids passed in."""
    p = {
        "submissions": {
            "1": {"fills": {"a": "x"}, "partial": False},
            "999": {"fills": {"a": "y"}, "partial": False},
        },
    }
    assert submitted_count(p, [1, 2]) == 1


# ── get_prior_submission ─────────────────────────────────────────────


def test_get_prior_submission_returns_complete_flag():
    p = {"submissions": {"42": {"fills": {"a": "apple"}, "partial": False}}}
    prior, had_complete = get_prior_submission(p, 42)
    assert prior == {"a": "apple"}
    assert had_complete is True


def test_get_prior_submission_partial_returns_false_flag():
    p = {"submissions": {"42": {"fills": {"a": "apple"}, "partial": True}}}
    prior, had_complete = get_prior_submission(p, 42)
    assert prior == {"a": "apple"}
    assert had_complete is False


def test_get_prior_submission_no_prior_returns_empty():
    p = {"submissions": {}}
    prior, had_complete = get_prior_submission(p, 42)
    assert prior == {}
    assert had_complete is False


def test_get_prior_submission_handles_missing_submissions_key():
    prior, had_complete = get_prior_submission({}, 42)
    assert prior == {}
    assert had_complete is False


def test_get_prior_submission_handles_string_uid():
    p = {"submissions": {"42": {"fills": {"a": "x"}, "partial": False}}}
    prior, had_complete = get_prior_submission(p, "42")
    assert prior == {"a": "x"}
    assert had_complete is True


def test_get_prior_submission_handles_malformed_entry():
    """If somehow the stored entry isn't a dict (older payload?), return
    safe defaults rather than crash."""
    p = {"submissions": {"42": "garbage"}}
    prior, had_complete = get_prior_submission(p, 42)
    assert prior == {}
    assert had_complete is False


def test_get_prior_submission_handles_malformed_fills():
    p = {"submissions": {"42": {"fills": "not-a-dict", "partial": False}}}
    prior, had_complete = get_prior_submission(p, 42)
    assert prior == {}
    # had_complete is still True per the entry's partial=False
    assert had_complete is True


# ── collect_complete_submissions ─────────────────────────────────────


def test_collect_complete_submissions_filters_partial():
    submissions = {
        "1": {"fills": {"a": "x"}, "partial": False},
        "2": {"fills": {"a": "y"}, "partial": True},
        "3": {"fills": {"a": "z"}, "partial": False},
    }
    complete = collect_complete_submissions(submissions)
    assert set(complete.keys()) == {"1", "3"}


def test_collect_complete_submissions_empty():
    assert collect_complete_submissions({}) == {}


def test_collect_complete_submissions_treats_missing_partial_as_complete():
    submissions = {"1": {"fills": {"a": "x"}}}
    assert "1" in collect_complete_submissions(submissions)


def test_collect_complete_submissions_does_not_mutate_input():
    submissions = {
        "1": {"fills": {"a": "x"}, "partial": False},
        "2": {"fills": {"a": "y"}, "partial": True},
    }
    snapshot = dict(submissions)
    _ = collect_complete_submissions(submissions)
    assert submissions == snapshot


# ── shuffle_reveal_order ─────────────────────────────────────────────


def test_shuffle_reveal_order_preserves_all_uids():
    rng = random.Random(0)
    uids = ["1", "2", "3", "4"]
    out = shuffle_reveal_order(uids, rng=rng)
    assert sorted(out) == sorted(uids)


def test_shuffle_reveal_order_does_not_mutate_input():
    rng = random.Random(0)
    uids = ["1", "2", "3"]
    original = list(uids)
    _ = shuffle_reveal_order(uids, rng=rng)
    assert uids == original


def test_shuffle_reveal_order_deterministic_with_seeded_rng():
    rng1 = random.Random(42)
    rng2 = random.Random(42)
    a = shuffle_reveal_order(["a", "b", "c", "d"], rng=rng1)
    b = shuffle_reveal_order(["a", "b", "c", "d"], rng=rng2)
    assert a == b


def test_shuffle_reveal_order_uses_module_random_when_omitted():
    out = shuffle_reveal_order(["a"])
    assert out == ["a"]


def test_shuffle_reveal_order_empty_list():
    assert shuffle_reveal_order([]) == []


# ── rendering builders follow the guild accent ───────────────────────
#
# The Quiplash embeds (join / fill / reveal / no-submissions) now take a
# ``color`` the cog threads in from ``resolve_accent_color``. When accent
# resolution fails the cog passes ``None`` and each builder falls back to
# its original phase color, so a branding hiccup never crashes a game.

import discord  # noqa: E402

from bot_modules.cogs.games_legitlibs import rendering as ll_rendering  # noqa: E402
from bot_modules.games.constants import (  # noqa: E402
    PHASE_JOINING,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)


def test_tier_colors_gradient_removed():
    """The old gold→blue→orange→red heat gradient is gone entirely."""
    assert not hasattr(ll_rendering, "_TIER_COLORS")


def test_build_join_embed_uses_passed_accent():
    accent = discord.Color(0x123456)
    embed = ll_rendering.build_join_embed("Host", "T", 3, "quiplash", 1, 2, color=accent)
    assert embed.color == accent


def test_build_join_embed_falls_back_to_phase_color_without_accent():
    embed = ll_rendering.build_join_embed("Host", "T", 3, "quiplash", 1, 2)
    assert embed.color == discord.Color(PHASE_JOINING)


def test_build_fill_embed_uses_passed_accent():
    accent = discord.Color(0x0AABBC)
    embed = ll_rendering.build_fill_embed("Host", "T", 3, 2, 0, color=accent)
    assert embed.color == accent


def test_build_fill_embed_falls_back_without_accent():
    embed = ll_rendering.build_fill_embed("Host", "T", 3, 2, 0)
    assert embed.color == discord.Color(PHASE_PLAYING)


def test_build_reveal_embed_uses_passed_accent():
    accent = discord.Color(0xABCDEF)
    embed = ll_rendering.build_reveal_embed("T", 3, "body", 1, 1, color=accent)
    assert embed.color == accent


def test_build_reveal_embed_falls_back_without_accent():
    """No single 'funniest' winner is declared — the reveal is a neutral
    tally, so it follows the accent (results-green only as fallback)."""
    embed = ll_rendering.build_reveal_embed("T", 3, "body", 1, 1)
    assert embed.color == discord.Color(PHASE_RESULTS)


def test_build_no_submissions_embed_uses_passed_accent():
    accent = discord.Color(0x445566)
    embed = ll_rendering.build_no_submissions_embed("T", 3, color=accent)
    assert embed.color == accent


def test_build_no_submissions_embed_falls_back_without_accent():
    embed = ll_rendering.build_no_submissions_embed("T", 3)
    assert embed.color == discord.Color(PHASE_RECAP)
