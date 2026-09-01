"""Behavior tests for contributors_service.

The five views replaced the member quality score.  What matters most here is
that each one is measured *relative to its opportunity* — the investigation in
docs/plans/quality-score-revisit.md found that two of the metrics were pure
artifacts of which channel someone posts in, and two more just re-ranked members
by raw activity, until they were adjusted.  Those adjustments are what these
tests pin down.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from bot_modules.services import contributors_service as cs
from bot_modules.services.contributors_service import build_contributors_report
from bot_modules.services.message_store import (
    init_known_users_table,
    init_message_tables,
)

GUILD = 111
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
NOW_TS = NOW.timestamp()
HOUR = 3600.0


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_message_tables(c)
    init_known_users_table(c)
    yield c
    c.close()


@pytest.fixture()
def low_floors(monkeypatch):
    """Shrink the sample floors so fixtures stay legible.

    The production values (15 posts, 8 revival attempts, 150 replies) exist to
    keep noisy small samples off the panel, not to define behavior.
    """
    for name, value in (
        ("MIN_POSTS", 2),
        ("MIN_REVIVAL_ATTEMPTS", 2),
        ("MIN_REPLIES_SENT", 2),
        ("MIN_ACTS_GIVEN", 2),
        ("MIN_CHANNEL_POSTS", 2),
        ("MIN_CHANNEL_ATTEMPTS", 2),
        ("MIN_EXPECTED_REVIVALS", 0.25),
        ("MIN_EXPECTED_NEWCOMER_REPLIES", 0.5),
    ):
        monkeypatch.setattr(cs, name, value)


_mid = iter(range(1, 1_000_000))


def msg(conn, author, hours_ago, *, channel=1, reply_to=None, attachment=False):
    mid = next(_mid)
    conn.execute(
        "INSERT INTO messages (message_id, guild_id, channel_id, author_id, ts,"
        " content, reply_to_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mid, GUILD, channel, author, NOW_TS - hours_ago * HOUR, "hi", reply_to),
    )
    if attachment:
        conn.execute(
            "INSERT INTO message_attachments (message_id, url) VALUES (?, ?)",
            (mid, f"https://x/{mid}"),
        )
    return mid


def react(conn, reactor, author, message_id, hours_ago):
    conn.execute(
        "INSERT OR IGNORE INTO reaction_log (guild_id, reactor_id, author_id,"
        " channel_id, message_id, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (GUILD, reactor, author, 1, message_id, NOW_TS - hours_ago * HOUR),
    )


def known(conn, user_id, *, is_bot=False, current=True):
    conn.execute(
        "INSERT OR REPLACE INTO known_users (guild_id, user_id, username,"
        " display_name, updated_at, is_bot, current_member)"
        " VALUES (?, ?, '', '', 0, ?, ?)",
        (GUILD, user_id, int(is_bot), int(current)),
    )


def run(conn, **kw):
    return build_contributors_report(conn, GUILD, now=NOW, **kw)


def row(entries, uid):
    return next((e for e in entries if e.user_id == uid), None)


def background(conn, channel, *, uids=(10, 11, 12), posts=3, responders=2, hours=60):
    """Other members posting, so the channel has a baseline of its own.

    Every lift is leave-one-out, so a view only scores a member where somebody
    *else* also used that channel.
    """
    for uid in uids:
        for _ in range(posts):
            m = msg(conn, uid, hours, channel=channel)
            for r in range(90, 90 + responders):
                react(conn, r, uid, m, hours - 1)


# ── Popular content ────────────────────────────────────────────────────


def test_popular_is_relative_to_the_channel_not_absolute(conn, low_floors):
    """Fewer responders in a quiet channel can beat more in a busy one.

    This is the correction that dropped the raw leader out of the catalyst top
    twelve: an unadjusted count mostly measures which room someone likes.
    """
    # busy channel 1: baseline ~4 responders/post, set by two other members
    for uid in (10, 11):
        for _ in range(3):
            m = msg(conn, uid, 50, channel=1)
            for r in (90, 91, 92, 93):
                react(conn, r, uid, m, 49)
    # quiet channel 2: baseline ~1 responder/post
    for uid in (12, 13):
        for _ in range(3):
            m = msg(conn, uid, 50, channel=2)
            react(conn, 90, uid, m, 49)

    # A posts in the busy channel and gets 5 — barely above its baseline
    for _ in range(3):
        m = msg(conn, 1, 40, channel=1)
        for r in (90, 91, 92, 93, 94):
            react(conn, r, 1, m, 39)
    # B posts in the quiet channel and gets 3 — triple its baseline
    for _ in range(3):
        m = msg(conn, 2, 40, channel=2)
        for r in (90, 91, 92):
            react(conn, r, 2, m, 39)

    popular = run(conn).popular
    a, b = row(popular, 1), row(popular, 2)
    assert a is not None and b is not None
    assert a.own_rate > b.own_rate, "A drew more responders in absolute terms"
    assert b.score > a.score, "but B outperformed its room by more"


def test_popular_skips_channels_with_no_baseline(conn, low_floors, monkeypatch):
    """A channel below the sample floor earns no baseline, so no lift."""
    monkeypatch.setattr(cs, "MIN_CHANNEL_POSTS", 99)
    for _ in range(3):
        m = msg(conn, 1, 40, channel=7)
        react(conn, 90, 1, m, 39)
    assert run(conn).popular == []


def test_popular_requires_minimum_posts(conn, low_floors):
    background(conn, 1)
    for _ in range(5):
        m = msg(conn, 1, 40, channel=1)
        react(conn, 90, 1, m, 39)
    msg(conn, 2, 40, channel=1)  # one post only — under MIN_POSTS of 2
    popular = run(conn).popular
    assert row(popular, 1) is not None
    assert row(popular, 2) is None


def test_posts_are_starters_or_attachments_not_replies(conn, low_floors):
    """A reply is participation, not a post — it shouldn't dilute the rate."""
    background(conn, 1)
    base = msg(conn, 5, 60, channel=1)
    for _ in range(3):
        msg(conn, 1, 40, channel=1)
        msg(conn, 1, 39, channel=1, reply_to=base)  # replies: not posts
    assert row(run(conn).popular, 1).volume == 3


# ── Conversation catalyst ──────────────────────────────────────────────


def _revival(conn, starter, hours_ago, *, channel, responders, count):
    """A message after a lull, answered by *count* messages from *responders*."""
    msg(conn, 99, hours_ago + 5, channel=channel)  # sets up the lull
    msg(conn, starter, hours_ago, channel=channel)
    for i in range(count):
        msg(conn, responders[i % len(responders)], hours_ago - 0.1, channel=channel)


def test_catalyst_needs_replies_from_multiple_people(conn, low_floors):
    """Three messages from one person is a monologue, not a restarted room."""
    for uid in (30, 31):  # other members set channel 1's base rate
        for h in (300, 290, 280):
            _revival(conn, uid, h, channel=1, responders=[50, 51], count=3)
    for h in (200, 190, 180):
        _revival(conn, 1, h, channel=1, responders=[50, 51], count=3)
    for h in (100, 90, 80):
        _revival(conn, 2, h, channel=1, responders=[50], count=3)

    catalyst = run(conn).catalyst
    assert row(catalyst, 1).own_rate == 1.0
    assert row(catalyst, 2).own_rate == 0.0
    assert row(catalyst, 1).score > row(catalyst, 2).score


def test_catalyst_omits_rooms_nobody_ever_restarts(conn, low_floors):
    """A channel with no successes has no baseline, so no lift is computable.

    Members there are left off the view rather than scored zero — a lift
    against nothing is not a measurement.
    """
    for h in (200, 190, 180):
        _revival(conn, 1, h, channel=3, responders=[50], count=1)
    assert row(run(conn).catalyst, 1) is None


def test_catalyst_ignores_messages_not_after_a_lull(conn, low_floors):
    """Posting into an active conversation is never a revival attempt."""
    for i in range(6):
        msg(conn, 1, 50 - i * 0.1, channel=1)  # dense, no 3h gaps
    assert row(run(conn).catalyst, 1) is None


def test_catalyst_is_relative_to_the_channel(conn, low_floors):
    """Succeeding where nobody else can beats succeeding where everyone does.

    Each member gets their own well-separated block of hours: overlapping
    timestamps make members answer *each other*, which silently turns a
    supposedly hard room into an easy one.
    """
    slot = iter(range(400, 40, -8))  # distinct, >5h apart, so lulls are clean

    for uid in (20, 21, 24, 25):  # channel 1 restarts easily
        for _ in range(3):
            _revival(conn, uid, next(slot), channel=1, responders=[50, 51], count=4)
    for uid in (22, 23, 26, 27):  # channel 2 almost never does
        for _ in range(3):
            _revival(conn, uid, next(slot), channel=2, responders=[50], count=1)
    # a couple of successes, so the hard room has a low base rate rather than
    # none at all — a room nobody ever restarts has no computable lift, and one
    # too thin to expect a single restart is dropped by MIN_EXPECTED_REVIVALS
    for uid in (28, 29):
        _revival(conn, uid, next(slot), channel=2, responders=[50, 51], count=4)

    for _ in range(6):  # A succeeds in the easy room
        _revival(conn, 1, next(slot), channel=1, responders=[50, 51], count=4)
    for _ in range(6):  # B succeeds in the hard room
        _revival(conn, 2, next(slot), channel=2, responders=[52, 53], count=4)

    catalyst = run(conn).catalyst
    a, b = row(catalyst, 1), row(catalyst, 2)
    assert a.own_rate == b.own_rate == 1.0, "both succeeded every time"
    assert b.baseline < a.baseline, "channel 2 is the harder room"
    assert b.score > a.score, "so B's identical hit rate is worth more"


# ── Connectors ─────────────────────────────────────────────────────────


def test_connectors_counts_breadth_and_concentration(conn, low_floors):
    for target in range(20, 26):  # six partners, evenly
        for _ in range(10):
            react(conn, 1, target, msg(conn, target, 50), 40)
    for _ in range(60):  # one partner, heavily
        react(conn, 2, 30, msg(conn, 30, 50), 40)
    react(conn, 2, 31, msg(conn, 31, 50), 40)

    connectors = run(conn).connectors
    a, b = row(connectors, 1), row(connectors, 2)
    assert a.partners > b.partners
    assert b.concentration > a.concentration


def test_connectors_reciprocity_ratio(conn, low_floors):
    for _ in range(30):
        react(conn, 1, 40, msg(conn, 40, 50), 40)
    giver = row(run(conn).connectors, 1)
    assert giver.given > 0
    assert giver.received == 0
    assert giver.own_rate == 0.0  # nothing came back


# ── Welcomers ──────────────────────────────────────────────────────────


def test_welcomers_credits_replies_to_newcomers_only(conn, low_floors):
    """A newcomer is inside 14 days of their *first* message."""
    old = msg(conn, 50, 90 * 24)  # established member, first seen long ago
    new_first = msg(conn, 60, 10 * 24)  # newcomer's first message

    for _ in range(4):  # A answers the newcomer
        msg(conn, 1, 9 * 24, reply_to=new_first)
    for _ in range(4):  # B answers the veteran
        msg(conn, 2, 9 * 24, reply_to=old)

    welcomers = run(conn).welcomers
    assert row(welcomers, 1).own_rate == 1.0
    assert row(welcomers, 2).own_rate == 0.0
    assert row(welcomers, 1).partners == 1


def test_welcomers_expires_after_the_newcomer_window(conn, low_floors):
    """Answering a member who joined months ago isn't welcoming."""
    stale = msg(conn, 60, 80 * 24)
    fresh = msg(conn, 61, 10 * 24)
    for _ in range(4):
        msg(conn, 1, 10 * 24, reply_to=stale)  # 70 days after their first post
    for _ in range(4):
        msg(conn, 2, 9 * 24, reply_to=fresh)  # keeps the server baseline non-zero

    assert row(run(conn).welcomers, 1).own_rate == 0.0
    assert row(run(conn).welcomers, 2).own_rate == 1.0


def test_welcomers_drops_rows_with_too_few_expected_newcomer_replies(
    conn, low_floors, monkeypatch
):
    """Twenty replies can't establish a welcoming rate.

    Same rule as the catalyst floor: what bounds the estimate is how many
    newcomer-replies you'd *expect* at the server's own rate, not how many
    replies were sent.
    """
    monkeypatch.setattr(cs, "MIN_EXPECTED_NEWCOMER_REPLIES", 5.0)
    newbie = msg(conn, 60, 10 * 24)
    veteran = msg(conn, 50, 80 * 24)
    for _ in range(60):  # sets a server-wide share of roughly a third
        msg(conn, 3, 9 * 24, reply_to=newbie)
    for _ in range(120):
        msg(conn, 3, 9 * 24, reply_to=veteran)

    for _ in range(4):  # a four-reply member: expects ~1.3, far under the floor
        msg(conn, 1, 9 * 24, reply_to=newbie)

    assert row(run(conn).welcomers, 1) is None
    assert row(run(conn).welcomers, 3) is not None


def test_welcomers_empty_when_nobody_answers_a_newcomer(conn, low_floors):
    """No server-wide share means no baseline, so the view is empty."""
    stale = msg(conn, 60, 80 * 24)
    for _ in range(4):
        msg(conn, 1, 10 * 24, reply_to=stale)
    assert run(conn).welcomers == []


def test_welcomers_is_a_share_not_a_count(conn, low_floors):
    """The busiest replier shouldn't top the list just for being busy."""
    veteran = msg(conn, 50, 90 * 24)
    newbie = msg(conn, 60, 10 * 24)
    for _ in range(6):  # A: 6 of 30 replies to the newcomer
        msg(conn, 1, 9 * 24, reply_to=newbie)
    for _ in range(24):
        msg(conn, 1, 9 * 24, reply_to=veteran)
    for _ in range(3):  # B: 3 of 4 replies to the newcomer
        msg(conn, 2, 9 * 24, reply_to=newbie)
    msg(conn, 2, 9 * 24, reply_to=veteran)

    welcomers = run(conn).welcomers
    a, b = row(welcomers, 1), row(welcomers, 2)
    assert a.given > b.given, "A answered the newcomer more times"
    assert b.score > a.score, "but B aimed a far larger share of their replies there"


# ── Lifts the under-attended ───────────────────────────────────────────


def test_under_attended_favours_engaging_the_overlooked(conn, low_floors):
    popular_post = msg(conn, 70, 50)
    for r in range(80, 95):  # member 70 is showered with attention
        react(conn, r, 70, popular_post, 45)
    quiet_post = msg(conn, 71, 50)
    react(conn, 96, 71, quiet_post, 45)

    for _ in range(60):  # A engages the popular member
        react(conn, 1, 70, msg(conn, 70, 40), 30)
    for _ in range(60):  # B engages the overlooked one
        react(conn, 2, 71, msg(conn, 71, 40), 30)

    under = run(conn).under_attended
    assert row(under, 2).score > row(under, 1).score


# ── Shrinkage ──────────────────────────────────────────────────────────


def test_small_samples_cannot_top_a_view_on_noise(conn, low_floors):
    """A huge lift over three acts must not outrank a solid one over hundreds.

    This is what replaced the sample threshold: a cutoff answers the wrong
    question, since two lifts differ because one estimate is noisy, not because
    one cleared a bar.
    """
    # the room's rate has to be set by other people, since every baseline is
    # leave-one-out — otherwise these two would mostly be measured against
    # each other
    background(conn, 1, uids=(10, 11, 12), posts=70, responders=2)

    for _ in range(3):  # tiny sample, spectacular rate
        m = msg(conn, 1, 40, channel=1)
        for r in range(90, 98):
            react(conn, r, 1, m, 39)
    for _ in range(200):  # large sample, merely good rate
        m = msg(conn, 2, 40, channel=1)
        for r in (90, 91, 92):
            react(conn, r, 2, m, 39)

    popular = run(conn).popular
    a, b = row(popular, 1), row(popular, 2)
    assert a.own_rate > b.own_rate, "the tiny sample has the higher raw rate"
    assert b.score > a.score, "but the reliable one ranks higher"


def test_shrinkage_leaves_large_samples_almost_untouched(conn, low_floors):
    """The prior washes out as evidence accumulates."""
    background(conn, 1, uids=(10, 11, 12), posts=40, responders=2)
    for _ in range(400):
        m = msg(conn, 1, 40, channel=1)
        for r in (90, 91, 92):
            react(conn, r, 1, m, 39)
    e = row(run(conn).popular, 1)
    raw = e.own_rate / e.baseline
    assert abs(e.score - raw) / raw < 0.10


def test_catalyst_drops_rows_with_too_few_expected_restarts(
    conn, low_floors, monkeypatch
):
    """One lucky restart in a room that almost never restarts is not a signal.

    A rate estimate's precision comes from the number of events expected, not
    the number of tries — so the floor is on expected restarts, not attempts.
    """
    monkeypatch.setattr(cs, "MIN_EXPECTED_REVIVALS", 1.0)  # the production value
    slot = iter(range(900, 40, -8))
    for uid in (20, 21, 24, 25, 28, 29):  # a room that seldom restarts
        for _ in range(6):
            _revival(conn, uid, next(slot), channel=1, responders=[50], count=1)
    for uid in (30, 31):  # a couple of successes set a low, non-zero base rate
        _revival(conn, uid, next(slot), channel=1, responders=[50, 51], count=4)
    for _ in range(11):  # our member: one fluke restart out of twelve
        _revival(conn, 1, next(slot), channel=1, responders=[50], count=1)
    _revival(conn, 1, next(slot), channel=1, responders=[50, 51], count=4)

    entry = row(run(conn).catalyst, 1)
    assert entry is None, "expected restarts is well under 1, so there is nothing to measure"


def test_catalyst_is_not_shrunk(conn, low_floors):
    """Shrinkage measurably lowers this view's split-half reliability."""
    assert cs.SHRINK_REVIVALS == 0
    slot = iter(range(900, 40, -8))
    for uid in (20, 21, 24):
        for _ in range(6):
            _revival(conn, uid, next(slot), channel=1, responders=[50, 51], count=4)
    for _ in range(6):
        _revival(conn, 1, next(slot), channel=1, responders=[50, 51], count=4)
    e = row(run(conn).catalyst, 1)
    assert e.score == pytest.approx(e.own_rate / e.baseline)


# ── Exclusions ─────────────────────────────────────────────────────────


def test_bots_are_excluded_from_every_view(conn, low_floors):
    known(conn, 1, is_bot=True)
    for _ in range(5):
        m = msg(conn, 1, 40, channel=1)
        react(conn, 90, 1, m, 39)
        react(conn, 1, 90, m, 39)
    r = run(conn)
    assert all(
        row(view, 1) is None
        for view in (r.popular, r.catalyst, r.connectors, r.welcomers, r.under_attended)
    )


def test_self_interaction_never_counts(conn, low_floors):
    for _ in range(60):
        m = msg(conn, 1, 40)
        react(conn, 1, 1, m, 39)  # reacting to yourself
        msg(conn, 1, 39, reply_to=m)  # replying to yourself
    r = run(conn)
    assert row(r.connectors, 1) is None
    assert row(r.welcomers, 1) is None


def test_departed_members_are_filtered_out(conn, low_floors):
    for _ in range(5):
        m = msg(conn, 1, 40, channel=1)
        react(conn, 90, 1, m, 39)
        m2 = msg(conn, 2, 40, channel=1)
        react(conn, 90, 2, m2, 39)
    assert {e.user_id for e in run(conn, member_ids={1}).popular} == {1}


def test_window_excludes_older_activity(conn, low_floors):
    for _ in range(5):
        m = msg(conn, 1, 200 * 24, channel=1)  # 200 days ago
        react(conn, 90, 1, m, 199 * 24)
    assert run(conn).popular == []
    assert run(conn).members_considered == 0
