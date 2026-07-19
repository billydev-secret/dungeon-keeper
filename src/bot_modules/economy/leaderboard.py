"""Economy leaderboard panel — the live, auto-updating channel embed.

One branded embed showing today's pulse, the top earners over a rolling
window, community goal progress with pace, a per-cadence quest-board
summary (members draw personal boards, so no full-pool menu), and an
anonymous live feed of today's completions. Posted by ``/bank
post-leaderboard``; refreshed in place by the hourly economy loop AND by the
debounced live loop (``leaderboard_live_loop``) whenever economy activity
marks the guild dirty — so the panel moves within a couple of minutes of the
action. The panel's channel and message ids live in the ``econ_`` config
(``leaderboard_channel_id`` / ``leaderboard_message_id``, same pattern as
the how-to guide panel) so a repost replaces the old panel instead of
stacking duplicates.

Pure collector + builder — all Discord I/O stays in the cog and the loops.
The builder takes a ``resolve_name`` callable so it never touches the
gateway itself. The live feed is anonymous by design (2026-07-18 decision):
quest titles, counts, and timestamps — never member names. Countdowns render
as Discord relative timestamps, which tick client-side between edits.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy import quests as quest_rules
from bot_modules.economy.logic import local_day_bounds, local_day_for
from bot_modules.services.economy_quests_service import (
    list_quests,
    spotlight_kind,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from bot_modules.services.economy_service import EconSettings

# Rolling window for the earner ranking, in days.
ROLLING_DAYS = 7

# How many earners make the board.
TOP_N = 5

# How many aggregated completion lines the live feed shows.
FEED_LINES = 5

_MEDALS = ("🥇", "🥈", "🥉", "🏅", "🏅")

# Quest-board display order and per-cadence labels.
_QTYPE_LABELS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "event": "Anytime",
}

# Cap listed "Anytime" (event) quest lines so the field stays inside
# Discord's 1024-char limit; board cadences summarize to one line each.
_MAX_QUEST_LINES = 12

# Pace rule shared with the Statistics page's "Happening now" card: expected
# progress is linear across the ISO week; under 90% of that reads "behind".
_PACE_OK = 0.9


def progress_bar(current: int, target: int, width: int = 10) -> str:
    """A text meter for a community quest's running total."""
    if target <= 0:
        return f"{current:,}"
    filled = max(0, min(width, round(width * current / target)))
    return f"{'▰' * filled}{'▱' * (width - filled)} {current:,}/{target:,}"


def _rel(ts: float) -> str:
    """A Discord relative timestamp — ticks live in every client."""
    return f"<t:{int(ts)}:R>"


@dataclass(frozen=True)
class Pulse:
    """Today's guild-local totals — the panel's heartbeat line."""

    coins_today: int = 0
    quests_today: int = 0
    earners_today: int = 0


@dataclass(frozen=True)
class FeedLine:
    """One anonymous live-feed entry: a quest's completions today."""

    title: str
    count: int
    last_ts: float


@dataclass(frozen=True)
class CommunityGoal:
    title: str
    current: int
    target: int | None
    completed: bool
    settled: bool
    # Auto-tracking weekly (trigger_kind set): tier markers, pace, deadline.
    auto: bool = False
    tiers: int = 0
    contributors: int = 0
    # Today's contribution count (None when unknowable — channel-scoped).
    today_delta: int | None = None
    on_track: bool = True
    ends_ts: float | None = None


@dataclass(frozen=True)
class QuestLine:
    qtype: str
    title: str
    reward: int
    reward_xp: int
    spotlight: bool = False


@dataclass(frozen=True)
class LeaderboardData:
    top_earners: list[tuple[int, int]]  # (user_id, amount), ranked
    community: list[CommunityGoal]
    quests: list[QuestLine]
    spotlight_kind: str | None = None
    spotlight_label: str = ""
    pulse: Pulse = Pulse()
    today_by_user: dict[int, int] = field(default_factory=dict)
    feed: tuple[FeedLine, ...] = ()
    set_bonuses_today: int = 0
    # Next guild-local day roll (dailies reset) / ISO-week roll (weeklies
    # flip, spotlight changes, community weeklies end). None = omit clocks.
    day_roll_ts: float | None = None
    week_roll_ts: float | None = None


def collect_leaderboard_data(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> LeaderboardData:
    """Everything the embed shows, in one sync read.

    Earner income matches the Statistics page definition: positive ledger
    amounts excluding ``transfer_in`` (a transfer moves currency between
    members, it isn't earned). "Today" is the guild-local calendar day; the
    week clock is the guild-local ISO week (Monday 00:00), matching every
    quest cadence.
    """
    offset = get_tz_offset_hours(conn, guild_id)
    today = local_day_for(now_ts, offset)
    day_start, day_end = local_day_bounds(today, offset)
    day_obj = date.fromisoformat(today)
    next_monday = day_obj + timedelta(days=7 - day_obj.weekday())
    week_end, _ = local_day_bounds(next_monday.isoformat(), offset)

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

    today_by_user: dict[int, int] = {}
    if earners:
        marks = ",".join("?" * len(earners))
        today_by_user = {
            int(r["user_id"]): int(r["s"])
            for r in conn.execute(
                "SELECT user_id, SUM(amount) AS s FROM econ_ledger "
                "WHERE guild_id = ? AND created_at >= ? AND amount > 0 "
                f"AND kind != 'transfer_in' AND user_id IN ({marks}) "
                "GROUP BY user_id",
                (guild_id, day_start, *[uid for uid, _ in earners]),
            ).fetchall()
        }

    pulse_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s, COUNT(DISTINCT user_id) AS n "
        "FROM econ_ledger WHERE guild_id = ? AND created_at >= ? "
        "AND amount > 0 AND kind != 'transfer_in'",
        (guild_id, day_start),
    ).fetchone()
    quests_today = conn.execute(
        "SELECT COUNT(*) AS n FROM econ_quest_claims "
        "WHERE guild_id = ? AND state = 'paid' AND created_at >= ?",
        (guild_id, day_start),
    ).fetchone()["n"]
    pulse = Pulse(
        coins_today=int(pulse_row["s"]),
        quests_today=int(quests_today),
        earners_today=int(pulse_row["n"]),
    )

    feed = tuple(
        FeedLine(
            title=str(r["title"]),
            count=int(r["n"]),
            last_ts=float(r["last_ts"]),
        )
        for r in conn.execute(
            "SELECT q.title AS title, COUNT(*) AS n, "
            "MAX(c.created_at) AS last_ts "
            "FROM econ_quest_claims c JOIN econ_quests q ON q.id = c.quest_id "
            "WHERE c.guild_id = ? AND c.state = 'paid' AND c.created_at >= ? "
            "GROUP BY c.quest_id ORDER BY last_ts DESC LIMIT ?",
            (guild_id, day_start, FEED_LINES),
        ).fetchall()
    )
    set_bonuses_today = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM econ_ledger "
            "WHERE guild_id = ? AND kind = 'quest_bonus' AND created_at >= ?",
            (guild_id, day_start),
        ).fetchone()["n"]
    )

    week = quest_rules.iso_week_for(today)
    spot = spotlight_kind(conn, guild_id, week)

    # Pace baseline, shared with compute_live: day 1 of the ISO week counts
    # as one elapsed day.
    elapsed_days = day_obj.weekday() + 1

    community: list[CommunityGoal] = []
    quests: list[QuestLine] = []
    for row in list_quests(conn, guild_id, active_only=True):
        if row["qtype"] == "community":
            qid = int(row["id"])
            prog = conn.execute(
                "SELECT current, completed_at, settled_at "
                "FROM econ_community_progress WHERE quest_id = ?",
                (qid,),
            ).fetchone()
            current = int(prog["current"]) if prog else 0
            target = row["community_target"]
            auto = bool(row["trigger_kind"])
            contributors = 0
            today_delta: int | None = None
            on_track = True
            if auto:
                contributors = int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM econ_community_contrib "
                        "WHERE quest_id = ? AND count > 0",
                        (qid,),
                    ).fetchone()["n"]
                )
                if row["trigger_channel_id"] is None:
                    # The kind-activity ledger is scope-blind, so today's
                    # delta is only honest for unscoped goals.
                    today_delta = int(
                        conn.execute(
                            "SELECT COALESCE(SUM(count), 0) AS s "
                            "FROM econ_kind_activity WHERE guild_id = ? "
                            "AND kind = ? AND local_day = ?",
                            (guild_id, str(row["trigger_kind"]), today),
                        ).fetchone()["s"]
                    )
                expected = (target or 0) * elapsed_days / 7
                on_track = expected == 0 or current >= _PACE_OK * expected
            community.append(
                CommunityGoal(
                    title=row["title"],
                    current=current,
                    target=target,
                    completed=bool(prog and prog["completed_at"] is not None),
                    settled=bool(prog and prog["settled_at"] is not None),
                    auto=auto,
                    tiers=quest_rules.community_tiers_crossed(
                        current, int(target or 0)
                    ) if auto else 0,
                    contributors=contributors,
                    today_delta=today_delta,
                    on_track=on_track,
                    ends_ts=week_end if auto else None,
                )
            )
        elif row["qtype"] in _QTYPE_LABELS:
            quests.append(
                QuestLine(
                    qtype=row["qtype"],
                    title=row["title"],
                    reward=int(row["reward"]),
                    reward_xp=int(row["reward_xp"]),
                    spotlight=bool(
                        spot and str(row["trigger_kind"] or "") == spot
                    ),
                )
            )
    order = {q: i for i, q in enumerate(_QTYPE_LABELS)}
    quests.sort(key=lambda q: order[q.qtype])
    return LeaderboardData(
        top_earners=earners,
        community=community,
        quests=quests,
        spotlight_kind=spot,
        spotlight_label=quest_rules.TRIGGER_KINDS.get(spot, spot) if spot else "",
        pulse=pulse,
        today_by_user=today_by_user,
        feed=feed,
        set_bonuses_today=set_bonuses_today,
        day_roll_ts=day_end,
        week_roll_ts=week_end,
    )


def _pulse_lines(data: LeaderboardData, emoji: str, plural: str) -> str:
    """The heartbeat field: today's totals + the two reset clocks."""
    p = data.pulse
    if p.coins_today > 0:
        head = (
            f"{emoji} **{p.coins_today:,}** {plural} paid out · "
            f"✅ **{p.quests_today}** quest"
            f"{'s' if p.quests_today != 1 else ''} completed · "
            f"👥 **{p.earners_today}** member"
            f"{'s' if p.earners_today != 1 else ''} earning"
        )
    else:
        head = "The day is young — nothing banked yet. Be the first!"
    clocks = []
    if data.day_roll_ts:
        clocks.append(f"dailies reset {_rel(data.day_roll_ts)}")
    if data.week_roll_ts:
        clocks.append(f"new weeklies {_rel(data.week_roll_ts)}")
    if clocks:
        return f"{head}\n🕛 {' · '.join(clocks)}"
    return head


def _community_block(g: CommunityGoal) -> str:
    """One goal's lines: bar + state, then tier/pace/crowd detail for autos."""
    if g.settled:
        state = " — ✅ paid out"
    elif g.completed:
        state = " — 🎉 complete, payout coming"
    else:
        state = ""
    lines = [f"**{g.title}**", f"{progress_bar(g.current, g.target or 0)}{state}"]
    target = int(g.target or 0)
    if g.auto and target > 0 and not g.settled:
        if not g.completed:
            # round() before ceil(): 70×0.7 is 49.000…003 in floats, and a
            # naive ceil would print the 49-action tier as "next at 50".
            thresholds = [
                math.ceil(round(target * frac, 6))
                for frac in quest_rules.COMMUNITY_TIERS
            ]
            if g.tiers > 0:
                tier_bit = f"🏁 tier {g.tiers}/3 secured"
                nxt = (
                    f" · next at {thresholds[g.tiers]:,}"
                    if g.tiers < len(thresholds)
                    else ""
                )
            else:
                tier_bit = f"🎯 first tier at {thresholds[0]:,}"
                nxt = ""
            lines.append(f"{tier_bit}{nxt}")
        detail = ["📈 on pace" if g.on_track else "🐢 needs a push"]
        if g.contributors > 0:
            detail.append(f"👥 {g.contributors} contributing")
        if g.today_delta:
            detail.append(f"+{g.today_delta:,} today")
        if g.ends_ts and not g.completed:
            detail.append(f"ends {_rel(g.ends_ts)}")
        lines.append(" · ".join(detail))
    return "\n".join(lines)


def _feed_lines(data: LeaderboardData) -> str:
    """Today's anonymous completion feed — titles and counts, never names."""
    lines = [
        f"✅ **{f.title}** ×{f.count} · {_rel(f.last_ts)}"
        if f.count > 1
        else f"✅ **{f.title}** · {_rel(f.last_ts)}"
        for f in data.feed
    ]
    if data.set_bonuses_today > 0:
        lines.append(
            f"🎁 Full-board bonus paid ×{data.set_bonuses_today} today"
        )
    if not lines:
        return "Quiet so far today — complete a quest to light this board up."
    return "\n".join(lines)


def build_leaderboard_embed(
    settings: EconSettings,
    data: LeaderboardData,
    resolve_name: Callable[[int], str],
    *,
    now_ts: float,
    color: discord.Color | None = None,
) -> discord.Embed:
    """The member-facing leaderboard embed, templated on the guild's branding."""
    emoji = settings.currency_emoji
    plural = settings.currency_plural

    embed = discord.Embed(
        title=f"{emoji} {plural} — leaderboard & quest board",
        description=(
            "Who's earning, what's running, and what there is to do — live."
        ),
        color=color,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    embed.add_field(
        name="📡 Today's pulse",
        value=_pulse_lines(data, emoji, plural),
        inline=False,
    )

    if data.top_earners:
        earner_lines = []
        for i, (uid, amount) in enumerate(data.top_earners):
            today = data.today_by_user.get(uid, 0)
            delta = f" (+{today:,} today)" if today > 0 else ""
            earner_lines.append(
                f"{_MEDALS[i]} **{resolve_name(uid)}** — "
                f"{emoji} {amount:,}{delta}"
            )
    else:
        earner_lines = ["Nobody has earned yet this week — be the first!"]
    embed.add_field(
        name=f"Top earners (last {ROLLING_DAYS} days)",
        value="\n".join(earner_lines),
        inline=False,
    )

    if data.community:
        embed.add_field(
            name="Community goals — everyone gets paid when we hit them",
            value="\n".join(_community_block(g) for g in data.community),
            inline=False,
        )

    if data.quests:
        quest_lines = []
        if data.spotlight_label:
            until = (
                f" — until {_rel(data.week_roll_ts)}"
                if data.week_roll_ts
                else " this week"
            )
            quest_lines.append(
                f"⚡ **Spotlight:** {data.spotlight_label} pays "
                f"**double**{until}!"
            )
        # Members never face the whole pool: each draws a personal board of
        # board_size quests per cadence. Summarize the draw instead of
        # listing a menu nobody actually has; only board-less "Anytime"
        # (event) quests are named, because those really are open to all.
        sizes = {
            "daily": settings.quest_board_daily,
            "weekly": settings.quest_board_weekly,
            "monthly": settings.quest_board_monthly,
        }
        label_width = max(len(v) for v in _QTYPE_LABELS.values())
        body: list[str] = []
        for qtype, qtype_label in _QTYPE_LABELS.items():
            pool = [q for q in data.quests if q.qtype == qtype]
            if not pool:
                continue
            label = qtype_label.ljust(label_width)
            if qtype == "event":
                for q in pool[:_MAX_QUEST_LINES]:
                    xp = f" +⭐{q.reward_xp}xp" if q.reward_xp > 0 else ""
                    spot_tag = "⚡ " if q.spotlight else ""
                    body.append(
                        f"`{label}` {spot_tag}**{q.title}** — "
                        f"{emoji} {q.reward:,}{xp}"
                    )
                if len(pool) > _MAX_QUEST_LINES:
                    body.append(
                        f"…and {len(pool) - _MAX_QUEST_LINES} more on "
                        "`/quests`."
                    )
                continue
            n = min(sizes.get(qtype, 0), len(pool))
            if n <= 0:
                continue
            lo = min(q.reward for q in pool)
            hi = max(q.reward for q in pool)
            reward = f"{emoji} {lo:,}" + (
                f"–{hi:,}" if hi != lo else ""
            ) + " each"
            body.append(
                f"`{label}` **{n}** on your board, "
                f"drawn from {len(pool)} — {reward}"
            )
        if body:
            quest_lines.extend(body)
            quest_lines.append(
                "Boards reshuffle each reset — `/quests` shows yours."
            )
        board = "\n".join(quest_lines)
        if not board:
            board = "No quests running right now — check back soon."
    else:
        board = "No quests running right now — check back soon."
    embed.add_field(name="Quest board", value=board, inline=False)

    embed.add_field(
        name="📰 Live feed — today",
        value=_feed_lines(data),
        inline=False,
    )

    embed.add_field(
        name="Your progress",
        value=(
            "`/quests` shows your own quest progress and claims, and "
            f"`/bank wallet` your {plural} balance and recent earnings — "
            "both are private, only you see the reply."
        ),
        inline=False,
    )

    embed.set_footer(text="⚡ Live — updates within ~2 min of activity")
    embed.timestamp = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    return embed
