"""The moderator stats panel's numbers: today read against its own history.

A sticky panel in a mod channel showing the server's day so far against a band
over the last 8 *matching weekdays*, who was moderating during those same hours,
and where the XP came from over 30 days and over the guild's whole life. See
``docs/mod_stats_panel_spec.md``.

Nearly all of the work is already done by
:func:`~bot_modules.services.activity_graphs.query_activity_overlay`, which owns
the guild-local day boundary, the partial current day, the percentile band and
the "fewer than three comparable days means no band" rule. This module asks it
for a same-weekday band and derives the text block from what comes back.

**The comparison is like against like, twice over.** First by weekday: a
Wednesday is drawn against the last eight Wednesdays, because a server whose
weekend triples its traffic reports a crash every Monday when it is measured
against "the last 8 days". Second by hour: today at 09:00 has lived nine hours
and a day of history has lived twenty-four, so every "usual" figure here sums
the band's median over *only the hours today has actually lived*. The projection
is the one number that deliberately reaches past them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot_modules.services.activity_graphs import (
    MIN_BAND_PERIODS,
    XP_SOURCE_ORDER,
    XP_SOURCE_OTHER,
    OverlayResult,
    overlay_period_start,
    overlay_weekday_name,
    query_activity_overlay,
    query_xp_activity_with_breakdown,
    query_xp_all_time_with_breakdown,
)

#: How many matching weekdays today is drawn against. Eight is two months of
#: Wednesdays — far enough back for a band, near enough that the server it
#: describes is still recognisably this one.
SAME_WEEKDAY_COUNT = 8

#: The recent XP stack's window, in days. Matches the dashboard's own "day"
#: resolution so the panel and the Activity report cannot disagree.
XP_STACK_DAYS = 30


@dataclass(frozen=True)
class Comparison:
    """One "today vs usual" figure, both sides measured over the same hours."""

    today: float
    #: Median of the same hours across the near window. ``None`` when there is
    #: no band to compare against yet.
    typical: float | None

    @property
    def change_pct(self) -> float | None:
        """Percent change against ``typical``.

        ``None`` when there is nothing to divide by — no band yet, or a typical
        of zero. A quiet hour of the night legitimately has a median of 0, and
        "+infinity%" is not a thing to print at 04:00.
        """
        if self.typical is None or self.typical <= 0:
            return None
        return round((self.today - self.typical) / self.typical * 100, 1)


@dataclass(frozen=True)
class ModPresence:
    """Who was moderating, hour by hour, today.

    "Present" means a moderator posted a message **or** left a reaction in that
    hour. A mod reading a channel and reacting is watching it just as much as
    one who talks, and counting only messages would report the quiet half of
    the team as absent.
    """

    #: Distinct mods active in each hour, ``None`` past the hour in progress —
    #: the same shape as the overlay's own current-day series, so the two rows
    #: of the chart stop in the same place.
    by_hour: list[int | None]
    #: Distinct mods active at any point today. **Not** the sum of ``by_hour``:
    #: one mod active at 09:00 and again at 15:00 is one person, not two.
    distinct_today: int
    #: The busiest single hour's count.
    peak: int
    #: False when the guild has no moderator role configured, which is a
    #: different thing from a day on which no moderator showed up.
    configured: bool


@dataclass(frozen=True)
class XpStack:
    """One stacked-bar chart's worth of XP, split by source."""

    labels: list[str]
    by_source: dict[str, list[float]]
    #: ``source -> bucket index`` where that source first paid out.
    starts: dict[str, int] = field(default_factory=dict)

    @property
    def series(self) -> list[tuple[str, list[float]]]:
        """Sources in palette order, everything past the sixth folded into one.

        The shared palette is six slots wide and ``static/js/charts.js`` states
        why there is no seventh: past six, adjacent classes blur whatever hue
        you pick. So the tail is summed into ``"other"`` rather than handed a
        generated colour — which also keeps a source added next year from
        silently taking a slot that already means something else.
        """
        ordered: list[tuple[str, list[float]]] = []
        width = len(self.labels)
        for source in XP_SOURCE_ORDER:
            values = self.by_source.get(source)
            if values and any(values):
                ordered.append((source, values))
        tail = [
            values
            for source, values in self.by_source.items()
            if source not in XP_SOURCE_ORDER and any(values)
        ]
        if tail:
            ordered.append(
                (
                    XP_SOURCE_OTHER,
                    [round(sum(col), 1) for col in zip(*tail)] if width else [],
                )
            )
        return ordered

    @property
    def fold_starts(self) -> dict[str, int]:
        """``starts``, with folded-away sources dropped.

        A dotted rule in a colour that appears nowhere in the legend is a rule
        the reader cannot attribute to anything.
        """
        return {s: at for s, at in self.starts.items() if s in XP_SOURCE_ORDER}


@dataclass(frozen=True)
class ModStatsData:
    """Everything the panel renders, in one pass over the database."""

    #: Today against the last :data:`SAME_WEEKDAY_COUNT` matching weekdays.
    near: OverlayResult
    #: Index of the local hour in progress, 0-23. Also how many hours of the
    #: day the comparisons above are measured over, minus one.
    hour_index: int
    #: The guild-local weekday today falls on, e.g. ``"Tuesday"``.
    weekday: str
    messages: Comparison
    members: Comparison
    #: Where the day lands if the rest of it runs at the near band's median.
    #: ``None`` without a band.
    projected_today: float | None
    #: What a whole matching weekday usually totals.
    typical_day: float | None
    presence: ModPresence
    #: XP by source over the last :data:`XP_STACK_DAYS` days, and over the
    #: guild's whole history a week at a time.
    xp_recent: XpStack
    xp_all_time: XpStack

    @property
    def signature(self) -> tuple[object, ...]:
        """Fingerprint of everything drawn, for the sticky panel's edit gate.

        Deliberately built from the *data* rather than the rendered embed: it
        decides whether an hourly refresh costs an API call and a fresh PNG
        upload, and a panel whose numbers have not moved should cost neither.
        The rendered clock time is excluded for the same reason — re-uploading
        an identical chart to advance a timestamp is the churn this prevents.
        """
        return (
            self.hour_index,
            self.messages.today,
            self.messages.typical,
            self.members.today,
            self.members.typical,
            tuple(self.near.current),
            tuple(self.near.band_mid),
            tuple(self.presence.by_hour),
            tuple(self.xp_recent.labels),
            tuple(
                (source, tuple(values))
                for source, values in self.xp_recent.series
            ),
            tuple(self.xp_all_time.labels),
            tuple(
                (source, tuple(values))
                for source, values in self.xp_all_time.series
            ),
        )


def _sum_lived(values: list[float], hour_index: int) -> float:
    """Sum a band over the hours today has lived, inclusive of the one in progress."""
    return round(sum(values[: hour_index + 1]), 1)


def query_partial_day_members(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    days: int,
    hour_index: int,
    utc_offset_hours: float = 0,
    exclude_user_ids: set[int] | None = None,
    stride_days: int = 1,
) -> tuple[int, list[int]]:
    """Distinct members who spoke by this hour, today and on each prior day.

    Returns ``(today, [prior days, oldest first])``. The overlay cannot answer
    this: it returns per-hour *counts*, and a distinct-member total is not the
    sum of its hours — someone who talks at noon and again at three is one
    member, not two.

    Every day is truncated at the same local hour, so the comparison is like
    for like (see the module docstring). Days with nobody at all are dropped
    rather than counted as zero: a day that predates the archive would
    otherwise drag the median down for a reason that is a fact about when
    logging started, not about the server.

    *stride_days* samples every Nth day back rather than all of them, so a
    same-weekday overlay can take its member median over the same eight
    Wednesdays the band was built from. Reach back ``days`` and step
    ``stride_days``: the caller asking for 8 Wednesdays passes ``days=56,
    stride_days=7``. Left at 1 this walks every day, as it always did.
    """
    now = datetime.now(timezone.utc)
    current_start = overlay_period_start(now, utc_offset_hours, "day")
    since_ts = current_start - days * 86400
    since_i = int(since_ts)

    elapsed = f"(CAST(created_at AS INTEGER) - {since_i})"
    params: list[object] = [guild_id, since_ts]
    where = f"guild_id = ? AND created_at >= ? AND ({elapsed} % 86400) / 3600 <= ?"
    params.append(hour_index)
    if exclude_user_ids:
        ph = ",".join("?" * len(exclude_user_ids))
        where += f" AND user_id NOT IN ({ph})"
        params.extend(sorted(exclude_user_ids))

    rows = conn.execute(
        f"""
        SELECT CAST({elapsed} / 86400 AS INTEGER) AS didx,
               COUNT(DISTINCT user_id) AS members
        FROM processed_messages
        WHERE {where}
        GROUP BY didx
        """,
        params,
    ).fetchall()

    counts = {int(didx): int(members) for didx, members in rows}
    today = counts.get(days, 0)
    prior = [counts[i] for i in range(0, days, stride_days) if counts.get(i)]
    return today, prior


def query_mod_presence_by_hour(
    conn: sqlite3.Connection,
    guild_id: int,
    mod_ids: set[int] | None,
    *,
    hour_index: int,
    utc_offset_hours: float = 0,
) -> ModPresence:
    """Distinct moderators active in each hour of today.

    Active means *posted or reacted*: the two tables are ``UNION``-ed on
    ``(hour, user)`` so a mod who both talks and reacts inside one hour counts
    once, and a mod who only reacts still counts.

    Hours the day has not reached come back as ``None`` rather than 0, so the
    chart stops at the live edge instead of drawing a cliff down to the floor
    across hours nobody has lived yet — the same convention
    :func:`query_activity_overlay` uses for its own current-day series.

    An unconfigured moderator role returns all-``None`` with
    ``configured=False``. That is deliberately distinguishable from a day with
    no moderator activity: "nobody was watching" and "we were never told who
    the moderators are" want different responses from whoever reads the panel.
    """
    if not mod_ids:
        return ModPresence(
            by_hour=[None] * 24, distinct_today=0, peak=0, configured=False
        )

    now = datetime.now(timezone.utc)
    start = overlay_period_start(now, utc_offset_hours, "day")
    ids = sorted(mod_ids)
    placeholders = ",".join("?" * len(ids))

    rows = conn.execute(
        f"""
        SELECT DISTINCT CAST((created_at - ?) / 3600 AS INTEGER) AS hour,
               user_id AS actor
        FROM processed_messages
        WHERE guild_id = ? AND created_at >= ? AND user_id IN ({placeholders})
        UNION
        SELECT DISTINCT CAST((ts - ?) / 3600 AS INTEGER) AS hour,
               reactor_id AS actor
        FROM reaction_log
        WHERE guild_id = ? AND ts >= ? AND reactor_id IN ({placeholders})
        """,
        [start, guild_id, start, *ids, start, guild_id, start, *ids],
    ).fetchall()

    per_hour: dict[int, set[int]] = {}
    everyone: set[int] = set()
    for hour, actor in rows:
        hour = int(hour)
        if 0 <= hour <= 23:
            per_hour.setdefault(hour, set()).add(int(actor))
            everyone.add(int(actor))

    by_hour: list[int | None] = [
        len(per_hour.get(hour, ())) if hour <= hour_index else None
        for hour in range(24)
    ]
    return ModPresence(
        by_hour=by_hour,
        distinct_today=len(everyone),
        peak=max((len(v) for v in per_hour.values()), default=0),
        configured=True,
    )


def _median(values: list[int]) -> float | None:
    """Median of *values*, or None when there is nothing to take one of.

    Written out rather than taken from ``statistics`` so it matches the
    overlay's own ``_percentile`` at q=0.5 — the band's median and this one are
    read side by side, and two definitions of "middle" that disagree on an even
    sample would show up as the two halves of the panel contradicting each other.
    """
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def build_mod_stats(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    utc_offset_hours: float = 0,
    exclude_user_ids: set[int] | None = None,
    mod_ids: set[int] | None = None,
) -> ModStatsData:
    """Assemble the panel: the weekday overlay, mod presence, and two XP stacks."""
    now = datetime.now(timezone.utc)
    current_start = overlay_period_start(now, utc_offset_hours, "day")
    # The same arithmetic query_activity_overlay uses for its own live edge, so
    # the text block and the end of the drawn line can never disagree about
    # which hour the day has reached.
    hour_index = min(23, max(0, int((now.timestamp() - current_start) // 3600)))

    near = query_activity_overlay(
        conn,
        guild_id,
        "day",
        mode="messages",
        compare_periods=SAME_WEEKDAY_COUNT,
        same_weekday=True,
        exclude_user_ids=exclude_user_ids,
        utc_offset_hours=utc_offset_hours,
    )

    messages_today = round(sum(v for v in near.current if v is not None), 1)
    typical_so_far = _sum_lived(near.band_mid, hour_index) if near.has_band else None

    # Stride a week, matching the overlay's own same-weekday sampling: the
    # member median has to be taken over the same days the band was, or the two
    # halves of the panel are comparing today with two different pasts.
    members_today, members_prior = query_partial_day_members(
        conn,
        guild_id,
        days=SAME_WEEKDAY_COUNT * 7,
        hour_index=hour_index,
        utc_offset_hours=utc_offset_hours,
        exclude_user_ids=exclude_user_ids,
        stride_days=7,
    )

    if near.has_band:
        typical_day = round(sum(near.band_mid), 1)
        # The hours still to come, valued at what they usually hold. Anchored on
        # what today has actually done rather than scaled up from it, so a busy
        # morning does not multiply itself across a quiet night.
        remaining = round(sum(near.band_mid[hour_index + 1 :]), 1)
        projected_today = round(messages_today + remaining, 1)
    else:
        typical_day = None
        projected_today = None

    presence = query_mod_presence_by_hour(
        conn,
        guild_id,
        mod_ids,
        hour_index=hour_index,
        utc_offset_hours=utc_offset_hours,
    )

    recent_labels, _totals, _members, recent_sources = (
        query_xp_activity_with_breakdown(
            conn,
            guild_id,
            "day",
            exclude_user_ids=exclude_user_ids,
            utc_offset_hours=utc_offset_hours,
        )
    )
    all_labels, _all_totals, all_sources, all_starts = (
        query_xp_all_time_with_breakdown(
            conn, guild_id, exclude_user_ids=exclude_user_ids
        )
    )

    return ModStatsData(
        near=near,
        hour_index=hour_index,
        weekday=overlay_weekday_name(now, utc_offset_hours),
        messages=Comparison(today=messages_today, typical=typical_so_far),
        members=Comparison(
            today=float(members_today), typical=_median(members_prior)
        ),
        projected_today=projected_today,
        typical_day=typical_day,
        presence=presence,
        xp_recent=XpStack(labels=recent_labels, by_source=recent_sources),
        xp_all_time=XpStack(
            labels=all_labels, by_source=all_sources, starts=all_starts
        ),
    )


# ---------------------------------------------------------------------------
# The text block under the charts
# ---------------------------------------------------------------------------

#: Column widths for the monospace rows. One inline-code cell per row with the
#: label and the number inside it, per docs/embed_style_guide.md — the arrow and
#: its percentage stay outside, where bold still renders.
_LABEL_W = 16
_VALUE_W = 7


def _row(label: str, value: str, payload: str = "") -> str:
    cell = f"`{label.ljust(_LABEL_W)}{value.rjust(_VALUE_W)}`"
    return f"{cell} {payload}".rstrip()


def _delta(comparison: Comparison) -> str:
    """The ``▲ **12%**`` half of a row, or nothing when there is no baseline."""
    change = comparison.change_pct
    if change is None:
        return ""
    if change >= 0:
        return f"▲ **{change:g}%**"
    # The minus sign lives in the arrow, not the number: "▼ **-3%**" reads as a
    # double negative.
    return f"▼ **{abs(change):g}%**"


def render_stats_lines(data: ModStatsData) -> str:
    """The volume-and-pace block, as embed markdown."""
    rows = [
        _row("Messages today", f"{data.messages.today:,.0f}", _delta(data.messages)),
        _row("Members talking", f"{data.members.today:,.0f}", _delta(data.members)),
    ]
    if data.projected_today is not None and data.typical_day is not None:
        rows.append(
            _row(
                "On track for",
                f"~{data.projected_today:,.0f}",
                f"usual {data.typical_day:,.0f}",
            )
        )
    if data.presence.configured:
        # The count never stands on its own: a bare "3" invites the reader to
        # decide for themselves whether three is a lot, and the peak is the
        # denominator that stops them. Per the house rule the Contributors
        # panel and attention_report.py already follow.
        rows.append(
            _row(
                "Mods around",
                f"{data.presence.distinct_today:,.0f}",
                f"peak {data.presence.peak:,.0f} in an hour",
            )
        )
    if not data.near.has_band:
        # Said once, plainly. Every row above is missing its right-hand half in
        # this state, and three blank columns with no explanation read as a bug.
        rows.append("")
        rows.append(
            f"No comparison yet — needs {MIN_BAND_PERIODS} past "
            f"{data.weekday}s of history."
        )
    return "\n".join(rows)


def render_description(data: ModStatsData) -> str:
    """The one line above the charts, naming what is being compared."""
    hour = f"{data.hour_index:02d}:00"
    return (
        f"{data.weekday} to {hour}, against the last "
        f"{SAME_WEEKDAY_COUNT} {data.weekday}s."
    )
