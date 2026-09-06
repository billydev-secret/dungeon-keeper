"""A lobby lives five minutes from its last action, says so, warns its host,
and starts itself when it fills (duels-party-115).

The three group games swept a lobby 90 seconds after the last join with
nothing on the card about a clock: a full ten-player Musical Chairs lobby
expired under its host on 2026-08-17 while people were reading the rules, and
the card blamed the players ("Not enough players started in time").
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.cogs.musical_chairs import db as mcdb
from bot_modules.duels.db import LOBBY_IDLE_SECONDS, LOBBY_WARNING_SECONDS
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, FakeMessageableChannel, fake_interaction

GUILD = 9001
CH = 100


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest.fixture(autouse=True)
def _stub_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color.blurple()),
    ):
        yield


def _field(embed: discord.Embed, name: str):
    return next((f for f in embed.fields if f.name == name), None)


# ── the card ──────────────────────────────────────────────────────────────────


async def test_lobby_card_counts_down_to_the_close(db, sync_db_path):
    """The deadline is a live timestamp, and it is measured from the last
    action, not from when the lobby opened."""
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    last = time.time() - 42
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, last_action_at=last)
    game = await hpgdb.get_game(db, gid)

    embed = await cog._lobby_embed(game, bot.guild, 2, 10, 0)

    closes = _field(embed, "⏱️ Closes")
    assert closes is not None
    assert f"<t:{int(last + LOBBY_IDLE_SECONDS)}:R>" in (closes.value or "")
    assert "resets the clock" in (closes.value or "")
    assert "starts on its own once 10 are in" in (embed.description or "")


async def test_a_join_resets_the_clock_on_the_card(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await hpgdb.set_game_state(
        db, gid, "LOBBY", message_id=555, last_action_at=time.time() - 200,
    )
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild)

    await cog._handle_lobby_join(interaction, gid)

    embed = interaction.response.edit_message.await_args.kwargs["embed"]
    value = _field(embed, "⏱️ Closes").value or ""
    deadline = int(value[value.index("<t:") + 3 : value.index(":R>")])
    assert deadline == pytest.approx(time.time() + LOBBY_IDLE_SECONDS, abs=3)


# ── the sweep ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mod",
    [
        pytest.param(chdb, id="chicken"),
        pytest.param(hpgdb, id="hot-potato-group"),
        pytest.param(mcdb, id="musical-chairs"),
    ],
)
@pytest.mark.parametrize(
    ("age", "swept"),
    [
        pytest.param(LOBBY_IDLE_SECONDS - 10, False, id="inside-the-window"),
        pytest.param(LOBBY_IDLE_SECONDS + 10, True, id="past-the-window"),
        pytest.param(95, False, id="the-old-90s-window-is-gone"),
    ],
)
async def test_lobby_sweep_uses_the_shared_window(db, mod, age, swept):
    gid = await mod.create_lobby(db, GUILD, CH, 1, None)
    await mod.set_game_state(db, gid, "LOBBY", last_action_at=time.time() - age)
    ids = {g.id for g in await mod.fetch_sweepable_games(db, time.time())}
    assert (gid in ids) is swept


def test_the_window_is_five_minutes():
    assert LOBBY_IDLE_SECONDS == 300
    assert LOBBY_WARNING_SECONDS == 60


# ── the dead-lobby card is honest ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("roster_size", "min_players", "expected"),
    [
        pytest.param(4, 3, "Nobody pressed **▶️ Start** in time", id="full-but-unstarted"),
        pytest.param(1, 3, "Not enough people joined", id="short-of-the-floor"),
    ],
)
def test_dead_lobby_copy_says_what_happened(roster_size, min_players, expected):
    cog = ChickenCog.__new__(ChickenCog)
    copy = cog._dead_lobby_copy(roster_size, min_players)
    assert expected in copy
    assert "5 minutes" in copy


async def test_expired_lobby_goes_terminal(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    gid = await chdb.create_lobby(db, GUILD, CH, 1, None)
    await chdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2, 3]))
    await cog._expire_lobby(await chdb.get_game(db, gid))
    assert (await chdb.get_game(db, gid)).state == "EXPIRED_LOBBY"


# ── one warning to the host ───────────────────────────────────────────────────


async def test_host_is_warned_once_and_rearmed_by_a_join(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    bot.channel = FakeMessageableChannel(CH)  # type: ignore[assignment]
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    now = time.time()
    stale = now - (LOBBY_IDLE_SECONDS - LOBBY_WARNING_SECONDS) - 5
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, last_action_at=stale)

    await cog._warn_stale_lobbies(now)
    await cog._warn_stale_lobbies(now + 30)  # the sweep comes round again

    texts = bot.channel.texts
    assert len(texts) == 1
    assert "<@1>" in texts[0] and "▶️ Start" in texts[0]
    assert f"<t:{int(stale + LOBBY_IDLE_SECONDS)}:R>" in texts[0]

    # A join moves the clock; the next approach to the deadline warns again.
    later = now + 100
    await hpgdb.set_game_state(db, gid, "LOBBY", last_action_at=later)
    await cog._warn_stale_lobbies(later + (LOBBY_IDLE_SECONDS - LOBBY_WARNING_SECONDS) + 1)
    assert len(bot.channel.texts) == 2


@pytest.mark.parametrize("game_type", ["pressure", "quickdraw", "hot_potato"])
async def test_a_duel_has_no_lobby_to_warn_about(db, game_type):
    """pressure_games has no last_action_at column at all: the query must
    not run there, or the sweep logs an error every minute."""
    from bot_modules.duels import db as duels_db

    assert await duels_db.fetch_lobby_warning_ids(db, game_type, time.time()) == []


async def test_a_fresh_lobby_is_not_warned(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1])
    bot.channel = FakeMessageableChannel(CH)  # type: ignore[assignment]
    cog = HotPotatoGroupGameCog(bot)  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555, last_action_at=time.time())
    await cog._warn_stale_lobbies(time.time())
    assert bot.channel.texts == []


# ── a full lobby starts itself ────────────────────────────────────────────────


async def test_the_join_that_fills_the_lobby_starts_the_game(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    await chdb.upsert_config(db, GUILD, min_players=2, max_players=2)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await chdb.set_game_state(db, gid, "LOBBY", message_id=555, last_action_at=time.time())
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild)

    try:
        await cog._handle_lobby_join(interaction, gid)
        game = await chdb.get_game(db, gid)
        assert game.state == "ACTIVE"
        assert game.roster == [1, 2] and game.alive == [1, 2]
        # The lobby message became the game card in the joiner's own edit.
        embed = interaction.response.edit_message.await_args.kwargs["embed"]
        assert "Lobby" not in (embed.title or "")
    finally:
        cog._cancel_timers(gid)


async def test_a_join_short_of_the_ceiling_only_refreshes_the_card(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    await chdb.upsert_config(db, GUILD, min_players=2, max_players=3)
    gid = await chdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await chdb.set_game_state(db, gid, "LOBBY", message_id=555, last_action_at=time.time())
    interaction = fake_interaction(user=FakeMember(id=2), guild=bot.guild)

    await cog._handle_lobby_join(interaction, gid)

    game = await chdb.get_game(db, gid)
    assert game.state == "LOBBY" and game.roster == [1, 2]
    embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert "Lobby" in (embed.title or "")
