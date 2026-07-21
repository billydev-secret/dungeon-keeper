"""Tests for the extracted Two Truths and a Lie pure-logic modules.

Covers ``bot_modules/games_ttl/logic.py`` (lie-index parsing,
submission storage, shuffle, vote tally, score updates, recap stats)
and ``bot_modules/games_ttl/embeds.py`` (lobby, guess, reveal, recap
embed builders). Mirrors the pressure_cooker / games_traditional
pattern: the cog file stays thin; this module proves the extracted
pieces work without spinning up Discord.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.games_ttl.embeds import (
    build_guess_embed,
    build_lobby_embed,
    build_recap_embed,
    build_reveal_embed,
)
from bot_modules.games_ttl.logic import (
    add_submission,
    compute_recap_winners,
    mark_played,
    parse_lie_index,
    played_ids_from_payload,
    shuffle_statements,
    submission_locked,
    tally_votes,
    update_scores,
)


# ── parse_lie_index ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", 0), ("2", 1), ("3", 2),
        ("a", 0), ("b", 1), ("c", 2),
        ("A", 0), ("B", 1), ("C", 2),
        ("first", 0), ("second", 1), ("third", 2),
        ("one", 0), ("two", 1), ("three", 2),
        ("FIRST", 0), ("Second", 1),
        ("  1  ", 0),  # whitespace stripped
    ],
)
def test_parse_lie_index_accepts_all_known_forms(raw, expected):
    assert parse_lie_index(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "4", "x", "fourth", "1.0", " "])
def test_parse_lie_index_rejects_unknown_forms(raw):
    assert parse_lie_index(raw) is None


def test_parse_lie_index_none_input_returns_none():
    assert parse_lie_index(None) is None  # type: ignore[arg-type]


# ── add_submission ───────────────────────────────────────────────────


def test_add_submission_creates_submissions_and_names_dicts():
    payload: dict = {}
    add_submission(payload, 42, "Alice", ["s1", "s2", "s3"], 1)
    assert payload["submissions"] == {
        "42": {"statements": ["s1", "s2", "s3"], "lie": 1}
    }
    assert payload["submitter_names"] == {"42": "Alice"}
    assert payload["submission_count"] == 1


def test_add_submission_increments_count_for_multiple_players():
    payload: dict = {}
    add_submission(payload, 1, "Alice", ["a", "b", "c"], 0)
    add_submission(payload, 2, "Bob", ["d", "e", "f"], 2)
    assert payload["submission_count"] == 2
    assert set(payload["submissions"].keys()) == {"1", "2"}
    assert payload["submitter_names"] == {"1": "Alice", "2": "Bob"}


def test_add_submission_overwrites_existing_player():
    """Resubmission overwrites — preserves the cog's previous behavior
    where modal resubmit replaced the player's prior entry, and
    submission_count stays equal to len(submissions)."""
    payload: dict = {}
    add_submission(payload, 42, "Alice", ["a", "b", "c"], 0)
    add_submission(payload, 42, "Alice2", ["x", "y", "z"], 2)
    assert payload["submissions"]["42"] == {"statements": ["x", "y", "z"], "lie": 2}
    assert payload["submitter_names"]["42"] == "Alice2"
    assert payload["submission_count"] == 1


def test_add_submission_string_user_id_is_stringified():
    payload: dict = {}
    add_submission(payload, "42", "Alice", ["s1", "s2", "s3"], 0)
    assert "42" in payload["submissions"]


def test_add_submission_copies_statements_list():
    """The stored statements list must not alias the caller's list,
    otherwise later mutations would corrupt the payload."""
    payload: dict = {}
    stmts = ["s1", "s2", "s3"]
    add_submission(payload, 1, "Alice", stmts, 0)
    stmts.append("rogue")
    assert payload["submissions"]["1"]["statements"] == ["s1", "s2", "s3"]


# ── shuffle_statements ───────────────────────────────────────────────


def test_shuffle_statements_preserves_all_statements():
    rng = random.Random(0)
    statements = ["A", "B", "C"]
    new_stmts, _ = shuffle_statements(statements, 0, rng=rng)
    assert sorted(new_stmts) == sorted(statements)


def test_shuffle_statements_tracks_lie_index_through_shuffle():
    """The lie must remain identifiable after shuffling — whichever
    position holds the lie in new_stmts must equal the original lie."""
    rng = random.Random(0)
    statements = ["truth1", "truth2", "LIE"]
    new_stmts, new_lie = shuffle_statements(statements, 2, rng=rng)
    assert new_stmts[new_lie] == "LIE"


def test_shuffle_statements_uses_module_random_when_rng_omitted():
    statements = ["A", "B", "C"]
    new_stmts, new_lie = shuffle_statements(statements, 1)
    assert sorted(new_stmts) == ["A", "B", "C"]
    assert new_stmts[new_lie] == "B"


def test_shuffle_statements_with_deterministic_rng_is_reproducible():
    rng1 = random.Random(123)
    rng2 = random.Random(123)
    s1, l1 = shuffle_statements(["A", "B", "C"], 0, rng=rng1)
    s2, l2 = shuffle_statements(["A", "B", "C"], 0, rng=rng2)
    assert s1 == s2
    assert l1 == l2


# ── tally_votes ──────────────────────────────────────────────────────


def test_tally_votes_splits_correctly():
    votes = {1: 2, 2: 0, 3: 2, 4: 1}
    correct, fooled = tally_votes(votes, lie_index=2)
    assert set(correct) == {1, 3}
    assert set(fooled) == {2, 4}


def test_tally_votes_empty_votes_returns_empty_lists():
    correct, fooled = tally_votes({}, lie_index=0)
    assert correct == []
    assert fooled == []


def test_tally_votes_all_correct():
    votes = {1: 1, 2: 1, 3: 1}
    correct, fooled = tally_votes(votes, lie_index=1)
    assert set(correct) == {1, 2, 3}
    assert fooled == []


def test_tally_votes_all_fooled():
    votes = {1: 0, 2: 0, 3: 0}
    correct, fooled = tally_votes(votes, lie_index=2)
    assert correct == []
    assert set(fooled) == {1, 2, 3}


# ── update_scores ────────────────────────────────────────────────────


def test_update_scores_creates_subject_entry_when_new():
    scores: dict = {}
    update_scores(scores, 42, correct_voters=[1, 2], fooled_voters=[3], total_voters=3)
    assert scores["42"] == {"fooled": 1, "correct_guesses": 0, "total_guessers": 3}


def test_update_scores_credits_each_correct_voter():
    scores: dict = {}
    update_scores(scores, 42, [1, 2], [3], 3)
    assert scores["1"]["correct_guesses"] == 1
    assert scores["2"]["correct_guesses"] == 1
    assert scores["1"]["fooled"] == 0


def test_update_scores_accumulates_across_rounds():
    scores: dict = {}
    update_scores(scores, 1, correct_voters=[2], fooled_voters=[3], total_voters=2)
    update_scores(scores, 1, correct_voters=[3], fooled_voters=[2], total_voters=2)
    assert scores["1"]["fooled"] == 2  # 1+1
    assert scores["1"]["total_guessers"] == 4  # 2+2
    # Voter 2 was correct once, fooled once
    assert scores["2"]["correct_guesses"] == 1
    # Voter 3 was correct once, fooled once
    assert scores["3"]["correct_guesses"] == 1


def test_update_scores_does_not_touch_other_subjects():
    scores = {"99": {"fooled": 5, "correct_guesses": 7, "total_guessers": 12}}
    update_scores(scores, 1, [2], [], 1)
    assert scores["99"] == {"fooled": 5, "correct_guesses": 7, "total_guessers": 12}


# ── compute_recap_winners ────────────────────────────────────────────


def test_compute_recap_winners_empty_scores():
    stats = compute_recap_winners({}, played_ids=set())
    assert stats["best_liar"] == []
    assert stats["most_honest"] == []
    assert stats["best_guesser"] == []
    assert stats["max_correct"] == 0
    assert stats["most_fooled_count"] == 0


def test_compute_recap_winners_picks_unique_best_liar():
    scores = {
        "1": {"fooled": 5, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},
    }
    played = {"1", "2"}
    stats = compute_recap_winners(scores, played)
    assert stats["best_liar"] == ["1"]
    assert stats["most_fooled_count"] == 5
    assert stats["most_honest"] == ["2"]


def test_compute_recap_winners_ties_for_best_liar():
    scores = {
        "1": {"fooled": 3, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 3, "correct_guesses": 0, "total_guessers": 5},
        "3": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},
    }
    stats = compute_recap_winners(scores, played_ids={"1", "2", "3"})
    assert set(stats["best_liar"]) == {"1", "2"}
    assert stats["most_fooled_count"] == 3


def test_compute_recap_winners_excludes_non_subjects_from_liar():
    """A pure guesser (not in played_ids) must not appear as best liar
    even if they have a fooled count (e.g. residual from earlier
    round)."""
    scores = {
        "1": {"fooled": 99, "correct_guesses": 3, "total_guessers": 5},  # guesser only
        "2": {"fooled": 1, "correct_guesses": 0, "total_guessers": 5},   # subject
    }
    stats = compute_recap_winners(scores, played_ids={"2"})
    assert stats["best_liar"] == ["2"]
    assert stats["most_fooled_count"] == 1


def test_compute_recap_winners_best_guesser_considers_everyone():
    """Best Guesser includes non-subjects — a player who only guessed
    can still win."""
    scores = {
        "1": {"fooled": 0, "correct_guesses": 5, "total_guessers": 0},  # pure guesser
        "2": {"fooled": 3, "correct_guesses": 1, "total_guessers": 5},  # subject
    }
    stats = compute_recap_winners(scores, played_ids={"2"})
    assert stats["best_guesser"] == ["1"]
    assert stats["max_correct"] == 5


def test_compute_recap_winners_best_guesser_ties():
    scores = {
        "1": {"fooled": 0, "correct_guesses": 3, "total_guessers": 0},
        "2": {"fooled": 0, "correct_guesses": 3, "total_guessers": 0},
    }
    stats = compute_recap_winners(scores, played_ids=set())
    assert set(stats["best_guesser"]) == {"1", "2"}
    assert stats["max_correct"] == 3


def test_compute_recap_winners_all_subjects_zero_fooled_makes_everyone_most_honest():
    scores = {
        "1": {"fooled": 0, "correct_guesses": 0, "total_guessers": 5},
        "2": {"fooled": 0, "correct_guesses": 0, "total_guessers": 5},
    }
    stats = compute_recap_winners(scores, played_ids={"1", "2"})
    assert set(stats["most_honest"]) == {"1", "2"}
    # Tied -> best liar is also everyone, since min == max
    assert set(stats["best_liar"]) == {"1", "2"}


def test_compute_recap_winners_accepts_played_ids_as_list():
    scores = {"1": {"fooled": 1, "correct_guesses": 0, "total_guessers": 1}}
    stats = compute_recap_winners(scores, played_ids=["1"])
    assert stats["best_liar"] == ["1"]


# ── build_lobby_embed ────────────────────────────────────────────────


def test_build_lobby_embed_default_no_prompt():
    embed = build_lobby_embed()
    assert embed.title is not None
    assert "TWO TRUTHS AND A LIE" in embed.title
    assert embed.description is not None
    assert "Prompt" not in embed.description
    by_name = {f.name: f.value for f in embed.fields}
    # Initial player count field
    assert "Players (0)" in by_name
    assert by_name["Players (0)"] == "—"


def test_build_lobby_embed_includes_prompt_when_provided():
    embed = build_lobby_embed(prompt="best worst date")
    assert embed.description is not None
    assert "best worst date" in embed.description
    assert "**Prompt:**" in embed.description


def test_build_lobby_embed_has_footer():
    embed = build_lobby_embed()
    assert embed.footer.text is not None
    assert "Two Truths and a Lie" in embed.footer.text


# ── build_guess_embed ────────────────────────────────────────────────


def test_build_guess_embed_title_uses_subject_name():
    embed = build_guess_embed("Alice", ["s1", "s2", "s3"], {})
    assert embed.title is not None
    assert "Alice" in embed.title
    assert "GUESS THE LIE" in embed.title


def test_build_guess_embed_closed_flag_changes_title():
    embed = build_guess_embed("Alice", ["s1", "s2", "s3"], {}, closed=True)
    assert embed.title is not None
    assert "REVEAL" in embed.title


def test_build_guess_embed_renders_three_statement_fields():
    embed = build_guess_embed("Alice", ["s1", "s2", "s3"], {})
    assert len(embed.fields) == 3
    for field in embed.fields:
        assert field.value is not None


def test_build_guess_embed_counts_votes_per_statement():
    votes = {1: 0, 2: 0, 3: 1}  # 2 for stmt0, 1 for stmt1, 0 for stmt2
    embed = build_guess_embed("Alice", ["s1", "s2", "s3"], votes)
    # Field names include vote counts; just check the (count) renders
    assert "(2)" in (embed.fields[0].name or "")
    assert "(1)" in (embed.fields[1].name or "")
    assert "(0)" in (embed.fields[2].name or "")


def test_build_guess_embed_escapes_markdown_in_statements():
    embed = build_guess_embed("Alice", ["*bold*", "s2", "s3"], {})
    # Discord.utils.escape_markdown should backslash-escape the asterisks
    assert embed.fields[0].value is not None
    assert "\\*" in embed.fields[0].value


# ── build_reveal_embed ───────────────────────────────────────────────


def test_build_reveal_embed_shows_the_lie():
    embed = build_reveal_embed(
        subject_name="Alice",
        statements=["truth1", "truth2", "LIE!"],
        lie_index=2,
        correct_voters=[1, 2],
        fooled_voters=[3],
        name_resolver=str,
    )
    assert embed.title is not None
    assert "Alice" in embed.title
    assert any("LIE!" in (f.value or "") for f in embed.fields)
    field_names = [f.name or "" for f in embed.fields]
    # The voter-count badges
    assert any("Correct (2)" in n for n in field_names)
    assert any("Fooled (1)" in n for n in field_names)


def test_build_reveal_embed_uses_name_resolver():
    resolver = {"1": "Alice", "2": "Bob", "3": "Carol"}.get
    embed = build_reveal_embed(
        subject_name="Subj",
        statements=["s1", "s2", "s3"],
        lie_index=0,
        correct_voters=[1],
        fooled_voters=[2, 3],
        name_resolver=lambda uid: resolver(uid) or uid,
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    correct_val = next(v for n, v in by_name.items() if "Correct" in n)
    fooled_val = next(v for n, v in by_name.items() if "Fooled" in n)
    assert "Alice" in correct_val
    assert "Bob" in fooled_val
    assert "Carol" in fooled_val


def test_build_reveal_embed_renders_dash_when_no_voters():
    embed = build_reveal_embed(
        subject_name="Alice",
        statements=["s1", "s2", "s3"],
        lie_index=1,
        correct_voters=[],
        fooled_voters=[],
        name_resolver=str,
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    correct_val = next(v for n, v in by_name.items() if "Correct" in n)
    assert correct_val == "—"


# ── build_recap_embed ────────────────────────────────────────────────


def test_build_recap_embed_with_typical_stats():
    stats = {
        "best_liar": ["1"],
        "most_fooled_count": 3,
        "most_honest": ["2"],
        "least_fooled_count": 0,
        "best_guesser": ["2"],
        "max_correct": 4,
    }
    name_map = {"1": "Alice", "2": "Bob"}
    mention_map = {"1": "<@1>", "2": "<@2>"}
    embed, mentions = build_recap_embed(
        stats,
        name_resolver=lambda u: name_map[u],
        mention_resolver=lambda u: mention_map.get(u),
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "🤥 Best Liar" in by_name
    assert "Alice" in by_name["🤥 Best Liar"]
    assert "(3 fooled)" in by_name["🤥 Best Liar"]
    assert "🪞 Open Book" in by_name
    assert "Bob" in by_name["🪞 Open Book"]
    assert "🎯 Best Guesser" in by_name
    assert "(4 correct)" in by_name["🎯 Best Guesser"]
    assert mentions == {"<@1>", "<@2>"}


def test_build_recap_embed_empty_stats_omits_fields():
    stats = {
        "best_liar": [],
        "most_fooled_count": 0,
        "most_honest": [],
        "least_fooled_count": 0,
        "best_guesser": [],
        "max_correct": 0,
    }
    embed, mentions = build_recap_embed(
        stats, name_resolver=str, mention_resolver=lambda u: None
    )
    assert len(embed.fields) == 0
    assert mentions == set()


def test_build_recap_embed_mention_resolver_none_for_left_user_drops_from_pings():
    """When a winner has left the guild, mention_resolver returns None
    and they should not appear in the mentions set (but still in the
    embed text via name_resolver)."""
    stats = {
        "best_liar": ["1"],
        "most_fooled_count": 5,
        "most_honest": [],
        "least_fooled_count": 0,
        "best_guesser": [],
        "max_correct": 0,
    }
    embed, mentions = build_recap_embed(
        stats,
        name_resolver=lambda u: u,  # fallback bare uid
        mention_resolver=lambda u: None,  # user left the guild
    )
    by_name = {(f.name or ""): (f.value or "") for f in embed.fields}
    assert "🤥 Best Liar" in by_name
    assert "1" in by_name["🤥 Best Liar"]
    assert mentions == set()


def test_build_recap_embed_no_mention_resolver_skips_pings():
    """Calling without a mention_resolver should still produce a valid
    embed but mentions stays empty."""
    stats = {
        "best_liar": ["1"],
        "most_fooled_count": 2,
        "most_honest": ["2"],
        "least_fooled_count": 0,
        "best_guesser": ["3"],
        "max_correct": 1,
    }
    embed, mentions = build_recap_embed(stats, name_resolver=lambda u: f"User{u}")
    assert len(embed.fields) == 3
    assert mentions == set()


def test_build_recap_embed_de_dupes_mentions():
    """Same uid in two winner categories (e.g. best_liar + best_guesser)
    must only ping once."""
    stats = {
        "best_liar": ["1"],
        "most_fooled_count": 3,
        "most_honest": ["2"],
        "least_fooled_count": 0,
        "best_guesser": ["1"],
        "max_correct": 4,
    }
    _, mentions = build_recap_embed(
        stats,
        name_resolver=lambda u: f"U{u}",
        mention_resolver=lambda u: f"<@{u}>",
    )
    assert mentions == {"<@1>", "<@2>"}


# ── played tracking / resubmission lock ──────────────────────────────


def test_mark_played_creates_list_and_is_idempotent():
    payload = {}
    mark_played(payload, 1)
    mark_played(payload, 1)
    mark_played(payload, "2")
    assert payload["played"] == ["1", "2"]


def test_played_ids_prefers_explicit_list_over_scores():
    payload = {"played": ["1"], "scores": {"1": {}, "2": {}, "3": {}}}
    assert played_ids_from_payload(payload) == {"1"}


def test_played_ids_falls_back_to_scores_for_legacy_payloads():
    payload = {"scores": {"1": {}, "2": {}}}
    assert played_ids_from_payload(payload) == {"1", "2"}


def test_played_ids_empty_payload():
    assert played_ids_from_payload({}) == set()


def test_submission_locked_only_after_reveal():
    payload = {"submissions": {"1": {}, "2": {}}, "played": ["1"]}
    assert submission_locked(payload, 1) is True
    assert submission_locked(payload, 2) is False
    # A player who hasn't even submitted isn't locked either.
    assert submission_locked(payload, 3) is False


# ── prompt on the guess embed ────────────────────────────────────────


def test_guess_embed_repeats_prompt_for_late_joiners():
    embed = build_guess_embed("mimi", ["a", "b", "c"], {}, prompt="Monday trauma?")
    assert embed.description == "**Prompt:** Monday trauma?"


def test_guess_embed_no_prompt_keeps_description_empty():
    embed = build_guess_embed("mimi", ["a", "b", "c"], {})
    assert embed.description is None


# ── Open Book award rename ───────────────────────────────────────────


def test_recap_open_book_shows_fooled_count():
    stats = {
        "best_liar": ["1"], "most_fooled_count": 5,
        "most_honest": ["2"], "least_fooled_count": 1,
        "best_guesser": ["3"], "max_correct": 4,
    }
    embed, _ = build_recap_embed(stats, lambda uid: f"U{uid}")
    open_book = [f for f in embed.fields if f.name == "🪞 Open Book"]
    assert len(open_book) == 1
    assert open_book[0].value == "U2 (fooled only 1)"


def test_recap_open_book_zero_fooled_reads_naturally():
    stats = {
        "best_liar": ["1"], "most_fooled_count": 5,
        "most_honest": ["2"], "least_fooled_count": 0,
        "best_guesser": ["3"], "max_correct": 4,
    }
    embed, _ = build_recap_embed(stats, lambda uid: f"U{uid}")
    open_book = [f for f in embed.fields if f.name == "🪞 Open Book"]
    assert open_book[0].value == "U2 (fooled no one)"


def test_recap_no_most_honest_field_remains_absent():
    embed, _ = build_recap_embed({}, lambda uid: uid)
    assert all("Open Book" not in (f.name or "") for f in embed.fields)


# ── submission modal (components v2) ─────────────────────────────────
#
# The modal is Discord glue, but its structure caused two live bugs
# (truncated prompt, lobby-embed clobbering), so we pin the parts that
# are testable without a gateway: prompt as static TextDisplay, prefill
# of existing statements, and no fallback edit target.


def _modal(**kwargs):
    from bot_modules.cogs.games_ttl_cog import SubmitStatementsModal
    return SubmitStatementsModal("game-1", db=None, **kwargs)


def test_modal_shows_full_prompt_as_text_display():
    import discord
    prompt = "how are you gonna get over your Monday traumas?"
    modal = _modal(prompt=prompt)
    displays = [c for c in modal.children if isinstance(c, discord.ui.TextDisplay)]
    assert len(displays) == 1
    assert prompt in displays[0].content  # full text, not truncated to 45 chars
    assert prompt not in modal.title


def test_modal_without_prompt_has_no_text_display():
    import discord
    modal = _modal()
    assert not any(isinstance(c, discord.ui.TextDisplay) for c in modal.children)


def test_modal_prefills_existing_submission():
    existing = {"statements": ["s1", "s2", "s3"], "lie": 2}
    modal = _modal(existing=existing)
    assert [ti.default for ti in modal._inputs] == ["s1", "s2", "s3"]
    assert modal._lie_input.default == "3"  # 0-indexed lie → 1-indexed display


def test_modal_never_edits_without_explicit_origin_message():
    # The live bug: falling back to interaction.message clobbered the
    # active guess embed's first field. The modal must only ever target
    # the message explicitly handed to it.
    modal = _modal()
    assert modal._origin_message is None
