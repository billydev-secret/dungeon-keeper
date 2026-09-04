"""Cog-runtime tests for Hot Potato (duel): the hand-rolled _explode resolution.

Hot Potato never routes through BaseDuel._finalize_result — _explode writes the
terminal state itself — so its payout needs its own pin.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import pytest_asyncio

from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_service import get_balance, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeGuild, fake_interaction

GUILD = 9001
CH = 100
P1, P2 = 1, 2


@pytest_asyncio.fixture
async def db(sync_db_path: Path) -> GamesDb:
    return GamesDb(sync_db_path)


def _econ_cog(db: GamesDb, db_path: Path) -> HotPotatoDuel:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
    bot = FakeEconGamesBot(db, db_path, [P1, P2])
    return HotPotatoDuel(bot)  # type: ignore[arg-type]


async def _holding_game(db: GamesDb, holder: int, *, held_for: float = 3.0):
    gid = await hpdb.create_game(db, GUILD, CH, P1, P2, None)
    now = time.time()
    log = json.dumps([{"holder_id": holder, "received_at": now - held_for, "passed_at": None}])
    await hpdb.set_game_state(
        db, gid, "ACTIVE",
        holder_id=holder,
        started_at=now - 10.0,
        timer_seconds=10.0,
        pass_log=log,
        last_action_at=now,
    )
    return await hpdb.get_game(db, gid)


# ── minimum hold (duels-party-119) ────────────────────────────────────────────

@pytest.mark.parametrize(
    ("min_hold", "held_for", "expected"),
    [
        pytest.param(2.0, 0.2, "rejected", id="inside-the-hold"),
        pytest.param(2.0, 3.0, "continue", id="hold-served"),
        pytest.param(0.0, 0.2, "continue", id="dial-off"),
    ],
)
async def test_pass_honours_the_min_hold_dial(db, sync_db_path, min_hold, held_for, expected):
    """The duel had no minimum hold, so passes were instant and the loser
    was whoever held at a random tick. The group cog's wait applies now."""
    await hpdb.upsert_config(db, GUILD, min_hold=min_hold)
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2, held_for=held_for)
    interaction = fake_interaction(guild=FakeGuild())
    interaction.user.id = P2
    status, _ = await cog.handle_interaction(interaction, game)
    assert status == expected
    g = await hpdb.get_game(db, game.id)
    assert g.holder_id == (P2 if expected == "rejected" else P1)
    if expected == "rejected":
        interaction.followup.send.assert_awaited()
        assert "Hold it" in interaction.followup.send.await_args.args[0]


# ── style totals on the card ──────────────────────────────────────────────────

async def test_result_card_quotes_the_running_style_total(db, sync_db_path):
    """Per-game points were already on the card; the cumulative table was
    written and never read anywhere."""
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2, held_for=3.0)
    game.winner_id, game.loser_id = P1, P2
    embed = cog.render_result_state(game, cog.bot.guild, style_totals={P2: 174})
    field = next(f for f in embed.fields if f.name.startswith("✨ Style Points"))
    assert "U2" in (field.value or "")
    assert "now has 174 style points" in (field.value or "")


async def test_explode_accumulates_and_reports_the_total(db, sync_db_path):
    await hpdb.add_style_points(db, GUILD, P2, 144)
    cog = _econ_cog(db, sync_db_path)
    seen: dict = {}
    original = cog.render_result_state

    def spy(game, guild, **kw):
        seen.update(kw)
        return original(game, guild, **kw)

    cog.render_result_state = spy  # type: ignore[method-assign]
    cog.bot.channel = None  # no channel → no post; the totals are still computed
    game = await _holding_game(db, holder=P2, held_for=3.0)
    await cog._explode(game.id)
    # P2 held the last 3s of a 10s fuse: all 3s are in the danger zone → 30 pts.
    assert await hpdb.get_style_total(db, GUILD, P2) == 174


async def test_explode_resolves_and_pays(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2)
    await cog._explode(game.id)
    g = await hpdb.get_game(db, game.id)
    assert g.state == "RESOLVED"
    assert g.winner_id == P1
    assert g.loser_id == P2
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 25   # participation + win
        assert get_balance(conn, GUILD, P2) == 5    # participation only


async def test_explode_noop_when_already_resolved(db, sync_db_path):
    cog = _econ_cog(db, sync_db_path)
    game = await _holding_game(db, holder=P2)
    await hpdb.set_game_state(db, game.id, "RESOLVED", winner_id=P1, loser_id=P2)
    await cog._explode(game.id)
    with open_db(sync_db_path) as conn:
        assert get_balance(conn, GUILD, P1) == 0
        assert get_balance(conn, GUILD, P2) == 0
