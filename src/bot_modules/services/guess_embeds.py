"""Pure Guess embed builders — names in, ``discord.Embed`` out.

Every builder here that names a member takes a ``name_fn``
(:mod:`bot_modules.services.name_resolver`) and emits plain text. A ``<@id>``
mention is only turned into a name by the *reading* client, from its own cache,
so inside an embed it degrades to a bare numeric id for anyone who hasn't seen
that user — which on a public card is the normal case, not an edge case. See
``docs/embed_style_guide.md`` § Naming members in embeds.

``name_fn`` defaults to ``mention`` so an un-wired caller keeps its pre-resolver
output rather than crashing; ``tests/test_guess_embeds.py`` holds both halves of
the contract (no card leaves a raw reference, and every render site passes a
resolver).
"""

from __future__ import annotations

import discord

from bot_modules.services.guess_models import GuessRound
from bot_modules.services.name_resolver import NameFn, mention

#: Rows are ``(user_id, posted, solved)`` for posters, ``(user_id, solved)``
#: for guessers — straight from the repo's leaderboard queries.
_MEDALS = ["🥇", "🥈", "🥉", "4.", "5."]


def reveal_names(
    answer_id: int,
    submitter_id: int,
    *,
    no_contact: bool,
    name_fn: NameFn = mention,
) -> tuple[str, str]:
    """How the solved reveal refers to the answer and the submitter.

    Normally both are resolved to display names. When the two are a
    **no-contact pair**, both degrade to a bare ``User <id>`` instead: embed
    mentions don't fire a notification, but the reveal still names them
    together in the bot's own voice, which is the bot manufacturing the
    association the pair exists to prevent. A raw id is the *correct* output
    there, and this is the one place in Guess where a number is not the bug —
    ``docs/no_contact_spec.md``.
    """
    if no_contact:
        return f"User {answer_id}", f"User {submitter_id}"
    return name_fn(answer_id), name_fn(submitter_id)


def solved_embed(
    round_id: int,
    answer_name: str,
    submitter_name: str,
    solver_id: int,
    guess_count: int,
    unique_count: int,
    *,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """The public reveal card.

    ``answer_name``/``submitter_name`` arrive already rendered, from
    :func:`reveal_names` — that pair's text is a no-contact decision the
    caller makes with the database, not something a builder can re-derive.
    The solver is named here: a member who is a no-contact partner of either
    side can't have guessed the round in the first place.
    """
    guesses_txt = f"{guess_count} guess{'es' if guess_count != 1 else ''}"
    guessers_txt = f"{unique_count} guesser{'s' if unique_count != 1 else ''}"
    return discord.Embed(
        title=f"✅ Round #{round_id} — Solved!",
        color=discord.Color.green(),
        description=(
            f"**Answer:** {answer_name}\n"
            f"**Submitted by:** {submitter_name}\n"
            f"**Solved by:** {name_fn(solver_id)} in {guesses_txt} "
            f"(across {guessers_txt})"
        ),
    )


def leaderboard_embed(
    posters: list[tuple[int, int, int]],
    guessers: list[tuple[int, int]],
    *,
    color: "discord.Color | None" = None,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """The public 🏆 leaderboard — two ranked fields."""

    def _poster_line(i: int, row: tuple[int, int, int]) -> str:
        user_id, posted, solved = row
        pct = f"{solved / posted * 100:.0f}%" if posted else "—"
        return (
            f"{_MEDALS[i]} {name_fn(user_id)} — "
            f"**{posted}** posted, {solved} solved ({pct})"
        )

    def _guesser_line(i: int, row: tuple[int, int]) -> str:
        user_id, solved = row
        return f"{_MEDALS[i]} {name_fn(user_id)} — **{solved}** solved"

    poster_text = (
        "\n".join(_poster_line(i, r) for i, r in enumerate(posters))
        if posters else "_No rounds posted yet._"
    )
    guesser_text = (
        "\n".join(_guesser_line(i, r) for i, r in enumerate(guessers))
        if guessers else "_No rounds solved yet._"
    )

    embed = discord.Embed(title="🏆 Guess Leaderboard", color=color)
    embed.add_field(name="Top Posters", value=poster_text, inline=False)
    embed.add_field(name="Top Guessers", value=guesser_text, inline=False)
    return embed


def _named_with_id(user_id: int, name_fn: NameFn) -> str:
    """``Name (id)`` — the mod-facing rendering.

    Mods act on ids (look a member up, run another command against them), so
    the inspector keeps the number *alongside* the name rather than instead of
    it. Same call the whisper mod-log embeds make.
    """
    return f"{name_fn(user_id)} (`{user_id}`)"


def round_inspector_embed(
    round_row: GuessRound,
    guess_count: int,
    unique_count: int,
    *,
    color: "discord.Color | None" = None,
    name_fn: NameFn = mention,
) -> discord.Embed:
    """The mod-only ephemeral round inspector."""
    if round_row.deleted_at is not None:
        status = "🗑 Deleted"
    elif round_row.solved_at is not None:
        solver = (
            _named_with_id(round_row.solver_id, name_fn)
            if round_row.solver_id else "someone"
        )
        status = f"✅ Solved by {solver}"
    else:
        status = "⏳ Open"

    embed = discord.Embed(
        title=f"🔍 Round #{round_row.id} — inspector",
        color=color,
        description=(
            f"**Status:** {status}\n"
            f"**Submitter:** {_named_with_id(round_row.submitter_id, name_fn)}\n"
            f"**Answer:** {_named_with_id(round_row.answer_id, name_fn)}\n"
            f"**Difficulty:** {round_row.difficulty}\n"
            f"**Guesses:** {guess_count} ({unique_count} unique guessers)\n"
            f"**Re-rolls:** {round_row.reroll_count}\n"
            f"**Created:** <t:{int(round_row.created_at)}:R>"
        ),
    )
    if round_row.crop_url:
        embed.set_image(url=round_row.crop_url)
    return embed
