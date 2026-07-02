"""Embed builders for the Anonymous AMA cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API.

The main lobby embed needs to render queue display names from raw
uids; rather than depend on a ``Guild`` object, it takes a
``name_resolver`` callable (``int_uid -> str``). The cog passes a
closure that does the guild lookup; tests pass an identity / dict.
"""

from __future__ import annotations

from typing import Any, Callable

import discord

from bot_modules.games.constants import GAME_ICONS, BRAND_COLOR
from bot_modules.games.utils.live_bar import build_bar
from bot_modules.games_ama.logic import remaining_questions_text

# A function that maps a uid (as int) to a display name.
NameResolver = Callable[[int], str]


def build_lobby_embed(
    host_name: str,
    mode: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the initial ``/ama`` lobby embed (no hot seat yet).

    Shown immediately after ``/ama`` runs — fixed copy and "—" for the
    hot-seat slot. Once a volunteer takes the seat the cog edits the
    message to the richer :func:`build_main_embed` output.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    embed = discord.Embed(
        title=f"{GAME_ICONS['ama']} ANONYMOUS AMA",
        description="Who's taking the hot seat?",
        color=colour,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Hot Seat", value="—", inline=True)
    embed.add_field(name="Mode", value=mode, inline=True)
    embed.set_footer(text=f"{GAME_ICONS['ama']} Anonymous AMA")
    return embed


def build_main_embed(
    host_name: str,
    mode: str,
    hot_seat_name: str | None,
    questions_this_turn: int,
    queue: list[int],
    name_resolver: NameResolver,
    payload: dict[str, Any] | None = None,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the live game embed (hot seat + queue + progress bar).

    Called from ``AMAView.refresh_status`` to edit the lobby message in
    place. When ``hot_seat_name`` is ``None`` the seat is open and the
    description prompts for a volunteer. When ``payload`` is provided a
    progress bar field is appended; tests can omit it to exercise the
    "no progress yet" branch.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    if hot_seat_name is None:
        desc = "Who's taking the hot seat?"
        hot_seat_str = "—"
    else:
        remaining_blurb = remaining_questions_text(questions_this_turn)
        desc = (
            f"Ask **{hot_seat_name}** anything — questions are anonymous.\n"
            f"{remaining_blurb}"
        )
        hot_seat_str = hot_seat_name

    embed = discord.Embed(
        title=f"{GAME_ICONS['ama']} ANONYMOUS AMA",
        description=desc,
        color=colour,
    )
    embed.add_field(name="Host", value=host_name, inline=True)
    embed.add_field(name="Hot Seat", value=hot_seat_str, inline=True)
    embed.add_field(name="Mode", value=mode, inline=True)

    if queue:
        queue_names = [name_resolver(uid) for uid in queue]
        embed.add_field(
            name=f"📋 Queue ({len(queue)})",
            value=" → ".join(queue_names),
            inline=False,
        )

    if payload:
        total_q = len(payload.get("questions", []))
        answered = payload.get("total_answered", 0)
        passed = payload.get("total_passed", 0)
        bar, pct = build_bar(answered, total_q) if total_q else ("░" * 14, "0%")
        embed.add_field(
            name="📊 Progress",
            value=(
                f"Questions: **{total_q}**  •  Answered: **{answered}**  •  Passed: **{passed}**\n"
                f"`{bar}` {pct} answered"
            ),
            inline=False,
        )

    embed.set_footer(text=f"{GAME_ICONS['ama']} Anonymous AMA")
    return embed


def build_question_embed(
    question_text: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the per-question embed posted to the channel.

    Shared between the unfiltered-post path and the screened-approval
    path so both produce the same look. The question text is
    markdown-escaped here so callers don't need to remember.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    return discord.Embed(
        title=f"{GAME_ICONS['ama']} QUESTION",
        description=f'"{discord.utils.escape_markdown(question_text)}"',
        color=colour,
    )


def build_idle_ai_question_embed(
    question_text: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the embed for an idle-AI fallback question.

    Same shape as :func:`build_question_embed` but with a footer
    distinguishing the source so players know nobody wrote this in
    chat.
    """
    embed = build_question_embed(question_text, colour=colour)
    embed.set_footer(text="Auto-generated after 15 minutes with no player questions")
    return embed


def build_answered_embed(
    question_text: str,
    answer_text: str,
    answerer_display_name: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the in-place edit shown once the hot seat replies.

    Replaces the question's view with the Q + A in one embed; the
    answerer's display name is footered so the post stays anonymous on
    the asker's side.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    q_escaped = discord.utils.escape_markdown(question_text)
    a_escaped = discord.utils.escape_markdown(answer_text)
    embed = discord.Embed(
        title=f"{GAME_ICONS['ama']} QUESTION + ANSWER",
        description=f'**Q:** "{q_escaped}"\n\n**A:** "{a_escaped}"',
        color=colour,
    )
    embed.set_footer(text=f"— {answerer_display_name}")
    return embed


def build_asker_dm_embed(
    channel_mention: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the DM the asker receives when their question gets a reply.

    Keeps the channel-mention text consistent so the embed colour and
    wording don't drift between branches.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    return discord.Embed(
        description=(
            f"🔔 Your anonymous question in **{channel_mention}** got a reply!"
        ),
        color=colour,
    )


def build_recap_embed(
    mode: str,
    stats: dict[str, int],
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Build the game-over recap embed from a precomputed ``stats`` dict.

    ``stats`` must come from
    :func:`bot_modules.games_ama.logic.compute_recap_stats` — it has
    the keys ``total_q``, ``total_answered``, ``total_passed``,
    ``rotations`` and ``unique_askers``.
    """
    if colour is None:
        colour = discord.Colour(BRAND_COLOR)
    total_q = stats.get("total_q", 0)
    total_answered = stats.get("total_answered", 0)
    total_passed = stats.get("total_passed", 0)
    rotations = stats.get("rotations", 0)
    unique_askers = stats.get("unique_askers", 0)

    embed = discord.Embed(
        title=f"{GAME_ICONS['ama']} ANONYMOUS AMA — GAME OVER",
        description="Thanks for playing! Here's how the session went:",
        color=colour,
    )
    bar, pct = build_bar(total_answered, total_q) if total_q else ("░" * 14, "0%")
    embed.add_field(
        name="📊 Session Stats",
        value=(
            f"**{total_q}** questions asked by **{unique_askers}** people\n"
            f"**{total_answered}** answered  •  **{total_passed}** passed\n"
            f"`{bar}` {pct} answered"
        ),
        inline=False,
    )
    embed.add_field(name="🔄 Hot Seat Rotations", value=str(rotations), inline=True)
    embed.add_field(name="🎙️ Mode", value=mode.title(), inline=True)
    embed.set_footer(text=f"{GAME_ICONS['ama']} Thanks for playing Anonymous AMA!")
    return embed
