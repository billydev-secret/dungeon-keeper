"""Cog-runtime tests for Musical Chairs: the MUSIC→SCRAMBLE→ELIMINATE machine."""
from __future__ import annotations

import json
from pathlib import Path

import pytest_asyncio

from bot_modules.cogs.musical_chairs import db as mcdb
from bot_modules.cogs.musical_chairs.cog import MusicalChairsCog
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeGuild, fake_interaction

GUILD = 9001
CH = 100


class FakeBot:
    def __init__(self, db: GamesDb) -> None:
        self.games_db = db

    def add_view(self, *a, **k) -> None:
        pass

    def get_guild(self, gid):
        return None

    def get_channel(self, cid):
        return None


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


@pytest_asyncio.fixture
async def cog(db: GamesDb) -> MusicalChairsCog:
    return MusicalChairsCog(FakeBot(db))  # type: ignore[arg-type]


async def _game(db, *, phase, alive, seated=None, elim=None, stakes=None):
    gid = await mcdb.create_lobby(db, GUILD, CH, alive[0], stakes)
    await mcdb.set_game_state(
        db, gid, "ACTIVE",
        phase=phase, round=1,
        chairs=max(0, len(alive) - 1),
        alive=json.dumps(alive),
        seated=json.dumps(seated or []),
        elimination_order=json.dumps(elim or []),
    )
    return await mcdb.get_game(db, gid)


# ── load smoke ─────────────────────────────────────────────────────────────────

def test_cog_exposes_group(db):
    cog = MusicalChairsCog(FakeBot(db))  # type: ignore[arg-type]
    assert "musicalchairs" in {c.name for c in cog.get_app_commands()}


def test_build_view_has_sit_button(db):
    view = MusicalChairsCog(FakeBot(db)).build_game_view(8)  # type: ignore[arg-type]
    assert "mc_sit:8" in [getattr(c, "custom_id", None) for c in view.children]


# ── _close_round_locked ────────────────────────────────────────────────────────

async def test_close_round_non_terminal_next_round(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2, 3])
    try:
        resolved = await cog._close_round_locked(game)
        assert resolved is False
        g = await mcdb.get_game(db, game.id)
        assert g.state == "ACTIVE"
        assert g.phase == "MUSIC"
        assert g.alive == [2, 3]
        assert g.elimination_order == [1]
        assert g.round == 2
    finally:
        cog._cancel_timer(game.id)


async def test_close_round_terminal_resolves(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 2
    assert g.loser_id == 1


async def test_close_round_no_show_multi_elim(cog, db):
    # 3 alive, only player 1 sat → 2 and 3 both out → 1 wins
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[1])
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 1
    assert g.loser_id in (2, 3)  # last eliminated of the pair


# ── _on_sit: false start (MUSIC) ───────────────────────────────────────────────

async def test_on_sit_false_start_eliminates(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.alive == [2, 3]
    assert g.elimination_order == [1]
    interaction.followup.send.assert_awaited()  # "you sat too early"


async def test_on_sit_false_start_terminal(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 1
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == 2
    assert g.loser_id == 1


# ── _on_sit: scramble seat claim ───────────────────────────────────────────────

async def test_on_sit_scramble_claims_seat(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == [2]
    assert g.state == "ACTIVE"
    interaction.edit_original_response.assert_awaited()


async def test_on_sit_scramble_close_on_fill(cog, db):
    # 3 alive, 2 chairs; player 2 already seated, player 3 sits → seats full → close
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 3
    try:
        await cog._on_sit(interaction, game.id)
        g = await mcdb.get_game(db, game.id)
        assert g.alive == [2, 3]          # player 1 (never sat) is out
        assert g.elimination_order == [1]
        assert g.phase == "MUSIC"          # next round
    finally:
        cog._cancel_timer(game.id)


async def test_on_sit_rejects_double_sit(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[2])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 2  # already seated
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == [2]
    interaction.followup.send.assert_awaited()


async def test_on_sit_rejects_outsider(cog, db):
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = 999
    await cog._on_sit(interaction, game.id)
    g = await mcdb.get_game(db, game.id)
    assert g.seated == []


# ── _start_scramble transition ─────────────────────────────────────────────────

async def test_start_scramble_flips_phase(cog, db):
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    try:
        await cog._start_scramble(game.id)
        g = await mcdb.get_game(db, game.id)
        assert g.phase == "SCRAMBLE"
        assert g.chairs == 2
        assert g.seated == []
    finally:
        cog._cancel_timer(game.id)


# ── lobby start (custom-stakes mode skips nick preflight) ──────────────────────

async def test_lobby_start_begins_music(cog, db):
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await mcdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2, 3]), message_id=42)
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    try:
        await cog._handle_lobby_start(interaction, gid)
        g = await mcdb.get_game(db, gid)
        assert g.state == "ACTIVE"
        assert g.phase == "MUSIC"
        assert g.alive == [1, 2, 3]
        assert g.chairs == 2
    finally:
        cog._cancel_timer(gid)


async def test_lobby_start_below_min_players_refused(cog, db):
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, "loser sings")
    await mcdb.set_game_state(db, gid, "LOBBY", roster=json.dumps([1, 2]))  # min is 3
    interaction = fake_interaction()
    interaction.user.id = 1
    interaction.guild = FakeGuild()
    await cog._handle_lobby_start(interaction, gid)
    assert (await mcdb.get_game(db, gid)).state == "LOBBY"
