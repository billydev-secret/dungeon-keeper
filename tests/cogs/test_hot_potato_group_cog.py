"""Cog-runtime tests for Hot Potato (group).

Covers the parts unit/CRUD tests don't reach: the shared elimination brain
(`_group_eliminate`), `_handle_group_button` routing, the lobby join/start flow,
and a load smoke for the 3-level Cog + class-attr app_commands.Group.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.hot_potato_group import db as hpgdb
from bot_modules.cogs.hot_potato_group.cog import HotPotatoGroupGameCog
from bot_modules.core.db_utils import open_db
from bot_modules.duels import db as duels_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeGuild, fake_interaction

GUILD = 9001
CH = 100


class FakeBot:
    """Minimal bot: real games_db, no-op view registry, no guild/channel resolution."""

    def __init__(self, db: GamesDb, guild: FakeGuild | None = None) -> None:
        self.games_db = db
        self._guild = guild

    def add_view(self, *args, **kwargs) -> None:
        pass

    def get_guild(self, guild_id: int):
        return self._guild

    def get_channel(self, channel_id: int):
        return None


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest.fixture(autouse=True)
def _stub_lobby_accent():
    """The lobby embed (base_game) and the active game embed (this cog, via
    ``_prime_accent`` on start/resume) both resolve the guild accent; the
    FakeGuild here has no real avatar to read, so stub both import paths."""
    with (
        patch(
            "bot_modules.duels.base_game.resolve_accent_color",
            new=AsyncMock(return_value=discord.Color.blurple()),
        ),
        patch(
            "bot_modules.cogs.hot_potato_group.cog.resolve_accent_color",
            new=AsyncMock(return_value=discord.Color.blurple()),
        ),
    ):
        yield


@pytest_asyncio.fixture
async def cog(db: GamesDb) -> HotPotatoGroupGameCog:
    return HotPotatoGroupGameCog(FakeBot(db))  # type: ignore[arg-type]


async def _active_game(db: GamesDb, alive: list[int], **extra):
    gid = await hpgdb.create_lobby(db, GUILD, CH, alive[0], extra.pop("stakes_text", None))
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps(alive),
        alive=json.dumps(alive),
        elimination_order=json.dumps(extra.pop("elimination_order", [])),
        **extra,
    )
    return await hpgdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_cog_instantiates_and_exposes_group(db):
    cog = HotPotatoGroupGameCog(FakeBot(db))  # type: ignore[arg-type]
    cmds = {c.name for c in cog.get_app_commands()}
    assert "hotpotatogroup" in cmds
    assert cog.GAME_KEY == "hot_potato_group"


def test_build_game_view_has_pass_button(db):
    cog = HotPotatoGroupGameCog(FakeBot(db))  # type: ignore[arg-type]
    view = cog.build_game_view(7)
    ids = [getattr(c, "custom_id", None) for c in view.children]
    assert "hpg_pass:7" in ids


# ── _group_eliminate (shared brain) ────────────────────────────────────────────

async def test_group_eliminate_non_terminal_shrinks_roster(cog, db):
    game = await _active_game(db, [1, 2, 3])
    await cog._group_eliminate(game, 1, interaction=None)
    refreshed = await hpgdb.get_game(db, game.id)
    assert refreshed.state == "ACTIVE"
    assert refreshed.alive == [2, 3]
    assert refreshed.elimination_order == [1]
    assert refreshed.winner_id is None


async def test_group_eliminate_terminal_sets_winner_and_loser(cog, db):
    game = await _active_game(db, [1, 2])
    await cog._group_eliminate(game, 1, interaction=None)
    refreshed = await hpgdb.get_game(db, game.id)
    assert refreshed.state == "RESOLVED"
    assert refreshed.winner_id == 2          # sole survivor
    assert refreshed.loser_id == 1           # last eliminated


async def test_group_eliminate_terminal_sets_group_cooldowns(cog, db):
    game = await _active_game(db, [1, 2])
    await cog._group_eliminate(game, 1, interaction=None)
    # every roster member is now on group cooldown
    for uid in (1, 2):
        remaining = await duels_db.check_group_cooldown(
            db, GUILD, "hot_potato_group", uid, 48
        )
        assert remaining is not None and remaining > 0


async def test_detonate_final_resolves_and_pays(db, sync_db_path):
    """The last detonation pays the whole roster — including players
    eliminated in earlier rounds — with the win bonus for the survivor."""
    with open_db(sync_db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    cog = HotPotatoGroupGameCog(FakeEconGamesBot(db, sync_db_path, [1, 2, 3]))  # type: ignore[arg-type]
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, None)
    now = time.time()
    await hpgdb.set_game_state(
        db, gid, "ACTIVE",
        roster=json.dumps([1, 2, 3]),
        alive=json.dumps([1, 2]),
        elimination_order=json.dumps([3]),
        holder_id=1,
        fuse_seconds=5.0,
        phase_started_at=now - 6.0,
        pass_log=json.dumps([{"holder_id": 1, "received_at": now - 3.0, "passed_at": None}]),
    )
    await cog._detonate(gid)
    g = await hpgdb.get_game(db, gid)
    assert g.state == "RESOLVED"
    assert g.winner_id == 2
    assert g.loser_id == 1
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 2) == 25   # participation + win
        assert get_balance(conn, GUILD, 1) == 5
        assert get_balance(conn, GUILD, 3) == 5


# ── _handle_group_button routing ───────────────────────────────────────────────

async def test_handle_group_button_continue_rerenders(cog, db, monkeypatch):
    game = await _active_game(db, [1, 2, 3], holder_id=1)
    monkeypatch.setattr(cog, "handle_interaction", AsyncMock(return_value=("continue", None)))
    interaction = fake_interaction(guild=FakeGuild())
    await cog._handle_group_button(interaction, game.id)
    interaction.response.defer.assert_awaited()
    interaction.edit_original_response.assert_awaited()
    assert (await hpgdb.get_game(db, game.id)).state == "ACTIVE"


async def test_handle_group_button_eliminate_routes(cog, db, monkeypatch):
    game = await _active_game(db, [1, 2, 3], holder_id=1)
    monkeypatch.setattr(cog, "handle_interaction", AsyncMock(return_value=("eliminate", 1)))
    interaction = fake_interaction(guild=FakeGuild())
    await cog._handle_group_button(interaction, game.id)
    refreshed = await hpgdb.get_game(db, game.id)
    assert refreshed.alive == [2, 3]
    assert refreshed.elimination_order == [1]


async def test_handle_group_button_done_resolves_with_last_eliminated(cog, db, monkeypatch):
    game = await _active_game(db, [2, 3], elimination_order=[1])
    monkeypatch.setattr(cog, "handle_interaction", AsyncMock(return_value=("done", 2)))
    interaction = fake_interaction(guild=FakeGuild())
    await cog._handle_group_button(interaction, game.id)
    refreshed = await hpgdb.get_game(db, game.id)
    assert refreshed.state == "RESOLVED"
    assert refreshed.winner_id == 2
    assert refreshed.loser_id == 1  # last eliminated


async def test_handle_group_button_rejected_no_state_change(cog, db, monkeypatch):
    game = await _active_game(db, [1, 2, 3], holder_id=1)
    monkeypatch.setattr(cog, "handle_interaction", AsyncMock(return_value=("rejected", None)))
    interaction = fake_interaction(guild=FakeGuild())
    await cog._handle_group_button(interaction, game.id)
    refreshed = await hpgdb.get_game(db, game.id)
    assert refreshed.alive == [1, 2, 3]
    interaction.edit_original_response.assert_not_awaited()


async def test_handle_group_button_ignores_non_active(cog, db, monkeypatch):
    game = await _active_game(db, [1, 2, 3])
    await hpgdb.set_game_state(db, game.id, "RESOLVED")
    handler = AsyncMock(return_value=("continue", None))
    monkeypatch.setattr(cog, "handle_interaction", handler)
    interaction = fake_interaction(guild=FakeGuild())
    await cog._handle_group_button(interaction, game.id)
    handler.assert_not_awaited()
    interaction.followup.send.assert_awaited()


# ── lobby join / start flow (custom-stakes mode → skips nick preflight) ─────────

async def test_lobby_join_appends_to_roster(cog, db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await hpgdb.set_game_state(db, gid, "LOBBY", message_id=555)
    interaction = fake_interaction(user_id=2)  # type: ignore[call-arg]
    interaction.user.id = 2
    interaction.guild = FakeGuild()
    await cog._handle_lobby_join(interaction, gid)
    refreshed = await hpgdb.get_game(db, gid)
    assert refreshed.roster == [1, 2]
    interaction.response.edit_message.assert_awaited()


async def test_lobby_join_rejects_duplicate(cog, db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    interaction = fake_interaction()
    interaction.user.id = 1  # already host
    interaction.guild = FakeGuild()
    await cog._handle_lobby_join(interaction, gid)
    interaction.response.send_message.assert_awaited()
    assert (await hpgdb.get_game(db, gid)).roster == [1]


async def test_lobby_start_requires_min_players(cog, db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")  # only host
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    await cog._handle_lobby_start(interaction, gid)
    # default min_players is 2 → start refused, stays LOBBY
    assert (await hpgdb.get_game(db, gid)).state == "LOBBY"
    interaction.response.send_message.assert_awaited()


async def test_lobby_start_transitions_to_active(cog, db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await hpgdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]), message_id=555)
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    try:
        await cog._handle_lobby_start(interaction, gid)
        refreshed = await hpgdb.get_game(db, gid)
        assert refreshed.state == "ACTIVE"
        assert refreshed.alive == [1, 2]
        assert refreshed.holder_id in (1, 2)
        assert refreshed.fuse_seconds is not None
    finally:
        cog._cancel_timer(gid)  # cancel the real detonate task scheduled by on_game_start


async def test_lobby_start_rejects_non_host(cog, db):
    gid = await hpgdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await hpgdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]))
    interaction = fake_interaction()
    interaction.user.id = 2  # not host
    interaction.guild = FakeGuild()
    await cog._handle_lobby_start(interaction, gid)
    assert (await hpgdb.get_game(db, gid)).state == "LOBBY"
