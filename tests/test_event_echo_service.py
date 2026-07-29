"""Event Echo's store and sender: dedupe, the destination gate, silence.

The cooldown *arithmetic* is tested in test_event_echo_logic; what's here is
whether the service applies it — and the three ways an echo can be correctly
refused (unconfigured, already-seen, cooled down), each of which must be
silent rather than an error.
"""
from __future__ import annotations

import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.services import event_echo_service as svc
from bot_modules.services.games_db import GamesDb
from bot_modules.services.event_echo_logic import (
    SOURCE_GAMEBOT,
    SOURCE_PARTY_GAME,
)

GUILD_ID = 4242
NOW = 1_800_000_000.0


# ── Store ───────────────────────────────────────────────────────────────────

class TestStore:
    def test_last_echo_times_empty(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            assert svc.last_echo_times(conn, GUILD_ID, "mfk") == (None, None)

    def test_last_echo_times_splits_own_key_from_any(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="mfk", ref="a", now=NOW - 500, suppressed=False)
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="story", ref="b", now=NOW - 100, suppressed=False)
            same, any_ = svc.last_echo_times(conn, GUILD_ID, "mfk")
        assert same == NOW - 500
        assert any_ == NOW - 100

    def test_suppressed_rows_never_extend_a_window(self, sync_db_path):
        """A refusal must not itself become the thing that refuses the next one.

        If suppressed rows counted, one busy minute would push the window out
        again on every tick and the feature would go permanently quiet.
        """
        with open_db(sync_db_path) as conn:
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="mfk", ref="a", now=NOW, suppressed=True)
            assert svc.last_echo_times(conn, GUILD_ID, "mfk") == (None, None)

    def test_claim_is_per_guild(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="mfk", ref="a", now=NOW, suppressed=False)
            assert svc.last_echo_times(conn, 999, "mfk") == (None, None)

    def test_claim_dedupes_on_ref(self, sync_db_path):
        """The poll loop re-sees the same lobby every 15s; only the first wins."""
        with open_db(sync_db_path) as conn:
            first = svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                                   echo_key="mfk", ref="game-1", now=NOW, suppressed=False)
            second = svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                                    echo_key="mfk", ref="game-1", now=NOW + 15,
                                    suppressed=False)
        assert first is True
        assert second is False

    def test_same_ref_in_a_different_source_is_not_a_duplicate(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            assert svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                                  echo_key="k", ref="1", now=NOW, suppressed=False)
            assert svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_GAMEBOT,
                                  echo_key="cah", ref="1", now=NOW, suppressed=False)

    def test_prune_drops_only_old_rows(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="k", ref="old", now=NOW - 200_000, suppressed=False)
            svc.claim_echo(conn, guild_id=GUILD_ID, source=SOURCE_PARTY_GAME,
                           echo_key="k", ref="new", now=NOW - 10, suppressed=False)
            assert svc.prune_echo_log(conn, NOW) == 1
            assert svc.last_echo_times(conn, GUILD_ID, "k") == (NOW - 10, NOW - 10)

    @pytest.mark.parametrize(
        "stored, expected",
        [
            pytest.param("", None, id="unset-means-off"),
            pytest.param("0", None, id="zero-means-off"),
            pytest.param("not-a-channel", None, id="garbage-means-off"),
            pytest.param("1469491363287531553", 1469491363287531553, id="real-snowflake"),
        ],
    )
    def test_echo_channel_id(self, sync_db_path, stored, expected):
        with open_db(sync_db_path) as conn:
            set_config_value(conn, svc.CONFIG_CHANNEL_KEY, stored, GUILD_ID)
            assert svc.echo_channel_id(conn, GUILD_ID) == expected


# ── Sender ──────────────────────────────────────────────────────────────────

@pytest.fixture
def bot(sync_db_path, monkeypatch):
    """A bot stub whose destination channel records what it was sent."""
    channel = MagicMock()
    channel.send = AsyncMock()

    stub = types.SimpleNamespace(
        ctx=types.SimpleNamespace(db_path=sync_db_path, guild_id=GUILD_ID),
        get_channel=MagicMock(return_value=channel),
        sent_channel=channel,
    )
    monkeypatch.setattr(
        svc, "resolve_accent_color", AsyncMock(return_value=discord.Color(0x5A32A8))
    )
    return stub


@pytest.fixture
def guild():
    g = MagicMock(spec=discord.Guild)
    g.id = GUILD_ID
    return g


def configure(db_path, channel_id="55501"):
    with open_db(db_path) as conn:
        set_config_value(conn, svc.CONFIG_CHANNEL_KEY, channel_id, GUILD_ID)


async def echo(bot, guild, *, ref="r1", key="mfk", now=NOW):
    return await svc.echo_event(
        bot, guild=guild, source=SOURCE_PARTY_GAME, echo_key=key, ref=ref,
        name="Marry, Fornicate, Kiss", origin_channel_id=777,
        url="https://discord.com/channels/1/2/3", now=now,
    )


@pytest.mark.asyncio
class TestEchoEvent:
    async def test_posts_when_configured(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await echo(bot, guild) is True
        bot.sent_channel.send.assert_awaited_once()

    async def test_unconfigured_channel_posts_nothing(self, bot, guild):
        """Unset is off. No default channel is invented."""
        assert await echo(bot, guild) is False
        bot.sent_channel.send.assert_not_awaited()

    async def test_unconfigured_does_not_burn_the_ref(self, bot, guild, sync_db_path):
        """Turning the feature on shouldn't find every game already 'handled'."""
        assert await echo(bot, guild) is False
        configure(sync_db_path)
        assert await echo(bot, guild) is True

    async def test_echo_is_silent(self, bot, guild, sync_db_path):
        """The whole design rests on this post not notifying anyone."""
        configure(sync_db_path)
        await echo(bot, guild)
        kwargs = bot.sent_channel.send.await_args.kwargs
        assert kwargs["allowed_mentions"].roles is False
        assert kwargs["allowed_mentions"].everyone is False
        assert kwargs["allowed_mentions"].users is False
        assert "content" not in kwargs  # embed only — no text line to carry a ping

    async def test_same_ref_posts_once(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await echo(bot, guild, ref="game-1") is True
        assert await echo(bot, guild, ref="game-1", now=NOW + 15) is False
        assert bot.sent_channel.send.await_count == 1

    async def test_global_floor_blocks_a_different_game(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await echo(bot, guild, ref="a", key="mfk") is True
        assert await echo(bot, guild, ref="b", key="story", now=NOW + 60) is False
        assert await echo(bot, guild, ref="c", key="story", now=NOW + 700) is True
        assert bot.sent_channel.send.await_count == 2

    async def test_per_type_cooldown_blocks_a_repeat(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await echo(bot, guild, ref="a", key="mfk") is True
        # Past the global floor, still inside mfk's hour.
        assert await echo(bot, guild, ref="b", key="mfk", now=NOW + 1200) is False
        assert await echo(bot, guild, ref="c", key="mfk", now=NOW + 3700) is True

    async def test_a_suppressed_game_stays_suppressed(self, bot, guild, sync_db_path):
        """Skip means skip — not 'announce it later when it's stale'.

        The poll loop re-offers the same lobby every tick, so without the
        suppressed row this game would post the moment its cooldown expired,
        by which point it is an hour old and probably over.
        """
        configure(sync_db_path)
        await echo(bot, guild, ref="a", key="mfk")
        assert await echo(bot, guild, ref="b", key="story", now=NOW + 60) is False
        assert await echo(bot, guild, ref="b", key="story", now=NOW + 5000) is False
        assert bot.sent_channel.send.await_count == 1

    @pytest.mark.parametrize(
        "break_it",
        [
            pytest.param("unreachable", id="channel-unreachable"),
            pytest.param("forbidden", id="send-forbidden"),
            pytest.param("accent", id="accent-lookup-raises"),
        ],
    )
    async def test_a_send_that_never_landed_does_not_burn_the_cooldown(
        self, bot, guild, sync_db_path, monkeypatch, break_it
    ):
        """An echo nobody saw must not refuse the next real game.

        The claim is taken before the send (so a crash loses an echo rather
        than repeating one), which means a *known* failure has to release it —
        otherwise picking a channel the bot can't post in silently blocks
        every game for the length of both cooldowns.
        """
        configure(sync_db_path)
        if break_it == "unreachable":
            bot.get_channel = MagicMock(return_value=None)
            bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "x"))
        elif break_it == "forbidden":
            bot.sent_channel.send.side_effect = discord.Forbidden(MagicMock(), "no perms")
        else:
            monkeypatch.setattr(
                svc, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("boom"))
            )

        assert await echo(bot, guild, ref="failed") is False
        with open_db(sync_db_path) as conn:
            assert svc.last_echo_times(conn, GUILD_ID, "mfk") == (None, None)

    async def test_a_failed_ref_is_not_retried_every_tick(self, bot, guild, sync_db_path):
        """Released, but still claimed — the sweep must not hammer a dead channel."""
        configure(sync_db_path)
        bot.sent_channel.send.side_effect = discord.Forbidden(MagicMock(), "no perms")
        assert await echo(bot, guild, ref="game-1") is False
        bot.sent_channel.send.side_effect = None
        assert await echo(bot, guild, ref="game-1", now=NOW + 15) is False
        # A *different* game still gets its chance — the window wasn't burned.
        assert await echo(bot, guild, ref="game-2", now=NOW + 30) is True


# ── Gamebot source ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestGamebotLobby:
    def _message(self, guild):
        msg = MagicMock(spec=discord.Message)
        msg.id = 9001
        msg.guild = guild
        msg.channel = MagicMock()
        msg.channel.id = 1483834493491085412
        msg.jump_url = "https://discord.com/channels/1/2/9001"
        return msg

    async def test_cah_is_echoed(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await svc.echo_gamebot_lobby(bot, self._message(guild), "cah") is True

    @pytest.mark.parametrize("sub_game", ["connect4", "anagrams", "chess"])
    async def test_other_gamebot_games_are_not(self, bot, guild, sync_db_path, sub_game):
        """Scope is CAH; two-player and quickfire games don't warrant main chat."""
        configure(sync_db_path)
        assert await svc.echo_gamebot_lobby(bot, self._message(guild), sub_game) is False
        bot.sent_channel.send.assert_not_awaited()

    async def test_post_then_edit_yields_one_echo(self, bot, guild, sync_db_path):
        """Gamebot posts "Loading…" then edits the real embed in.

        Both paths call the collector, so the echo fires twice for one game
        unless the message id dedupes it.
        """
        configure(sync_db_path)
        msg = self._message(guild)
        assert await svc.echo_gamebot_lobby(bot, msg, "cah") is True
        assert await svc.echo_gamebot_lobby(bot, msg, "cah") is False
        assert bot.sent_channel.send.await_count == 1


# ── created_at parsing ──────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="null"),
        pytest.param("", id="empty"),
        pytest.param("not a timestamp", id="garbage"),
    ],
)
def test_opened_at_tolerates_bad_timestamps(raw):
    """An unparseable created_at must not take the sweep down."""
    row = {"created_at": raw}
    assert svc._opened_at(row) is None


def test_opened_at_reads_sqlite_utc_text():
    row = dict({"created_at": "2026-07-28 12:00:00"})
    assert svc._opened_at(row) == pytest.approx(1785240000.0, abs=1)


# ── The party-game sweep ────────────────────────────────────────────────────

def _game_row(**over):
    row = {
        "game_id": "g-1",
        "channel_id": 777,
        "message_id": 888,
        "game_type": "mfk",
        "host_id": 12345,
        "state": "open",
        "created_at": None,
    }
    row.update(over)
    return row


@pytest.mark.asyncio
class TestProcessGame:
    @pytest.fixture
    def sweep_bot(self, bot, guild):
        """The game's own channel resolves to a guild, as in production."""
        origin = MagicMock()
        origin.guild = guild
        guild.get_member = MagicMock(return_value=None)
        dest = bot.sent_channel
        bot.get_channel = MagicMock(
            side_effect=lambda cid: origin if cid == 777 else dest
        )
        return bot

    async def test_guild_comes_from_the_games_own_channel(self, sweep_bot, sync_db_path):
        """games_active_games has no guild_id; the channel is the only source.

        Using the home guild id instead would build a jump link whose guild
        segment points at the wrong server — a dead link.
        """
        configure(sync_db_path)
        await svc._process_game(sweep_bot, _game_row(), NOW)
        embed = sweep_bot.sent_channel.send.await_args.kwargs["embed"]
        assert f"/{GUILD_ID}/777/888" in embed.description

    async def test_a_game_in_an_unknown_channel_is_skipped(self, bot, sync_db_path):
        configure(sync_db_path)
        bot.get_channel = MagicMock(return_value=None)
        await svc._process_game(bot, _game_row(), NOW)
        bot.sent_channel.send.assert_not_awaited()

    async def test_a_lobby_with_no_message_yet_waits(self, sweep_bot, sync_db_path):
        """create_game runs before the lobby is posted — there's nothing to link."""
        configure(sync_db_path)
        await svc._process_game(sweep_bot, _game_row(message_id=None), NOW)
        sweep_bot.sent_channel.send.assert_not_awaited()

    async def test_a_stale_game_is_skipped(self, sweep_bot, sync_db_path):
        """After downtime the sweep sees every open game; old ones stay quiet."""
        configure(sync_db_path)
        row = _game_row(created_at="2026-07-28 12:00:00")
        await svc._process_game(sweep_bot, row, 1785240000.0 + 5000)
        sweep_bot.sent_channel.send.assert_not_awaited()

    async def test_host_is_named_when_resolvable(self, sweep_bot, guild, sync_db_path):
        configure(sync_db_path)
        member = MagicMock()
        member.display_name = "Ada"
        guild.get_member = MagicMock(return_value=member)
        await svc._process_game(sweep_bot, _game_row(), NOW)
        embed = sweep_bot.sent_channel.send.await_args.kwargs["embed"]
        assert embed.footer.text == "Hosted by Ada"

    async def test_an_unknown_game_type_still_gets_a_name(self, sweep_bot, sync_db_path):
        """A new game shows up in main chat the day it ships."""
        configure(sync_db_path)
        await svc._process_game(sweep_bot, _game_row(game_type="brand_new"), NOW)
        embed = sweep_bot.sent_channel.send.await_args.kwargs["embed"]
        assert "Brand New" in (embed.title or "")


SWEEP_NOW = 1785240000.0  # matches "2026-07-28 12:00:00" UTC


def _insert_game(conn, game_id, *, state="open", message_id=888, created_at=None):
    conn.execute(
        "INSERT INTO games_active_games "
        "(game_id, channel_id, message_id, game_type, host_id, state, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (game_id, 777, message_id, "mfk", 1, state, created_at or "2026-07-28 11:58:00"),
    )


@pytest.mark.asyncio
async def test_live_games_returns_every_state(sync_db_path):
    """The sweep's query must not enumerate states.

    The six lobby games sit in 'joining' and most others in 'open', but
    wyr / nhie / price are created straight into 'playing' — and all three are
    schedulable, so a state filter silently excluded them from a feature whose
    docs promise scheduled games are covered.
    """
    states = ["joining", "open", "playing"]
    with open_db(sync_db_path) as conn:
        for i, state in enumerate(states):
            _insert_game(conn, f"g-{i}", state=state)

    rows = await svc.live_games(GamesDb(sync_db_path), SWEEP_NOW)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_live_games_filters_the_cheap_cases_in_sql(sync_db_path):
    """Unposted lobbies and stale rows never reach Python.

    Both are re-checked downstream; doing the bulk cut in SQL is what keeps a
    15s sweep off rows it can't act on.
    """
    with open_db(sync_db_path) as conn:
        _insert_game(conn, "echoable")
        _insert_game(conn, "no-lobby-yet", message_id=None)
        _insert_game(conn, "stale", created_at="2026-07-28 09:00:00")

    rows = await svc.live_games(GamesDb(sync_db_path), SWEEP_NOW)
    assert [r["game_id"] for r in rows] == ["echoable"]


@pytest.mark.asyncio
async def test_live_games_does_not_read_the_payload_blob(sync_db_path):
    """payload holds rosters and story text — kilobytes this sweep never uses."""
    with open_db(sync_db_path) as conn:
        _insert_game(conn, "g-1")

    rows = await svc.live_games(GamesDb(sync_db_path), SWEEP_NOW)
    assert "payload" not in rows[0].keys()


# ── Economy sweeps: auctions, pools, bounties ───────────────────────────────

def _open_auction(conn, aid, *, ends_at, state="open", message_id=900):
    conn.execute(
        "INSERT INTO econ_auctions "
        "(id, guild_id, channel_id, message_id, title, created_by, state, "
        " min_bid, min_increment, soft_close_seconds, ends_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, GUILD_ID, 555, message_id, "A rare hat", 1, state, 10, 1, 60,
         ends_at, ends_at - 86400),
    )


def _pools_round(conn, rid, *, closes_at, status="open", message_id=900):
    # local_day varies with rid: the schema allows only one round per guild
    # per measured day, so two rows in one test must be different days.
    conn.execute(
        "INSERT INTO casino_pools_rounds "
        "(id, guild_id, channel_id, message_id, status, local_day, line, "
        " opened_at, closes_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rid, GUILD_ID, 555, message_id, status, f"2026-07-{rid:02d}", 12.5,
         closes_at - 64800, closes_at),
    )


def _bounty(conn, bid, *, created_at, state="open", card_message_id=900):
    conn.execute(
        "INSERT INTO econ_bounties "
        "(id, guild_id, poster_id, title, state, card_channel_id, "
        " card_message_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (bid, GUILD_ID, 1, "Draw my cat", state, 555, card_message_id, created_at),
    )


class TestEconSweeps:
    @pytest.mark.parametrize(
        "ends_at, found",
        [
            pytest.param(NOW + 1800, True, id="inside-the-hour"),
            pytest.param(NOW + 7200, False, id="too-early"),
            pytest.param(NOW - 60, False, id="already-ended"),
        ],
    )
    def test_closing_auctions_window(self, sync_db_path, ends_at, found):
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=ends_at)
            assert bool(svc.closing_auctions(conn, NOW)) is found

    @pytest.mark.parametrize(
        "state, message_id, found",
        [
            pytest.param("open", 900, True, id="live"),
            pytest.param("closed", 900, False, id="already-closed"),
            pytest.param("cancelled", 900, False, id="cancelled"),
            pytest.param("open", 0, False, id="no-card-to-link-to"),
        ],
    )
    def test_closing_auctions_state(self, sync_db_path, state, message_id, found):
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=NOW + 1800, state=state,
                          message_id=message_id)
            assert bool(svc.closing_auctions(conn, NOW)) is found

    @pytest.mark.parametrize(
        "closes_at, found",
        [
            pytest.param(NOW + 600, True, id="betting-shuts-soon"),
            pytest.param(NOW + 7200, False, id="too-early"),
            # A round sits in status='open' for hours after betting shuts,
            # waiting to settle (migration 140), so status alone would
            # announce "last call" to a round that stopped taking bets before
            # lunch. Only closes_at can tell them apart.
            pytest.param(NOW - 3600, False, id="betting-already-shut"),
        ],
    )
    def test_pools_filters_on_closes_at_not_status(
        self, sync_db_path, closes_at, found
    ):
        with open_db(sync_db_path) as conn:
            _pools_round(conn, 1, closes_at=closes_at)
            assert bool(svc.closing_pools(conn, NOW)) is found

    @pytest.mark.parametrize(
        "created_at, state, found",
        [
            pytest.param(NOW - 60, "open", True, id="just-posted"),
            # A restart must not announce a backlog of old bounties.
            pytest.param(NOW - 9000, "open", False, id="stale"),
            pytest.param(NOW - 60, "awarded", False, id="already-awarded"),
            pytest.param(NOW - 60, "cancelled", False, id="cancelled"),
        ],
    )
    def test_new_bounties(self, sync_db_path, created_at, state, found):
        with open_db(sync_db_path) as conn:
            _bounty(conn, 1, created_at=created_at, state=state)
            assert bool(svc.new_bounties(conn, NOW)) is found

    def test_a_bounty_with_no_card_is_skipped(self, sync_db_path):
        """econ_bounty_channel_id is 0 in prod, so cards have nowhere to post."""
        with open_db(sync_db_path) as conn:
            _bounty(conn, 1, created_at=NOW - 60, card_message_id=0)
            assert svc.new_bounties(conn, NOW) == []


# Sunday 23:30 UTC — half an hour before the week rolls.
RAFFLE_NOW = datetime(2026, 8, 2, 23, 30, tzinfo=timezone.utc).timestamp()


def _enable_raffle(conn, *, shop_channel="555", shop_message="900"):
    set_config_value(conn, "econ_raffle_enabled", "1", GUILD_ID)
    set_config_value(conn, "econ_price_raffle_ticket", "10", GUILD_ID)
    set_config_value(conn, "econ_shop_channel_id", shop_channel, GUILD_ID)
    set_config_value(conn, "econ_shop_message_id", shop_message, GUILD_ID)


class TestRaffleLastCall:
    def test_fires_in_the_final_hour(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            _enable_raffle(conn)
            call = svc.raffle_last_call(conn, GUILD_ID, RAFFLE_NOW)
        assert call is not None
        assert call.iso_week == "2026-W31"
        assert call.message_id == 900

    def test_quiet_earlier_in_the_week(self, sync_db_path):
        with open_db(sync_db_path) as conn:
            _enable_raffle(conn)
            midweek = RAFFLE_NOW - 3 * 86400
            assert svc.raffle_last_call(conn, GUILD_ID, midweek) is None

    def test_disabled_raffle_is_silent(self, sync_db_path):
        """Never advertise a draw that isn't going to happen."""
        with open_db(sync_db_path) as conn:
            _enable_raffle(conn)
            set_config_value(conn, "econ_raffle_enabled", "0", GUILD_ID)
            assert svc.raffle_last_call(conn, GUILD_ID, RAFFLE_NOW) is None

    @pytest.mark.parametrize(
        "channel, message",
        [
            pytest.param("0", "900", id="no-shop-channel"),
            pytest.param("555", "0", id="no-shop-message"),
            pytest.param("555", "not-a-snowflake", id="garbage"),
        ],
    )
    def test_no_shop_panel_means_no_echo(self, sync_db_path, channel, message):
        """"The raffle closes soon" with nowhere to buy is just an alarm."""
        with open_db(sync_db_path) as conn:
            _enable_raffle(conn, shop_channel=channel, shop_message=message)
            assert svc.raffle_last_call(conn, GUILD_ID, RAFFLE_NOW) is None

    def test_zero_entrants_still_gets_the_nudge(self, sync_db_path):
        """No tickets sold is exactly when the reminder is worth most."""
        with open_db(sync_db_path) as conn:
            _enable_raffle(conn)
            assert conn.execute(
                "SELECT COUNT(*) c FROM econ_raffle_tickets"
            ).fetchone()["c"] == 0
            assert svc.raffle_last_call(conn, GUILD_ID, RAFFLE_NOW) is not None


@pytest.mark.asyncio
class TestEconEchoes:
    @pytest.fixture
    def econ_bot(self, bot, guild):
        bot.get_guild = MagicMock(return_value=guild)
        return bot

    async def test_auction_last_call_posts(self, econ_bot, sync_db_path):
        configure(sync_db_path)
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=NOW + 1800)
        await svc._sweep_econ(econ_bot, NOW)
        embed = econ_bot.sent_channel.send.await_args.kwargs["embed"]
        assert "Last call" in (embed.title or "")
        assert "A rare hat" in (embed.title or "")
        assert f"<t:{int(NOW + 1800)}:R>" in (embed.description or "")

    async def test_a_deadline_echo_beats_the_global_floor(
        self, econ_bot, guild, sync_db_path
    ):
        """The point of the exemption, end to end.

        A game echo seconds earlier would refuse any other game — the auction
        must still get through, because there is no later moment for it.
        """
        configure(sync_db_path)
        assert await echo(econ_bot, guild, ref="a-game") is True
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=NOW + 1800)
        await svc._sweep_econ(econ_bot, NOW + 30)
        assert econ_bot.sent_channel.send.await_count == 2

    async def test_the_same_auction_is_echoed_once(self, econ_bot, sync_db_path):
        """Exempt from the cooldowns still means once per auction.

        A late bid extends ends_at, keeping the auction inside the window for
        many more ticks; only the claim stops it re-announcing.
        """
        configure(sync_db_path)
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=NOW + 1800)
        await svc._sweep_econ(econ_bot, NOW)
        await svc._sweep_econ(econ_bot, NOW + 15)
        assert econ_bot.sent_channel.send.await_count == 1

    async def test_pools_names_its_line(self, econ_bot, sync_db_path):
        configure(sync_db_path)
        with open_db(sync_db_path) as conn:
            _pools_round(conn, 1, closes_at=NOW + 600)
        await svc._sweep_econ(econ_bot, NOW)
        embed = econ_bot.sent_channel.send.await_args.kwargs["embed"]
        assert "12.5" in (embed.title or "")

    async def test_bounty_is_a_start_echo_not_a_deadline(
        self, econ_bot, guild, sync_db_path
    ):
        """New bounties are ordinary echoes — the floor still applies."""
        configure(sync_db_path)
        assert await echo(econ_bot, guild, ref="a-game") is True
        with open_db(sync_db_path) as conn:
            _bounty(conn, 1, created_at=NOW - 60)
        await svc._sweep_econ(econ_bot, NOW + 30)
        assert econ_bot.sent_channel.send.await_count == 1

    async def test_raffle_links_to_the_shop_panel(self, econ_bot, sync_db_path):
        """The best jump target of any source — the buy button is right there."""
        with open_db(sync_db_path) as conn:
            set_config_value(conn, svc.CONFIG_CHANNEL_KEY, "55501", GUILD_ID)
            _enable_raffle(conn)
        await svc._sweep_econ(econ_bot, RAFFLE_NOW)
        embed = econ_bot.sent_channel.send.await_args.kwargs["embed"]
        assert "Last call" in (embed.title or "")
        assert f"/{GUILD_ID}/555/900" in (embed.description or "")

    async def test_the_raffle_is_echoed_once_per_week(self, econ_bot, sync_db_path):
        """Many ticks fall inside the final hour; the week is the identity."""
        with open_db(sync_db_path) as conn:
            set_config_value(conn, svc.CONFIG_CHANNEL_KEY, "55501", GUILD_ID)
            _enable_raffle(conn)
        await svc._sweep_econ(econ_bot, RAFFLE_NOW)
        await svc._sweep_econ(econ_bot, RAFFLE_NOW + 900)
        assert econ_bot.sent_channel.send.await_count == 1

    async def test_econ_echoes_are_silent_too(self, econ_bot, sync_db_path):
        configure(sync_db_path)
        with open_db(sync_db_path) as conn:
            _open_auction(conn, 1, ends_at=NOW + 1800)
        await svc._sweep_econ(econ_bot, NOW)
        allowed = econ_bot.sent_channel.send.await_args.kwargs["allowed_mentions"]
        assert (allowed.roles, allowed.everyone, allowed.users) == (False, False, False)


# ── Discord events ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestDiscordEvent:
    def _event(self, guild, channel_id=None):
        ev = MagicMock(spec=discord.ScheduledEvent)
        ev.id = 5150
        ev.guild = guild
        ev.name = "Movie Night"
        ev.url = "https://discord.com/events/1/5150"
        ev.creator = None
        if channel_id is None:
            ev.channel = None
        else:
            ev.channel = MagicMock()
            ev.channel.id = channel_id
        return ev

    async def test_uses_event_copy_not_game_copy(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        assert await svc.echo_discord_event(bot, self._event(guild, 4242)) is True
        embed = bot.sent_channel.send.await_args.kwargs["embed"]
        assert "A game is open" not in (embed.description or "")
        assert "<#4242>" in (embed.description or "")

    async def test_external_event_renders_no_dead_channel_mention(
        self, bot, guild, sync_db_path
    ):
        """An `external` event has a location string and no channel.

        Falling back to the guild id would render `<#guild_id>` — a mention
        Discord can't resolve, shown to members as a dead link.
        """
        configure(sync_db_path)
        assert await svc.echo_discord_event(bot, self._event(guild)) is True
        embed = bot.sent_channel.send.await_args.kwargs["embed"]
        assert "<#" not in (embed.description or "")
        assert str(GUILD_ID) not in (embed.description or "")

    async def test_repeated_updates_echo_once(self, bot, guild, sync_db_path):
        """Discord emits on_scheduled_event_update generously."""
        configure(sync_db_path)
        ev = self._event(guild, 4242)
        assert await svc.echo_discord_event(bot, ev) is True
        assert await svc.echo_discord_event(bot, ev) is False
        assert bot.sent_channel.send.await_count == 1
