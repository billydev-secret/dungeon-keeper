"""Embed builders for the Two Truths and a Lie cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.

The reveal/recap embeds need to render Discord member display names
from raw uids; rather than depend on a ``Guild`` object, they take a
``name_resolver`` callable (``str_uid -> str``). The cog passes a
closure that does the guild lookup; tests pass an identity / dict.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

import discord

from bot_modules.games.constants import (
    GAME_ICONS,
    PHASE_JOINING,
    PHASE_PLAYING,
    PHASE_RECAP,
    PHASE_RESULTS,
)
from bot_modules.games.utils.live_bar import build_bar

# Number emoji for statement 1/2/3 (shared between guess + reveal embeds).
_NUM_EMOJI: tuple[str, str, str] = ("1️⃣", "2️⃣", "3️⃣")

# A function that maps a uid (as str) to a display name.
NameResolver = Callable[[str], str]


def build_lobby_embed(
    prompt: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the initial ``/twotruths`` lobby embed.

    The optional ``prompt`` is rendered above the explanation so the
    submission modal title and the lobby share the same context.

    ``color`` is the resolved guild accent; when omitted (no guild /
    resolution failed) the historical ``PHASE_JOINING`` gold is used.
    """
    description = (
        "Submit your three statements — two true, one lie.\n"
        "When everyone's ready, the host will start the guessing!"
    )
    if prompt:
        description = f"**Prompt:** {prompt}\n\n{description}"
    embed = discord.Embed(
        title=f"{GAME_ICONS['ttl']} Two Truths and a Lie",
        description=description,
        color=color or discord.Color(PHASE_JOINING),
    )
    embed.add_field(name="Players (0)", value="—", inline=True)
    embed.set_footer(text=f"{GAME_ICONS['ttl']} Two Truths and a Lie")
    return embed


def build_guess_embed(
    subject_name: str,
    statements: list[str],
    votes: dict[int, int],
    closed: bool = False,
    prompt: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the per-round guessing embed (3 statements + live vote bars).

    ``closed`` flips the title from ``GUESS THE LIE`` to ``REVEAL``; the
    field layout is preserved so the existing message can be edited in
    place when the round ends.

    ``prompt`` is repeated on every round so mid-game joiners (who never
    saw the lobby embed) still know what the statements answer.

    ``color`` is the resolved guild accent; when omitted the historical
    phase colors (PLAYING blue / RESULTS green) are used as a fallback.
    Both the open and closed states share the accent — the reveal isn't
    a single win/loss embed (it lists correct *and* fooled voters), so
    it follows the guild accent rather than a semantic green.
    """
    title = f"{GAME_ICONS['ttl']} Guess the Lie — {subject_name}'s Turn"
    if closed:
        title = f"{GAME_ICONS['ttl']} Reveal — {subject_name}"
    embed = discord.Embed(
        title=title,
        description=f"**Prompt:** {prompt}" if prompt else None,
        color=color or discord.Color(PHASE_RESULTS if closed else PHASE_PLAYING),
    )

    vote_counts = [0, 0, 0]
    for v in votes.values():
        if 0 <= v < 3:
            vote_counts[v] += 1
    total = sum(vote_counts)

    for i, stmt in enumerate(statements):
        bar, pct = build_bar(vote_counts[i], total)
        count = vote_counts[i]
        embed.add_field(
            name=f"{_NUM_EMOJI[i]} {bar} {pct} ({count})",
            value=f'"{discord.utils.escape_markdown(stmt)}"',
            inline=False,
        )

    embed.set_footer(text=f"{GAME_ICONS['ttl']} Two Truths and a Lie")
    return embed


def build_reveal_embed(
    subject_name: str,
    statements: list[str],
    lie_index: int,
    correct_voters: Iterable[int],
    fooled_voters: Iterable[int],
    name_resolver: NameResolver,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Build the post-round reveal embed showing the lie + winner lists.

    ``name_resolver`` maps a stringified voter uid to a display name —
    the cog passes a closure that hits ``guild.get_member``; tests pass
    an identity / dict-backed resolver.

    ``color`` is the resolved guild accent; when omitted the historical
    ``PHASE_RESULTS`` green is used. The embed lists both correct and
    fooled voters (not a single winner), so it follows the accent rather
    than a semantic win/loss color.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['ttl']} Reveal — {subject_name}",
        color=color or discord.Color(PHASE_RESULTS),
    )
    lie_stmt = statements[lie_index]
    lie_num = _NUM_EMOJI[lie_index]
    embed.add_field(name=f"The lie was {lie_num}", value=f'"{lie_stmt}" ✅', inline=False)

    def _names(voters: Iterable[int]) -> str:
        parts = [name_resolver(str(uid)) for uid in voters]
        return ", ".join(parts) if parts else "—"

    correct_list = list(correct_voters)
    fooled_list = list(fooled_voters)
    embed.add_field(name=f"🎯 Correct ({len(correct_list)})", value=_names(correct_list), inline=False)
    embed.add_field(name=f"❌ Fooled ({len(fooled_list)})", value=_names(fooled_list), inline=False)
    return embed


def build_recap_embed(
    stats: dict[str, Any],
    name_resolver: NameResolver,
    mention_resolver: Callable[[str], str | None] | None = None,
    color: discord.Color | None = None,
) -> tuple[discord.Embed, set[str]]:
    """Build the final-results embed from a precomputed ``stats`` dict.

    ``stats`` must come from
    :func:`bot_modules.games_ttl.logic.compute_recap_winners` — it has
    the keys ``best_liar``, ``most_fooled_count``, ``most_honest``,
    ``best_guesser``, ``max_correct``.

    ``name_resolver`` maps a uid (str) to the display text that appears
    in the embed fields — typically a Discord mention string
    (``<@123>``) for current members, or a fallback (the raw uid) for
    users who've left the guild.

    ``mention_resolver`` maps a uid to a pingable mention (``<@123>``)
    or ``None`` for users who can't be pinged. If omitted, no mentions
    are returned. The cog passes this so the ping ``content`` doesn't
    include strings for users who've left.

    Returns ``(embed, mentions)``. ``mentions`` is the de-duplicated set
    of mention strings the cog joins into the message ``content`` to
    ping the winners.

    ``color`` is the resolved guild accent; when omitted the historical
    ``PHASE_RECAP`` dark gold is used.
    """
    embed = discord.Embed(
        title=f"{GAME_ICONS['ttl']} Two Truths and a Lie — Final Results",
        color=color or discord.Color(PHASE_RECAP),
    )
    mentions: set[str] = set()

    def _collect_mentions(uids: list[str]) -> None:
        if mention_resolver is None:
            return
        for uid in uids:
            m = mention_resolver(uid)
            if m is not None:
                mentions.add(m)

    best_liar = stats.get("best_liar", [])
    most_fooled_count = stats.get("most_fooled_count", 0)
    if best_liar:
        liar_names = [name_resolver(uid) for uid in best_liar]
        _collect_mentions(best_liar)
        embed.add_field(
            name="🤥 Best Liar",
            value=f"{', '.join(liar_names)} ({most_fooled_count} fooled)",
            inline=True,
        )

    most_honest = stats.get("most_honest", [])
    least_fooled_count = stats.get("least_fooled_count", 0)
    if most_honest:
        honest_names = [name_resolver(uid) for uid in most_honest]
        _collect_mentions(most_honest)
        fooled_note = (
            "fooled no one" if least_fooled_count == 0
            else f"fooled only {least_fooled_count}"
        )
        embed.add_field(
            name="🪞 Open Book",
            value=f"{', '.join(honest_names)} ({fooled_note})",
            inline=True,
        )

    best_guesser = stats.get("best_guesser", [])
    max_correct = stats.get("max_correct", 0)
    if best_guesser:
        guesser_names = [name_resolver(uid) for uid in best_guesser]
        _collect_mentions(best_guesser)
        embed.add_field(
            name="🎯 Best Guesser",
            value=f"{', '.join(guesser_names)} ({max_correct} correct)",
            inline=True,
        )

    return embed, mentions
