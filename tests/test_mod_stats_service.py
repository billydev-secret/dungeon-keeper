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
    SAME_WEEKDAY_COUNT,
    Comparison,
    ModPresence,
    ModStatsData,
    XpStack,
    _median,
    build_mod_stats,
    query_mod_presence_by_hour,
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
    _seed_full_days(db_conn, tz, [7, 14, 21, 28])
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
    _seed_full_days(db_conn, 0.0, [7 * n for n in range(1, MIN_BAND_PERIODS)])
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
    _seed_full_days(db_conn, 0.0, [7, 14, 21])
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
        db_conn, GUILD, days=SAME_WEEKDAY_COUNT, hour_index=_local_hour(0.0)
    )

    assert sorted(prior) == [2, 4, 6]
    assert _median(prior) == 4.0


def test_member_counts_truncate_every_day_at_the_same_hour(db_conn):
    """A prior day's evening crowd must not be counted against today's morning."""
    _seed(db_conn, 0.0, days_back=1, hour=2, users=(1, 2))
    _seed(db_conn, 0.0, days_back=1, hour=10, users=(3, 4, 5))
    _seed(db_conn, 0.0, days_back=0, hour=2, users=(1,))

    today, prior = query_partial_day_members(
        db_conn, GUILD, days=SAME_WEEKDAY_COUNT, hour_index=5
    )

    assert today == 1
    assert prior == [2]  # the three who arrived at 10:00 are still to come


def test_excluded_users_are_left_out_of_both_halves(db_conn):
    """Bots are excluded by default, and must vanish from the members figure as
    well as the message counts — a bot posting hourly would otherwise be a
    member who never sleeps."""
    _seed_full_days(db_conn, 0.0, [7, 14, 21], users=(100, 999))
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
        hour_index=9,
        weekday="Tuesday",
        messages=Comparison(today=1204, typical=1075.0),
        members=Comparison(today=87, typical=90.0),
        projected_today=1650.0,
        typical_day=1480.0,
        presence=ModPresence(
            by_hour=[None] * 24, distinct_today=0, peak=0, configured=False
        ),
        xp_recent=XpStack(labels=[], by_source={}),
        xp_all_time=XpStack(labels=[], by_source={}),
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


# ── the same-weekday band ────────────────────────────────────────────────


def _seed_matching_weekdays(conn, tz, *, per_hour=1, users=(100,)):
    """One message per user per hour on each of the last 8 matching weekdays."""
    for week in range(1, SAME_WEEKDAY_COUNT + 1):
        for hour in range(24):
            _seed(
                conn, tz, days_back=week * 7, hour=hour, users=users,
                per_user=per_hour,
            )


def _seed_other_weekdays(conn, tz, *, per_hour, users=(100,)):
    """Loud traffic on every day that is *not* a matching weekday."""
    for days_back in range(1, SAME_WEEKDAY_COUNT * 7 + 1):
        if days_back % 7 == 0:
            continue
        for hour in range(24):
            _seed(
                conn, tz, days_back=days_back, hour=hour, users=users,
                per_user=per_hour,
            )


def test_band_is_built_from_matching_weekdays_not_the_last_n_days(db_conn):
    """The reason the panel moved to weekdays at all.

    A server whose weekend triples its traffic reports a crash every Monday
    when today is measured against "the last 8 days". Here the eight matching
    weekdays run at 1 message an hour and every other day runs at 50; a band
    built from the wrong days would put "usual" fifty times too high.
    """
    tz = 0.0
    _seed_matching_weekdays(db_conn, tz, per_hour=1)
    _seed_other_weekdays(db_conn, tz, per_hour=50)
    for hour in range(_local_hour(tz) + 1):
        _seed(db_conn, tz, days_back=0, hour=hour)

    data = build_mod_stats(db_conn, GUILD, utc_offset_hours=tz)

    lived = _local_hour(tz) + 1
    assert data.messages.typical == pytest.approx(lived)


def test_member_median_walks_the_same_days_the_band_did(db_conn):
    """Both halves of the panel have to compare today with the *same* past.

    ``query_partial_day_members`` reaches back 56 days for an 8-Wednesday band,
    so without a stride it would take its median over all 56 rather than the 8
    the band was built from — and the two figures would disagree for a reason
    no reader could see.
    """
    tz = 0.0
    hour = _local_hour(tz)
    # Matching weekdays: one member each. Every other day: five.
    for week in range(1, SAME_WEEKDAY_COUNT + 1):
        _seed(db_conn, tz, days_back=week * 7, hour=0, users=(100,))
    for days_back in range(1, SAME_WEEKDAY_COUNT * 7 + 1):
        if days_back % 7:
            _seed(db_conn, tz, days_back=days_back, hour=0,
                  users=(200, 201, 202, 203, 204))

    _today, prior = query_partial_day_members(
        db_conn, GUILD,
        days=SAME_WEEKDAY_COUNT * 7, hour_index=hour,
        utc_offset_hours=tz, stride_days=7,
    )

    assert prior == [1] * SAME_WEEKDAY_COUNT
    assert _median(prior) == 1.0


# ── mod presence ─────────────────────────────────────────────────────────


def _seed_reaction(conn, tz, *, days_back, hour, reactor):
    start = overlay_period_start(datetime.now(timezone.utc), tz, "day")
    ts = start - days_back * _DAY + hour * 3600 + 1800
    conn.execute(
        "INSERT INTO reaction_log "
        "(guild_id, reactor_id, author_id, channel_id, message_id, ts)"
        " VALUES (?,?,?,?,?,?)",
        (GUILD, reactor, 999, 7, next(_ids), ts),
    )


def test_presence_counts_a_mod_who_only_reacted(db_conn):
    """The reason presence is not just "posted".

    A moderator reading a channel and reacting is watching it. Counting only
    messages reports the quiet half of a mod team as absent.
    """
    tz = 0.0
    _seed_reaction(db_conn, tz, days_back=0, hour=0, reactor=500)

    presence = query_mod_presence_by_hour(
        db_conn, GUILD, {500}, hour_index=_local_hour(tz), utc_offset_hours=tz
    )

    assert presence.by_hour[0] == 1
    assert presence.configured is True


def test_presence_counts_one_mod_once_per_hour(db_conn):
    """Posting *and* reacting in the same hour is one person, not two."""
    tz = 0.0
    _seed(db_conn, tz, days_back=0, hour=0, users=(500,))
    _seed_reaction(db_conn, tz, days_back=0, hour=0, reactor=500)

    presence = query_mod_presence_by_hour(
        db_conn, GUILD, {500}, hour_index=_local_hour(tz), utc_offset_hours=tz
    )

    assert presence.by_hour[0] == 1
    assert presence.peak == 1


def test_presence_distinct_today_is_not_the_sum_of_its_hours(db_conn):
    """One mod around at 00:00 and again at 02:00 is one mod, not two."""
    tz = 0.0
    if _local_hour(tz) < 2:
        pytest.skip("day has not reached 02:00 locally yet")
    _seed(db_conn, tz, days_back=0, hour=0, users=(500,))
    _seed(db_conn, tz, days_back=0, hour=2, users=(500,))

    presence = query_mod_presence_by_hour(
        db_conn, GUILD, {500}, hour_index=_local_hour(tz), utc_offset_hours=tz
    )

    assert sum(v for v in presence.by_hour if v) == 2
    assert presence.distinct_today == 1


def test_presence_ignores_members_who_are_not_mods(db_conn):
    tz = 0.0
    _seed(db_conn, tz, days_back=0, hour=0, users=(600,))
    _seed_reaction(db_conn, tz, days_back=0, hour=0, reactor=601)

    presence = query_mod_presence_by_hour(
        db_conn, GUILD, {500}, hour_index=_local_hour(tz), utc_offset_hours=tz
    )

    assert presence.distinct_today == 0
    assert presence.peak == 0


def test_presence_stops_at_the_live_edge(db_conn):
    """Hours nobody has lived are None, not a zero the chart would draw."""
    tz = 0.0
    hour = _local_hour(tz)
    presence = query_mod_presence_by_hour(
        db_conn, GUILD, {500}, hour_index=hour, utc_offset_hours=tz
    )

    assert all(v is not None for v in presence.by_hour[: hour + 1])
    assert all(v is None for v in presence.by_hour[hour + 1 :])
    # ...and the last of the drawn hours is the one still being lived, so the
    # chart can mark it rather than let four quiet minutes read as an
    # unwatched hour.
    assert presence.partial_index == hour


def test_no_mod_role_is_distinguishable_from_nobody_watching(db_conn):
    """"We were never told who the mods are" and "no mod showed up" want
    different responses from whoever reads the panel."""
    presence = query_mod_presence_by_hour(
        db_conn, GUILD, set(), hour_index=9, utc_offset_hours=0.0
    )

    assert presence.configured is False
    assert presence.by_hour == [None] * 24
    # Nothing is drawn, so there is no live edge to mark either.
    assert presence.partial_index is None


def test_stats_lines_show_the_peak_beside_the_mod_count():
    """A bare "6" invites the reader to decide for themselves whether six is a
    lot. The house rule is that the count comes with its denominator."""
    lines = render_stats_lines(
        _data(
            presence=ModPresence(
                by_hour=[1] * 24, distinct_today=6, peak=5, configured=True
            )
        )
    ).splitlines()

    assert lines[3] == "`Mods around           6` peak 5 in an hour"


def test_stats_lines_omit_the_mod_row_when_no_role_is_configured():
    assert not any("Mods around" in line for line in render_stats_lines(_data()).splitlines())


# ── the XP stacks ────────────────────────────────────────────────────────


def test_xp_stack_orders_by_the_palette_and_folds_the_tail():
    """Six slots and no seventh: static/js/charts.js states the rule, and a
    source with no slot folds into "Other" rather than taking a generated hue."""
    stack = XpStack(
        labels=["a", "b"],
        by_source={
            "grant": [1.0, 1.0],
            "reply": [2.0, 2.0],
            "text": [3.0, 3.0],
            "some_future_source": [4.0, 4.0],
        },
    )

    assert stack.series == [
        ("text", [3.0, 3.0]),
        ("reply", [2.0, 2.0]),
        ("other", [5.0, 5.0]),
    ]


def test_xp_stack_drops_sources_that_never_paid_out():
    stack = XpStack(labels=["a"], by_source={"text": [1.0], "voice": [0.0]})

    assert [source for source, _ in stack.series] == ["text"]


def test_fold_starts_drops_rules_for_folded_sources():
    """A dotted rule in a colour that appears nowhere in the legend is a rule
    the reader cannot attribute to anything."""
    stack = XpStack(
        labels=["a", "b"],
        by_source={"text": [1.0, 1.0], "grant": [0.0, 1.0]},
        starts={"text": 0, "grant": 1},
    )

    assert stack.fold_starts == {"text": 0}
