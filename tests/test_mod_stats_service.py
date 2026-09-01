"""Tests for services/mod_stats_service.py — the moderator stats panel's numbers.

The panel's whole claim is that today is being compared with something
comparable, so most of what is worth testing here is the part-day arithmetic:
a day nine hours old measured against nine hours of history, not twenty-four.
Getting that wrong doesn't crash anything — it just reports a collapse in
activity every morning — so it is pinned rather than trusted.

Wall-clock time is not frozen. Instead every expectation is derived from the
same local-hour index the code reads, and the fixtures seed *every* hour of
each comparison day, so the assertions hold whatever hour the suite runs at.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.activity_graphs import (
    MIN_BAND_PERIODS,
    OverlayResult,
    overlay_period_start,
)
from bot_modules.services.mod_stats_service import (
    NEAR_WINDOW_DAYS,
    Comparison,
    ModStatsData,
    _median,
    build_mod_stats,
    query_partial_day_members,
    render_stats_lines,
)
from tests.db_template import migrated_db

GUILD = 10
_DAY = 86400
_ids = itertools.count(1)


@pytest.fixture
def db_conn(tmp_path):
    path = tmp_path / "modstats.db"
    migrated_db(path)
    with open_db(path) as conn:
        yield conn


def _local_hour(tz: float) -> int:
    """The hour index the code under test will compute for right now."""
    now = datetime.now(timezone.utc)
    start = overlay_period_start(now, tz, "day")
    return min(23, max(0, int((now.timestamp() - start) // 3600)))


def _seed(conn, tz, *, days_back, hour, users=(100,), per_user=1):
    """Put messages in one local hour of the local day *days_back* ago."""
    start = overlay_period_start(datetime.now(timezone.utc), tz, "day")
    ts = start - days_back * _DAY + hour * 3600 + 1800
    for user in users:
        for _ in range(per_user):
            conn.execute(
                "INSERT INTO processed_messages "
                "(guild_id, message_id, channel_id, user_id, created_at, processed_at)"
                " VALUES (?,?,?,?,?,?)",
                (GUILD, next(_ids), 7, user, ts, ts),
            )


def _seed_full_days(conn, tz, days, *, users=(100,)):
    """One message per user in every hour of each of *days* past local days.

    Seeding all 24 hours is what makes the part-day assertions independent of
    the clock: the band's median is then 1 per hour, so "usual so far" is
    exactly the number of hours today has lived.
    """
    for days_back in days:
        for hour in range(24):
            _seed(conn, tz, days_back=days_back, hour=hour, users=users)


# ── the part-day comparison ──────────────────────────────────────────────


@pytest.mark.parametrize("tz", [0.0, -7.0], ids=["utc", "utc-7"])
def test_today_is_compared_over_only_the_hours_it_has_lived(db_conn, tz):
    """The bug this exists to prevent: comparing a part-day to a whole one.

    Four past days holding one message an hour make the band's median 1 in
    every hour. At 09:00 the honest "usual" is 9, not 24 — and reading 24 would
    report the server as 60% down every single morning.
    """
    _seed_full_days(db_conn, tz, [1, 2, 3, 4])
    _seed(db_conn, tz, days_back=0, hour=0, per_user=2)

    data = build_mod_stats(db_conn, GUILD, utc_offset_hours=tz)
    hour = _local_hour(tz)

    assert data.hour_index == hour
    assert data.messages.today == 2
    assert data.messages.typical == hour + 1
    # The projection is the one figure that deliberately reaches past now: what
    # today has done, plus what the hours still to come usually hold.
    assert data.typical_day == 24
    assert data.projected_today == 2 + (23 - hour)


def test_no_band_leaves_every_comparison_empty(db_conn):
    """Two days of history cannot make a percentile band, and must not pretend.

    A p25/p75 over two days is just the two days, so the overlay suppresses the
    band — and every "vs usual" figure here has to go with it rather than
    silently comparing against nothing.
    """
    _seed_full_days(db_conn, 0.0, list(range(1, MIN_BAND_PERIODS)))
    _seed(db_conn, 0.0, days_back=0, hour=0)

    data = build_mod_stats(db_conn, GUILD, utc_offset_hours=0.0)

    assert not data.near.has_band
    assert data.messages.typical is None
    assert data.messages.change_pct is None
    assert data.projected_today is None
    assert data.typical_day is None
    assert "No comparison yet" in render_stats_lines(data)


@pytest.mark.parametrize(
    ("typical", "expected"),
    [
        pytest.param(None, None, id="no-band"),
        # 04:00 on a quiet server genuinely has a median of 0, and "+inf%" is
        # not a thing to print on a panel.
        pytest.param(0.0, None, id="zero-median"),
        pytest.param(50.0, 20.0, id="up"),
        pytest.param(200.0, -70.0, id="down"),
    ],
)
def test_change_pct(typical, expected):
    assert Comparison(today=60.0, typical=typical).change_pct == expected


# ── members ──────────────────────────────────────────────────────────────


def test_members_counts_people_not_messages(db_conn):
    """Someone who talks all morning is one member, which is why this cannot be
    read off the overlay's per-hour counts."""
    _seed_full_days(db_conn, 0.0, [1, 2, 3])
    _seed(db_conn, 0.0, days_back=0, hour=0, users=(100, 101), per_user=5)

    data = build_mod_stats(db_conn, GUILD, utc_offset_hours=0.0)

    assert data.members.today == 2
    assert data.messages.today == 10


def test_member_median_ignores_days_that_predate_the_archive(db_conn):
    """Asking for 8 days when 3 exist must take the median of the 3.

    Counting the missing five as zero members would halve the baseline for a
    reason that is a fact about when logging started, not about the server.
    """
    for days_back, users in ((1, (1, 2)), (2, (1, 2, 3, 4)), (3, (1, 2, 3, 4, 5, 6))):
        _seed(db_conn, 0.0, days_back=days_back, hour=0, users=users)
    _seed(db_conn, 0.0, days_back=0, hour=0, users=(1,))

    _today, prior = query_partial_day_members(
        db_conn, GUILD, days=NEAR_WINDOW_DAYS, hour_index=_local_hour(0.0)
    )

    assert sorted(prior) == [2, 4, 6]
    assert _median(prior) == 4.0


def test_member_counts_truncate_every_day_at_the_same_hour(db_conn):
    """A prior day's evening crowd must not be counted against today's morning."""
    _seed(db_conn, 0.0, days_back=1, hour=2, users=(1, 2))
    _seed(db_conn, 0.0, days_back=1, hour=10, users=(3, 4, 5))
    _seed(db_conn, 0.0, days_back=0, hour=2, users=(1,))

    today, prior = query_partial_day_members(
        db_conn, GUILD, days=NEAR_WINDOW_DAYS, hour_index=5
    )

    assert today == 1
    assert prior == [2]  # the three who arrived at 10:00 are still to come


def test_excluded_users_are_left_out_of_both_halves(db_conn):
    """Bots are excluded by default, and must vanish from the members figure as
    well as the message counts — a bot posting hourly would otherwise be a
    member who never sleeps."""
    _seed_full_days(db_conn, 0.0, [1, 2, 3], users=(100, 999))
    _seed(db_conn, 0.0, days_back=0, hour=0, users=(100, 999))

    data = build_mod_stats(
        db_conn, GUILD, utc_offset_hours=0.0, exclude_user_ids={999}
    )

    assert data.members.today == 1
    assert data.messages.today == 1
    assert data.members.typical == 1


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param([], None, id="empty"),
        pytest.param([4], 4.0, id="one"),
        pytest.param([1, 9, 5], 5.0, id="odd"),
        pytest.param([2, 4, 6, 8], 5.0, id="even-interpolates"),
    ],
)
def test_median(values, expected):
    assert _median(values) == expected


# ── the text block ───────────────────────────────────────────────────────


def _data(**kwargs) -> ModStatsData:
    empty = OverlayResult(
        labels=[], current=[], band_low=[], band_mid=[1.0],
        band_high=[], periods_requested=8, periods_sampled=8, clamped=False,
    )
    defaults = dict(
        near=empty,
        far=empty,
        hour_index=9,
        weekday="Tuesday",
        messages=Comparison(today=1204, typical=1075.0),
        members=Comparison(today=87, typical=90.0),
        projected_today=1650.0,
        typical_day=1480.0,
    )
    return ModStatsData(**{**defaults, **kwargs})


def test_stats_lines_read_as_a_table():
    lines = render_stats_lines(_data()).splitlines()

    assert lines[0] == "`Messages today    1,204` ▲ **12%**"
    # The minus lives in the arrow, not the number — "▼ **-3.3%**" is a double
    # negative — and the label column is padded so the three rows line up.
    assert lines[1] == "`Members talking      87` ▼ **3.3%**"
    assert lines[2] == "`On track for     ~1,650` usual 1,480"


def test_stats_lines_drop_the_payload_when_there_is_no_baseline():
    """No band means no arrow and no projection, not a bare or invented one."""
    lines = render_stats_lines(
        _data(
            messages=Comparison(today=1204, typical=None),
            members=Comparison(today=87, typical=None),
            projected_today=None,
            typical_day=None,
        )
    ).splitlines()

    assert lines[0] == "`Messages today    1,204`"
    assert not any("On track" in line for line in lines)


def test_signature_moves_with_the_numbers():
    """The sticky panel's edit gate keys off this: two panels that would draw
    identically must share a signature, and any change must break it."""
    assert _data().signature == _data().signature
    assert _data().signature != _data(messages=Comparison(1205, 1075.0)).signature
    assert _data().signature != _data(hour_index=10).signature
