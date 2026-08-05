"""Tests for services/pools_metrics.py — the bettable-metric roster.

The properties worth pinning here are the ones that make a metric safe to
bet on at all, and they are exactly the ones a plausible-looking refactor
would quietly drop:

* **Per-member caps bind on the member, not the total.** A cap applied
  after summing looks identical on a normal day and does nothing at all on
  the day someone farms it. ``test_cap_binds_per_member`` is written to
  fail against that mistake.
* **A zero day holds a metric out of the draw.** Zero means the feature
  behind the metric was dormant, and a line drawn across dormancy prices
  whether the bot ran rather than how members behaved.
* **Interior gaps are real zeros, leading ones are not.** Zero-filling a
  metric before its feature shipped would invent history it never had.
"""

from __future__ import annotations

import random

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services import pools_metrics as pm
from bot_modules.services.pools_logic import HISTORY_DAYS, DayMetric
from tests.db_template import migrated_db

GUILD = 830
TZ = -7.0
DAY_ONE = "2026-07-01"


def _epoch(day: str, hour: float = 12.0) -> float:
    from bot_modules.economy.logic import local_day_bounds

    return local_day_bounds(day, TZ)[0] + hour * 3600


def _days(start: str, count: int) -> list[str]:
    from datetime import date, timedelta

    first = date.fromisoformat(start)
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _post(conn, day: str, author: int, n: int, **cols) -> None:
    """``n`` messages from one member on one guild-local day."""
    extra = {"media_kind": None, "emotion": None, **cols}
    for i in range(n):
        conn.execute(
            "INSERT INTO messages (message_id, guild_id, channel_id, "
            "author_id, ts, media_kind, emotion) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                hash((day, author, i)) & 0x7FFFFFFF, GUILD, 1, author,
                _epoch(day) + i, extra["media_kind"], extra["emotion"],
            ),
        )


def _now_after(day: str) -> float:
    return _epoch(day, 30.0)  # the following morning


# ── caps ───────────────────────────────────────────────────────────────


def test_cap_binds_per_member(db):
    """One member farming cannot outrun the cap.

    Written to fail against a cap applied to the day's total instead of to
    each member: with the cap at 30, five ordinary members and one farmer
    posting 500 must land on 5*10 + 30, not on 30 and not on 550.
    """
    with open_db(db) as conn:
        for member in range(5):
            _post(conn, DAY_ONE, 100 + member, 10)
        _post(conn, DAY_ONE, 999, 500)
        days = pm.SPECS["messages"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert [d.net for d in days] == [5 * 10 + 30]
    # …and the contributor count is people, not messages.
    assert days[0].volume == 6


def test_distinct_poster_metric_counts_each_member_once(db):
    with open_db(db) as conn:
        _post(conn, DAY_ONE, 501, 1)
        _post(conn, DAY_ONE, 502, 400)
        days = pm.SPECS["posters"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert [d.net for d in days] == [2]


@pytest.mark.parametrize(
    ("key", "cols", "cap"),
    [
        pytest.param("messages", {}, 30, id="messages"),
        pytest.param("media", {"media_kind": "image"}, 10, id="media"),
        pytest.param("joy", {"emotion": "joy"}, 20, id="joy"),
    ],
)
def test_every_message_metric_caps(db, key, cols, cap):
    with open_db(db) as conn:
        _post(conn, DAY_ONE, 601, cap + 50, **cols)
        days = pm.SPECS[key].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert [d.net for d in days] == [cap]


def test_media_and_joy_metrics_ignore_plain_messages(db):
    """The filters are real filters — a plain message is in neither."""
    with open_db(db) as conn:
        _post(conn, DAY_ONE, 701, 5)
        media = pm.SPECS["media"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
        joy = pm.SPECS["joy"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert media == []
    assert joy == []


def test_handle_metric_excludes_pools_own_stakes(db):
    """Money staked into the market must not move the market.

    The same exclusion the economy metric makes, for the same reason: bet
    under, stake heavily, and an unfiltered handle metric would drag the
    number being settled on.
    """
    import json

    with open_db(db) as conn:
        for game, amount in (("slots", -300), ("pools", -9000)):
            conn.execute(
                "INSERT INTO econ_ledger (guild_id, user_id, amount, kind, "
                "meta, created_at) VALUES (?, ?, ?, 'casino_stake', ?, ?)",
                (GUILD, 55, amount, json.dumps({"game": game}), _epoch(DAY_ONE)),
            )
        days = pm.SPECS["handle"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert [d.net for d in days] == [300]


def test_xp_metric_is_a_whole_number(db):
    """The +0.5 line only makes a hit impossible against integers."""
    with open_db(db) as conn:
        for amount in (1.4, 2.3, 0.5):
            conn.execute(
                "INSERT INTO xp_events (guild_id, user_id, source, amount, "
                "created_at) VALUES (?, ?, 'msg', ?, ?)",
                (GUILD, 77, amount, _epoch(DAY_ONE)),
            )
        days = pm.SPECS["xp"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(DAY_ONE)
        )
    assert [d.net for d in days] == [4]
    assert all(isinstance(d.net, int) for d in days)


# ── zero-filling ───────────────────────────────────────────────────────


def test_interior_gaps_are_zeros_not_missing_days(db):
    """A day nobody posted measured zero; it did not fail to measure."""
    first, _skipped, third = _days(DAY_ONE, 3)
    with open_db(db) as conn:
        _post(conn, first, 801, 3)
        _post(conn, third, 801, 4)
        days = pm.SPECS["messages"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(third)
        )
    assert [(d.day, d.net) for d in days] == [
        (first, 3), (_skipped, 0), (third, 4),
    ]


def test_no_leading_zeros_before_the_first_observed_day(db):
    """A metric whose feature shipped last week has a week of history, not
    the whole lookback window's worth of invented zeros."""
    late = _days(DAY_ONE, 40)[-1]
    with open_db(db) as conn:
        _post(conn, late, 802, 5)
        days = pm.SPECS["messages"].series(
            conn, GUILD, tz_offset_hours=TZ, now=_now_after(late)
        )
    assert [d.day for d in days] == [late]


# ── eligibility ────────────────────────────────────────────────────────


def _series(values: list[int]) -> list[DayMetric]:
    return [
        DayMetric(
            day=day, mint=0, burn=0, hold=0, net=v,
            open=0, high=max(v, 0), low=min(v, 0), close=v, volume=1,
        )
        for day, v in zip(_days(DAY_ONE, len(values)), values, strict=True)
    ]


def test_metric_sits_out_without_enough_history():
    spec = pm.SPECS["messages"]
    assert pm.line_for(spec, _series([50] * (HISTORY_DAYS - 1))) is None
    assert pm.line_for(spec, _series([50] * HISTORY_DAYS)) == 50.5


def test_a_zero_in_the_window_holds_a_count_metric_out():
    """A dormant feature is not a market. This is what keeps QOTD answers
    out of the roster while questions are posted only some days."""
    spec = pm.SPECS["qotd"]
    assert pm.line_for(spec, _series([20] * HISTORY_DAYS)) == 20.5
    assert pm.line_for(spec, _series([20, 20, 0, 20, 20, 20, 20])) is None


def test_the_economy_metric_is_exempt_from_the_zero_rule():
    """A net change of zero is a real reading of a busy day, not a silent
    one — the ledger had rows, they just balanced."""
    assert pm.line_for(pm.SPECS[pm.ANCHOR], _series([0] * HISTORY_DAYS)) == 0.5


def test_a_negative_day_holds_a_count_metric_out_too():
    """Counts cannot go negative, so one that did is a broken reading."""
    assert pm.line_for(
        pm.SPECS["messages"], _series([50, 50, -3, 50, 50, 50, 50])
    ) is None


# ── the draw ───────────────────────────────────────────────────────────


def test_draw_never_repeats_yesterday():
    rng = random.Random(7)
    roster = ["a", "b", "c"]
    for _ in range(200):
        assert pm.choose_metric(roster, "b", rng) != "b"


def test_draw_falls_back_to_a_repeat_rather_than_no_market():
    """One enabled metric should still run a market every day."""
    assert pm.choose_metric(["only"], "only", random.Random(1)) == "only"


def test_draw_returns_none_when_nothing_is_eligible():
    assert pm.choose_metric([], None, random.Random(1)) is None


def test_draw_covers_the_whole_roster():
    """Uniform, not merely non-repeating: every metric must be reachable."""
    rng = random.Random(3)
    seen = {pm.choose_metric(list(pm.ALL_KEYS), None, rng) for _ in range(400)}
    assert seen == set(pm.ALL_KEYS)


# ── the roster itself ──────────────────────────────────────────────────


def test_enabled_keys_defaults_to_the_whole_roster():
    assert pm.enabled_keys("") == pm.ALL_KEYS
    assert pm.enabled_keys("   ") == pm.ALL_KEYS


def test_enabled_keys_drops_unknown_keys_rather_than_raising():
    """Config outlives code: a key retired in a later build must not stop
    the market opening."""
    assert pm.enabled_keys("messages,not_a_metric") == ("messages",)


def test_spec_for_unknown_key_is_none_not_a_guess():
    """Settlement recomputes from the spec, so guessing one would settle a
    round against a metric nobody bet on."""
    assert pm.spec_for("not_a_metric") is None


def test_every_spec_is_self_describing():
    """A metric with no question or label reaches members as a blank card."""
    for key, spec in pm.SPECS.items():
        assert spec.key == key, "the registry key must match the spec's own"
        assert spec.label and spec.question and spec.chart_label
        assert "{line}" in spec.question
        assert spec.chart_kind in ("candles", "bars")


def test_capped_metrics_say_so_on_the_card():
    """The cap is the manipulation promise — if it is not stated where
    members bet, the promise is invisible to the people relying on it."""
    for spec in pm.SPECS.values():
        if spec.key == pm.ANCHOR:
            continue
        assert spec.cap_note, f"{spec.key} caps silently"
