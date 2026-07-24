"""Tests for the auction card renderer (economy/auction_views.render_auction_card).

The card is the one pure-ish piece of the Discord glue — it turns an auction row
into an embed. The Bid flow and settle path are exercised through the service
(test_economy_auction_service). Here we assert each lifecycle state renders the
right title, currency, and fields off a real service row.
"""

from __future__ import annotations

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.auction_views import render_auction_card
from bot_modules.services.economy_auction_service import (
    cancel_auction,
    end_auction_now,
    get_auction,
    open_auction,
    place_bid,
)
from bot_modules.services.economy_service import EconSettings, apply_credit
from migrations import apply_migrations_sync

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
    apply_migrations_sync(path)
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
