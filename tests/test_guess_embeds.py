"""Guess cards name their members — except the one place a number is right.

A ``<@id>`` inside an embed is resolved by the *reading* client, from its own
cache; Discord's servers do nothing to it. So on a Guess card it renders as a
bare numeric id for any viewer who hasn't seen that member — which for the
public leaderboard (which names *past* posters, the people a reader is least
likely to have cached) and the solved reveal is the normal case, not an edge
case. Every builder takes a ``name_fn`` and emits plain text.

The exception is the no-contact reveal: when the submitter and the answer are a
no-contact pair, a raw ``User <id>`` is the *correct* output, because naming
them together in the bot's own voice is the bot manufacturing the association
the pair exists to prevent (``docs/no_contact_spec.md``). That branch is pinned
below so a future find-and-replace over the ids can't quietly undo it.
"""

from __future__ import annotations

import re

import discord
import pytest

from bot_modules.services import guess_embeds
from bot_modules.services.guess_models import GuessRound

_MENTION = re.compile(r"<@!?\d+>")


def _named(uid: int) -> str:
    return f"Member{uid}"


def _seen(embed: discord.Embed) -> str:
    """Every string a reader actually sees on a card."""
    parts = [embed.title or "", embed.description or ""]
    if embed.footer is not None:
        parts.append(embed.footer.text or "")
    for f in embed.fields:
        parts += [f.name or "", f.value or ""]
    return "\n".join(parts)


def _round(**kw) -> GuessRound:
    base = dict(
        id=1, guild_id=99, submitter_id=7, answer_id=8, channel_id=5,
        message_id=6, crop_path="", crop_url="", original_path="",
        difficulty="medium", candidate_count=4, reroll_count=0,
        created_at=1_800_000_000.0, solved_at=None, solver_id=None,
        guesses_to_solve=None, unique_guessers_to_solve=None,
        answer_optout=False, deleted_at=None,
    )
    base.update(kw)
    return GuessRound(**base)  # type: ignore[arg-type]


# ── the contract: no card leaves a member as a raw Discord reference ─────────

_NAME_CASES: list[tuple[str, object]] = [
    ("solved_reveal", lambda n: guess_embeds.solved_embed(
        1, *guess_embeds.reveal_names(8, 7, no_contact=False, name_fn=n),
        9, 3, 2, name_fn=n)),
    ("leaderboard", lambda n: guess_embeds.leaderboard_embed(
        [(7, 4, 2)], [(8, 3)], name_fn=n)),
    ("leaderboard_empty", lambda n: guess_embeds.leaderboard_embed(
        [], [], name_fn=n)),
    ("inspector_open", lambda n: guess_embeds.round_inspector_embed(
        _round(), 3, 2, name_fn=n)),
    ("inspector_solved", lambda n: guess_embeds.round_inspector_embed(
        _round(solved_at=1_800_000_100.0, solver_id=9), 3, 2, name_fn=n)),
    ("inspector_deleted", lambda n: guess_embeds.round_inspector_embed(
        _round(deleted_at=1_800_000_100.0), 3, 2, name_fn=n)),
]


@pytest.mark.parametrize(
    ("label", "build"), _NAME_CASES, ids=[c[0] for c in _NAME_CASES]
)
def test_no_guess_card_leaves_a_raw_discord_reference(label, build):
    assert not _MENTION.search(_seen(build(_named))), (
        f"{label} left a raw <@id> reference"
    )


@pytest.mark.parametrize(
    ("label", "build"),
    [c for c in _NAME_CASES if c[0] not in ("leaderboard_empty",)],
    ids=[c[0] for c in _NAME_CASES if c[0] not in ("leaderboard_empty",)],
)
def test_every_guess_card_renders_the_resolved_name(label, build):
    text = _seen(build(_named))
    assert "Member7" in text or "Member8" in text or "Member9" in text, (
        f"{label} never rendered a resolved name"
    )


def test_builders_default_to_a_mention_so_an_unwired_caller_still_renders():
    """``name_fn`` defaults to ``mention`` — an un-wired call keeps its
    pre-resolver output rather than crashing, which is exactly why the AST
    guard below exists to catch one that forgets."""
    text = _seen(guess_embeds.leaderboard_embed([(7, 4, 2)], [(8, 3)]))
    assert "<@7>" in text and "<@8>" in text


# ── the reveal names everyone, including the solver ──────────────────────────


def test_solved_reveal_names_answer_submitter_and_solver():
    answer, submitter = guess_embeds.reveal_names(
        8, 7, no_contact=False, name_fn=_named
    )
    text = _seen(guess_embeds.solved_embed(
        1, answer, submitter, 9, 3, 2, name_fn=_named
    ))
    assert "**Answer:** Member8" in text
    assert "**Submitted by:** Member7" in text
    assert "**Solved by:** Member9" in text


@pytest.mark.parametrize(
    ("guess_count", "unique_count", "expected"),
    [
        (1, 1, "1 guess (across 1 guesser)"),
        (3, 2, "3 guesses (across 2 guessers)"),
    ],
)
def test_solved_reveal_pluralises_its_counts(guess_count, unique_count, expected):
    text = _seen(guess_embeds.solved_embed(
        1, "A", "B", 9, guess_count, unique_count, name_fn=_named
    ))
    assert expected in text


# ── the trap: a no-contact pair stays a pair of numbers ──────────────────────


def test_no_contact_pair_degrades_to_plain_ids_not_names():
    """The one place in Guess where a bare number is the *correct* output.

    Resolving these to display names would have the bot state the association
    the no-contact list exists to prevent, in its own voice. This must survive
    every future sweep that replaces raw ids with resolved names.
    """
    answer, submitter = guess_embeds.reveal_names(
        8, 7, no_contact=True, name_fn=_named
    )
    assert (answer, submitter) == ("User 8", "User 7")

    text = _seen(guess_embeds.solved_embed(
        1, answer, submitter, 9, 3, 2, name_fn=_named
    ))
    assert "Member8" not in text and "Member7" not in text
    assert "User 8" in text and "User 7" in text
    assert "Member9" in text  # the solver is still named — never a pair partner


def test_no_contact_degrade_does_not_leak_a_mention_either():
    """Degrading must land on plain ids, not back on ``<@id>``: an embed
    mention is what the reader's client would resolve into a name for anyone
    who *has* cached the pair, reintroducing the association."""
    answer, submitter = guess_embeds.reveal_names(8, 7, no_contact=True)
    assert not _MENTION.search(f"{answer} {submitter}")


# ── the mod inspector keeps the id alongside the name ────────────────────────


def test_inspector_shows_name_and_id_for_every_member():
    text = _seen(guess_embeds.round_inspector_embed(
        _round(solved_at=1_800_000_100.0, solver_id=9), 3, 2, name_fn=_named
    ))
    assert "Member7 (`7`)" in text   # submitter
    assert "Member8 (`8`)" in text   # answer
    assert "Member9 (`9`)" in text   # solver


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (dict(), "⏳ Open"),
        (dict(solved_at=1_800_000_100.0, solver_id=9), "✅ Solved by Member9"),
        (dict(deleted_at=1_800_000_100.0), "🗑 Deleted"),
    ],
    ids=["open", "solved", "deleted"],
)
def test_inspector_status_line(row, expected):
    text = _seen(guess_embeds.round_inspector_embed(
        _round(**row), 3, 2, name_fn=_named
    ))
    assert expected in text


def test_inspector_solved_without_a_recorded_solver_does_not_name_nobody():
    """Rounds solved before ``solver_id`` was recorded still render."""
    text = _seen(guess_embeds.round_inspector_embed(
        _round(solved_at=1_800_000_100.0, solver_id=None), 3, 2, name_fn=_named
    ))
    assert "✅ Solved by someone" in text


# ── the other half: every render site actually passes a resolver ─────────────


def test_every_guess_render_site_passes_a_resolver():
    """``name_fn`` defaults to ``mention``, so a render site that forgets to
    pass one silently reintroduces the bug and no builder test above would
    notice. This walks the cog and requires every call to a name-taking
    builder to hand a resolver over."""
    import ast
    import inspect
    import pathlib

    from bot_modules.cogs import guess_cog

    needs = {
        name
        for name, fn in inspect.getmembers(guess_embeds, inspect.isfunction)
        if "name_fn" in inspect.signature(fn).parameters
    }
    # Explicit utf-8: this source is full of em-dashes and the CI runner is
    # Windows, where the default encoding is cp1252.
    source = pathlib.Path(inspect.getfile(guess_cog)).read_text(encoding="utf-8")
    missed = [
        f"guess_cog.py:{node.lineno} {node.func.id}()"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in needs
        and not any(kw.arg == "name_fn" for kw in node.keywords)
    ]
    assert not missed, "render sites with no name_fn: " + ", ".join(missed)
