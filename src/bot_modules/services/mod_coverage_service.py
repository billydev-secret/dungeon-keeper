"""Moderator coverage: is a mod around when the server is busy?

Two questions, deliberately answered from two different windows, because one
window cannot answer both honestly.

*Today* — the hero chart. Today's hour-by-hour message count drawn against a
percentile band over recent same-weekdays, with the moderators' own line over
the top. This is :func:`~bot_modules.services.activity_graphs.query_activity_overlay`
run twice, once server-wide and once narrowed to the mod group.

*The pattern* — the coverage gaps. The overlay returns **percentiles**, which
cannot say "a mod was present on 3 of the last 26 Tuesdays": a median is not a
head-count. And gaps computed from today alone are meaningless at 02:00, when
one hour has been lived. So the gap section runs its own rolling window over
every recent day and asks, per hour of the local clock, *on what share of the
days that this hour had traffic was a moderator also talking?*

"Moderator" here is **anyone who can delete someone else's message** —
Discord's Manage Messages, which Administrator grants implicitly. That is the
practical floor of "someone is watching this channel", and it is a wider circle
than the kick/ban/manage-guild set the Mod Workload report counts. The two
reports answer different questions and are not expected to agree.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import TypedDict

from bot_modules.core.bot_exclusion import bot_filter_clause, bot_ids_subquery
from bot_modules.services.activity_graphs import (
    OVERLAY_SAME_WEEKDAY_MAX,
    overlay_labels,
    overlay_weekday_name,
    query_activity_overlay,
)

# How far back the gap section looks. Every day, not every same-weekday: the
# hero already carries the weekday-aware comparison, and the rota question
# ("who covers 3am") is asked of the week as a whole.
GAP_WINDOW_DAYS = 28

# Below this share of days, an hour is a gap rather than covered. A moderator
# who shows up in a given hour on fewer than half the days is not coverage
# anyone can plan around — the point of the number is whether a member arriving
# at that hour can expect to find someone, and a coin flip is a no.
COVERED_THRESHOLD_PCT = 50.0

# The busy hours the coverage percentage is reported against: the top quarter
# of the clock by volume, so six hours out of twenty-four.
BUSY_QUARTILE = 4


class CoverageHour(TypedDict):
    hour: int
    label: str
    server_messages: int
    days_observed: int
    days_with_mod: int
    coverage_pct: float
    busy: bool
    gap: bool


class LongestGap(TypedDict):
    start_hour: int
    end_hour: int
    hours: int


def _local_bucket_exprs(utc_offset_hours: float) -> tuple[str, str]:
    """SQL for the guild-local day index and hour-of-day of ``created_at``.

    The offset is folded into the timestamp *before* the division, which is the
    whole trick: once shifted, integer division by 86400 and 3600 lands each
    row in its local day and local hour with no further timezone arithmetic.
    Doing it the other way round — bucketing in UTC and adjusting after — is
    where the classic off-by-one-hour bug in this shape of query lives.
    """
    offset_secs = int(utc_offset_hours * 3600)
    shifted = f"(CAST(created_at AS INTEGER) + {offset_secs})"
    return f"({shifted} / 86400)", f"(({shifted} / 3600) % 24)"


def _gap_rows(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    mod_ids: list[int],
    utc_offset_hours: float,
    gap_days: int,
    now: float,
) -> list[CoverageHour]:
    """Per hour of the local clock: server traffic, and mod presence in it."""
    day_expr, hour_expr = _local_bucket_exprs(utc_offset_hours)
    since = now - gap_days * 86400
    bot_clause, bot_params = bot_filter_clause(guild_id, column="user_id")

    # Denominator and volume in one pass. ``days_observed`` counts only days on
    # which this hour actually had traffic: an hour nobody spoke in is not an
    # hour a moderator failed to cover, and folding those days into the
    # denominator would read every quiet stretch as a staffing failure.
    server = {
        int(h): (int(msgs), int(days))
        for h, msgs, days in conn.execute(
            f"""
            SELECT {hour_expr} AS h,
                   COUNT(*) AS msgs,
                   COUNT(DISTINCT {day_expr}) AS days
            FROM processed_messages
            WHERE guild_id = ? AND created_at >= ?{bot_clause}
            GROUP BY h
            """,
            (guild_id, since, *bot_params),
        ).fetchall()
    }

    # Numerator: distinct local days on which *some* moderator spoke in this
    # hour. Distinct days, not messages — one mod saying one thing is presence,
    # and a mod who posts forty times in an hour is not forty times covered.
    mod_days: dict[int, int] = {}
    if mod_ids:
        ph = ",".join("?" * len(mod_ids))
        mod_days = {
            int(h): int(days)
            for h, days in conn.execute(
                f"""
                SELECT h, COUNT(*) AS days FROM (
                    SELECT DISTINCT {hour_expr} AS h, {day_expr} AS d
                    FROM processed_messages
                    WHERE guild_id = ? AND created_at >= ?
                      AND user_id IN ({ph})
                ) GROUP BY h
                """,
                (guild_id, since, *sorted(mod_ids)),
            ).fetchall()
        }

    labels = overlay_labels("day")
    volumes = [server.get(h, (0, 0))[0] for h in range(24)]
    # The busy threshold is a quartile of the hours that actually saw traffic.
    # Ranking all 24 would let a run of dead overnight hours pull the cut-off
    # down until a merely-average hour counted as a peak.
    live = sorted((v for v in volumes if v > 0), reverse=True)
    busy_cut = live[max(0, len(live) // BUSY_QUARTILE - 1)] if live else 0

    rows: list[CoverageHour] = []
    for h in range(24):
        msgs, days = server.get(h, (0, 0))
        with_mod = min(mod_days.get(h, 0), days)
        pct = round(with_mod / days * 100, 1) if days else 0.0
        rows.append(
            CoverageHour(
                hour=h,
                label=labels[h],
                server_messages=msgs,
                days_observed=days,
                days_with_mod=with_mod,
                coverage_pct=pct,
                busy=bool(msgs) and msgs >= busy_cut,
                # An hour with no traffic at all is not a gap. Nothing happened
                # in it, so there was nothing to miss.
                gap=bool(days) and pct < COVERED_THRESHOLD_PCT,
            )
        )
    return rows


def _longest_gap(rows: list[CoverageHour]) -> LongestGap | None:
    """The longest unbroken stretch of gap hours, wrapping past midnight.

    The wrap is the point: a server whose moderators are asleep from 22:00 to
    04:00 has one six-hour hole, and a straight scan of hours 0..23 would
    report it as two unremarkable ones at either end of the day.
    """
    if not any(r["gap"] for r in rows):
        return None
    if all(r["gap"] for r in rows):
        return LongestGap(start_hour=0, end_hour=23, hours=24)

    best_start, best_len = 0, 0
    run_start, run_len = None, 0
    # Twice round the clock, so a run that straddles midnight is seen whole.
    for i in range(48):
        h = i % 24
        if rows[h]["gap"]:
            if run_start is None:
                run_start = h
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_start, run_len = None, 0
    best_len = min(best_len, 24)
    return LongestGap(
        start_hour=best_start,
        end_hour=(best_start + best_len - 1) % 24,
        hours=best_len,
    )


def compute_mod_coverage(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    mod_ids: list[int] | None = None,
    utc_offset_hours: float = 0.0,
    compare_periods: int = OVERLAY_SAME_WEEKDAY_MAX,
    gap_days: int = GAP_WINDOW_DAYS,
    now: float | None = None,
) -> dict:
    """Hero overlay plus the coverage-gap summary.

    With no moderators to look at, every field still comes back in its empty
    shape rather than the payload short-circuiting: the panel draws the server
    side regardless, and a report that renders half a chart is more use to a
    reader than one that renders an error. The caller is responsible for not
    *caching* an empty ``mod_ids`` — a gateway member cache that has not
    chunked yet reports no moderators at all, and fifteen minutes of "nobody
    covers anything" served as fact is the failure mode this guards against
    (see ``_cache_unless_degraded``).
    """
    now = now if now is not None else time.time()
    mods = sorted(set(mod_ids or ()))

    # The overlay takes an exclusion *set*, not the shared AND-clause, so the
    # bot ids are materialised here. Same source as bot_filter_clause: the
    # known_users table, which still holds bots that have left the guild where
    # a live member scan would not.
    bot_ids = {
        int(r[0])
        for r in conn.execute(bot_ids_subquery(), (guild_id,)).fetchall()
    }

    server = query_activity_overlay(
        conn,
        guild_id,
        "day",
        mode="messages",
        compare_periods=compare_periods,
        same_weekday=True,
        exclude_user_ids=bot_ids or None,
        utc_offset_hours=utc_offset_hours,
    )
    # Only when there is somebody to draw. An empty include-set applies no
    # filter at all, which would draw the server's own line a second time and
    # label it the moderators'.
    mod_line: list[float | None] = [None] * 24
    if mods:
        mod_line = query_activity_overlay(
            conn,
            guild_id,
            "day",
            mode="messages",
            compare_periods=compare_periods,
            same_weekday=True,
            include_user_ids=set(mods),
            utc_offset_hours=utc_offset_hours,
        ).current

    rows = _gap_rows(
        conn,
        guild_id,
        mod_ids=mods,
        utc_offset_hours=utc_offset_hours,
        gap_days=gap_days,
        now=now,
    )

    gaps = [r for r in rows if r["gap"]]
    busy = [r for r in rows if r["busy"]]
    busiest_uncovered = (
        max(gaps, key=lambda r: r["server_messages"]) if gaps else None
    )
    peak_coverage_pct = (
        round(sum(r["coverage_pct"] for r in busy) / len(busy), 1) if busy else 0.0
    )

    weekday = overlay_weekday_name(
        datetime.fromtimestamp(now, timezone.utc), utc_offset_hours
    )

    return {
        "labels": overlay_labels("day"),
        "tz_label": f"UTC{utc_offset_hours:+g}" if utc_offset_hours else "UTC",
        "weekday": weekday,
        "band_label": f"Typical {weekday}",
        "periods_sampled": server.periods_sampled,
        "server_current": server.current,
        "band_low": server.band_low,
        "band_mid": server.band_mid,
        "band_high": server.band_high,
        "mod_current": mod_line,
        "mod_count": len(mods),
        "gap_days": gap_days,
        "covered_threshold_pct": COVERED_THRESHOLD_PCT,
        "hours": rows,
        "busiest_uncovered": busiest_uncovered,
        "longest_gap": _longest_gap(rows),
        "peak_coverage_pct": peak_coverage_pct,
        "busy_hours": len(busy),
        "busy_hours_covered": sum(1 for r in busy if not r["gap"]),
    }
