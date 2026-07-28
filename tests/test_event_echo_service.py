"""Event Echo's store and sender: dedupe, the destination gate, silence.

The cooldown *arithmetic* is tested in test_event_echo_logic; what's here is
whether the service applies it — and the three ways an echo can be correctly
refused (unconfigured, already-seen, cooled down), each of which must be
silent rather than an error.
"""
from __future__ import annotations

import types
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
        game_name="Marry, Fornicate, Kiss", origin_channel_id=777,
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

    async def test_unreachable_destination_is_not_an_error(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        bot.get_channel = MagicMock(return_value=None)
        bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "gone"))
        assert await echo(bot, guild) is False

    async def test_send_failure_is_swallowed(self, bot, guild, sync_db_path):
        configure(sync_db_path)
        bot.sent_channel.send.side_effect = discord.Forbidden(MagicMock(), "no perms")
        assert await echo(bot, guild) is False

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
    assert svc._opened_at(_FakeRow(row)) is None


def test_opened_at_reads_sqlite_utc_text():
    row = _FakeRow({"created_at": "2026-07-28 12:00:00"})
    assert svc._opened_at(row) == pytest.approx(1785240000.0, abs=1)


class _FakeRow(dict):
    """sqlite3.Row exposes keys() and __getitem__; dict does both already."""


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
    return _FakeRow(row)


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
            conn.execute(
                "INSERT INTO games_active_games "
                "(game_id, channel_id, message_id, game_type, host_id, state) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"g-{i}", 777, 888, "mfk", 1, state),
            )

    rows = await svc.live_games(GamesDb(sync_db_path))
    assert sorted(r["state"] for r in rows) == sorted(states)


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
