"""Embed builders for the Clapback cog.

These functions accept plain dicts/primitives and return ``discord.Embed``
objects. They never call out to Discord — testable with no network and
no mocks of the Bot/Guild API. When a non-anonymous embed needs to
render a player display name, the caller passes a ``name_resolver``
callable so the builder stays guild-free.
"""

from __future__ import annotations

from typing import Any, Callable

import discord

from bot_modules.games.constants import GAME_ICONS
from bot_modules.games_clapback.logic import (
    find_best_answer_record,
    find_closest_matchup_record,
    sort_scores,
)
from bot_modules.services.embeds import COLOR_BLURPLE, COLOR_GREEN

ICON = GAME_ICONS["clapback"]

# Games follow the guild accent (ruling 2026-07-21); the old per-phase palette
# (orange-red / blurple / gold / gray) is retired. When no accent is resolvable
# (no guild), fall back to the sanctioned neutral no-guild fallback (blurple).
FALLBACK_COLOR = discord.Color(COLOR_BLURPLE)
# Semantic: a decided matchup is a win → green (the one sanctioned exception).
WIN_COLOR = discord.Color(COLOR_GREEN)

NameResolver = Callable[[int], str]


def build_lobby_embed(
    host_name: str,
    config: dict[str, Any],
    players: list[int],
    name_resolver: NameResolver,
    start_at: int | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the lobby embed shown while waiting for joiners.

    Renders the host name + round count header, then a Players field
    listing the first ten joinees by display name (with ``(+N more)``
    suffix when the list overflows). Used by both the initial /clapback
    send and the live ``_update_embed`` edit in the join view.
    """
    if color is None:
        color = FALLBACK_COLOR
    embed = discord.Embed(
        title=f"{ICON} CLAPBACK",
        description=(
            f"Hosted by: **{host_name}** | {config['rounds']} rounds\n\n"
            "Join the battle of wits! Write the funniest answer\n"
            "to each prompt, then vote head-to-head."
        ),
        color=color,
    )

    if start_at:
        embed.add_field(
            name="⏰ Starting",
            value=f"<t:{start_at}:R>",
            inline=True,
        )

    if not players:
        player_str = "(nobody yet)"
    elif len(players) <= 10:
        names = [name_resolver(uid) for uid in players]
        player_str = ", ".join(names)
    else:
        names = [name_resolver(uid) for uid in players[:10]]
        player_str = ", ".join(names) + f" (+{len(players) - 10} more)"

    embed.add_field(
        name=f"Players ({len(players)})",
        value=player_str,
        inline=False,
    )
    embed.set_footer(text=f"{ICON} Clapback")
    return embed


def build_submit_embed(
    prompt: str,
    round_num: int,
    total_rounds: int,
    deadline_str: str,
    answers_in: int,
    total_players: int,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the per-round submission prompt embed.

    ``deadline_str`` is a pre-formatted countdown string (the cog uses
    ``format_deadline(now_plus(timer))``) — kept as a string here so
    the builder stays free of timer / datetime imports.
    """
    if color is None:
        color = FALLBACK_COLOR
    embed = discord.Embed(
        title=f"{ICON} CLAPBACK — Round {round_num}/{total_rounds}",
        description=f'**"{prompt}"**',
        color=color,
    )
    embed.add_field(name="Timer", value=deadline_str, inline=True)
    embed.add_field(
        name="Answers In", value=f"{answers_in}/{total_players}", inline=True
    )
    embed.set_footer(text=f"{ICON} Clapback")
    return embed


def build_vote_embed(
    answer_a: str,
    answer_b: str,
    round_num: int,
    matchup_index: int,
    total_matchups: int,
    deadline_str: str,
    vote_count: int = 0,
    prompt: str | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the head-to-head voting embed for a single matchup.

    When ``prompt`` is supplied, the original round prompt is shown above
    the two answers so voters (including spectators) see what was being
    answered.
    """
    prompt_line = (
        f'💬 **"{discord.utils.escape_markdown(prompt)}"**\n\n' if prompt else ""
    )
    embed = discord.Embed(
        title=(
            f"{ICON} HEAD TO HEAD — Round {round_num}, "
            f"Matchup {matchup_index + 1}/{total_matchups}"
        ),
        description=(
            f"{prompt_line}"
            f"🅰️ *\"{discord.utils.escape_markdown(answer_a)}\"*\n\n"
            f"          ⚔️ VS ⚔️\n\n"
            f"🅱️ *\"{discord.utils.escape_markdown(answer_b)}\"*"
        ),
        color=color or FALLBACK_COLOR,
    )
    embed.add_field(name="Timer", value=deadline_str, inline=True)
    embed.add_field(name="Votes", value=str(vote_count), inline=True)
    embed.set_footer(text=f"{ICON} Clapback")
    return embed


def build_reveal_embed(
    result: dict[str, Any],
    answers: dict[str, str],
    player_a: int,
    player_b: int,
    anonymous: bool,
    name_resolver: NameResolver,
    prompt: str | None = None,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the post-vote reveal embed for a finished matchup.

    Has three branches that match the cog's pre-extraction logic:

    - **clapback** (unanimous, 2+ votes): "C L A P B A C K" title,
      winner + defeated fields, celebratory tail line.
    - **tie** (no winner): TIE title with both answers + 50/50 split.
    - **regular win**: MATCHUP RESULT title with winner / loser
      breakdown and computed vote percentages.

    Names are resolved via ``name_resolver`` so the builder stays
    Discord-guild-free. When ``anonymous`` is true the resolver is
    skipped and ``"???"`` is shown instead. When ``prompt`` is supplied,
    the original round prompt is shown under the title.
    """
    vc = result["vote_counts"]
    total_v = vc[player_a] + vc[player_b]

    if result["clapback"]:
        winner_id = result["winner"]
        loser_id = player_b if winner_id == player_a else player_a
        w_answer = answers.get(str(winner_id), "???")
        l_answer = answers.get(str(loser_id), "???")
        w_name = "???" if anonymous else name_resolver(winner_id)
        l_name = "???" if anonymous else name_resolver(loser_id)
        w_pts = result["scores"][winner_id]
        l_pts = result["scores"][loser_id]

        # A clapback is a decisive win → green stays semantic (accent ignored).
        reveal = discord.Embed(
            title=f"{ICON} C L A P B A C K",
            color=WIN_COLOR,
        )
        reveal.add_field(
            name="🏆 Winner",
            value=(
                f'*"{discord.utils.escape_markdown(w_answer)}"* — '
                f'{vc[winner_id]}/{total_v} votes (100%)\n'
                f'by **{w_name}** (+{w_pts} pts!)'
            ),
            inline=False,
        )
        reveal.add_field(
            name="💀 Defeated",
            value=(
                f'*"{discord.utils.escape_markdown(l_answer)}"* — 0 votes (0%)\n'
                f'by **{l_name}** (+{l_pts} pts)'
            ),
            inline=False,
        )
        reveal.add_field(
            name="", value=f"**{w_name}** got a CLAPBACK! 🎉", inline=False
        )

    elif result["winner"] is None:
        # Tie
        answer_a = answers.get(str(player_a), "???")
        answer_b = answers.get(str(player_b), "???")
        a_name = "???" if anonymous else name_resolver(player_a)
        b_name = "???" if anonymous else name_resolver(player_b)
        # A tie has no winner → neutral, so it follows the guild accent.
        reveal = discord.Embed(
            title=f"{ICON} MATCHUP RESULT — TIE!",
            color=color or FALLBACK_COLOR,
        )
        reveal.add_field(
            name="🤝",
            value=(
                f'*"{discord.utils.escape_markdown(answer_a)}"* — '
                f'{vc[player_a]} votes (50%)\n'
                f'by **{a_name}** (+50 pts)\n\n'
                f'*"{discord.utils.escape_markdown(answer_b)}"* — '
                f'{vc[player_b]} votes (50%)\n'
                f'by **{b_name}** (+50 pts)'
            ),
            inline=False,
        )
    else:
        winner_id = result["winner"]
        loser_id = player_b if winner_id == player_a else player_a
        w_answer = answers.get(str(winner_id), "???")
        l_answer = answers.get(str(loser_id), "???")
        w_name = "???" if anonymous else name_resolver(winner_id)
        l_name = "???" if anonymous else name_resolver(loser_id)
        w_pts = result["scores"][winner_id]
        l_pts = result["scores"][loser_id]
        w_pct = round((vc[winner_id] / total_v) * 100) if total_v else 0
        l_pct = 100 - w_pct

        # A decided matchup is a win → green stays semantic (accent ignored).
        reveal = discord.Embed(
            title=f"{ICON} MATCHUP RESULT",
            color=WIN_COLOR,
        )
        reveal.add_field(
            name="🏆 Winner",
            value=(
                f'*"{discord.utils.escape_markdown(w_answer)}"* — '
                f'{vc[winner_id]} votes ({w_pct}%)\n'
                f'by **{w_name}** (+{w_pts} pts)'
            ),
            inline=False,
        )
        reveal.add_field(
            name="💀",
            value=(
                f'*"{discord.utils.escape_markdown(l_answer)}"* — '
                f'{vc[loser_id]} votes ({l_pct}%)\n'
                f'by **{l_name}** (+{l_pts} pts)'
            ),
            inline=False,
        )

    if prompt:
        reveal.description = f'💬 *"{discord.utils.escape_markdown(prompt)}"*'

    reveal.set_footer(text=f"{ICON} Clapback")
    return reveal


def build_scoreboard_embed(
    payload: dict[str, Any],
    round_num: int,
    total_rounds: int,
    bye_player: int | None,
    final: bool = False,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the between-round (or final) scoreboard embed.

    Score field renders ``<@pid>`` mentions ranked highest-first with
    medal emojis for the top three. The ``final`` flag is preserved
    from the cog signature for parity — the pre-extraction
    implementation didn't actually branch on it, so we don't either.
    """
    if color is None:
        color = FALLBACK_COLOR
    scores = payload.get("scores", {})
    sorted_scores = sort_scores(scores)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (pid, pts) in enumerate(sorted_scores):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} <@{pid}> — **{pts}** pts")

    remaining = total_rounds - round_num
    title = f"{ICON} ROUND {round_num} COMPLETE"
    footer_line = (
        f"{remaining} round(s) remaining!"
        if remaining > 0
        else "Final round complete!"
    )

    embed = discord.Embed(title=title, color=color)
    embed.add_field(
        name="📊 Scoreboard",
        value="\n".join(lines) or "No scores yet",
        inline=False,
    )
    if bye_player:
        embed.add_field(
            name="Bye",
            value=f"<@{bye_player}> had a bye this round (+50 pts)",
            inline=False,
        )
    embed.add_field(name="", value=footer_line, inline=False)
    embed.set_footer(text=f"{ICON} Clapback")
    # `final` kept in signature for parity with the cog's pre-extraction
    # signature even though it didn't change rendering.
    _ = final
    return embed


def build_recap_embed(
    payload: dict[str, Any],
    config: dict[str, Any],
    name_resolver: NameResolver,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Build the final-results recap embed shown after the last round.

    Pulls scores / clapbacks / round_history out of ``payload`` and
    renders:

    - Winner header (highest scorer, or "Nobody" when scores empty).
    - Final scoreboard with medals + CLAPBACK counts.
    - Best Single Answer (highest vote %) — skipped if no matchup hit
      the 3-vote floor.
    - Closest Matchup — skipped if no matchups had any votes.
    - Total CLAPBACKS tally — only when non-zero.

    The ``anonymous`` config flag swaps the resolved name for ``???``
    on the Best Single Answer field.
    """
    scores = payload.get("scores", {})
    clapbacks = payload.get("clapbacks", {})
    round_history = payload.get("round_history", [])
    players = payload.get("players", [])
    anonymous = config.get("anonymous", False)

    sorted_scores = sort_scores(scores)
    winner_id = int(sorted_scores[0][0]) if sorted_scores else None
    winner_name = name_resolver(winner_id) if winner_id else "Nobody"

    rounds_played = len(round_history)

    embed = discord.Embed(
        title=f"{ICON} CLAPBACK — FINAL RESULTS",
        description=f"{rounds_played} rounds | {len(players)} players",
        color=color or FALLBACK_COLOR,
    )
    embed.add_field(
        name=f"🏆 WINNER: {winner_name}",
        value=f"**{sorted_scores[0][1]}** pts" if sorted_scores else "—",
        inline=False,
    )

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (pid, pts) in enumerate(sorted_scores):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = name_resolver(int(pid))
        ql_count = clapbacks.get(pid, 0)
        ql_str = (
            f" ({ql_count} CLAPBACK{'S' if ql_count != 1 else ''})"
            if ql_count
            else ""
        )
        lines.append(f"{prefix} **{name}** — {pts} pts{ql_str}")
    embed.add_field(
        name="📊 Final Scoreboard",
        value="\n".join(lines) or "—",
        inline=False,
    )

    # Best single answer (highest vote %)
    best_record = find_best_answer_record(round_history)
    if best_record is not None:
        author_name = (
            "???"
            if anonymous
            else name_resolver(int(best_record["author"]))
        )
        embed.add_field(
            name="⚡ Best Single Answer",
            value=(
                f'*"{best_record["text"]}"* by **{author_name}**\n'
                f'Round {best_record["round"]} — '
                f'{round(best_record["pct"] * 100)}% of votes'
            ),
            inline=False,
        )

    # Closest matchup
    closest_record = find_closest_matchup_record(round_history)
    if closest_record is not None:
        m = closest_record["matchup"]
        rnd = closest_record["round"]
        embed.add_field(
            name="🤣 Closest Matchup",
            value=(
                f'*"{m["answer_a"]}"* vs *"{m["answer_b"]}"*\n'
                f'{m["votes_a"]}–{m["votes_b"]} in Round {rnd}'
            ),
            inline=False,
        )

    total_ql = sum(clapbacks.values())
    if total_ql:
        embed.add_field(name="⚡ Total CLAPBACKS", value=str(total_ql), inline=True)

    embed.set_footer(text=f"{ICON} Clapback")
    # `anonymous` already consumed for best_record; kept for clarity.
    _ = anonymous
    return embed
