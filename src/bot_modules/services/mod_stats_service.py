"""The moderator stats panel's numbers: today read against its own history.

A sticky panel in a mod channel showing the server's day so far against a band
over the previous 8 days, the same day against a band over the previous 30, and
three figures underneath. See ``docs/mod_stats_panel_spec.md``.

Nearly all of the work is already done by
:func:`~bot_modules.services.activity_graphs.query_activity_overlay`, which owns
the guild-local day boundary, the partial current day, the percentile band and
the "fewer than three comparable days means no band" rule. This module runs it
twice and derives the text block from what comes back.

**The comparisons are part-day against part-day.** Today at 09:00 has lived nine
hours; a full day of history has lived twenty-four. Comparing the two would
report a collapse in activity every morning and a recovery every evening, both
of them artefacts of the clock. So every "usual" figure here sums the band's
median over *only the hours today has actually lived*, and the projection is the
one number that deliberately reaches past them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from bot_modules.services.activity_graphs import (
    MIN_BAND_PERIODS,
    OverlayResult,
    overlay_period_start,
    overlay_weekday_name,
    query_activity_overlay,
)

#: The two histories today is drawn against. Eight days is "the last week and a
#: bit" — close enough to feel like now, wide enough for a band. Thirty is the
#: month, which necessarily mixes weekends into a weekday and so reads wider;
#: that spread is the honest answer, and having both is what makes it legible.
NEAR_WINDOW_DAYS = 8
FAR_WINDOW_DAYS = 30


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
class ModStatsData:
    """Everything the panel renders, in one pass over the database."""

    #: Today against the previous 8 days, and against the previous 30.
    near: OverlayResult
    far: OverlayResult
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
    #: What a whole day usually totals, for the projection to be read against.
    typical_day: float | None

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
            tuple(self.far.band_mid),
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
    prior = [counts[i] for i in range(days) if counts.get(i)]
    return today, prior


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
) -> ModStatsData:
    """Assemble the panel: two overlays and the figures underneath them."""
    now = datetime.now(timezone.utc)
    current_start = overlay_period_start(now, utc_offset_hours, "day")
    # The same arithmetic query_activity_overlay uses for its own live edge, so
    # the text block and the end of the drawn line can never disagree about
    # which hour the day has reached.
    hour_index = min(23, max(0, int((now.timestamp() - current_start) // 3600)))

    def _overlay(periods: int) -> OverlayResult:
        return query_activity_overlay(
            conn,
            guild_id,
            "day",
            mode="messages",
            compare_periods=periods,
            exclude_user_ids=exclude_user_ids,
            utc_offset_hours=utc_offset_hours,
        )

    near = _overlay(NEAR_WINDOW_DAYS)
    far = _overlay(FAR_WINDOW_DAYS)

    messages_today = round(sum(v for v in near.current if v is not None), 1)
    typical_so_far = _sum_lived(near.band_mid, hour_index) if near.has_band else None

    members_today, members_prior = query_partial_day_members(
        conn,
        guild_id,
        days=NEAR_WINDOW_DAYS,
        hour_index=hour_index,
        utc_offset_hours=utc_offset_hours,
        exclude_user_ids=exclude_user_ids,
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

    return ModStatsData(
        near=near,
        far=far,
        hour_index=hour_index,
        weekday=overlay_weekday_name(now, utc_offset_hours),
        messages=Comparison(today=messages_today, typical=typical_so_far),
        members=Comparison(
            today=float(members_today), typical=_median(members_prior)
        ),
        projected_today=projected_today,
        typical_day=typical_day,
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
    if not data.near.has_band:
        # Said once, plainly. Every row above is missing its right-hand half in
        # this state, and three blank columns with no explanation read as a bug.
        rows.append("")
        rows.append(
            f"No comparison yet — needs {MIN_BAND_PERIODS} past days of history."
        )
    return "\n".join(rows)


def render_description(data: ModStatsData) -> str:
    """The one line above the charts, naming what is being compared."""
    hour = f"{data.hour_index:02d}:00"
    return (
        f"{data.weekday} to {hour}, against the last {NEAR_WINDOW_DAYS} days "
        f"and the last {FAR_WINDOW_DAYS}."
    )
