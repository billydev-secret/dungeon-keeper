"""Pools — the roster of bettable metrics, and the daily rotation draw.

See docs/plans/pools-metric-rotation.md. Until 2026-08-03 the market bet a
single hardcoded metric (the day's net change in the economy); this module
turns that into a roster the round rotates through, one metric drawn per
guild-local day.

**Manipulation resistance is still the design axis.** The original metric
was safe because pools' own stakes are excluded from it, so betting cannot
move the thing being bet on. A member-countable metric has no such
structural defence — "messages sent today" is farmable by anyone willing
to type — so every count metric here carries a **per-member cap** applied
inside its own series query. The cap is what makes the metric bettable:
uncapped, one member has already been observed posting 432 messages in a
day, so a determined bettor's contribution would be invisible against
normal behaviour. Capped at 30, their whole ceiling is under 3% of a
~1,190 line, which costs more to coordinate than a bet can return.

**Caps are constants, not dials.** A cap lives in code rather than in
guild config because changing one retroactively changes what every past
day measured — a round opened under one cap could settle under another.
Same reasoning that freezes the line onto the round row.

**Zero days are real zeros.** A count metric with no rows for a day had
none of that activity, not missing data, so the series zero-fills across
interior gaps. A metric whose trailing window contains a zero is then held
out of the draw entirely (``line_for``): a zero means the underlying
feature was dormant, and a line drawn across dormancy prices "did the bot
run today", not "how did members behave". That rule is what keeps QOTD
answers out of the roster at the current posting cadence — a market on a
question a mod has not posted yet is a market that mod can already call.

**Never delete a spec.** The outcome of a settled round is recomputed from
its metric's series, so a round row naming a key this module no longer
knows cannot be settled at all — ``plan_tick`` voids and refunds it rather
than guessing. Retire a metric by disabling it in guild config, which
stops it being drawn while leaving history settleable.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from typing import NamedTuple

from bot_modules.services.pools_logic import HISTORY_DAYS, DayMetric, derive_line

# How far back a count metric reads. Comfortably over HISTORY_DAYS plus the
# chart's window, and short enough that every query stays index-covered on
# the guild+timestamp indexes rather than scanning the table.
LOOKBACK_DAYS = 60

# The incumbent metric — the one the market bet before rotation existed,
# and the key migration 148 backfills onto every pre-rotation round.
ANCHOR = "economy_net"


class MetricSpec(NamedTuple):
    """One bettable metric: how to measure it, and how to say it out loud.

    ``series`` returns completed-day values oldest first, with the day's
    value in ``DayMetric.net``. Count metrics fill the candlestick fields
    as ``open=0, close=value`` so ``body == value`` — which is what lets
    ``derive_line``, ``median_band`` and the chart's bar/volume panels work
    on every metric without a branch. ``volume`` carries the number of
    distinct members who contributed, which is the one thing that panel
    never had a real meaning for before.
    """

    key: str
    label: str
    # Member-facing, formatted with {line}, {day} and {currency}.
    question: str
    series: Callable[..., list[DayMetric]]
    # What the settled number is measured in, for the result card.
    unit: str = ""
    # Most values are counts and read better unsigned; the economy's net
    # change is a delta and wants its sign.
    signed: bool = False
    # Candlesticks only make sense for a cumulative level. Everything else
    # is a daily count and draws as bars.
    chart_kind: str = "bars"
    chart_label: str = ""
    # Stated on the panel where members bet — the cap IS the manipulation
    # promise, so it belongs next to the buttons, not in the manual.
    cap_note: str = ""


def _zero_filled(
    rows: Sequence[tuple[str, float, int]],
) -> list[tuple[str, float, int]]:
    """Fill interior day gaps with zeros.

    Only *interior* gaps: the range runs from the first observed day to the
    last, so a metric whose feature shipped a fortnight ago does not get an
    invented year of leading zeros.
    """
    if not rows:
        return []
    by_day = {day: (value, members) for day, value, members in rows}
    cur = date.fromisoformat(min(by_day))
    end = date.fromisoformat(max(by_day))
    out: list[tuple[str, float, int]] = []
    while cur <= end:
        key = cur.isoformat()
        value, members = by_day.get(key, (0.0, 0))
        out.append((key, value, members))
        cur += timedelta(days=1)
    return out


def _count_days(
    rows: Sequence[tuple[str, float, int]], limit_days: int | None
) -> list[DayMetric]:
    """(day, value, contributors) rows -> the shared DayMetric shape."""
    days = [
        DayMetric(
            day=day, mint=0, burn=0, hold=0, net=round(value),
            open=0, high=max(round(value), 0), low=min(round(value), 0),
            close=round(value), volume=int(members),
        )
        for day, value, members in _zero_filled(rows)
    ]
    return days[-limit_days:] if limit_days is not None else days


def _cutoff(now: float) -> float:
    return now - LOOKBACK_DAYS * 86400


def _message_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
    cap: int,
    where: str = "",
) -> list[DayMetric]:
    """Messages per guild-local day, each member counted at most ``cap``.

    The inner aggregate is per member per day and the outer one caps before
    summing, so the cap binds on the member rather than on the total. The
    WHERE clause stays on indexed columns (``idx_messages_guild_ts``); the
    day expression only appears in SELECT/GROUP BY, so the index still
    drives the scan.
    """
    rows = conn.execute(
        f"""
        SELECT d, SUM(MIN(n, :cap)), COUNT(*) FROM (
            SELECT date(ts + :off, 'unixepoch') AS d,
                   author_id, COUNT(*) AS n
            FROM messages
            WHERE guild_id = :g AND ts >= :cut {where}
            GROUP BY d, author_id
        ) GROUP BY d
        """,
        {
            "cap": cap, "off": tz_offset_hours * 3600,
            "g": guild_id, "cut": _cutoff(now),
        },
    ).fetchall()
    return _count_days([(str(r[0]), float(r[1]), int(r[2])) for r in rows], limit_days)


def _distinct_message_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
) -> list[DayMetric]:
    """How many distinct members posted. Needs no cap — one member can move
    it by exactly one, so shifting it means recruiting other humans."""
    rows = conn.execute(
        """
        SELECT date(ts + :off, 'unixepoch') AS d, COUNT(DISTINCT author_id)
        FROM messages WHERE guild_id = :g AND ts >= :cut GROUP BY d
        """,
        {"off": tz_offset_hours * 3600, "g": guild_id, "cut": _cutoff(now)},
    ).fetchall()
    return _count_days(
        [(str(r[0]), float(r[1]), int(r[1])) for r in rows], limit_days
    )


def _reaction_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
    cap: int | None,
) -> list[DayMetric]:
    """Reactions added, capped per member — or distinct reactors when
    ``cap`` is None. A reaction is the cheapest action on the server, so
    the uncapped count is the most farmable number available."""
    if cap is None:
        rows = conn.execute(
            """
            SELECT date(ts + :off, 'unixepoch') AS d, COUNT(DISTINCT reactor_id)
            FROM reaction_log WHERE guild_id = :g AND ts >= :cut GROUP BY d
            """,
            {"off": tz_offset_hours * 3600, "g": guild_id, "cut": _cutoff(now)},
        ).fetchall()
        return _count_days(
            [(str(r[0]), float(r[1]), int(r[1])) for r in rows], limit_days
        )
    rows = conn.execute(
        """
        SELECT d, SUM(MIN(n, :cap)), COUNT(*) FROM (
            SELECT date(ts + :off, 'unixepoch') AS d,
                   reactor_id, COUNT(*) AS n
            FROM reaction_log
            WHERE guild_id = :g AND ts >= :cut
            GROUP BY d, reactor_id
        ) GROUP BY d
        """,
        {
            "cap": cap, "off": tz_offset_hours * 3600,
            "g": guild_id, "cut": _cutoff(now),
        },
    ).fetchall()
    return _count_days([(str(r[0]), float(r[1]), int(r[2])) for r in rows], limit_days)


def _xp_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
    cap: int,
) -> list[DayMetric]:
    """XP earned, capped per member.

    ``xp_events.amount`` is a float, so the per-day total is rounded to a
    whole number before it reaches the line: the +0.5 offset that makes an
    exact hit impossible only works against integers.
    """
    rows = conn.execute(
        """
        SELECT d, SUM(MIN(n, :cap)), COUNT(*) FROM (
            SELECT date(created_at + :off, 'unixepoch') AS d,
                   user_id, SUM(amount) AS n
            FROM xp_events
            WHERE guild_id = :g AND created_at >= :cut
            GROUP BY d, user_id
        ) GROUP BY d
        """,
        {
            "cap": cap, "off": tz_offset_hours * 3600,
            "g": guild_id, "cut": _cutoff(now),
        },
    ).fetchall()
    return _count_days([(str(r[0]), float(r[1]), int(r[2])) for r in rows], limit_days)


def _kind_activity_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
    kind: str,
    cap: int,
) -> list[DayMetric]:
    """A bot-event count off ``econ_kind_activity``.

    That table already stores per member per guild-local day, so the cap is
    a plain MIN and no timezone arithmetic is needed — the day column was
    written with the guild's offset when the event happened.

    Bot-event metrics are the least manipulable volume metrics on the
    roster: a member can change *who* catches a cat, but the spawn rate is
    the bot's, so the day's ceiling is not theirs to raise.
    """
    from bot_modules.economy.logic import local_day_for  # noqa: PLC0415

    first_day = local_day_for(_cutoff(now), tz_offset_hours)
    rows = conn.execute(
        """
        SELECT local_day, SUM(MIN(count, :cap)), COUNT(*)
        FROM econ_kind_activity
        WHERE guild_id = :g AND kind = :k AND local_day >= :cut
        GROUP BY local_day
        """,
        {"cap": cap, "g": guild_id, "k": kind, "cut": first_day},
    ).fetchall()
    return _count_days([(str(r[0]), float(r[1]), int(r[2])) for r in rows], limit_days)


def _handle_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,
    limit_days: int | None = None,
    cap: int,
) -> list[DayMetric]:
    """Petals staked across the casino, excluding pools' own stakes.

    The exclusion is the same one the economy metric makes and for the same
    reason: without it, money staked *into* the market would move the
    number the market is settling on.

    Filtered in Python rather than with a LIKE on the JSON, so it matches
    ``pools_service._row_meta``'s parse rather than a second, subtly
    different expression of "is this row pools".

    The per-member cap is deliberately independent of the admin-tunable
    ``daily_wager_cap``: an admin turning that cap off must not silently
    turn off this metric's manipulation guard too.
    """
    from bot_modules.economy.logic import local_day_for  # noqa: PLC0415
    from bot_modules.services.casino_service import POOLS_TABLES  # noqa: PLC0415

    per_member: dict[tuple[str, int], int] = {}
    for user_id, amount, meta_raw, created_at in conn.execute(
        "SELECT user_id, amount, meta, created_at FROM econ_ledger "
        "WHERE guild_id = ? AND kind = 'casino_stake' AND created_at >= ?",
        (guild_id, _cutoff(now)),
    ):
        game = None
        if meta_raw:
            try:
                parsed = json.loads(meta_raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                game = parsed.get("game")
        if game == POOLS_TABLES.game:
            continue
        day = local_day_for(float(created_at), tz_offset_hours)
        key = (day, int(user_id))
        per_member[key] = per_member.get(key, 0) + int(-amount)

    totals: dict[str, list[int]] = {}
    for (day, _user), staked in per_member.items():
        bucket = totals.setdefault(day, [0, 0])
        bucket[0] += min(staked, cap)
        bucket[1] += 1
    rows = [(day, float(v[0]), v[1]) for day, v in totals.items()]
    return _count_days(sorted(rows), limit_days)


def _economy_series(
    conn: sqlite3.Connection,
    guild_id: int,
    *,
    tz_offset_hours: float,
    now: float,  # noqa: ARG001 - the ledger series is always read whole
    limit_days: int | None = None,
) -> list[DayMetric]:
    """The incumbent metric, delegated to its owner.

    Imported inside the function because ``pools_service`` imports this
    module for the registry — the ledger logic stays where it lives rather
    than moving here just to satisfy import order.
    """
    from bot_modules.services.pools_service import daily_series  # noqa: PLC0415

    return daily_series(
        conn, guild_id, tz_offset_hours=tz_offset_hours, limit_days=limit_days
    )


# The roster. Order is display order on the dashboard; the draw is random.
SPECS: dict[str, MetricSpec] = {
    ANCHOR: MetricSpec(
        key=ANCHOR,
        label="Economy net change",
        question=(
            "Will the economy grow by more than **{line}** {currency} "
            "today ({day})?"
        ),
        series=_economy_series,
        unit="{currency}",
        signed=True,
        chart_kind="candles",
        chart_label="Net change",
    ),
    "messages": MetricSpec(
        key="messages",
        label="Messages sent",
        question="Will the server send more than **{line}** messages today ({day})?",
        series=lambda *a, **kw: _message_series(*a, cap=30, **kw),
        unit="messages",
        chart_label="Messages",
        cap_note="Counts at most 30 messages per person.",
    ),
    "posters": MetricSpec(
        key="posters",
        label="Members who posted",
        question="Will more than **{line}** people post today ({day})?",
        series=_distinct_message_series,
        unit="members",
        chart_label="Members posting",
        cap_note="Everyone counts once, however much they post.",
    ),
    "media": MetricSpec(
        key="media",
        label="Media posted",
        question=(
            "Will the server post more than **{line}** images, GIFs and "
            "clips today ({day})?"
        ),
        series=lambda *a, **kw: _message_series(
            *a, cap=10, where="AND media_kind IS NOT NULL", **kw
        ),
        unit="posts",
        chart_label="Media posts",
        cap_note="Counts at most 10 posts per person.",
    ),
    "joy": MetricSpec(
        key="joy",
        label="Happy messages",
        question="Will more than **{line}** messages read as happy today ({day})?",
        series=lambda *a, **kw: _message_series(
            *a, cap=20, where="AND emotion IN ('joy', 'playful')", **kw
        ),
        unit="messages",
        chart_label="Happy messages",
        cap_note="Counts at most 20 messages per person.",
    ),
    "reactions": MetricSpec(
        key="reactions",
        label="Reactions added",
        question="Will the server add more than **{line}** reactions today ({day})?",
        series=lambda *a, **kw: _reaction_series(*a, cap=20, **kw),
        unit="reactions",
        chart_label="Reactions",
        cap_note="Counts at most 20 reactions per person.",
    ),
    "reactors": MetricSpec(
        key="reactors",
        label="Members who reacted",
        question=(
            "Will more than **{line}** people react to something today ({day})?"
        ),
        series=lambda *a, **kw: _reaction_series(*a, cap=None, **kw),
        unit="members",
        chart_label="Members reacting",
        cap_note="Everyone counts once, however much they react.",
    ),
    "xp": MetricSpec(
        key="xp",
        label="XP earned",
        question="Will the server earn more than **{line}** XP today ({day})?",
        series=lambda *a, **kw: _xp_series(*a, cap=100, **kw),
        unit="XP",
        chart_label="XP",
        cap_note="Counts at most 100 XP per person.",
    ),
    "cats": MetricSpec(
        key="cats",
        label="Cats caught",
        question="Will more than **{line}** cats be caught today ({day})?",
        series=lambda *a, **kw: _kind_activity_series(
            *a, kind="cat_catch", cap=10, **kw
        ),
        unit="cats",
        chart_label="Cats caught",
        cap_note="Counts at most 10 catches per person.",
    ),
    "qotd": MetricSpec(
        key="qotd",
        label="Question-of-the-day answers",
        question=(
            "Will more than **{line}** people answer the question of the "
            "day ({day})?"
        ),
        series=lambda *a, **kw: _kind_activity_series(
            *a, kind="qotd_reply", cap=5, **kw
        ),
        unit="answers",
        chart_label="QOTD answers",
        cap_note="Counts at most 5 answers per person.",
    ),
    "handle": MetricSpec(
        key="handle",
        label="Casino handle",
        question=(
            "Will more than **{line}** {currency} be staked across the "
            "casino today ({day})?"
        ),
        series=lambda *a, **kw: _handle_series(*a, cap=1000, **kw),
        unit="{currency}",
        chart_label="Staked",
        cap_note=(
            "Counts at most 1,000 per person, and never counts stakes on "
            "this market."
        ),
    ),
}

ALL_KEYS: tuple[str, ...] = tuple(SPECS)


def spec_for(key: str) -> MetricSpec | None:
    """The spec a stored round names, or None if this build has never heard
    of it. Callers must treat None as unsettleable rather than guessing —
    the outcome is recomputed from the spec's own series."""
    return SPECS.get(key)


def enabled_keys(raw: str) -> tuple[str, ...]:
    """Parse the guild's stored roster. Empty/unset means the whole roster.

    Unknown keys are dropped rather than raising: config outlives code, and
    a key removed from a future build must not stop the market opening.
    """
    if not raw.strip():
        return ALL_KEYS
    chosen = {part.strip() for part in raw.split(",") if part.strip()}
    return tuple(key for key in ALL_KEYS if key in chosen)


def line_for(spec: MetricSpec, days: list[DayMetric]) -> float | None:
    """This metric's line off completed days, or None to sit today out.

    Two ways to be ineligible: not enough history for a median at all, and
    — for count metrics only — a zero anywhere in the trailing window. A
    zero day means the underlying activity did not happen, which nearly
    always means the feature behind it was dormant; a line drawn across
    dormancy prices whether the bot ran, not how members behaved. The
    economy metric is exempt because a net change of zero is a real
    reading of a busy day, not a silent one.
    """
    values = [d.net for d in days]
    if len(values) < HISTORY_DAYS:
        return None
    if spec.key != ANCHOR and any(v <= 0 for v in values[-HISTORY_DAYS:]):
        return None
    return derive_line(values)


def choose_metric(
    eligible: Sequence[str], previous: str | None, rng: random.Random
) -> str | None:
    """Draw tomorrow's metric: uniform over ``eligible``, never yesterday's.

    Falling back to the repeat when excluding yesterday empties the set is
    deliberate — one enabled metric should still run a market every day
    rather than opening one every other day.
    """
    if not eligible:
        return None
    fresh = [key for key in eligible if key != previous]
    return rng.choice(fresh or list(eligible))
