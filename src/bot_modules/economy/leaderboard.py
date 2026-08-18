"""The economy panel — the live, auto-updating channel embed.

One branded embed showing today's pulse, the top earners over a rolling
window, community goal progress with pace, a per-cadence quest-board
summary (members draw personal boards, so no full-pool menu), and an
anonymous live feed of today's completions. Placed from the dashboard's panel
poster; refreshed in place by the hourly economy loop AND by the debounced
live loop (``leaderboard_live_loop``) whenever economy activity marks the
guild dirty — so the panel moves within a couple of minutes of the action.

Since 2026-08-18 this is the guild's **only** economy panel: the how-it-works
guide it used to sit alongside became the ❓ button under this embed, and the
merged panel kept the guide's home and the guide's stored ids
(``guide_channel_id`` / ``guide_message_id`` in the ``econ_`` config — see
``plan_panel_merge`` in ``economy.logic`` for why that pair won). This module
still owns the content and both refresh loops; only the message it edits
changed.

Pure collector + builder — all Discord I/O stays in the cog and the loops.
The builder takes a ``resolve_name`` callable so it never touches the
gateway itself. The live feed is anonymous by design (2026-07-18 decision):
quest titles, counts, and timestamps — never member names. Countdowns render
as Discord relative timestamps, which tick client-side between edits.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from bot_modules.core import meters
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy import quests as quest_rules
from bot_modules.economy.logic import local_day_bounds, local_day_for
from bot_modules.services.economy_quests_service import (
    list_quests,
    spotlight_kind,
)
from bot_modules.services.economy_service import load_econ_settings
from bot_modules.services.embeds import EMBED_FIELD_LIMIT, pad_cell, rel_ts

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

# Quest-board display order and per-cadence labels. Event ("Anytime") quests
# aren't board-drawn and don't get a section here — they stay a surprise
# payout rather than a proactively-listed menu.
_QTYPE_LABELS = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
}

# Pace rule shared with the Statistics page's "Happening now" card: expected
# progress is linear across the ISO week; under 90% of that reads "behind".
_PACE_OK = 0.9


def bar_fill(current: int, target: int, width: int = 10) -> str:
    """Just the ``▰▱`` meter glyphs (no numbers) for a running total.

    Raw and unwrapped — the login digest and the ``/bank quests`` table both
    compose this into a code span they build themselves. Callers rendering a
    bare bar must wrap it (``meters.mono``) or it renders proportionally and
    wobbles as it fills.
    """
    return meters.fill(current, target, width)


def progress_bar(
    current: int, target: int, width: int = 10, *, code: bool = True
) -> str:
    """A text meter for a community quest's running total.

    Wrapped in a code span by default so the bar keeps one length as it
    fills. Pass ``code=False`` when the result goes inside a code span the
    caller is already building — backticks do not nest.
    """
    if target <= 0:
        return f"{current:,}"
    rendered = f"{bar_fill(current, target, width)} {current:,}/{target:,}"
    return meters.mono(rendered) if code else rendered


def community_progress_bar(current: int, target: int, width: int = 12) -> str:
    """A community goal's meter, split into the 3 tier regions.

    Divides the ``▰▱`` bar at the 40/70/100% tier thresholds
    (``quest_rules.COMMUNITY_TIERS``) with a ``┃`` divider, so members can
    see which milestone region they're in rather than just a flat fill.
    """
    if target <= 0:
        return f"{current:,}"
    bounds = [0]
    for frac in quest_rules.COMMUNITY_TIERS:
        bounds.append(max(bounds[-1] + 1, round(width * frac)))
    bounds[-1] = width
    filled = max(0, min(width, round(width * current / target)))
    segments = []
    for start, end in zip(bounds, bounds[1:]):
        seg_len = end - start
        seg_filled = max(0, min(seg_len, filled - start))
        segments.append(
            meters.BAR_FILLED * seg_filled
            + meters.BAR_EMPTY * (seg_len - seg_filled)
        )
    bar = "┃".join(segments)
    return meters.mono(f"{bar} {current:,}/{target:,}")


# Shared monospace-table helpers (docs/embed_style_guide.md names them).
# Aliased to the historical private names so the ~20 call sites below and the
# modules that import them from here keep working.
_pad = pad_cell
_rel = rel_ts


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
    # Warm per-kind one-liner (quests.TRIGGER_FLAVOR, falling back to the
    # functional TRIGGER_KINDS label), "" for manual goals — shown next to
    # the title, which stays descriptive on its own.
    kind_flavor: str = ""


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
    # Weekly raffle (sinks round 3, stage 5). raffle_on gates the section;
    # last_winner_id is announced BY NAME (the deliberate anonymous-ticker
    # carve-out — buying a ticket is opting in).
    raffle_on: bool = False
    raffle_tickets: int = 0
    raffle_entrants: int = 0
    last_winner_id: int | None = None
    last_winner_week: str = ""
    # Night at the Tables (casino fancy round): this ISO week's biggest
    # single win (user_id, amount) and best multiplier (user_id, ×100).
    # Winners are NAMED — public play is opting in, the raffle rule.
    casino_biggest: tuple[int, int] | None = None
    casino_luckiest: tuple[int, int] | None = None


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
    # Month clock for monthly guild-wide goals (1st of next month, guild-local).
    first_next_month = date(
        day_obj.year + (1 if day_obj.month == 12 else 0),
        1 if day_obj.month == 12 else day_obj.month + 1,
        1,
    )
    month_end, _ = local_day_bounds(first_next_month.isoformat(), offset)
    days_in_month = (first_next_month - date(day_obj.year, day_obj.month, 1)).days

    settings = load_econ_settings(conn, guild_id)
    this_week = quest_rules.iso_week_for(today)
    raffle_on = bool(settings.raffle_enabled) and settings.price_raffle_ticket > 0
    raffle_tickets = raffle_entrants = 0
    last_winner_id: int | None = None
    last_winner_week = ""
    if raffle_on:
        trow = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS t, COUNT(*) AS e "
            "FROM econ_raffle_tickets "
            "WHERE guild_id = ? AND iso_week = ? AND count > 0",
            (guild_id, this_week),
        ).fetchone()
        raffle_tickets, raffle_entrants = int(trow["t"]), int(trow["e"])
        drow = conn.execute(
            "SELECT iso_week, winner_id FROM econ_raffle_draws "
            "WHERE guild_id = ? AND winner_id IS NOT NULL "
            "ORDER BY drawn_at DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        if drow is not None:
            last_winner_id = int(drow["winner_id"])
            last_winner_week = str(drow["iso_week"])

    from bot_modules.services.casino_service import (  # noqa: PLC0415
        weekly_table_highlights,
    )

    big_row, lucky_row = weekly_table_highlights(conn, guild_id, this_week)
    casino_biggest = (
        (int(big_row["user_id"]), int(big_row["biggest_win"]))
        if big_row is not None
        else None
    )
    casino_luckiest = (
        (int(lucky_row["user_id"]), int(lucky_row["biggest_mult_x100"]))
        if lucky_row is not None
        else None
    )

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
        if row["qtype"] in ("community", "monthly"):
            # Both are guild-wide, tier-settled goals. Monthly runs on the
            # calendar-month clock; community on the ISO-week clock.
            is_monthly = row["qtype"] == "monthly"
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
                if is_monthly:
                    expected = (target or 0) * day_obj.day / days_in_month
                else:
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
                    ends_ts=(month_end if is_monthly else week_end) if auto else None,
                    kind_flavor=(
                        quest_rules.TRIGGER_FLAVOR.get(
                            str(row["trigger_kind"]),
                            quest_rules.TRIGGER_KINDS.get(
                                str(row["trigger_kind"]), ""
                            ),
                        )
                        if auto
                        else ""
                    ),
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
        raffle_on=raffle_on,
        raffle_tickets=raffle_tickets,
        raffle_entrants=raffle_entrants,
        last_winner_id=last_winner_id,
        last_winner_week=last_winner_week,
        casino_biggest=casino_biggest,
        casino_luckiest=casino_luckiest,
    )


def _pulse_lines(data: LeaderboardData, emoji: str, plural: str) -> str:
    """The heartbeat field: label | value rows, columns tab-aligned."""
    p = data.pulse
    rows: list[tuple[str, str, str]] = []  # (icon, label, rich value)
    if p.coins_today > 0:
        rows.append((emoji, "Paid out today", f"**{p.coins_today:,}** {plural}"))
        rows.append(("✅", "Quests done", f"**{p.quests_today}**"))
        rows.append(("👥", "Members earning", f"**{p.earners_today}**"))
    if data.day_roll_ts:
        rows.append(("🕛", "Dailies reset", _rel(data.day_roll_ts)))
    if data.week_roll_ts:
        rows.append(("🕛", "New weeklies", _rel(data.week_roll_ts)))
    lines = []
    if p.coins_today <= 0:
        lines.append("The day is young — nothing banked yet. Be the first!")
    if rows:
        width = max(len(label) for _, label, _ in rows)
        lines.extend(
            f"{icon} `{_pad(label, width)}` {value}" for icon, label, value in rows
        )
    return "\n".join(lines)


# One heading over both the community goals and the per-cadence board.
BOARD_HEADING = "📋 Quest board & community goals"

# Discord caps an embed field value at 1024 chars, and _add_section appends a
# 2-char zero-width spacer to every value. Merging two former sections into one
# field doubles the overrun risk, so blocks pack into "… (cont.)" fields the way
# quest_digest._pack does for the login digest.
_FIELD_LIMIT = EMBED_FIELD_LIMIT
_SPACER_LEN = 2  # "\n​"


def _pack_board(heading: str, blocks: list[str]) -> list[tuple[str, str]]:
    """Group ``blocks`` into (name, value) fields, "… (cont.)" on overflow.

    A single block longer than the limit is passed through rather than cut —
    every block here is already length-bounded by its own builder, so clipping
    mid-goal would lose more than it saves.
    """
    limit = _FIELD_LIMIT - _SPACER_LEN
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for block in blocks:
        added = (2 if current else 0) + len(block)  # 2 = "\n\n" separator
        if current and length + added > limit:
            chunks.append(current)
            current, length, added = [], 0, len(block)
        current.append(block)
        length += added
    if current:
        chunks.append(current)
    return [
        (heading if i == 0 else f"{heading} (cont.)", "\n\n".join(chunk))
        for i, chunk in enumerate(chunks)
    ]


def _community_block(g: CommunityGoal) -> str:
    """One goal's lines: title + flavor, a tier-region bar, then detail."""
    if g.settled:
        state = " — ✅ paid out"
    elif g.completed:
        state = " — 🎉 complete, payout coming"
    else:
        state = ""
    title = (
        f"**{g.title}** — *{g.kind_flavor}*"
        if g.kind_flavor
        else f"**{g.title}**"
    )
    bar = community_progress_bar(g.current, g.target or 0)
    lines = [title, f"{bar}{state}"]
    target = int(g.target or 0)
    if g.auto and target > 0 and not g.settled:
        if not g.completed and g.tiers > 0:
            lines.append(f"🏁 tier {g.tiers}/3 secured")
        detail = []
        if g.contributors > 0:
            detail.append(f"👥 {g.contributors} contributing")
        if g.today_delta:
            detail.append(f"+{g.today_delta:,} today")
        if g.ends_ts and not g.completed:
            detail.append(f"ends {_rel(g.ends_ts)}")
        if detail:
            lines.append(" · ".join(detail))
    return "\n".join(lines)


def _feed_lines(data: LeaderboardData) -> str:
    """Today's anonymous completion feed — titles and counts, never names.

    Title | count | when, with the title column tab-aligned (×1 is always
    printed so the count column lines up too).
    """
    lines = []
    if data.feed:
        width = min(max(len(f.title) for f in data.feed), 20)
        lines = [
            f"✅ `{_pad(f.title, width)}` ×{f.count} · {_rel(f.last_ts)}"
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

    # Titled as *the* economy panel rather than as a board, because since the
    # guide panel folded into this one (2026-08-18) it is the only economy
    # message in the channel — and it sits in the how-it-works channel, where
    # the reader arriving is as likely to be a confused newcomer as someone
    # checking the standings. The ❓ pointer earns its line for that reader:
    # the button is the only remaining route to the guide.
    embed = discord.Embed(
        title=f"{emoji} {plural} — The Bank",
        description=(
            "How it all works, who's earning, and what there is to do — live. "
            "Tap **❓ How it Works** below for the full guide."
            "\n\u200b"
        ),
        color=color,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    # Each section's body is a small table: fixed-width inline-code cells
    # align the columns (see _pad) while emoji, bold, and live timestamps
    # stay outside the backticks where Discord still renders them. Every
    # value (and the description) ends with a zero-width blank line so the
    # next section's heading has breathing room; the last field skips it.
    def _add_section(name: str, value: str) -> None:
        embed.add_field(name=name, value=f"{value}\n\u200b", inline=False)

    _add_section("📡 Today's pulse", _pulse_lines(data, emoji, plural))

    if data.top_earners:
        names = {uid: resolve_name(uid) for uid, _ in data.top_earners}
        name_w = min(max(len(n) for n in names.values()), 16)
        amount_w = max(len(f"{amt:,}") for _, amt in data.top_earners)
        earner_lines = []
        for i, (uid, amount) in enumerate(data.top_earners):
            today = data.today_by_user.get(uid, 0)
            delta = f" (+{today:,} today)" if today > 0 else ""
            earner_lines.append(
                f"{_MEDALS[i]} `{_pad(names[uid], name_w)}` "
                f"{emoji} `{f'{amount:,}'.rjust(amount_w)}`{delta}"
            )
    else:
        earner_lines = ["Nobody has earned yet this week — be the first!"]
    _add_section(
        f"🏆 Top earners (last {ROLLING_DAYS} days)", "\n".join(earner_lines)
    )

    # Community goals and the per-cadence quest board are one section: both
    # answer "what is there to do", and two adjacent headings made the panel
    # read as two competing boards. Goals lead (they're shared and time-boxed),
    # the personal-board summary follows.
    board_blocks: list[str] = []
    if data.community:
        board_blocks.extend(_community_block(g) for g in data.community)

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
        # listing a menu nobody actually has.
        sizes = {
            "daily": settings.quest_board_daily,
            "weekly": settings.quest_board_weekly,
            "monthly": settings.quest_board_monthly,
        }
        label_width = max(len(v) for v in _QTYPE_LABELS.values())
        # cadence | description | payment rows; the first two columns are
        # fixed-width code cells so payments line up down the field.
        rows: list[tuple[str, str, str]] = []
        for qtype, qtype_label in _QTYPE_LABELS.items():
            pool = [q for q in data.quests if q.qtype == qtype]
            if not pool:
                continue
            n = min(sizes.get(qtype, 0), len(pool))
            if n <= 0:
                continue
            lo = min(q.reward for q in pool)
            hi = max(q.reward for q in pool)
            reward = f"{emoji} {lo:,}" + (
                f"–{hi:,}" if hi != lo else ""
            ) + " each"
            rows.append(
                (qtype_label, f"{n} yours · pool {len(pool)}", reward)
            )
        body: list[str] = []
        if rows:
            desc_width = min(max(len(desc) for _, desc, _ in rows), 24)
            # One monospace cell per row (cadence + description together) rather
            # than two adjacent code spans — a single grey box keeps the columns
            # squared up, while the payment stays outside for its emoji + bold.
            body = [
                f"`{_pad(label, label_width)}  {_pad(desc, desc_width)}` {pay}"
                for label, desc, pay in rows
            ]
        if body:
            quest_lines.extend(body)
            quest_lines.append(
                "Boards reshuffle each reset — **Show My Quests** below shows yours."
            )
        board = "\n".join(quest_lines)
        if board:
            board_blocks.append(board)
    if not data.quests:
        board_blocks.append("No quests running right now — check back soon.")

    for name, value in _pack_board(BOARD_HEADING, board_blocks):
        _add_section(name, value)

    _add_section("📰 Live feed — today", _feed_lines(data))

    if data.casino_biggest or data.casino_luckiest:
        table_lines = []
        if data.casino_biggest:
            uid, amount = data.casino_biggest
            table_lines.append(
                f"💥 Biggest win: **{resolve_name(uid)}** — "
                f"{emoji} **{amount:,}** in one play"
            )
        if data.casino_luckiest:
            uid, mult_x100 = data.casino_luckiest
            table_lines.append(
                f"🍀 Luckiest hit: **{resolve_name(uid)}** — "
                f"**{mult_x100 / 100:g}×** their bet"
            )
        table_lines.append("Resets with the week — the tables are waiting.")
        _add_section("🎰 Night at the Tables", "\n".join(table_lines))

    # No trailing "/bank quests … /bank wallet …" explainer: both are buttons
    # under the panel now (QuestBoardView), so the field budget goes to content.
    #
    # That explainer used to be the one field _add_section didn't build, which
    # is what kept the trailing zero-width spacer off the last field. With it
    # gone, whichever section lands last has to shed its own spacer, or the
    # embed ends on a blank line (docs/embed_style_guide.md).
    if embed.fields:
        last = embed.fields[-1]
        embed.set_field_at(
            len(embed.fields) - 1,
            name=last.name,
            value=(last.value or "").removesuffix("\n​"),
            inline=False,
        )

    embed.set_footer(text="⚡ Live — updates within ~2 min of activity")
    embed.timestamp = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    return embed
