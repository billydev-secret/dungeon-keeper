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

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.auction_views import (
    _freeze_card,
    build_auction_panel,
    render_auction_card,
)
from bot_modules.services.economy_auction_service import (
    attach_card,
    cancel_auction,
    card_ids,
    end_auction_now,
    get_auction,
    open_auction,
    place_bid,
)
from bot_modules.services.economy_service import EconSettings, apply_credit
from tests.db_template import migrated_db
from tests.fakes import FakeGuild

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
        "bot_modules.economy.auction_views.resolve_accent_color",
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

    await _freeze_card(bot, _guild(), aid, 1, 4242)

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

    await _freeze_card(bot, _guild(), aid, 1, 4242)

    channel.get_partial_message.assert_not_called()
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 4242  # untouched


async def test_freeze_card_does_nothing_when_no_card_was_ever_posted(bot, db):
    """An auction rolled back at start has no card to move."""
    with open_db(db) as conn:
        aid = _open(conn)
        end_auction_now(conn, GUILD, aid, now=NOW + 60)
    bot.get_channel = MagicMock()
    await _freeze_card(bot, _guild(), aid, 0, 0)
    bot.get_channel.assert_not_called()


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

    await _freeze_card(bot, _guild(), aid, 1, 4242)

    posted = channel.send.await_args.kwargs["embed"]
    assert "cancelled" in (posted.title or "").lower()
    channel.get_partial_message.assert_called_once_with(4242)
    with open_db(db) as conn:
        assert int(get_auction(conn, aid)["message_id"]) == 5555
