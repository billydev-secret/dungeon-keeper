"""Economy leaderboard panel — the auto-updating channel embed.

One branded embed showing the top earners over a rolling window, community
goal progress, the active quest board, and a pointer to the personal
commands. Posted by ``/bank post-leaderboard`` and refreshed in place every
hour by the economy loop. The panel's channel and message ids live in the
``econ_`` config (``leaderboard_channel_id`` / ``leaderboard_message_id``,
same pattern as the how-to guide panel) so a repost replaces the old panel
instead of stacking duplicates.

Pure collector + builder — all Discord I/O stays in the cog and the loop.
The builder takes a ``resolve_name`` callable so it never touches the
gateway itself.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from bot_modules.services.economy_quests_service import list_quests

if TYPE_CHECKING:
    from collections.abc import Callable

    from bot_modules.services.economy_service import EconSettings

# Rolling window for the earner ranking, in days.
ROLLING_DAYS = 7

# How many earners make the board.
TOP_N = 5

_MEDALS = ("🥇", "🥈", "🥉", "🏅", "🏅")

# Quest-board display order and per-cadence labels.
_QTYPE_LABELS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "event": "Anytime",
}

# Keep the quest board inside Discord's 1024-char field limit.
_MAX_QUEST_LINES = 12


def progress_bar(current: int, target: int, width: int = 10) -> str:
    """A text meter for a community quest's running total."""
    if target <= 0:
        return f"{current:,}"
    filled = max(0, min(width, round(width * current / target)))
    return f"{'▰' * filled}{'▱' * (width - filled)} {current:,}/{target:,}"


@dataclass(frozen=True)
class CommunityGoal:
    title: str
    current: int
    target: int | None
    completed: bool
    settled: bool


@dataclass(frozen=True)
class QuestLine:
    qtype: str
    title: str
    reward: int
    reward_xp: int


@dataclass(frozen=True)
class LeaderboardData:
    top_earners: list[tuple[int, int]]  # (user_id, amount), ranked
    community: list[CommunityGoal]
    quests: list[QuestLine]


def collect_leaderboard_data(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> LeaderboardData:
    """Everything the embed shows, in one sync read.

    Earner income matches the Statistics page definition: positive ledger
    amounts excluding ``transfer_in`` (a transfer moves currency between
    members, it isn't earned).
    """
    cutoff = now_ts - ROLLING_DAYS * 86400
    earners = [
        (int(r["user_id"]), int(r["s"]))
        for r in conn.execute(
            "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
            "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
            "AND kind != 'transfer_in' "
            "GROUP BY user_id ORDER BY s DESC, user_id LIMIT ?",
            (guild_id, cutoff, TOP_N),
        ).fetchall()
    ]

    community: list[CommunityGoal] = []
    quests: list[QuestLine] = []
    for row in list_quests(conn, guild_id, active_only=True):
        if row["qtype"] == "community":
            prog = conn.execute(
                "SELECT current, completed_at, settled_at "
                "FROM econ_community_progress WHERE quest_id = ?",
                (int(row["id"]),),
            ).fetchone()
            community.append(
                CommunityGoal(
                    title=row["title"],
                    current=int(prog["current"]) if prog else 0,
                    target=row["community_target"],
                    completed=bool(prog and prog["completed_at"] is not None),
                    settled=bool(prog and prog["settled_at"] is not None),
                )
            )
        elif row["qtype"] in _QTYPE_LABELS:
            quests.append(
                QuestLine(
                    qtype=row["qtype"],
                    title=row["title"],
                    reward=int(row["reward"]),
                    reward_xp=int(row["reward_xp"]),
                )
            )
    order = {q: i for i, q in enumerate(_QTYPE_LABELS)}
    quests.sort(key=lambda q: order[q.qtype])
    return LeaderboardData(top_earners=earners, community=community, quests=quests)


def build_leaderboard_embed(
    settings: EconSettings,
    data: LeaderboardData,
    resolve_name: Callable[[int], str],
    *,
    now_ts: float,
    colour: discord.Colour | None = None,
) -> discord.Embed:
    """The member-facing leaderboard embed, templated on the guild's branding."""
    emoji = settings.currency_emoji
    plural = settings.currency_plural

    embed = discord.Embed(
        title=f"{emoji} {plural} — leaderboard & quest board",
        description=(
            "Who's earning, what's running, and what there is to do — "
            "refreshed every hour."
        ),
        colour=colour,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    if data.top_earners:
        earner_lines = [
            f"{_MEDALS[i]} **{resolve_name(uid)}** — {emoji} {amount:,}"
            for i, (uid, amount) in enumerate(data.top_earners)
        ]
    else:
        earner_lines = ["Nobody has earned yet this week — be the first!"]
    embed.add_field(
        name=f"Top earners (last {ROLLING_DAYS} days)",
        value="\n".join(earner_lines),
        inline=False,
    )

    if data.community:
        goal_lines = []
        for g in data.community:
            if g.settled:
                state = " — ✅ paid out"
            elif g.completed:
                state = " — 🎉 complete, payout coming"
            else:
                state = ""
            goal_lines.append(
                f"**{g.title}**\n{progress_bar(g.current, g.target or 0)}{state}"
            )
        embed.add_field(
            name="Community goals — everyone gets paid when we hit them",
            value="\n".join(goal_lines),
            inline=False,
        )

    if data.quests:
        quest_lines = []
        for q in data.quests[:_MAX_QUEST_LINES]:
            xp = f" +⭐{q.reward_xp}xp" if q.reward_xp > 0 else ""
            quest_lines.append(
                f"`{_QTYPE_LABELS[q.qtype]}` **{q.title}** — {emoji} {q.reward:,}{xp}"
            )
        hidden = len(data.quests) - _MAX_QUEST_LINES
        if hidden > 0:
            quest_lines.append(f"…and {hidden} more on `/quests`.")
        board = "\n".join(quest_lines)
    else:
        board = "No quests running right now — check back soon."
    embed.add_field(name="Quest board", value=board, inline=False)

    embed.add_field(
        name="Your progress",
        value=(
            "`/quests` shows your own quest progress and claims, and "
            f"`/bank wallet` your {plural} balance and recent earnings — "
            "both are private, only you see the reply."
        ),
        inline=False,
    )

    embed.set_footer(text="Updates hourly")
    embed.timestamp = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    return embed
