"""Tests for the extracted Fantasies & Dealbreakers pure-logic modules.

Covers ``bot_modules/games_fantasies/logic.py`` (normalize_category,
add_entry, apply_vote, tally_entry_votes, build_result_entry,
compute_recap_summary, get_round_entries) and
``bot_modules/games_fantasies/embeds.py`` (lobby, round-submit, vote,
recap embed builders). Mirrors the pressure_cooker / games_hottakes
pattern: the cog file stays thin; this module proves the extracted
pieces work without spinning up Discord.
"""

from __future__ import annotations

import pytest

from bot_modules.games_fantasies.embeds import (
    build_lobby_embed,
    build_recap_embed,
    build_round_submit_embed,
    build_vote_embed,
)
from bot_modules.games_fantasies.logic import (
    CATEGORY_DEALBREAKER,
    CATEGORY_FANTASY,
    add_entry,
    apply_vote,
    build_result_entry,
    compute_recap_summary,
    get_round_entries,
    normalize_category,
    tally_entry_votes,
)


# ── normalize_category ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["Fantasy", "fantasy", "FANTASY", "f", "Fan", "  fantasy  ", "Floral dream"],
)
def test_normalize_category_fantasy_variants(raw):
    assert normalize_category(raw) == CATEGORY_FANTASY


@pytest.mark.parametrize(
    "raw",
    ["Dealbreaker", "dealbreaker", "D", "deal", "  DEAL  ", "Disgusting"],
)
def test_normalize_category_dealbreaker_variants(raw):
    assert normalize_category(raw) == CATEGORY_DEALBREAKER


@pytest.mark.parametrize("raw", ["", "   ", "neither", "xyz", "123", "?"])
def test_normalize_category_unknown_returns_none(raw):
    assert normalize_category(raw) is None


def test_normalize_category_canonical_values():
    # Pin the canonical strings — the cog uses these in audit-log labels.
    assert CATEGORY_FANTASY == "Fantasy"
    assert CATEGORY_DEALBREAKER == "Dealbreaker"


# ── add_entry ────────────────────────────────────────────────────────


def test_add_entry_initializes_rounds_and_entries():
    payload: dict = {}
    add_entry(
        payload,
        round_num=1,
        user_id=42,
        text="Sunsets on the beach",
        category=CATEGORY_FANTASY,
    )
    assert payload["rounds"]["1"]["entries"] == [
        {
            "user_id": 42,
            "text": "Sunsets on the beach",
            "category": CATEGORY_FANTASY,
        }
    ]


def test_add_entry_appends_within_same_round():
    payload: dict = {}
    add_entry(payload, round_num=1, user_id=1, text="a", category="Fantasy")
    add_entry(payload, round_num=1, user_id=2, text="b", category="Dealbreaker")
    entries = payload["rounds"]["1"]["entries"]
    assert len(entries) == 2
    assert [e["text"] for e in entries] == ["a", "b"]
    assert [e["category"] for e in entries] == ["Fantasy", "Dealbreaker"]


def test_add_entry_separate_round_keys_dont_collide():
    payload: dict = {}
    add_entry(payload, round_num=1, user_id=1, text="r1", category="Fantasy")
    add_entry(payload, round_num=2, user_id=1, text="r2", category="Fantasy")
    assert payload["rounds"]["1"]["entries"][0]["text"] == "r1"
    assert payload["rounds"]["2"]["entries"][0]["text"] == "r2"


def test_add_entry_preserves_existing_round_metadata():
    payload = {"rounds": {"1": {"entries": [], "extra": "preserve me"}}}
    add_entry(payload, round_num=1, user_id=1, text="a", category="Fantasy")
    assert payload["rounds"]["1"]["extra"] == "preserve me"
    assert len(payload["rounds"]["1"]["entries"]) == 1


def test_add_entry_round_key_is_stringified():
    """Round keys are str so payload is JSON-friendly."""
    payload: dict = {}
    add_entry(payload, round_num=7, user_id=1, text="t", category="Fantasy")
    assert "7" in payload["rounds"]
    assert 7 not in payload["rounds"]


# ── apply_vote ───────────────────────────────────────────────────────


def test_apply_vote_same_adds_to_same_list():
    same: list[int] = []
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "same")
    assert changed is False
    assert same == [1]
    assert nope == []


def test_apply_vote_nope_adds_to_nope_list():
    same: list[int] = []
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "nope")
    assert changed is False
    assert nope == [1]
    assert same == []


def test_apply_vote_idempotent_when_already_voted_same_side():
    same: list[int] = [1]
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "same")
    assert changed is False
    assert same == [1]  # not duplicated


def test_apply_vote_switching_from_nope_to_same_flags_changed():
    same: list[int] = []
    nope: list[int] = [1]
    changed = apply_vote(same, nope, 1, "same")
    assert changed is True
    assert same == [1]
    assert nope == []


def test_apply_vote_switching_from_same_to_nope_flags_changed():
    same: list[int] = [1]
    nope: list[int] = []
    changed = apply_vote(same, nope, 1, "nope")
    assert changed is True
    assert same == []
    assert nope == [1]


def test_apply_vote_unknown_kind_raises():
    with pytest.raises(ValueError):
        apply_vote([], [], 1, "maybe")


def test_apply_vote_multiple_users_independent():
    same: list[int] = []
    nope: list[int] = []
    apply_vote(same, nope, 1, "same")
    apply_vote(same, nope, 2, "nope")
    apply_vote(same, nope, 3, "same")
    assert same == [1, 3]
    assert nope == [2]


# ── tally_entry_votes ────────────────────────────────────────────────


def test_tally_entry_votes_no_votes():
    same, nope, pct = tally_entry_votes([], [])
    assert same == 0
    assert nope == 0
    assert pct == 0.0


def test_tally_entry_votes_all_same_yields_100_pct():
    same, nope, pct = tally_entry_votes([1, 2, 3], [])
    assert same == 3
    assert nope == 0
    assert pct == 1.0


def test_tally_entry_votes_all_nope_yields_0_pct():
    same, nope, pct = tally_entry_votes([], [1, 2])
    assert same == 0
    assert nope == 2
    assert pct == 0.0


def test_tally_entry_votes_50_50_yields_half():
    same, nope, pct = tally_entry_votes([1, 2], [3, 4])
    assert same == 2
    assert nope == 2
    assert pct == 0.5


def test_tally_entry_votes_uneven_split():
    same, nope, pct = tally_entry_votes([1], [2, 3, 4])
    assert same == 1
    assert nope == 3
    assert pct == 0.25


# ── build_result_entry ───────────────────────────────────────────────


def test_build_result_entry_includes_all_metadata():
    entry = build_result_entry(
        text="My fantasy",
        category="Fantasy",
        author=99,
        same_votes=[1, 2, 3],
        nope_votes=[4],
    )
    assert entry["text"] == "My fantasy"
    assert entry["category"] == "Fantasy"
    assert entry["author"] == 99
    assert entry["same"] == 3
    assert entry["nope"] == 1
    assert entry["same_pct"] == 0.75


def test_build_result_entry_voters_concatenates_both_lists():
    entry = build_result_entry(
        text="t",
        category="Fantasy",
        author=1,
        same_votes=[1, 2],
        nope_votes=[3, 4],
    )
    # Order doesn't matter for correctness, but it should be a list of all 4.
    assert set(entry["voters"]) == {1, 2, 3, 4}
    assert len(entry["voters"]) == 4


def test_build_result_entry_empty_votes_yields_zero_pct():
    entry = build_result_entry(
        text="orphan", category="Fantasy", author=1, same_votes=[], nope_votes=[]
    )
    assert entry["same"] == 0
    assert entry["nope"] == 0
    assert entry["same_pct"] == 0.0
    assert entry["voters"] == []


def test_build_result_entry_voters_is_a_copy_not_a_reference():
    """The cog mutates the vote lists after we build the result; make
    sure the result entry's ``voters`` doesn't alias them."""
    same = [1, 2]
    nope = [3]
    entry = build_result_entry(
        text="t", category="Fantasy", author=1, same_votes=same, nope_votes=nope
    )
    same.append(99)
    nope.append(88)
    # The result entry's voters should still be the original 3 IDs.
    assert set(entry["voters"]) == {1, 2, 3}


# ── compute_recap_summary ────────────────────────────────────────────


def test_compute_recap_summary_returns_none_for_empty_results():
    assert compute_recap_summary([]) is None


def test_compute_recap_summary_single_result_all_three_point_to_same():
    results = [
        {"text": "lone", "same_pct": 0.5, "voters": [1, 2]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_shared"]["text"] == "lone"
    assert summary["most_polar"]["text"] == "lone"
    assert summary["biggest_outlier"]["text"] == "lone"
    assert summary["total_voters"] == {1, 2}
    assert summary["total_results"] == 1


def test_compute_recap_summary_most_shared_picks_highest_pct():
    results = [
        {"text": "low", "same_pct": 0.2, "voters": [1]},
        {"text": "mid", "same_pct": 0.5, "voters": [2]},
        {"text": "high", "same_pct": 0.9, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_shared"]["text"] == "high"


def test_compute_recap_summary_biggest_outlier_picks_lowest_pct():
    results = [
        {"text": "low", "same_pct": 0.2, "voters": [1]},
        {"text": "mid", "same_pct": 0.5, "voters": [2]},
        {"text": "high", "same_pct": 0.9, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["biggest_outlier"]["text"] == "low"


def test_compute_recap_summary_most_polar_picks_closest_to_half():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "mid", "same_pct": 0.55, "voters": [2]},
        {"text": "high", "same_pct": 0.95, "voters": [3]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["most_polar"]["text"] == "mid"


def test_compute_recap_summary_dedupes_voters_across_entries():
    results = [
        {"text": "a", "same_pct": 0.5, "voters": [1, 2, 3]},
        {"text": "b", "same_pct": 0.5, "voters": [2, 3, 4]},
        {"text": "c", "same_pct": 0.5, "voters": [4, 5]},
    ]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["total_voters"] == {1, 2, 3, 4, 5}
    assert summary["total_results"] == 3


def test_compute_recap_summary_handles_missing_voters_key():
    results = [{"text": "t", "same_pct": 0.5}]
    summary = compute_recap_summary(results)
    assert summary is not None
    assert summary["total_voters"] == set()


# ── get_round_entries ────────────────────────────────────────────────


def test_get_round_entries_returns_empty_when_payload_lacks_rounds():
    assert get_round_entries({}, 1) == []


def test_get_round_entries_returns_empty_when_round_missing():
    payload = {"rounds": {}}
    assert get_round_entries(payload, 1) == []


def test_get_round_entries_returns_entries_when_present():
    payload = {
        "rounds": {
            "1": {
                "entries": [
                    {"user_id": 1, "text": "a", "category": "Fantasy"},
                ]
            }
        }
    }
    assert get_round_entries(payload, 1) == [
        {"user_id": 1, "text": "a", "category": "Fantasy"}
    ]


def test_get_round_entries_uses_stringified_round_num():
    payload = {"rounds": {"3": {"entries": ["x"]}}}
    # The lookup key is str — passing int still finds it.
    assert get_round_entries(payload, 3) == ["x"]


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_shows_host_name():
    embed = build_lobby_embed("Alice")
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Host"] == "Alice"


def test_build_lobby_embed_has_title_and_footer():
    embed = build_lobby_embed("Alice")
    assert embed.title is not None
    assert "FANTASIES" in embed.title
    assert embed.footer.text is not None
    assert "Fantasies" in embed.footer.text


# ── build_round_submit_embed ────────────────────────────────────────


def test_build_round_submit_embed_includes_round_num_in_title():
    embed = build_round_submit_embed(3)
    assert embed.title is not None
    assert "ROUND 3" in embed.title


def test_build_round_submit_embed_has_description():
    embed = build_round_submit_embed(1)
    assert embed.description is not None
    assert "anonymously" in embed.description


# ── build_vote_embed ─────────────────────────────────────────────────


def test_build_vote_embed_open_has_no_closed_suffix():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
    )
    assert embed.title is not None
    assert "Fantasy #1" in embed.title
    assert "CLOSED" not in embed.title


def test_build_vote_embed_closed_appends_vote_closed_suffix():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=2,
        category="Dealbreaker",
        same_votes=[1],
        nope_votes=[2],
        closed=True,
    )
    assert embed.title is not None
    assert "VOTE CLOSED" in embed.title
    assert "Dealbreaker #2" in embed.title


def test_build_vote_embed_escapes_markdown_in_entry_text():
    embed = build_vote_embed(
        entry_text="**bold** _italic_",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
    )
    entry_field = next(f for f in embed.fields if f.name == "Entry")
    assert entry_field.value is not None
    assert "\\*\\*bold\\*\\*" in entry_field.value
    assert "\\_italic\\_" in entry_field.value


def test_build_vote_embed_votes_field_shows_both_options():
    embed = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[1, 2],
        nope_votes=[3],
    )
    votes_field = next(f for f in embed.fields if f.name == "Votes")
    assert votes_field.value is not None
    assert "✅ Same" in votes_field.value
    assert "❌ Not for me" in votes_field.value
    # Counts surfaced as raw numbers in parens.
    assert "(2)" in votes_field.value
    assert "(1)" in votes_field.value


def test_build_vote_embed_progress_only_when_total_entries_set():
    embed_no_total = build_vote_embed(
        entry_text="t",
        entry_num=1,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
        total_entries=0,
    )
    assert all(f.name != "Progress" for f in embed_no_total.fields)

    embed_with_total = build_vote_embed(
        entry_text="t",
        entry_num=2,
        category="Fantasy",
        same_votes=[],
        nope_votes=[],
        total_entries=5,
    )
    progress = next(
        f for f in embed_with_total.fields if f.name == "Progress"
    )
    assert progress.value == "Entry 2/5"


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_returns_none_for_empty_results():
    assert build_recap_embed([]) is None


def test_build_recap_embed_single_result_includes_all_sections():
    results = [
        {"text": "lone", "same_pct": 0.5, "voters": [1, 2], "category": "Fantasy"},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert "🌟 Most Universally Shared" in by_name
    assert "⚡ Most Polarizing" in by_name
    assert "🏔️ Biggest Outlier" in by_name
    assert by_name["Total Submissions"] == "1"
    assert by_name["Total Voters"] == "2"


def test_build_recap_embed_formats_pct_as_percent():
    results = [
        {"text": "x", "same_pct": 0.75, "voters": [1]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    shared = by_name["🌟 Most Universally Shared"]
    assert shared is not None
    assert "75% Same" in shared


def test_build_recap_embed_dedupes_total_voters():
    results = [
        {"text": "a", "same_pct": 0.5, "voters": [1, 2]},
        {"text": "b", "same_pct": 0.5, "voters": [2, 3]},
        {"text": "c", "same_pct": 0.5, "voters": [3, 4]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    assert by_name["Total Voters"] == "4"
    assert by_name["Total Submissions"] == "3"


def test_build_recap_embed_picks_highest_pct_for_most_shared():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "high", "same_pct": 0.9, "voters": [2]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    shared = by_name["🌟 Most Universally Shared"]
    assert shared is not None
    assert "high" in shared


def test_build_recap_embed_picks_lowest_pct_for_biggest_outlier():
    results = [
        {"text": "low", "same_pct": 0.1, "voters": [1]},
        {"text": "high", "same_pct": 0.9, "voters": [2]},
    ]
    embed = build_recap_embed(results)
    assert embed is not None
    by_name = {f.name: f.value for f in embed.fields}
    outlier = by_name["🏔️ Biggest Outlier"]
    assert outlier is not None
    assert "low" in outlier
