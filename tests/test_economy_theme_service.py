"""Tests for services/economy_theme_service.py — the paid, mod-approved themed day.

The money-critical paths: charged at submit, refunded on every exit that
didn't give the member their day (deny, withdraw-from-queue, pending expiry),
and NOT refunded on the one that did (the window running out, or a mod yanking
a running theme). Plus the guards that keep one theme live per guild and one
submission in flight per member, and the FIFO promotion that only fires when
the channel is actually free.

The enablement tests are the ones worth reading twice: this product is the
first where price 0 means "free", not "off".
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
)
from bot_modules.services.economy_theme_service import (
    MAX_BLURB_LEN,
    MAX_THEME_HOURS,
    MAX_TITLE_LEN,
    MIN_THEME_HOURS,
    anonymise_live_theme,
    approve,
    deny,
    expire_live_themes,
    expire_stale_pending,
    get_submission,
    go_live,
    list_submissions,
    live_theme,
    next_approved,
    open_submission,
    queue_depth,
    submit_theme,
    take_down,
    theme_enabled,
    theme_price,
    theme_window_seconds,
    withdraw_approved,
)
from tests.db_template import migrated_db

GUILD = 800
USER = 3001
USER_2 = 3002
MOD = 9001
THEME_CH = 6666
MSG = 777
NOW = 1_800_000_000.0
DAY = 86400.0

SETTINGS = EconSettings(
    enabled=True,
    flash_theme_enabled=True,
    price_flash_theme=300,
    theme_channel_id=THEME_CH,
    theme_expire_days=3,
    theme_hours=24,
)
TITLE = "Cursed Cooking"
BLURB = "Post the worst thing you have ever eaten. Photos strongly encouraged."


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _fund(conn, amount, user_id=USER):
    apply_credit(conn, GUILD, user_id, amount, "grant", actor_id=MOD)


def _ledger(conn, user_id=USER):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger WHERE guild_id = ? AND user_id = ? "
            "ORDER BY id",
            (GUILD, user_id),
        )
    ]


def _queued(conn, user_id=USER, funds=1000):
    """A theme sitting in the queue, ready to go live."""
    _fund(conn, funds, user_id)
    out = submit_theme(conn, SETTINGS, GUILD, user_id, TITLE, BLURB)
    approve(conn, out.submission_id, resolver_id=MOD)
    return out.submission_id


# ── enablement ─────────────────────────────────────────────────────────


def test_the_toggle_and_a_channel_are_both_required():
    assert theme_enabled(SETTINGS) is True
    assert theme_enabled(replace(SETTINGS, flash_theme_enabled=False)) is False
    assert theme_enabled(replace(SETTINGS, theme_channel_id=0)) is False


def test_a_free_themed_day_is_enabled_not_disabled():
    """The whole reason this product has a toggle: price 0 used to be the off
    switch, so 'free' and 'off' were the same value and neither could be said
    on its own."""
    free = replace(SETTINGS, price_flash_theme=0)
    assert theme_price(free) == 0
    assert theme_enabled(free) is True


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        pytest.param(24, 24, id="ordinary-day"),
        pytest.param(0, MIN_THEME_HOURS, id="zero-clamps-up"),
        pytest.param(-5, MIN_THEME_HOURS, id="negative-clamps-up"),
        pytest.param(99999, MAX_THEME_HOURS, id="absurd-clamps-down"),
    ],
)
def test_the_window_is_clamped(hours, expected):
    """A misconfigured dial must not let one purchase buy the channel forever."""
    settings = replace(SETTINGS, theme_hours=hours)
    assert theme_window_seconds(settings) == expected * 3600.0


# ── submit ─────────────────────────────────────────────────────────────


def test_submit_charges_and_queues(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        assert out.price == 300
        assert get_balance(conn, GUILD, USER) == 700
        row = get_submission(conn, out.submission_id)
        assert (row["state"], row["title"], row["blurb"]) == ("pending", TITLE, BLURB)
        assert _ledger(conn) == [("grant", 1000), ("flash_theme", -300)]


def test_submit_while_disabled_costs_nothing(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        off = replace(SETTINGS, flash_theme_enabled=False)
        with pytest.raises(ValueError, match="isn't enabled"):
            submit_theme(conn, off, GUILD, USER, TITLE, BLURB)
        assert get_balance(conn, GUILD, USER) == 1000
        assert open_submission(conn, GUILD, USER) is None


def test_submit_without_the_money_writes_nothing(db):
    with open_db(db) as conn:
        _fund(conn, 10)
        with pytest.raises(ValueError, match="you have 10"):
            submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        assert get_balance(conn, GUILD, USER) == 10
        assert open_submission(conn, GUILD, USER) is None


@pytest.mark.parametrize(
    ("title", "blurb", "match"),
    [
        pytest.param("ab", BLURB, "bit short", id="title-too-short"),
        pytest.param("x" * (MAX_TITLE_LEN + 1), BLURB, "limited to", id="title-too-long"),
        pytest.param(TITLE, "   ", "a line or two", id="blurb-empty"),
        pytest.param(TITLE, "x" * (MAX_BLURB_LEN + 1), "limited to", id="blurb-too-long"),
    ],
)
def test_bad_content_is_rejected_before_any_charge(db, title, blurb, match):
    with open_db(db) as conn:
        _fund(conn, 1000)
        with pytest.raises(ValueError, match=match):
            submit_theme(conn, SETTINGS, GUILD, USER, title, blurb)
        assert get_balance(conn, GUILD, USER) == 1000


def test_whitespace_is_normalised(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, "  Cursed   Cooking  ", BLURB)
        assert get_submission(conn, out.submission_id)["title"] == "Cursed Cooking"


@pytest.mark.parametrize("state", ["pending", "approved", "live"])
def test_one_theme_in_flight_per_member(db, state):
    """You cannot buy the next five themed days out from under everyone else."""
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        if state in ("approved", "live"):
            approve(conn, out.submission_id, resolver_id=MOD)
        if state == "live":
            go_live(conn, out.submission_id, theme_channel_id=THEME_CH,
                    theme_message_id=MSG, window_seconds=DAY, now=NOW)
        with pytest.raises(ValueError, match="already have a theme"):
            submit_theme(conn, SETTINGS, GUILD, USER, "Second Go", BLURB)
        assert get_balance(conn, GUILD, USER) == 700


def test_a_finished_theme_frees_the_member_to_buy_again(db):
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        expire_live_themes(conn, GUILD, now=NOW + DAY)
        assert open_submission(conn, GUILD, USER) is None
        submit_theme(conn, SETTINGS, GUILD, USER, "Second Go", BLURB)


# ── the refund line ────────────────────────────────────────────────────


def test_denying_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        row = deny(conn, out.submission_id, resolver_id=MOD, deny_reason="not this one")
        assert row["state"] == "denied"
        assert get_balance(conn, GUILD, USER) == 1000
        assert _ledger(conn)[-1] == ("flash_theme_refund", 300)


def test_withdrawing_from_the_queue_refunds(db):
    """It never had its day, so the money goes back."""
    with open_db(db) as conn:
        sub = _queued(conn)
        row = withdraw_approved(conn, sub, resolver_id=MOD, reason="clearing the queue")
        assert row["state"] == "denied"
        assert get_balance(conn, GUILD, USER) == 1000


def test_a_theme_that_ran_its_window_is_not_refunded(db):
    """The member got their day — the same call Pin of the Day makes."""
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        expired = expire_live_themes(conn, GUILD, now=NOW + DAY)
        assert [r["id"] for r in expired] == [sub]
        assert get_submission(conn, sub)["state"] == "expired"
        assert get_balance(conn, GUILD, USER) == 700
        assert ("flash_theme_refund", 300) not in _ledger(conn)


def test_a_yanked_running_theme_is_not_refunded(db):
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        row = take_down(conn, sub, resolver_id=MOD)
        assert row["state"] == "expired"
        assert get_balance(conn, GUILD, USER) == 700


def test_a_double_deny_pays_out_once(db):
    """Two mods clicking Decline at the same moment must refund once."""
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        deny(conn, out.submission_id, resolver_id=MOD)
        with pytest.raises(ValueError, match="already denied"):
            deny(conn, out.submission_id, resolver_id=MOD + 1)
        assert get_balance(conn, GUILD, USER) == 1000
        assert _ledger(conn).count(("flash_theme_refund", 300)) == 1


# ── the queue ──────────────────────────────────────────────────────────


def test_the_queue_is_fifo(db):
    with open_db(db) as conn:
        first = _queued(conn, USER)
        second = _queued(conn, USER_2)
        assert next_approved(conn, GUILD)["id"] == first
        assert queue_depth(conn, GUILD) == 2
        go_live(conn, first, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        assert queue_depth(conn, GUILD) == 1
        assert next_approved(conn, GUILD)["id"] == second


def test_an_empty_queue_offers_nothing(db):
    """A day with no theme is a normal day — the caller posts nothing."""
    with open_db(db) as conn:
        assert next_approved(conn, GUILD) is None
        assert live_theme(conn, GUILD) is None


def test_approving_does_not_start_the_clock(db):
    """Approval only queues; the window starts when the channel frees up, or a
    theme approved during a busy week would expire before it ever ran."""
    with open_db(db) as conn:
        sub = _queued(conn)
        row = get_submission(conn, sub)
        assert row["state"] == "approved"
        assert row["expires_at"] is None and row["went_live_at"] is None


def test_going_live_records_the_announcement_and_the_clock(db):
    with open_db(db) as conn:
        sub = _queued(conn)
        row = go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                      window_seconds=DAY, now=NOW)
        assert row["state"] == "live"
        assert (row["theme_channel_id"], row["theme_message_id"]) == (THEME_CH, MSG)
        assert (row["went_live_at"], row["expires_at"]) == (NOW, NOW + DAY)
        assert live_theme(conn, GUILD)["id"] == sub


def test_only_one_theme_can_hold_the_channel(db):
    """There is no supersede here: a paid theme is never cut short by a newer
    one, which is the divergence from Pin of the Day."""
    with open_db(db) as conn:
        first = _queued(conn, USER)
        second = _queued(conn, USER_2)
        go_live(conn, first, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        with pytest.raises(sqlite3.IntegrityError):
            go_live(conn, second, theme_channel_id=THEME_CH, theme_message_id=MSG + 1,
                    window_seconds=DAY, now=NOW)


def test_a_theme_denied_before_it_ran_cannot_go_live(db):
    """The caller deletes the card it just posted when this raises."""
    with open_db(db) as conn:
        sub = _queued(conn)
        withdraw_approved(conn, sub, resolver_id=MOD)
        with pytest.raises(ValueError, match="isn't waiting in the queue"):
            go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                    window_seconds=DAY, now=NOW)


def test_a_live_theme_is_left_alone_until_its_window_is_up(db):
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        assert expire_live_themes(conn, GUILD, now=NOW + DAY - 1) == []
        assert get_submission(conn, sub)["state"] == "live"


# ── the stale sweep ────────────────────────────────────────────────────


def test_a_pending_theme_nobody_reviewed_expires_and_refunds(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        rows = expire_stale_pending(conn, SETTINGS, GUILD, now=NOW + 4 * DAY)
        assert len(rows) == 1
        assert get_balance(conn, GUILD, USER) == 1000


def test_a_queued_theme_never_goes_stale(db):
    """It is waiting for the channel, not for a mod — expiring it would refund
    a member whose theme was about to run."""
    with open_db(db) as conn:
        sub = _queued(conn)
        assert expire_stale_pending(conn, SETTINGS, GUILD, now=NOW + 99 * DAY) == []
        assert get_submission(conn, sub)["state"] == "approved"


def test_the_stale_sweep_can_be_switched_off(db):
    with open_db(db) as conn:
        _fund(conn, 1000)
        submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        never = replace(SETTINGS, theme_expire_days=0)
        assert expire_stale_pending(conn, never, GUILD, now=NOW + 99 * DAY) == []


# ── the dashboard queue ────────────────────────────────────────────────


def test_the_queue_view_reads_oldest_first(db):
    with open_db(db) as conn:
        _queued(conn, USER)
        _queued(conn, USER_2)
        rows = list_submissions(conn, GUILD, "approved")
        assert [r["user_id"] for r in rows] == [USER, USER_2]


# ── erasure ────────────────────────────────────────────────────────────


def test_erasure_detaches_a_running_theme_instead_of_deleting_it(db):
    """Deleting the row would strip the name AND strand the pinned
    announcement, which only the sweep — reading live rows — can take down."""
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        assert anonymise_live_theme(conn, GUILD, USER) == 1
        row = get_submission(conn, sub)
        assert row["user_id"] == 0
        assert row["state"] == "live"
        assert (row["theme_message_id"], row["expires_at"]) == (MSG, NOW + DAY)


def test_a_detached_theme_still_comes_down_on_schedule(db):
    with open_db(db) as conn:
        sub = _queued(conn)
        go_live(conn, sub, theme_channel_id=THEME_CH, theme_message_id=MSG,
                window_seconds=DAY, now=NOW)
        anonymise_live_theme(conn, GUILD, USER)
        expired = expire_live_themes(conn, GUILD, now=NOW + DAY)
        assert [r["theme_message_id"] for r in expired] == [MSG]


@pytest.mark.parametrize("state", ["pending", "approved"])
def test_erasure_leaves_non_running_themes_for_the_ordinary_delete(db, state):
    """Only a live theme holds something outside its own table."""
    with open_db(db) as conn:
        _fund(conn, 1000)
        out = submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        if state == "approved":
            approve(conn, out.submission_id, resolver_id=MOD)
        assert anonymise_live_theme(conn, GUILD, USER) == 0
        assert get_submission(conn, out.submission_id)["user_id"] == USER


def test_the_purge_sweep_removes_a_members_themes(db):
    """The table has to be in _PURGE_USER_ID_TABLES or erasure can't see it."""
    from bot_modules.services.economy_service import econ_purge_user

    with open_db(db) as conn:
        _fund(conn, 1000)
        submit_theme(conn, SETTINGS, GUILD, USER, TITLE, BLURB)
        econ_purge_user(conn, GUILD, USER)
        assert list_submissions(conn, GUILD) == []
