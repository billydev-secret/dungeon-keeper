"""Crash-recovery seam: after a restart, in-flight games re-register their views.

Each party cog registers a ``recover_game`` in ``bot.game_recoverers``; the
startup sweep (``recover_active_games``) walks ``games_active_games`` and hands
each row to its recoverer, which rebuilds the view and re-binds it to the stored
message via ``bot.add_view(view, message_id=...)``. These tests launch a game,
simulate a restart by clearing the in-memory view map, run the sweep, and assert
the view is back and bound to the right message.
"""

import pytest

from bot_modules.cogs.games_ffa_cog import FFACog
from bot_modules.cogs.games_mfk_cog import MFKCog
from bot_modules.cogs.games_traditional_cog import TraditionalCog
from bot_modules.games.utils.recovery import recover_active_games
from bot_modules.services.games_db import GamesDb


class _FakeMessage:
    def __init__(self, mid: int):
        self.id = mid
        self.embeds: list = []

    async def edit(self, **kwargs):
        return None


class _FakeChannel:
    guild = None

    def __init__(self, cid: int):
        self.id = cid
        self.name = "games"
        self.sends: list = []
        self._messages: dict[int, _FakeMessage] = {}
        self._next = 1000

    async def send(self, *args, **kwargs):
        self._next += 1
        msg = _FakeMessage(self._next)
        self._messages[msg.id] = msg
        self.sends.append((args, kwargs))
        return msg

    async def fetch_message(self, mid: int):
        msg = self._messages.get(int(mid))
        if msg is None:
            raise KeyError(mid)  # mimics discord.NotFound for a deleted message
        return msg


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.game_recoverers: dict = {}
        self.added_views: list = []
        self._channels: dict[int, _FakeChannel] = {}

    def get_channel(self, cid: int):
        return self._channels.get(int(cid))

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))


async def _launch_and_restart(cog_cls, game_type, launch_options):
    """Launch a game, then simulate a restart (clear the in-memory view map)."""
    db = GamesDb(_launch_and_restart.db_path)
    bot = _FakeBot(db)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    bot.game_recoverers[game_type] = cog.recover_game

    channel = _FakeChannel(4242)
    bot._channels[channel.id] = channel

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options=launch_options,
    )
    assert game_id is not None

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (game_id,))
    assert row["message_id"] is not None
    launched_msg_id = row["message_id"]

    # Simulate the restart: the persistent-view registry and active_views are bare.
    bot.active_views.clear()

    await recover_active_games(bot)
    return bot, game_id, launched_msg_id


@pytest.fixture(autouse=True)
def _bind_db(sync_db_path):
    _launch_and_restart.db_path = sync_db_path
    yield


async def test_traditional_recovers_view_bound_to_message():
    bot, game_id, msg_id = await _launch_and_restart(TraditionalCog, "traditional", {})

    assert game_id in bot.active_views, "view not re-registered in active_views"
    assert len(bot.added_views) == 1, "expected exactly one add_view call"
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id, "view must be bound to the game's message_id"
    assert bot.active_views[game_id] is view


async def test_ffa_recovers_with_question_from_payload():
    bot, game_id, msg_id = await _launch_and_restart(
        FFACog, "ffa", {"question": "best pizza topping?"}
    )

    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id
    assert view.question == "best pizza topping?", "question not restored from payload"


async def test_mfk_recovers_view():
    bot, game_id, msg_id = await _launch_and_restart(MFKCog, "mfk", {})

    assert game_id in bot.active_views
    _, bound_id = bot.added_views[0]
    assert bound_id == msg_id


async def test_unknown_game_type_is_skipped_not_crashed(sync_db_path):
    """A game whose type has no registered recoverer is skipped cleanly."""
    from bot_modules.games.utils.game_manager import create_game, update_game_message

    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)  # no recoverers registered
    channel = _FakeChannel(777)
    bot._channels[channel.id] = channel
    msg = await channel.send()

    game_id = await create_game(db, channel.id, 1, "no_such_game", state="open", payload={})
    await update_game_message(db, game_id, msg.id)

    await recover_active_games(bot)  # must not raise

    assert bot.added_views == []
    assert game_id not in bot.active_views


async def test_deleted_anchor_message_is_skipped(sync_db_path):
    """When the anchor message is gone, recovery skips rather than crashing."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = TraditionalCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["traditional"] = cog.recover_game

    channel = _FakeChannel(888)
    bot._channels[channel.id] = channel
    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )
    bot.active_views.clear()

    # Drop the message so fetch_message raises (simulates a deleted message).
    channel._messages.clear()

    await recover_active_games(bot)  # must not raise

    assert bot.added_views == []
    assert game_id not in bot.active_views
