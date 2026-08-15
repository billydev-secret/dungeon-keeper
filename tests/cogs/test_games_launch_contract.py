"""``launch`` must return None when the game didn't actually start.

The scheduler reads that return value as "did this work": a truthy id marks the
row ``launched`` and, since todo #97, fires the announcement. Two games could
return an id for a game that had already ended itself — an empty question bank
posts a notice, calls ``end_game``, and unwinds normally — which put a
``🎮 X is starting now!`` ping directly beneath "the bank is empty".
"""

import pytest

from bot_modules.cogs.games_nhie_cog import NHIECog
from bot_modules.cogs.games_wyr_cog import WYRCog
from bot_modules.services.games_db import GamesDb


class _FakeMessage:
    id = 5555

    async def edit(self, **kwargs):
        return None


class _FakeChannel:
    guild = None
    id = 4242
    name = "games"

    def __init__(self):
        self.sends: list = []

    async def send(self, *args, **kwargs):
        self.sends.append(args[0] if args else kwargs.get("content"))
        return _FakeMessage()


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}

    def get_channel(self, cid: int):
        return None


@pytest.mark.parametrize(
    "cog_cls, game_type, empty_notice",
    [
        pytest.param(WYRCog, "wyr", "question bank is empty", id="wyr"),
        pytest.param(NHIECog, "nhie", "statement bank is empty", id="nhie"),
    ],
)
async def test_launch_returns_none_when_the_bank_is_empty(
    sync_db_path, cog_cls, game_type, empty_notice
):
    """An empty bank is a failed launch, so nothing downstream should announce it."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    channel = _FakeChannel()

    gid = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )

    assert any(empty_notice in str(s) for s in channel.sends), "expected the empty-bank notice"
    assert gid is None, "a game that ended itself must not report as launched"
    # And it left nothing behind for the scheduler or the echo sweep to find.
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel.id,)
    )
    assert row is None
