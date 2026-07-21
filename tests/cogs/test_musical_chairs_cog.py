"""Cog-runtime tests for Musical Chairs: the MUSIC→SCRAMBLE→ELIMINATE machine."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import discord
import pytest_asyncio

from bot_modules.cogs.musical_chairs import cog as mc_cog
from bot_modules.cogs.musical_chairs import db as mcdb
from bot_modules.cogs.musical_chairs.cog import MusicalChairsCog
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.embeds import COLOR_GREEN, COLOR_RED, COLOR_YELLOW
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeGuild, fake_interaction

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


# ── embed colors: accent-aware round / win=green / loss=red ────────────────────

async def test_round_active_embed_uses_guild_accent(db, sync_db_path, monkeypatch):
    accent = discord.Color(0x3FA7FF)
    monkeypatch.setattr(mc_cog, "resolve_accent_color", AsyncMock(return_value=accent))
    cog = _econ_cog(db, sync_db_path)  # bot has ctx + a resolvable guild
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    guild = cog.bot.get_guild(GUILD)
    await cog._resolve_accent(game.id, guild)  # resolve once, cache
    embed = cog.render_game_state(game, guild)
    assert embed.color == accent
    assert cog._accents[game.id] == accent  # cached for reuse across edits


async def test_round_active_embed_falls_back_without_accent(cog, db):
    # No accent resolved (plain FakeBot: no ctx / guild) → old COLOR_YELLOW.
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    embed = cog.render_game_state(game, FakeGuild())
    assert embed.color.value == COLOR_YELLOW


async def test_resolve_accent_guards_no_guild_no_ctx(cog, db):
    # FakeBot has no ctx; guild None → fall back, cache nothing, never crash.
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    assert await cog._resolve_accent(game.id, None) == discord.Color(COLOR_YELLOW)
    assert await cog._resolve_accent(game.id, FakeGuild()) == discord.Color(COLOR_YELLOW)
    assert game.id not in cog._accents


async def test_resolve_accent_error_falls_back(db, sync_db_path, monkeypatch):
    monkeypatch.setattr(
        mc_cog, "resolve_accent_color", AsyncMock(side_effect=RuntimeError("boom"))
    )
    cog = _econ_cog(db, sync_db_path)
    game = await _game(db, phase="MUSIC", alive=[1, 2, 3])
    assert await cog._resolve_accent(game.id, cog.bot.get_guild(GUILD)) == discord.Color(COLOR_YELLOW)
    assert game.id not in cog._accents


async def test_scramble_embed_stays_red(cog, db):
    # SCRAMBLE marks the elimination round — stays COLOR_RED regardless of accent.
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2, 3], seated=[])
    cog._accents[game.id] = discord.Color(0x3FA7FF)
    embed = cog.render_game_state(game, FakeGuild())
    assert embed.color.value == COLOR_RED


def test_winner_embed_is_green(cog, db):
    game = mc_cog.MusicalChairsGame(
        id=1, guild_id=GUILD, channel_id=CH, host_id=1, state="RESOLVED",
        winner_id=2, loser_id=1, roster=[1, 2, 3], stakes_text="loser sings",
    )
    embed = cog.render_result_state(game, FakeGuild())
    assert embed.color.value == COLOR_GREEN


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


def _econ_cog(db, db_path, member_ids=(1, 2, 3)):
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    return MusicalChairsCog(FakeEconGamesBot(db, db_path, member_ids))  # type: ignore[arg-type]


async def test_close_round_terminal_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _game(db, phase="SCRAMBLE", alive=[1, 2], seated=[2])
    await mcdb.set_game_state(db, game.id, "ACTIVE", roster=json.dumps([1, 2, 3]))
    game = await mcdb.get_game(db, game.id)
    resolved = await cog._close_round_locked(game)
    assert resolved is True
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, 2) == 25   # winner: participation + win
        assert get_balance(conn, GUILD, 1) == 5    # participation only
        assert get_balance(conn, GUILD, 3) == 5    # eliminated earlier, still played


async def test_close_round_degenerate_no_winner_pays_nothing(db, sync_db_path):
    """The winner=None branch: nobody left to seat, nobody eliminated this
    round. The game is left ACTIVE for the sweep to abandon; no payout."""
    econ_cog = _econ_cog(db, sync_db_path)
    gid = await mcdb.create_lobby(db, GUILD, CH, 1, None)
    await mcdb.set_game_state(
        db, gid, "ACTIVE",
        phase="SCRAMBLE", round=1, chairs=0,
        roster=json.dumps([1, 2, 3]),
        alive="[]", seated="[]", elimination_order="[]",
    )
    game = await mcdb.get_game(db, gid)
    resolved = await econ_cog._close_round_locked(game)
    assert resolved is True
    g = await mcdb.get_game(db, gid)
    assert g.state == "ACTIVE"  # no terminal write — the sweep abandons it
    await econ_cog._expire_active(g)
    g = await mcdb.get_game(db, gid)
    assert g.state == "ABANDONED"
    with open_db(sync_db_path) as conn:
        for uid in (1, 2, 3):
            assert get_balance(conn, GUILD, uid) == 0


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
