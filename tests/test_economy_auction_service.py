"""Tests for services/economy_auction_service.py — mod-run ascending auctions.

The money-critical paths: escrow-at-bid, instant full refund of the outbid
member, the winning bid burned at close (never refunded), the compare-and-swap
race guard, insufficient-balance abort with no state change, cancel refunds the
standing bid, soft-close extension, and every settle/cancel claim exactly-once.
"""

from __future__ import annotations

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_auction_service import (
    attach_card,
    attach_card_to_latest,
    bid_count,
    cancel_auction,
    card_ids,
    end_auction_now,
    get_auction,
    get_open_auction,
    latest_auction,
    min_next_bid,
    open_auction,
    place_bid,
    place_bid_now,
    settle_due_auctions,
    bot_chasing_resident,
    sticky_panel_channels,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    get_balance,
    save_econ_settings,
)
from tests.db_template import migrated_db

GUILD = 800
A, B, C, MOD = 3001, 3002, 3003, 9001
CH = 555
NOW = 1_800_000_000.0
HOUR = 3600.0

SETTINGS = EconSettings(
    enabled=True,
    auction_min_bid=10,
    auction_min_increment=5,
    auction_soft_close_seconds=300,
    auction_max_duration_hours=168,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _fund(conn, user, amount):
    apply_credit(conn, GUILD, user, amount, "grant", actor_id=MOD)


def _ledger(conn, user):
    return [
        (r["kind"], r["amount"])
        for r in conn.execute(
            "SELECT kind, amount FROM econ_ledger "
            "WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (GUILD, user),
        )
    ]


def _open(conn, *, duration_hours=48.0, settings=SETTINGS, now=NOW):
    return open_auction(
        conn, settings, GUILD, created_by=MOD, title="Name the QOTD theme",
        description="Winner picks next week's theme.", duration_hours=duration_hours,
        channel_id=CH, now=now,
    )


# ── open ───────────────────────────────────────────────────────────────


def test_open_creates_a_live_auction(db):
    with open_db(db) as conn:
        aid = _open(conn)
        row = get_open_auction(conn, GUILD)
        assert row is not None
        assert int(row["id"]) == aid
        assert row["state"] == "open"
        assert row["high_bid"] is None
        assert float(row["ends_at"]) == NOW + 48 * HOUR


def test_open_rejects_a_second_live_auction(db):
    with open_db(db) as conn:
        _open(conn)
        with pytest.raises(ValueError, match="already a live auction"):
            _open(conn)


@pytest.mark.parametrize("dur", [0.0, 0.5, 169.0])
def test_open_rejects_bad_duration(db, dur):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="Duration"):
            _open(conn, duration_hours=dur)


def test_open_rejects_short_title(db):
    with open_db(db) as conn:
        with pytest.raises(ValueError, match="title"):
            open_auction(
                conn, SETTINGS, GUILD, created_by=MOD, title="x",
                description="", duration_hours=1.0, now=NOW,
            )


# ── first bid + escrow ───────────────────────────────────────────────────


def test_first_bid_escrows_at_the_floor(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        assert min_next_bid(SETTINGS, get_auction(conn, aid)) == 10
        res = place_bid(conn, SETTINGS, GUILD, aid, A, 25, now=NOW)
        assert res.outbid_user_id is None
        assert get_balance(conn, GUILD, A) == 75  # 25 escrowed
        row = get_auction(conn, aid)
        assert int(row["high_bid"]) == 25
        assert int(row["high_bidder_id"]) == A
        assert _ledger(conn, A) == [("grant", 100), ("auction_bid", -25)]


def test_first_bid_below_floor_is_rejected(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="at least 10"):
            place_bid(conn, SETTINGS, GUILD, aid, A, 9, now=NOW)
        assert get_balance(conn, GUILD, A) == 100  # nothing moved


def test_bid_beyond_balance_aborts_with_no_state_change(db):
    # Escrow-first means an unaffordable bid returns from apply_debit having
    # written nothing, so the rejection touches no auction state — no reliance
    # on rollback for the common case.
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 20)
        with pytest.raises(ValueError, match="enough"):
            place_bid(conn, SETTINGS, GUILD, aid, A, 25, now=NOW)
        assert get_balance(conn, GUILD, A) == 20
        row = get_auction(conn, aid)
        assert row["high_bid"] is None
        assert bid_count(conn, aid) == 0


# ── outbid + refund ──────────────────────────────────────────────────────


def test_outbid_refunds_the_previous_bidder_in_full(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        res = place_bid(conn, SETTINGS, GUILD, aid, B, 40, now=NOW + 1)
        assert res.outbid_user_id == A
        assert res.outbid_amount == 30
        assert get_balance(conn, GUILD, A) == 100  # fully refunded
        assert get_balance(conn, GUILD, B) == 60   # 40 escrowed
        assert _ledger(conn, A) == [
            ("grant", 100), ("auction_bid", -30), ("auction_refund", 30),
        ]


def test_new_bid_must_beat_high_by_the_increment(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        # high 30 + increment 5 = 34 floor
        assert min_next_bid(SETTINGS, get_auction(conn, aid)) == 35
        with pytest.raises(ValueError, match="at least 35"):
            place_bid(conn, SETTINGS, GUILD, aid, B, 34, now=NOW + 1)
        assert get_balance(conn, GUILD, B) == 100  # nothing moved


def test_high_bidder_cannot_bid_against_themselves(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        with pytest.raises(ValueError, match="already the high bidder"):
            place_bid(conn, SETTINGS, GUILD, aid, A, 50, now=NOW + 1)
        assert get_balance(conn, GUILD, A) == 70  # unchanged (still just the 30)


# ── the compare-and-swap race ────────────────────────────────────────────


def test_a_fast_follow_up_below_the_new_floor_is_rejected(db):
    # The practical guard: B validates against high=30, but C's 50 lands first,
    # so B's 40 is now below the real floor (55) and rejected on the floor
    # check — money intact, C still high, A refunded.
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        _fund(conn, C, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        place_bid(conn, SETTINGS, GUILD, aid, C, 50, now=NOW + 1)
        with pytest.raises(ValueError):
            place_bid(conn, SETTINGS, GUILD, aid, B, 40, now=NOW + 2)
        assert get_balance(conn, GUILD, B) == 100
        assert int(get_auction(conn, aid)["high_bidder_id"]) == C
        assert get_balance(conn, GUILD, A) == 100


def test_cas_guard_rejects_a_claim_built_on_a_stale_high(db):
    # The concurrency backstop, tested at the mechanism directly: once the slot
    # reads high=30/A, a claim that asserts a *stale* prior state (high IS 20)
    # must miss — this is what rejects a bid whose validation snapshot went out
    # of date between read and write under real concurrency. The floor check
    # can't cover this because it runs against the same stale read.
    from bot_modules.services.economy_auction_service import _claim_high_slot

    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        # Stale claim: asserts the slot still holds (20, someone) — it holds
        # (30, A) — so the CAS misses.
        assert _claim_high_slot(
            conn, aid, 20, B, new_amount=99, new_bidder=C, new_end=NOW + HOUR,
        ) is False
        # A real claim against the true current state (30, A) wins.
        assert _claim_high_slot(
            conn, aid, 30, A, new_amount=99, new_bidder=C, new_end=NOW + HOUR,
        ) is True
        # And the first-bid form: NULL/NULL matches only a virgin slot.
        aid2 = None  # new auction after this closes
    # A fresh auction's empty slot: claim with (None, None) wins; (0, x) misses.
    with open_db(db) as conn:
        cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 1)
        aid2 = _open(conn, now=NOW + 2)
        assert _claim_high_slot(
            conn, aid2, 0, A, new_amount=10, new_bidder=A, new_end=NOW + HOUR,
        ) is False
        assert _claim_high_slot(
            conn, aid2, None, None, new_amount=10, new_bidder=A, new_end=NOW + HOUR,
        ) is True


# ── soft close ───────────────────────────────────────────────────────────


def test_bid_in_the_soft_close_window_extends_the_end(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=1.0)  # ends at NOW + 3600
        _fund(conn, A, 100)
        # Bid 100s before the end, inside the 300s window → push end to now+300.
        t = NOW + HOUR - 100
        res = place_bid(conn, SETTINGS, GUILD, aid, A, 20, now=t)
        assert res.extended is True
        assert res.ends_at == t + 300
        assert float(get_auction(conn, aid)["ends_at"]) == t + 300


def test_bid_outside_the_window_leaves_the_end_alone(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        _fund(conn, A, 100)
        res = place_bid(conn, SETTINGS, GUILD, aid, A, 20, now=NOW + 1)
        assert res.extended is False
        assert float(get_auction(conn, aid)["ends_at"]) == NOW + 48 * HOUR


def test_bid_after_the_end_is_rejected(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=1.0)
        _fund(conn, A, 100)
        with pytest.raises(ValueError, match="ended"):
            place_bid(conn, SETTINGS, GUILD, aid, A, 20, now=NOW + HOUR + 1)


# ── place_bid_now (BEGIN IMMEDIATE wrapper the cog uses) ──────────────────


def test_place_bid_now_commits_a_bid_in_its_own_transaction(db):
    # The cog entry point manages its own IMMEDIATE transaction; a committed bid
    # is visible on a fresh connection afterward.
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
    res = place_bid_now(db, SETTINGS, GUILD, aid, A, 25, now=NOW)
    assert res.amount == 25
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["high_bid"]) == 25
        assert get_balance(conn, GUILD, A) == 75


def test_place_bid_now_serialized_bids_outbid_and_refund_correctly(db):
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
        _fund(conn, B, 100)
    place_bid_now(db, SETTINGS, GUILD, aid, A, 30, now=NOW)
    res = place_bid_now(db, SETTINGS, GUILD, aid, B, 40, now=NOW + 1)
    assert res.outbid_user_id == A
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, A) == 100  # refunded when outbid
        assert get_balance(conn, GUILD, B) == 60
        assert int(get_auction(conn, aid)["high_bidder_id"]) == B


def test_place_bid_now_propagates_a_rejection_without_retrying(db):
    # A ValueError (below floor) is a real rejection, not a lock conflict — it
    # must surface immediately, not be swallowed by the retry loop.
    with open_db(db) as conn:
        aid = _open(conn)
        _fund(conn, A, 100)
    with pytest.raises(ValueError, match="at least 10"):
        place_bid_now(db, SETTINGS, GUILD, aid, A, 5, now=NOW)
    with open_db(db) as conn:
        assert get_balance(conn, GUILD, A) == 100  # nothing escrowed


# ── close: the burn ──────────────────────────────────────────────────────


def test_settle_burns_the_winning_bid(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=1.0)
        _fund(conn, A, 100)
        _fund(conn, B, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        place_bid(conn, SETTINGS, GUILD, aid, B, 40, now=NOW + 1)
        settled = settle_due_auctions(conn, GUILD, now=NOW + HOUR + 1)
        assert len(settled) == 1
        s = settled[0]
        assert s.winner_id == B
        assert s.winning_bid == 40
        # B's 40 is gone for good (never credited back) — that's the sink.
        assert get_balance(conn, GUILD, B) == 60
        assert _ledger(conn, B) == [("grant", 100), ("auction_bid", -40)]
        # A was refunded when outbid; net zero.
        assert get_balance(conn, GUILD, A) == 100
        assert get_auction(conn, aid)["state"] == "closed"


def test_settle_with_no_bids_has_no_winner_and_no_burn(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=1.0)
        settled = settle_due_auctions(conn, GUILD, now=NOW + HOUR + 1)
        assert len(settled) == 1
        assert settled[0].winner_id is None
        assert settled[0].winning_bid == 0
        assert get_auction(conn, aid)["state"] == "closed"


def test_settle_is_exactly_once(db):
    with open_db(db) as conn:
        _open(conn, duration_hours=1.0)
        _fund(conn, A, 100)
        # (no bid needed) — settle, then a replay finds nothing to close.
        first = settle_due_auctions(conn, GUILD, now=NOW + HOUR + 1)
        assert len(first) == 1
        second = settle_due_auctions(conn, GUILD, now=NOW + HOUR + 2)
        assert second == []


def test_settle_leaves_an_unexpired_auction_open(db):
    with open_db(db) as conn:
        _open(conn, duration_hours=48.0)
        assert settle_due_auctions(conn, GUILD, now=NOW + HOUR) == []
        assert get_open_auction(conn, GUILD) is not None


def test_end_now_force_closes_and_burns(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        _fund(conn, A, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        s = end_auction_now(conn, GUILD, aid, now=NOW + 60)
        assert s is not None and s.winner_id == A and s.winning_bid == 30
        assert get_balance(conn, GUILD, A) == 70  # burned
        assert end_auction_now(conn, GUILD, aid, now=NOW + 61) is None  # once


# ── cancel ───────────────────────────────────────────────────────────────


def test_cancel_refunds_the_standing_bid_and_burns_nothing(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        _fund(conn, A, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        row = cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 60)
        assert row is not None
        assert get_balance(conn, GUILD, A) == 100  # fully refunded
        assert get_auction(conn, aid)["state"] == "cancelled"
        assert _ledger(conn, A) == [
            ("grant", 100), ("auction_bid", -30), ("auction_refund", 30),
        ]


def test_cancel_with_no_bids_just_closes(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        row = cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 60)
        assert row is not None
        assert get_auction(conn, aid)["state"] == "cancelled"


def test_cancel_is_exactly_once(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        _fund(conn, A, 100)
        place_bid(conn, SETTINGS, GUILD, aid, A, 30, now=NOW)
        assert cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 1) is not None
        # A replay must not double-refund.
        assert cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 2) is None
        assert get_balance(conn, GUILD, A) == 100


def test_cancel_lets_a_new_auction_open(db):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 1)
        # single-live rule no longer blocks a fresh one
        assert _open(conn, now=NOW + 2) != aid


# ── sticky card ids (the lifecycle the StickyPanel contract encodes) ─────────
#
# Every other sticky panel in the bot is permanent; an auction ends. These are
# the functions that make "stop sticking at close, never resurrect" true, so
# each state gets a row rather than a happy-path test.


@pytest.mark.parametrize(
    ("finish", "expected_live"),
    [
        pytest.param(None, True, id="open-sticks"),
        pytest.param("end", False, id="closed-goes-dormant"),
        pytest.param("cancel", False, id="cancelled-goes-dormant"),
        pytest.param("settle", False, id="timed-close-goes-dormant"),
    ],
)
def test_card_ids_only_while_open(db, finish, expected_live):
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        attach_card(conn, aid, CH, 4242)
        if finish == "end":
            end_auction_now(conn, GUILD, aid, now=NOW + 60)
        elif finish == "cancel":
            cancel_auction(conn, GUILD, aid, resolver_id=MOD, now=NOW + 60)
        elif finish == "settle":
            settle_due_auctions(conn, GUILD, now=NOW + 100 * HOUR)

        assert card_ids(conn, GUILD) == ((CH, 4242) if expected_live else (0, 0))
        # The card message itself is never forgotten — it is still repainted
        # and still deleted by the freeze repost. Only the *sticking* stops.
        row = get_auction(conn, aid)
        assert (int(row["channel_id"]), int(row["message_id"])) == (CH, 4242)


def test_card_ids_is_zero_with_no_auction_at_all(db):
    with open_db(db) as conn:
        assert card_ids(conn, GUILD) == (0, 0)
        assert latest_auction(conn, GUILD) is None


def test_card_ids_follows_the_newest_auction_not_an_old_one(db):
    """A finished auction must never pull the panel back to its dead card."""
    with open_db(db) as conn:
        old = _open(conn, duration_hours=48.0)
        attach_card(conn, old, CH, 1111)
        end_auction_now(conn, GUILD, old, now=NOW + 60)
        assert card_ids(conn, GUILD) == (0, 0)

        new = _open(conn, now=NOW + 120)
        attach_card(conn, new, 777, 2222)
        assert card_ids(conn, GUILD) == (777, 2222)


def test_attach_card_to_latest_still_lands_after_the_auction_closed(db):
    """The regression that would re-create the casino hub storm.

    ``StickyPanel.save_ids`` only gets a guild id, so resolving "the open
    auction" loses the write when a placement returns after the settle loop
    has closed the auction: old card deleted, new card recorded nowhere, the
    stored id frozen on a dead message. Writing to the newest row regardless
    of state is correct under either ordering.
    """
    with open_db(db) as conn:
        aid = _open(conn, duration_hours=48.0)
        attach_card(conn, aid, CH, 1111)
        # The auction closes while a repost is mid-flight...
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
        assert get_open_auction(conn, GUILD) is None
        # ...and the placement's save lands afterwards. It must not be dropped.
        attach_card_to_latest(conn, GUILD, CH, 9999)

        row = get_auction(conn, aid)
        assert int(row["message_id"]) == 9999
        # Still dormant, though — a recorded id is not a reason to re-stick.
        assert card_ids(conn, GUILD) == (0, 0)


def test_attach_card_to_latest_writes_only_the_newest_row(db):
    with open_db(db) as conn:
        old = _open(conn, duration_hours=48.0)
        attach_card(conn, old, CH, 1111)
        end_auction_now(conn, GUILD, old, now=NOW + 60)
        new = _open(conn, now=NOW + 120)

        attach_card_to_latest(conn, GUILD, 777, 2222)
        assert int(get_auction(conn, new)["message_id"]) == 2222
        assert int(get_auction(conn, old)["message_id"]) == 1111  # untouched


def test_attach_card_to_latest_is_a_no_op_without_an_auction(db):
    with open_db(db) as conn:
        attach_card_to_latest(conn, GUILD, CH, 9999)  # must not raise
        assert card_ids(conn, GUILD) == (0, 0)


# ── the panel-collision warning ─────────────────────────────────────────────


def test_sticky_panel_channels_lists_configured_panels(db):
    """The two panels that only re-stick under human messages, so an auction
    sharing their channel is warned about rather than refused.

    A retired ``leaderboard_channel_id`` must not resurface as a resident: the
    panel it named merged into the economy panel on 2026-08-18, so warning
    about it would name a channel that no longer holds anything.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "guide_channel_id": 11,
            "leaderboard_channel_id": 22,
            "shop_channel_id": 33,
        })
        found = sticky_panel_channels(conn, GUILD)
    assert {cid: r.name for cid, r in found.items()} == {
        11: "the economy panel",
        33: "the shop panel",
    }
    assert not any(r.restick_on_bot for r in found.values())


def test_sticky_panel_channels_includes_the_casino_hub(db):
    """The verified real collision: auction #1 in prod ran in the casino hub's
    channel, and the hub re-sticks under bot messages where the card does not."""
    with open_db(db) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "1530328883449040967"),
        )
        found = sticky_panel_channels(conn, GUILD)
    assert found[1530328883449040967].name == "the casino hub panel"
    assert found[1530328883449040967].restick_on_bot is True


def test_sticky_panel_channels_includes_the_bounty_board(db):
    """The bounty hub is the fifth sticky panel reachable from a config read.
    It keys off bounty_channel_id, not where the panel was last posted — the
    board channel is where the hub lives and where its cards land."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 44})
        found = sticky_panel_channels(conn, GUILD)
    assert found[44].name == "the bounty board panel"


@pytest.mark.parametrize(
    ("setting", "channel_id", "blocks"),
    [
        # These two chase the bot's own posts, so an auction card here is
        # buried after every render and never resurfaces → refuse.
        pytest.param("bounty_channel_id", 44, True, id="bounty-board-blocks"),
        pytest.param("shop_channel_id", 33, False, id="shop-only-warns"),
    ],
)
def test_restick_on_bot_marks_the_residents_that_block_an_auction(
    db, setting, channel_id, blocks
):
    """The flag is what splits refuse-outright from merely-warn, so it is worth
    pinning per resident rather than trusting the tuple table by eye."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {setting: channel_id})
        found = sticky_panel_channels(conn, GUILD)
    assert found[channel_id].restick_on_bot is blocks


def test_sticky_panel_channels_is_empty_when_nothing_is_configured(db):
    """An unconfigured guild must not warn about channel 0."""
    with open_db(db) as conn:
        assert sticky_panel_channels(conn, GUILD) == {}


def test_sticky_panel_channels_merges_two_residents_in_one_channel(db):
    """This was built by comprehension, so a shared channel reported only
    whichever panel came last in the table — a mod warned about a shared channel
    was told about one of the two things they were sharing it with
    (2026-08-06 review, F1)."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"shop_channel_id": 77})
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "77"),
        )
        found = sticky_panel_channels(conn, GUILD)
    assert "the shop panel" in found[77].name
    assert "the casino hub panel" in found[77].name
    # restick_on_bot is the union: one bot-chaser in the channel is enough to
    # bury an auction card reliably, so the block must still fire.
    assert found[77].restick_on_bot is True


# ── the two-bot-chasers collision ───────────────────────────────────────────


def test_bot_chasing_resident_finds_the_other_opted_in_panel(db):
    """Two panels that both chase bot posts in one channel take the bottom slot
    from each other on every trigger, and before core.sticky learned to ignore
    another panel's placement they re-posted forever with nobody typing. A live
    guild had bounty_channel_id == casino_panel_channel_id (2026-08-06, F1)."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 99})
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (GUILD, "casino_panel_channel_id", "99"),
        )
        assert bot_chasing_resident(
            conn, GUILD, 99, excluding="bounty"
        ) == "the casino hub panel"


def test_bot_chasing_resident_excludes_the_asking_panel(db):
    """The bounty hub lives in the bounty channel by definition, so without the
    exclusion it would always find itself and never be postable at all."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {"bounty_channel_id": 99})
        assert bot_chasing_resident(conn, GUILD, 99, excluding="bounty") is None


def test_bot_chasing_resident_ignores_human_only_panels(db):
    """The guide/leaderboard/shop panels only move under human messages, so they
    trade places visibly rather than looping — that warns, it does not block."""
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD, {"bounty_channel_id": 99, "shop_channel_id": 99}
        )
        assert bot_chasing_resident(conn, GUILD, 99, excluding="bounty") is None
