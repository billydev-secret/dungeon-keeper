"""AMA crash recovery: after a restart, an in-flight AMA re-registers all of
its persistent views (top control panel, sticky bottom bar, open question
cards) and re-arms the bottom-bar re-stick loop.

Before the fix the boot pass rebuilt only per-question ``QuestionView``s (and
those with ``ama_view=None``, so Reply/Pass couldn't refresh the main embed);
``AMAView``/``AMABottomView`` were bound only at post time and empty after boot,
and ``_active_channels`` was never repopulated — leaving Volunteer/Ask/New-Hot-
Seat dead, the bottom bar stuck, and the channel "in progress".
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import discord

import bot_modules.cogs.games_ama_cog as ama_mod
from bot_modules.cogs.games_ama_cog import (
    AMABottomView,
    AMACog,
    AMAView,
    QuestionView,
)
from bot_modules.games.utils.game_manager import (
    create_game,
    update_game_message,
)
from bot_modules.games.utils.recovery import recover_active_games
from bot_modules.services.games_db import GamesDb


class _FakeMessage:
    def __init__(self, mid: int, guild=None):
        self.id = mid
        self.guild = guild
        self.jump_url = f"http://discord/{mid}"
        self.embeds: list = []

    async def edit(self, **kwargs):
        return None

    async def delete(self):
        return None


def _FakeMember(uid: int, name: str):
    m = MagicMock(spec=discord.Member)
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    return m


class _FakeGuild:
    def __init__(self, members):
        self.id = 900
        self._members = {m.id: m for m in members}

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeChannel:
    def __init__(self, cid: int, guild):
        self.id = cid
        self.name = "games"
        self.guild = guild
        self._messages: dict[int, _FakeMessage] = {}
        self.sends: list = []
        self._next = 5000

    def _register(self, msg):
        self._messages[msg.id] = msg
        return msg

    async def send(self, *args, **kwargs):
        self._next += 1
        msg = _FakeMessage(self._next, self.guild)
        self._messages[msg.id] = msg
        self.sends.append((args, kwargs))
        return msg

    async def fetch_message(self, mid: int):
        msg = self._messages.get(int(mid))
        if msg is None:
            raise KeyError(mid)
        return msg


class _FakeCtx:
    db_path = "unused-branding-db"


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.game_recoverers: dict = {}
        self.added_views: list = []
        self.ctx = _FakeCtx()
        self._channels: dict[int, _FakeChannel] = {}

    def get_channel(self, cid: int):
        return self._channels.get(int(cid))

    async def fetch_channel(self, cid: int):
        ch = self._channels.get(int(cid))
        if ch is None:
            raise KeyError(cid)
        return ch

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))

    def get_cog(self, _name):
        return None


async def _seed_ama(db, bot, *, host, hot_seat, channel):
    """Create an in-flight hot-seat AMA row with a persisted bottom bar and one
    open question card, plus the anchor/bottom/question messages."""
    guild = channel.guild
    main = channel._register(_FakeMessage(4001, guild))
    bottom = channel._register(_FakeMessage(4002, guild))
    qmsg = channel._register(_FakeMessage(4003, guild))

    now = datetime.now(timezone.utc).isoformat()
    game_id = await create_game(
        db, channel.id, host.id, "ama", state="open",
        payload={
            "mode": "unfiltered",
            "format": "hot_seat",
            "hot_seat_id": hot_seat.id,
            "bottom_message_id": bottom.id,
            "questions": [
                {
                    "text": "What's your walk-up song?",
                    "status": "approved",
                    "asker_id": 77,
                    "hot_seat_id": hot_seat.id,
                    "question_message_id": qmsg.id,
                    "asked_at": now,
                }
            ],
        },
    )
    await update_game_message(db, game_id, main.id)
    return game_id, main, bottom, qmsg


async def _restart_and_recover(sync_db_path, monkeypatch):
    async def _accent(_db_path, _guild):
        return discord.Color(0x5865F2)

    monkeypatch.setattr(ama_mod, "resolve_accent_color", _accent)

    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = AMACog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["ama"] = cog.recover_game

    host = _FakeMember(1, "Host")
    hot_seat = _FakeMember(2, "Seated")
    guild = _FakeGuild([host, hot_seat])
    channel = _FakeChannel(4242, guild)
    bot._channels[channel.id] = channel

    game_id, main, bottom, qmsg = await _seed_ama(
        db, bot, host=host, hot_seat=hot_seat, channel=channel
    )

    # Simulate the restart: nothing left in the in-memory view map.
    bot.active_views.clear()
    await recover_active_games(bot)
    return bot, cog, channel, game_id, main, bottom, qmsg, hot_seat


async def test_ama_recovers_control_panel_bound_to_main(sync_db_path, monkeypatch):
    bot, cog, channel, game_id, main, bottom, qmsg, hot_seat = await _restart_and_recover(
        sync_db_path, monkeypatch
    )

    view = bot.active_views.get(game_id)
    assert isinstance(view, AMAView)
    assert view._game_msg is main
    assert view.hot_seat_id == hot_seat.id
    assert view._hot_seat_name == "Seated"  # re-derived from the guild member
    # The panel was rebound to the main embed message id.
    assert (view, main.id) in bot.added_views


async def test_ama_recovers_bottom_bar_rebound_to_persisted_message(sync_db_path, monkeypatch):
    bot, cog, channel, game_id, main, bottom, qmsg, hot_seat = await _restart_and_recover(
        sync_db_path, monkeypatch
    )

    bottom_view = bot.active_views.get(f"{game_id}_bottom")
    assert isinstance(bottom_view, AMABottomView)
    assert bottom_view.message_id == bottom.id
    top_view = bot.active_views[game_id]
    assert top_view._bottom_msg is bottom  # so it can keep re-sticking
    assert (bottom_view, bottom.id) in bot.added_views


async def test_ama_recovers_question_view_wired_to_ama_view(sync_db_path, monkeypatch):
    bot, cog, channel, game_id, main, bottom, qmsg, hot_seat = await _restart_and_recover(
        sync_db_path, monkeypatch
    )

    top_view = bot.active_views[game_id]
    qviews = [
        (v, mid) for (v, mid) in bot.added_views if isinstance(v, QuestionView)
    ]
    assert len(qviews) == 1
    qview, bound = qviews[0]
    assert bound == qmsg.id
    # ama_view is the live panel (was None before the fix) so Reply/Pass refresh
    # the main embed.
    assert qview.ama_view is top_view
    assert qview.hot_seat_id == hot_seat.id


async def test_ama_recovery_repopulates_active_channels(sync_db_path, monkeypatch):
    bot, cog, channel, game_id, main, bottom, qmsg, hot_seat = await _restart_and_recover(
        sync_db_path, monkeypatch
    )
    assert cog._active_channels.get(channel.id) == game_id


async def test_ama_setup_registers_recoverer(monkeypatch):
    """setup() wires ama into bot.game_recoverers so the shared sweep recovers it."""
    from discord import app_commands

    from bot_modules.cogs import games_ama_cog

    monkeypatch.setattr(
        games_ama_cog, "play", app_commands.Group(name="play", description="x")
    )

    class _Bot:
        def __init__(self):
            self.game_launchers: dict = {}
            self.game_recoverers: dict = {}
            self.tree = self

        def remove_command(self, _name):
            return None

        async def add_cog(self, _cog):
            return None

    bot = _Bot()
    await games_ama_cog.setup(bot)
    assert "ama" in bot.game_recoverers
