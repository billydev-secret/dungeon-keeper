"""Headless game-launch seam: the scheduler launches games with no Interaction.

Proves WYRCog.launch() works without a Discord interaction — the contract every
party cog's launcher must satisfy for the scheduler to drive it.
"""

from bot_modules.cogs.games_wyr_cog import WYRCog
from bot_modules.services.games_db import GamesDb


class _FakeMessage:
    id = 555


class _FakeChannel:
    id = 4242
    name = "games"

    def __init__(self):
        self.sends: list[tuple] = []

    async def send(self, *args, **kwargs):
        self.sends.append((args, kwargs))
        return _FakeMessage()


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}


async def test_wyr_headless_launch_creates_active_game(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = WYRCog(bot)
    channel = _FakeChannel()

    # Custom question bypasses the question bank, guaranteeing a round posts
    # (an empty bank would take the bank-empty branch and end the game).
    game_id = await cog.launch(
        channel=channel,
        host_id=2001,
        host_name="Tester",
        guild_id=9001,
        options={"question": "fly | be invisible"},
    )

    assert game_id is not None

    # A round message was posted with an embed + interactive view.
    assert channel.sends, "launch posted no message"
    _, kwargs = channel.sends[-1]
    assert "embed" in kwargs and "view" in kwargs

    # The active-game row exists for the channel (so the busy-check sees it).
    row = await db.fetchone(
        "SELECT * FROM games_active_games WHERE channel_id = ?", (channel.id,)
    )
    assert row is not None
    assert row["game_type"] == "wyr"
    assert row["host_id"] == 2001

    # The view is tracked for interaction routing.
    assert game_id in bot.active_views
