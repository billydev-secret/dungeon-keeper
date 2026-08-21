"""Tests for the auction card renderer (economy/auction_views.render_auction_card)
and the sticky card's lifecycle glue.

The card is the one pure-ish piece of the Discord glue — it turns an auction row
into an embed. The Bid flow and settle path are exercised through the service
(test_economy_auction_service). Here we assert each lifecycle state renders the
right title, currency, and fields off a real service row.

The second half covers what makes the card safe to run on ``core.sticky``: it
is the only sticky panel in the bot that ends, so ``build_auction_panel`` must
refuse a finished auction and ``_freeze_card`` must move the result to the
bottom exactly once.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.core.sticky import StickyPanel
from bot_modules.economy.auction_views import (
    _freeze_card,
    build_auction_panel,
    _sticky_check,
    render_auction_card,
    start_auction,
)
from bot_modules.services.economy_auction_service import (
    attach_card,
    cancel_auction,
    card_ids,
    end_auction_now,
    get_auction,
    get_open_auction,
    open_auction,
    place_bid,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD = 900
HOST, A, B = 5001, 5002, 5003
NOW = 1_800_000_000.0
ACCENT = discord.Color.blurple()

SETTINGS = EconSettings(
    enabled=True, currency_emoji="🪙", currency_name="Coin", currency_plural="Coins",
    auction_min_bid=10, auction_min_increment=5, auction_soft_close_seconds=300,
    auction_max_duration_hours=168,
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "test.db"
    migrated_db(path)
    return path


def _open(conn, **kw):
    return open_auction(
        conn, SETTINGS, GUILD, created_by=HOST, title="Founder role",
        description="One-of-a-kind holographic role for a fortnight.",
        duration_hours=48.0, channel_id=1, now=NOW, **kw,
    )


def _field(embed, name):
    for f in embed.fields:
        if f.name == name:
            return f.value
    return None


def test_open_auction_card_shows_opening_bid_and_ends(db):
    with open_db(db) as conn:
        aid = _open(conn)
        row = get_auction(conn, aid)
    embed = render_auction_card(ACCENT, SETTINGS, row, bids=0)
    assert "Founder role" in embed.title
    assert embed.color == ACCENT
    assert "holographic" in _field(embed, "🎁 Up for auction")
    assert f"<@{HOST}>" in _field(embed, "🎙️ Hosted by")
    # No bids yet → shows the opening floor (min_bid), not a "current bid".
    assert "10" in _field(embed, "🔨 Opening bid")
    assert _field(embed, "🔨 Current bid") is None


def test_open_auction_card_shows_current_high_bid(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        row = get_auction(conn, aid)
    embed = render_auction_card(ACCENT, SETTINGS, row, bids=1)
    assert "40" in _field(embed, "🔨 Current bid")
    assert f"<@{A}>" in _field(embed, "🙋 High bidder")


def test_closed_with_winner_card_is_sold(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        end_auction_now(conn, GUILD, aid, now=NOW + 2)
        row = get_auction(conn, aid)
    embed = render_auction_card(ACCENT, SETTINGS, row, bids=1)
    assert "Sold" in embed.title
    assert embed.color == discord.Color.green()
    assert f"<@{A}>" in _field(embed, "🏆 Winner")
    assert "40" in _field(embed, "🔨 Winning bid")


def test_closed_with_no_bids_card(db):
    with open_db(db) as conn:
        aid = _open(conn)
        end_auction_now(conn, GUILD, aid, now=NOW + 2)
        row = get_auction(conn, aid)
    embed = render_auction_card(ACCENT, SETTINGS, row, bids=0)
    assert "closed" in embed.title.lower()
    assert _field(embed, "No bids") is not None


def test_cancelled_card_says_refunded(db):
    with open_db(db) as conn:
        aid = _open(conn)
        apply_credit(conn, GUILD, A, 100, "grant")
        place_bid(conn, SETTINGS, GUILD, aid, A, 40, now=NOW + 1)
        cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 2)
        row = get_auction(conn, aid)
    embed = render_auction_card(ACCENT, SETTINGS, row, bids=1)
    assert "cancelled" in embed.title.lower()
    assert embed.color == discord.Color.red()
    assert _field(embed, "↩️ Refunded") is not None


# ── the sticky card's lifecycle glue ────────────────────────────────────────
#
# The auction card is the only StickyPanel site in the bot that ENDS. These
# cover the two pieces of glue that make that safe: the build callback's
# refusal to render a finished auction (the resurrection guard) and the
# one-shot freeze repost that leaves the result at the bottom.


@pytest.fixture
def bot(db):
    b = MagicMock()
    b.ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    return b


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=ACCENT),
    ):
        yield


def _guild():
    return cast(discord.Guild, FakeGuild(id=GUILD))


def _text_channel(cid=1):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = cid
    ch.send = AsyncMock(return_value=SimpleNamespace(id=5555))
    ch.get_partial_message = MagicMock(
        return_value=SimpleNamespace(delete=AsyncMock())
    )
    return ch


async def test_build_auction_panel_renders_the_live_auction(bot, db):
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
    content = await build_auction_panel(bot, _guild())
    assert content is not None
    assert "Founder role" in (content.embed.title or "")
    # Open auctions carry the Bid button.
    assert content.view is not discord.utils.MISSING


@pytest.mark.parametrize(
    "finish",
    [
        pytest.param("end", id="closed"),
        pytest.param("cancel", id="cancelled"),
    ],
)
async def test_build_auction_panel_refuses_a_finished_auction(bot, db, finish):
    """The resurrection guard, and it is load-bearing rather than defensive.

    card_ids going to (0, 0) stops a restick being *armed*, but one armed just
    before settlement can still be in flight, and _place_locked reads a (0, 0)
    stored id as "not at the bottom" — it would post a fresh card for the
    finished auction. build() runs before send(), so refusing here is what
    makes that interleaving post nothing.
    """
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        if finish == "end":
            end_auction_now(conn, GUILD, aid, now=NOW + 60)
        else:
            cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 60)
    assert await build_auction_panel(bot, _guild()) is None


async def test_build_auction_panel_is_none_with_no_auction(bot):
    assert await build_auction_panel(bot, _guild()) is None


async def test_freeze_card_reposts_the_result_and_drops_the_old_card(bot, db):
    """The settlement ping lands below the card, so without this one repost
    the final result is left buried under the announcement of it."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        settled = end_auction_now(conn, GUILD, aid, now=NOW + 60)
    assert settled is not None

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    channel.send.assert_awaited_once()
    # The closed card is posted fresh, then the buried one is removed.
    channel.get_partial_message.assert_called_once_with(4242)
    # ...and the new id is recorded, so nothing is left pointing at a dead
    # message (the failure shape behind the casino hub storm).
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 5555
        assert card_ids(conn, GUILD) == (0, 0)  # still dormant


async def test_freeze_card_keeps_the_old_card_when_the_repost_fails(bot, db):
    """Post-before-delete: a failed send must not destroy the working card."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        end_auction_now(conn, GUILD, aid, now=NOW + 60)

    channel = _text_channel(cid=1)
    channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    channel.get_partial_message.assert_not_called()
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 4242  # untouched


async def test_freeze_card_does_nothing_when_no_card_was_ever_posted(bot, db):
    """An auction whose card never posted has nothing to move."""
    with open_db(db) as conn:
        aid = _open(conn)  # no attach_card — message_id stays 0
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)
    channel.send.assert_not_awaited()


async def test_freeze_card_uses_the_current_card_id_not_a_stale_snapshot(bot, db):
    """A restick that placed between the settle claim and the freeze has
    already replaced the card, and the caller's snapshot is a dead id.

    Deleting the snapshot would leave the re-sticked card — rendered while the
    auction was still open, Bid button and all — sitting above the frozen
    result forever. _freeze_card re-reads the row instead.
    """
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)          # the id the caller snapshots
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
        attach_card(conn, aid, 1, 7777)          # ...a restick moved it here

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    # The live card is removed, not the stale snapshot.
    channel.get_partial_message.assert_called_once_with(7777)


async def test_freeze_card_cannot_clobber_the_next_auctions_card(bot, db):
    """A mod may start the next auction while this freeze is mid-send.

    The freeze knows its own auction id, so it must write there — the
    state-blind attach_card_to_latest is only correct on the save_ids path,
    where a guild id is all there is. Writing to "the newest row" here would
    hand the new auction the dead card's ids, and its next restick would
    delete the frozen result and orphan its own card.
    """
    with open_db(db) as conn:
        old_id = _open(conn)
        attach_card(conn, old_id, 1, 4242)
        end_auction_now(conn, GUILD, old_id, now=NOW + 60)

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    # The next auction opens and posts its own card while the freeze runs.
    async def _send(*a, **kw):
        with open_db(db) as conn:
            new_id = open_auction(
                conn, SETTINGS, GUILD, created_by=HOST, title="Next up",
                description="", duration_hours=48.0, channel_id=1, now=NOW + 120,
            )
            attach_card(conn, new_id, 1, 9999)
        return SimpleNamespace(id=5555)

    channel.send = AsyncMock(side_effect=_send)
    await _freeze_card(bot, _guild(), old_id, 1)

    with open_db(db) as conn:
        assert int(get_auction(conn, old_id)["message_id"]) == 5555
        # The new auction keeps its own card, and stays stickable.
        assert card_ids(conn, GUILD) == (1, 9999)


@pytest.mark.parametrize(
    ("channel_kind", "expect", "blocking"),
    [
        pytest.param("thread", "threads", False, id="thread-never-sticks"),
        pytest.param(
            "panel", "stuck to the bottom", False, id="human-only-panel-warns"
        ),
        pytest.param("plain", None, False, id="ordinary-channel-no-warning"),
        # The two residents that follow the bot's own posts. An auction card
        # here is pushed up after every render and never comes back, so the
        # mod is sent elsewhere instead of being handed a broken auction.
        pytest.param(
            "bounty", "another channel", True, id="bounty-board-blocks"
        ),
        pytest.param(
            "casino", "another channel", True, id="casino-hub-blocks"
        ),
    ],
)
async def test_sticky_check_splits_warning_from_refusal(
    bot, db, channel_kind, expect, blocking
):
    """manual.html promises the card stays at the bottom. These are the channels
    where it can't — warned about when the loss is intermittent and visible,
    refused when the resident panel chases bot posts and the card is guaranteed
    to disappear."""
    if channel_kind == "thread":
        channel = MagicMock(spec=discord.Thread)
        channel.id = 77
    else:
        channel = _text_channel(cid=77)
    if channel_kind == "panel":
        with open_db(db) as conn:
            save_econ_settings(conn, GUILD, {"shop_channel_id": 77})
    if channel_kind == "bounty":
        with open_db(db) as conn:
            save_econ_settings(conn, GUILD, {"bounty_channel_id": 77})
    if channel_kind == "casino":
        with open_db(db) as conn:
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                (GUILD, "casino_panel_channel_id", "77"),
            )

    check = await _sticky_check(bot, _guild(), channel)
    if expect is None:
        assert check is None
    else:
        assert check is not None
        assert expect in check.message
        assert check.blocking is blocking


async def test_freeze_card_also_runs_for_a_cancelled_auction(bot, db):
    """Cancel ends the auction too, so the refund notice gets the same
    one-last-move treatment as a sold card."""
    with open_db(db) as conn:
        aid = _open(conn)
        attach_card(conn, aid, 1, 4242)
        cancel_auction(conn, GUILD, aid, resolver_id=HOST, now=NOW + 60)

    channel = _text_channel(cid=1)
    bot.get_channel = MagicMock(return_value=channel)
    bot.get_cog = MagicMock(return_value=None)

    await _freeze_card(bot, _guild(), aid, 1)

    posted = channel.send.await_args.kwargs["embed"]
    assert "cancelled" in (posted.title or "").lower()
    channel.get_partial_message.assert_called_once_with(4242)
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 5555


# ── priming the panel at start ──────────────────────────────────────────────


def _start_interaction(bot, channel):
    member = MagicMock(spec=discord.Member)
    member.id = HOST
    member.guild_permissions = discord.Permissions.all()
    member.roles = []
    inter = fake_interaction(guild=cast("FakeGuild", _guild()), user=member)
    inter.client = bot
    inter.channel = channel
    return inter


async def test_start_auction_refuses_the_bounty_boards_channel(bot, db):
    """The block must land before open_auction, not after.

    The old warning fired once the auction was already committed and the card
    posted, which for a guaranteed-invisible card is the worst of both: escrow
    rules now apply, the single-live-auction slot is consumed, and the mod's only
    way out is /bank auction cancel. Refusing up front costs nothing.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
            # The board channel *is* where the hub lives, and the hub follows
            # bot posts down since 2026-07-29.
            "bounty_channel_id": 77,
        })
    channel = _text_channel(cid=77)

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_not_awaited()  # no card posted
    with open_db(db) as conn:
        assert get_open_auction(conn, GUILD) is None, (
            "a refused auction must not consume the single-live-auction slot"
        )


async def test_start_auction_still_runs_where_the_clash_only_warns(bot, db):
    """The mirror of the test above: a human-only resident panel is a warning,
    so the auction must still open and post its card."""
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
            "shop_channel_id": 77,
        })
    channel = _text_channel(cid=77)
    panel = StickyPanel(
        "econ auction", MagicMock(),
        load_ids=lambda gid: (0, 0),
        save_ids=lambda gid, cid, mid: None,
        build=AsyncMock(),
    )
    bot.get_cog = MagicMock(return_value=SimpleNamespace(auction_panel=panel))

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_awaited_once()
    with open_db(db) as conn:
        assert get_open_auction(conn, GUILD) is not None


async def test_start_auction_drops_the_panels_stale_no_card_cache(bot, db):
    """The card is posted here, not through StickyPanel.place, so nothing
    calls ``_remember`` — and ``on_message`` caches "no panel" for five
    minutes off any member message in the guild. Without an explicit
    ``forget`` the brand-new auction simply does not stick until that entry
    lapses, which is the whole feature not working for up to 300s.
    """
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD, {
            "enabled": True,
            "auction_min_bid": 10,
            "auction_min_increment": 5,
            "auction_max_duration_hours": 168,
        })
    channel = _text_channel(cid=77)
    panel = StickyPanel(
        "econ auction", MagicMock(),
        load_ids=lambda gid: (0, 0),
        save_ids=lambda gid, cid, mid: None,
        build=AsyncMock(),
    )
    # Chat before the auction existed poisons the cache with (0, 0).
    panel._ref[GUILD] = (time.monotonic() + 300.0, 0, 0)
    bot.get_cog = MagicMock(return_value=SimpleNamespace(auction_panel=panel))

    await start_auction(
        _start_interaction(bot, channel),
        title="Founder role", prize="A shiny thing", duration_hours=48.0,
    )

    channel.send.assert_awaited_once()
    assert GUILD not in panel._ref, (
        "start_auction must drop the panel's cached ids, or the new card "
        "will not re-stick until the 300s TTL lapses"
    )
    with open_db(db) as conn:
        assert card_ids(conn, GUILD) == (77, 5555)
