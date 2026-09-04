"""The rematch cooldown is enforced, ships at zero, and Run It Back is one
press (duels-party-116 / duels-party-117).

"Wait Before a Rematch" sat on the three duel panels and was read by nothing,
while the group games enforced the same dial at 48 hours — one nickname
Chicken locked every player in the roster out of nickname Chicken for two
days. Both now enforce it, nickname games only, and the default is 0 because
no cooldown is what every duel actually behaved like.

Run It Back re-creates the game with the same people, stakes and wager. A
duel re-posts the challenge card from the presser to the other duelist (their
Accept is still theirs to press, so the wager is declared now and taken at
accept, exactly as a typed challenge is); a group game reopens a lobby with
the host seated and pings the old roster to Join.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
import pytest_asyncio

from bot_modules.cogs.chicken import db as chdb
from bot_modules.cogs.chicken.cog import ChickenCog
from bot_modules.cogs.hot_potato import db as hpdb
from bot_modules.cogs.hot_potato.cog import HotPotatoDuel
from bot_modules.core.db_utils import open_db
from bot_modules.duels import db as duels_db
from bot_modules.duels.db import REMATCH_WINDOW_SECONDS
from bot_modules.duels.views import REMATCH_EXPIRED_TEXT, ChallengeView
from bot_modules.services import economy_wager_service as wager_svc
from bot_modules.services import no_contact_service as ncs
from bot_modules.services.economy_service import apply_credit, save_econ_settings
from bot_modules.services.games_db import GamesDb
from tests.fakes import FakeEconGamesBot, FakeMember, fake_interaction

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


def _seed_economy(db_path: Path, *user_ids: int, amount: int = 500) -> None:
    with open_db(db_path) as conn:
        save_econ_settings(conn, GUILD, {"enabled": True})
        for uid in user_ids:
            apply_credit(conn, GUILD, uid, amount, "test_seed")


def _stake(db_path: Path, game_type: str, game_id: int, user_id: int, amount: int) -> None:
    with open_db(db_path) as conn:
        wager_svc.hold_stake(conn, GUILD, game_type, game_id, user_id, amount)


def _balance(db_path: Path, user_id: int) -> int:
    from bot_modules.services.economy_service import get_balance

    with open_db(db_path) as conn:
        return get_balance(conn, GUILD, user_id)


def _renameable(bot) -> None:
    """A guild whose bot can rename everyone, so the nickname preflight runs
    instead of crashing on a missing ``guild.me``."""
    bot.guild.me = SimpleNamespace(
        guild_permissions=SimpleNamespace(manage_nicknames=True), top_role=99,
    )
    bot.guild.owner_id = 0


def _interaction(bot, user_id: int):
    i = fake_interaction(user=FakeMember(id=user_id), guild=bot.guild, channel_id=CH)
    i.original_response = AsyncMock(return_value=SimpleNamespace(id=555))
    return i


def _sent(interaction) -> list[str]:
    return [c.args[0] for c in interaction.response.send_message.call_args_list if c.args]


# ── the dial ──────────────────────────────────────────────────────────────────


def test_the_cooldown_default_is_zero_in_both_places():
    from web_server.routes.config import _DUEL_SHARED_DEFAULTS

    assert duels_db._CONFIG_DEFAULTS["cooldown_hours"] == 0
    assert _DUEL_SHARED_DEFAULTS["cooldown_hours"] == 0


async def test_a_fresh_config_row_does_not_inherit_the_old_sql_default(db):
    """The column's DEFAULT is still 48; the upsert seeds the code default."""
    await duels_db.upsert_config(db, GUILD, "hot_potato", sentence_hours=12)
    cfg = await duels_db.get_config(db, GUILD, "hot_potato")
    assert cfg["cooldown_hours"] == 0 and cfg["sentence_hours"] == 12


@pytest.mark.parametrize(
    ("wager", "refused"),
    [
        pytest.param(None, True, id="nickname-game-is-held-back"),
        pytest.param(50, False, id="wagered-game-runs-now"),
    ],
)
async def test_the_duel_rematch_cooldown_guards_the_nickname_stake_only(
    db, sync_db_path, wager, refused
):
    _seed_economy(sync_db_path, 1, 2)
    await duels_db.upsert_config(db, GUILD, "hot_potato", cooldown_hours=1)
    await duels_db.set_cooldown(db, GUILD, "hot_potato", 1, 2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    _renameable(bot)
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    interaction = _interaction(bot, 1)

    await cog._base_challenge(interaction, FakeMember(id=2), None, wager=wager)  # type: ignore[arg-type]

    game = await hpdb.get_game(db, 1)
    if not refused:
        assert game is not None and game.state == "PENDING"
        return
    assert game is None
    (text,) = _sent(interaction)
    assert text.startswith("❌ ") and "rematch cooldown" in text
    assert "59m" in text or "1h 0m" in text  # the remaining time, not a bare no
    assert "nickname: False" in text  # …and the way round it


async def test_a_zero_cooldown_lets_the_pair_play_again_at_once(db, sync_db_path):
    _seed_economy(sync_db_path, 1, 2)
    await duels_db.set_cooldown(db, GUILD, "hot_potato", 1, 2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    _renameable(bot)
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]

    await cog._base_challenge(_interaction(bot, 1), FakeMember(id=2), None)  # type: ignore[arg-type]

    game = await hpdb.get_game(db, 1)
    assert game is not None and game.state == "PENDING"


# ── Run It Back on a duel ─────────────────────────────────────────────────────


async def _settled_duel(db, sync_db_path, *, ante: int = 0, age: float = 30.0) -> int:
    gid = await hpdb.create_game(db, GUILD, CH, 1, 2, None if ante == 0 else "coins", nick_stake=False)
    if ante:
        for uid in (1, 2):
            _stake(sync_db_path, "hot_potato", gid, uid, ante)
    await hpdb.set_game_state(
        db, gid, "RESOLVED_NO_NICK", winner_id=1, loser_id=2,
        resolved_at=time.time() - age, result_message_id=700,
    )
    return gid


async def test_run_it_back_reposts_the_challenge_with_the_same_wager(db, sync_db_path):
    _seed_economy(sync_db_path, 1, 2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50)
    interaction = _interaction(bot, 2)  # the loser wants revenge

    await cog._handle_rematch(interaction, old)

    new = await hpdb.get_game(db, old + 1)
    assert new is not None and new.state == "PENDING"
    assert (new.challenger_id, new.target_id) == (2, 1)
    assert "50" in (new.stakes_text or "") and "winner takes" in (new.stakes_text or "")
    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["view"], ChallengeView)
    assert kwargs["content"] == "<@1>"
    # The wager is declared, not taken: nothing moves until Accept.
    with open_db(sync_db_path) as conn:
        assert wager_svc.game_ante(conn, "hot_potato", new.id) == 50
        rows = conn.execute(
            "SELECT state FROM econ_game_wagers WHERE game_type = ? AND game_id = ?",
            ("hot_potato", new.id),
        ).fetchall()
    assert [r["state"] for r in rows] == ["pending"]


async def test_run_it_back_is_for_the_two_who_played(db, sync_db_path):
    _seed_economy(sync_db_path, 1, 2, 3)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50)

    interaction = _interaction(bot, 3)
    await cog._handle_rematch(interaction, old)

    assert await hpdb.get_game(db, old + 1) is None
    (text,) = _sent(interaction)
    assert text.startswith("❌ ") and "two who played" in text


async def test_run_it_back_stops_working_after_five_minutes(db, sync_db_path):
    _seed_economy(sync_db_path, 1, 2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50, age=REMATCH_WINDOW_SECONDS + 5)

    interaction = _interaction(bot, 1)
    await cog._handle_rematch(interaction, old)

    assert await hpdb.get_game(db, old + 1) is None
    assert _sent(interaction) == [REMATCH_EXPIRED_TEXT]


async def test_run_it_back_goes_through_the_enabled_switch(db, sync_db_path):
    _seed_economy(sync_db_path, 1, 2)
    with open_db(sync_db_path) as conn:
        conn.execute(
            "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, 0)",
            (GUILD, "hot_potato"),
        )
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50)

    interaction = _interaction(bot, 1)
    await cog._handle_rematch(interaction, old)

    assert await hpdb.get_game(db, old + 1) is None
    assert any("switched off" in t for t in _sent(interaction))


async def test_run_it_back_honours_the_no_contact_list(db, sync_db_path):
    """A pair added after the first game gets the ordinary in-progress line,
    the same one a typed challenge gets."""
    _seed_economy(sync_db_path, 1, 2)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50)
    ncs.add_pair(sync_db_path, GUILD, 2, 1, created_by=2, protected_user_id=2)

    interaction = _interaction(bot, 1)
    await cog._handle_rematch(interaction, old)

    assert await hpdb.get_game(db, old + 1) is None
    assert _sent(interaction) == ["❌ You two already have a game in progress."]


async def test_run_it_back_refuses_a_presser_who_cannot_cover_the_wager(db, sync_db_path):
    """The old pot was 50 a side; the loser staked their last 50 on it and
    has nothing left to declare a new one with."""
    _seed_economy(sync_db_path, 1, amount=500)
    _seed_economy(sync_db_path, 2, amount=50)
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2])
    cog = HotPotatoDuel(bot)  # type: ignore[arg-type]
    old = await _settled_duel(db, sync_db_path, ante=50)
    assert _balance(sync_db_path, 2) == 0

    interaction = _interaction(bot, 2)
    await cog._handle_rematch(interaction, old)

    assert await hpdb.get_game(db, old + 1) is None
    (text,) = _sent(interaction)
    assert text.startswith("❌ ") and "You need" in text


# ── Run It Back on a group game ───────────────────────────────────────────────


async def _settled_chicken(db, roster: list[int], *, stakes: str | None = "loser sings") -> int:
    gid = await chdb.create_lobby(db, GUILD, CH, roster[0], stakes)
    await chdb.set_game_state(
        db, gid, "RESOLVED_NO_NICK", roster=json.dumps(roster), alive="[]",
        winner_id=roster[-1], loser_id=roster[0], resolved_at=time.time() - 10,
        result_message_id=700,
    )
    return gid


async def test_run_it_back_reopens_a_lobby_and_pings_the_old_roster(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    old = await _settled_chicken(db, [1, 2, 3])

    interaction = _interaction(bot, 1)
    await cog._handle_rematch(interaction, old)

    new = await chdb.get_game(db, old + 1)
    assert new is not None and new.state == "LOBBY"
    assert new.host_id == 1 and new.roster == [1]
    assert new.stakes_text == "loser sings"
    ping = interaction.followup.send.await_args.args[0]
    assert "<@2>" in ping and "<@3>" in ping and "<@1>" not in ping
    assert "✋ Join" in ping


async def test_run_it_back_on_a_group_game_is_the_hosts_button(db, sync_db_path):
    bot = FakeEconGamesBot(db, sync_db_path, [1, 2, 3])
    cog = ChickenCog(bot)  # type: ignore[arg-type]
    old = await _settled_chicken(db, [1, 2, 3])

    interaction = _interaction(bot, 2)
    await cog._handle_rematch(interaction, old)

    assert await chdb.get_game(db, old + 1) is None
    (text,) = _sent(interaction)
    assert text.startswith("❌ ") and "host" in text


# ── the card carries the button ───────────────────────────────────────────────


def _ids(view) -> set[str]:
    return {str(getattr(i, "custom_id", "")) for i in view.children}


@pytest.mark.parametrize(
    ("state", "stakes", "age", "expected"),
    [
        pytest.param("RESOLVED", None, 10, {"set_nick:7", "rematch:7"}, id="nickname-game"),
        pytest.param("RESOLVED_NO_NICK", "coins", 10, {"rematch:7"}, id="wager-game"),
        pytest.param("NICKED", None, 10, {"rematch:7"}, id="already-renamed"),
        pytest.param("RESOLVED", None, REMATCH_WINDOW_SECONDS + 1, {"set_nick:7"}, id="window-closed"),
    ],
)
def test_result_view_buttons_follow_the_game(state, stakes, age, expected):
    cog = HotPotatoDuel.__new__(HotPotatoDuel)
    game = SimpleNamespace(
        id=7, state=state, stakes_text=stakes, nick_stake=stakes is None,
        winner_id=1, loser_id=2, resolved_at=time.time() - age,
    )
    assert _ids(cog._result_view(game)) == expected


def test_a_disabled_result_view_greys_out_every_button():
    cog = HotPotatoDuel.__new__(HotPotatoDuel)
    game = SimpleNamespace(
        id=7, state="RESOLVED", stakes_text=None, nick_stake=True,
        winner_id=1, loser_id=2, resolved_at=time.time(),
    )
    view = cog._result_view(game, disabled=True)
    assert view.children and all(i.disabled for i in view.children)
