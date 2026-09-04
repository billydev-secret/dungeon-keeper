"""Name Your Price's recap relaunch goes through the slash entry's gate.

The recap card's Run Again button called ``cog.launch`` directly, so it was
the one door with no guard on it: an admin could untick the game on the
dashboard and the host could keep it alive from the recap indefinitely — the
"toggle that isn't enforced" CLAUDE.md forbids. The guard itself
(``relaunch_refusal``) is shared with Rushmore and Clapback, so its branches
are pinned here once; the Rushmore file only proves its button is wired.

Run Again is also the hand-off: any member may press it and hosts the next
lobby, the same shape as Rushmore's recap card (the separate Hand Off button
is gone).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_modules.cogs.games_price_cog as cog_module
from bot_modules.cogs.games_price_cog import PriceCog, PriceRecapView
from bot_modules.games.utils.game_manager import create_game, relaunch_refusal
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 777
HOST = 1


async def _allow_channel(db: GamesDb, channel_id: int = CHAN) -> None:
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (channel_id, GUILD),
    )


async def _set_enabled(db: GamesDb, game_type: str, enabled: bool) -> None:
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, game_type, int(enabled)),
    )


# ── the shared guard ─────────────────────────────────────────────────────────


async def test_relaunch_is_allowed_when_every_check_passes(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    assert await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price") is None


async def test_relaunch_refuses_a_channel_games_may_not_run_in(sync_db_path):
    db = GamesDb(sync_db_path)
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg is not None and "isn't set up for games" in msg


async def test_relaunch_refuses_when_the_dial_is_off(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    await _set_enabled(db, "price", False)
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg == "Name Your Price is currently disabled on this server."


async def test_relaunch_refuses_when_another_game_is_running_here(sync_db_path):
    db = GamesDb(sync_db_path)
    await _allow_channel(db)
    await create_game(db, CHAN, 5, "wyr", state="playing", payload={})
    msg = await relaunch_refusal(db, "price", CHAN, GUILD, label="Name Your Price")
    assert msg is not None and "already a game running" in msg


# ── the button is wired through it ───────────────────────────────────────────


def _interaction(*, user_id: int = HOST):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, display_name="Host"),
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(id=CHAN, name="games", guild=None),
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
    )


def _cog(db_path) -> tuple[PriceCog, AsyncMock]:
    bot = SimpleNamespace(
        games_db=GamesDb(db_path), active_views={},
        ctx=SimpleNamespace(db_path=db_path),
    )
    cog = PriceCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    return cog, launch


@pytest.mark.parametrize("presser", [HOST, 42], ids=["host", "anyone-in-the-room"])
@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_run_again_honours_the_enabled_dial(sync_db_path, enabled, presser, monkeypatch):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    cog, launch = _cog(sync_db_path)
    await _allow_channel(cog.db)
    await _set_enabled(cog.db, "price", enabled)
    view = PriceRecapView("old-gid", HOST, cog, {"rounds": 3})
    interaction = _interaction(user_id=presser)

    await view.run_again.callback(interaction)  # type: ignore[arg-type]

    if enabled:
        launch.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
    else:
        launch.assert_not_awaited()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert "disabled" in interaction.response.send_message.await_args.args[0]
        # The recap card is left alone so the host can retry once it is back on.
        interaction.message.edit.assert_not_awaited()


# ── Run Again is the hand-off: the presser hosts the next lobby ──────────────


@pytest.mark.parametrize("presser", [HOST, 42], ids=["host", "anyone-in-the-room"])
async def test_run_again_opens_a_lobby_hosted_by_the_presser(sync_db_path, presser, monkeypatch):
    monkeypatch.setattr(cog_module, "sign_off_game_chore", AsyncMock())
    cog, launch = _cog(sync_db_path)
    await _allow_channel(cog.db)
    view = PriceRecapView("old-gid", HOST, cog, {"rounds": 3, "source": "host"})
    interaction = _interaction(user_id=presser)

    await view.run_again.callback(interaction)  # type: ignore[arg-type]

    launch.assert_awaited_once()
    kwargs = launch.await_args_list[0].kwargs
    assert kwargs["host_id"] == presser
    assert kwargs["options"]["rounds"] == 3 and kwargs["options"]["source"] == "host"
    # No "only the host can restart" refusal and no "retype the command"
    # hint — the presser is simply the new host.
    interaction.response.send_message.assert_not_awaited()
    interaction.message.edit.assert_awaited_once()


def test_recap_view_offers_only_run_again():
    view = PriceRecapView("g", HOST, None, {})  # type: ignore[arg-type]
    assert [getattr(child, "label", None) for child in view.children] == ["🔁 Run Again"]
    assert not hasattr(view, "hand_off")


# ── launch opens a lobby and defaults the source to the bank (trivia-tail-85) ─


class _Msg(SimpleNamespace):
    pass


class _Channel:
    id = CHAN
    name = "games"
    guild = None

    def __init__(self) -> None:
        self.sends: list[tuple] = []

    def is_nsfw(self) -> bool:
        return False

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return _Msg(id=555, channel=self, edit=AsyncMock())


def _real_cog(db_path) -> PriceCog:
    bot = SimpleNamespace(
        games_db=GamesDb(db_path), active_views={},
        ctx=SimpleNamespace(db_path=db_path),
    )
    return PriceCog(bot)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("bank_rows", "requested", "expected"),
    [
        pytest.param(1, None, "bank", id="bank-full-no-choice"),
        pytest.param(0, None, "host", id="bank-empty-no-choice"),
        pytest.param(1, "players", "players", id="explicit-choice"),
    ],
)
async def test_launch_opens_a_joining_lobby_with_the_resolved_source(sync_db_path, bank_rows, requested, expected):
    from bot_modules.games.utils.game_manager import get_game_payload

    cog = _real_cog(sync_db_path)
    for i in range(bank_rows):
        await cog.db.execute(
            "INSERT INTO games_question_bank (game_type, category, question_text) VALUES ('price', 'sfw', ?)",
            (f"scenario {i}",),
        )
    channel = _Channel()
    gid = await cog.launch(
        channel=channel, host_id=HOST, host_name="Host", guild_id=GUILD,
        options={"source": requested, "start_in": 5},
    )
    assert gid
    row = await cog.db.fetchone("SELECT state FROM games_active_games WHERE game_id = ?", (gid,))
    assert row["state"] == "joining"
    payload = await get_game_payload(cog.db, gid)
    assert payload["settings"]["source"] == expected
    assert payload["players"] == [] and payload["start_epoch"] > 0
    # The lobby, with its Join/Start view, and no public host ping.
    ((args, kwargs),) = channel.sends
    assert not args and isinstance(kwargs["view"], cog_module.PriceLobbyView)
    assert isinstance(cog.bot.active_views[gid], cog_module.PriceLobbyView)


async def test_start_refuses_below_the_floor_and_begins_at_it(sync_db_path, monkeypatch):
    from bot_modules.games.utils.game_manager import create_game, get_game_payload

    monkeypatch.setattr(cog_module, "is_host_or_mod", lambda *_: True)
    cog = _real_cog(sync_db_path)
    payload = {"settings": {"rounds": 2, "timer": 30, "vote_timer": 20, "source": "bank", "tags": []},
               "total_rounds": 2, "rounds": {}, "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
               "players": [HOST]}
    gid = await create_game(cog.db, CHAN, HOST, "price", state="joining", payload=payload, guild_id=GUILD)
    view = cog_module.PriceLobbyView(gid, HOST, cog.db, cog.bot, cog)
    view.message = _Msg(id=555, edit=AsyncMock())
    started: list = []

    async def _fake_start(*a, **kw):
        started.append(a)

    cog._start_rounds = _fake_start  # type: ignore[method-assign]
    interaction = _interaction()
    interaction.response.edit_message = AsyncMock()

    await view.start.callback(interaction)  # type: ignore[arg-type]
    assert "Need at least 2 players" in interaction.response.send_message.await_args.args[0]
    assert not started

    # A second player joins, and Start takes the lobby into play.
    await cog.db.execute(
        "UPDATE games_active_games SET payload = json_set(payload, '$.players', json('[1, 2]')) WHERE game_id = ?",
        (gid,),
    )
    await view.start.callback(interaction)  # type: ignore[arg-type]
    assert started
    row = await cog.db.fetchone("SELECT state FROM games_active_games WHERE game_id = ?", (gid,))
    assert row["state"] == "playing"
    assert (await get_game_payload(cog.db, gid))["players"] == [1, 2]


async def test_auto_start_needs_a_live_lobby_and_the_floor(sync_db_path):
    from bot_modules.games.utils.game_manager import create_game

    cog = _real_cog(sync_db_path)
    payload = {"settings": {"rounds": 2, "timer": 30, "vote_timer": 20, "source": "bank", "tags": []},
               "total_rounds": 2, "rounds": {}, "scores": {"reasonable_wins": {}, "unhinged_wins": {}},
               "players": [1]}
    gid = await create_game(cog.db, CHAN, HOST, "price", state="joining", payload=payload, guild_id=GUILD)
    row = {"game_id": gid, "host_id": HOST, "message_id": 555}
    # No registered view: the sweep nudges instead.
    assert await cog.auto_start(row, payload, _Channel()) is False
    view = cog_module.PriceLobbyView(gid, HOST, cog.db, cog.bot, cog)
    view.message = _Msg(id=555, edit=AsyncMock())
    cog.bot.active_views[gid] = view
    # One player: short of the floor.
    assert await cog.auto_start(row, payload, _Channel()) is False
    await cog.db.execute(
        "UPDATE games_active_games SET payload = json_set(payload, '$.players', json('[1, 2]')) WHERE game_id = ?",
        (gid,),
    )
    started: list = []

    async def _fake_start(*a, **kw):
        started.append(a)

    cog._start_rounds = _fake_start  # type: ignore[method-assign]
    assert await cog.auto_start(row, payload, _Channel()) is True
    assert started and view.is_finished()


async def test_recover_game_re_registers_a_lobby(sync_db_path):
    cog = _real_cog(sync_db_path)
    cog.bot.add_view = lambda *a, **kw: None  # type: ignore[attr-defined]
    row = {"game_id": "g1", "host_id": HOST, "state": "joining", "message_id": 555}
    payload = {"settings": {"rounds": 2}, "players": [1]}
    message = _Msg(id=555, edit=AsyncMock())
    assert await cog.recover_game(row, payload, _Channel(), message) is True
    view = cog.bot.active_views["g1"]
    assert isinstance(view, cog_module.PriceLobbyView) and view.message is message
