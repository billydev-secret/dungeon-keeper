"""Tests for /games dev fill + /games dev answer (games_dev_cog.py).

Locks in: per-game_type player-list key dispatch (players vs participants,
traditional's extra prefs entry), explicit refusal for submission-based
games with no player-list concept (ttl, hottakes), and that a failed or
skipped lobby-embed refresh is surfaced to the caller rather than silently
swallowed while still reporting unconditional success.
"""

import json

import discord

from bot_modules.cogs.games_dev_cog import GamesDevCog
from bot_modules.games.utils.game_manager import create_game, update_game_message
from bot_modules.services.games_db import GamesDb


# ── Fakes ────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self):
        self.messages: list[tuple] = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeMessage:
    def __init__(self, mid: int):
        self.id = mid
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel(discord.abc.Messageable):
    """Satisfies dev_fill's `isinstance(channel, discord.abc.Messageable)`."""

    def __init__(self):
        self._by_id: dict[int, FakeMessage] = {}

    async def _get_channel(self):  # type: ignore[override]
        return self

    async def fetch_message(self, mid):  # type: ignore[override]
        msg = self._by_id.get(int(mid))
        if msg is None:
            raise discord.DiscordException(f"message {mid} not found")
        return msg


class FakeCtx:
    db_path = "unused-branding-db"


class FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.cogs: dict = {}
        self.ctx = FakeCtx()


class FakeInteraction:
    def __init__(self, channel_id: int, *, guild=None, channel=None):
        self.channel_id = channel_id
        self.guild = guild
        self.channel = channel
        self.response = FakeResponse()


async def _payload(db: GamesDb, game_id: str) -> dict:
    row = await db.fetchone(
        "SELECT payload FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    assert row is not None
    return json.loads(row["payload"])


def _reply(inter: FakeInteraction) -> str:
    return inter.response.messages[0][0]


# ── dev fill: per-game_type key dispatch ──────────────────────────────


async def test_dev_fill_uses_players_key_for_mlt(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    game_id = await create_game(db, 4001, 1, "mlt", state="joining", payload={})
    inter = FakeInteraction(channel_id=4001)

    await cog.dev_fill.callback(cog, inter, count=3)  # type: ignore[arg-type,call-arg]

    payload = await _payload(db, game_id)
    assert len(payload["players"]) == 3
    assert "participants" not in payload
    assert "Added 3 fake player(s)" in _reply(inter)


async def test_dev_fill_uses_participants_key_for_compliment_and_mfk(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    for i, game_type in enumerate(("compliment", "mfk")):
        channel_id = 4100 + i
        game_id = await create_game(db, channel_id, 1, game_type, state="joining", payload={})
        inter = FakeInteraction(channel_id=channel_id)

        await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

        payload = await _payload(db, game_id)
        assert len(payload["participants"]) == 2, game_type
        assert "players" not in payload, game_type
        assert "Added 2 fake player(s)" in _reply(inter)


async def test_dev_fill_traditional_also_fills_prefs(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    game_id = await create_game(db, 4200, 1, "traditional", state="joining", payload={})
    inter = FakeInteraction(channel_id=4200)

    await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

    payload = await _payload(db, game_id)
    assert len(payload["participants"]) == 2
    fake_ids = payload["participants"]
    for uid in fake_ids:
        assert payload["prefs"][str(uid)] == ["sfw_truth"]


async def test_dev_fill_refuses_submission_based_games(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    for i, game_type in enumerate(("ttl", "hottakes")):
        channel_id = 4300 + i
        game_id = await create_game(
            db, channel_id, 1, game_type, state="joining", payload={"submissions": {}}
        )
        inter = FakeInteraction(channel_id=channel_id)

        await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

        payload = await _payload(db, game_id)
        assert "players" not in payload, game_type
        assert "participants" not in payload, game_type
        assert "doesn't support" in _reply(inter), game_type


async def test_dev_fill_refuses_outside_joining_state(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    game_id = await create_game(db, 4400, 1, "mlt", state="playing", payload={"players": []})
    inter = FakeInteraction(channel_id=4400)

    await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

    payload = await _payload(db, game_id)
    assert payload["players"] == []
    assert "only works during the lobby" in _reply(inter)


# ── dev fill: clapback lobby-embed refresh ────────────────────────────


async def test_dev_fill_clapback_updates_embed_when_message_exists(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    game_id = await create_game(
        db, 4500, 1, "clapback", state="joining",
        payload={"config": {"rounds": 3}, "players": []},
    )
    channel = FakeChannel()
    msg = FakeMessage(9999)
    channel._by_id[msg.id] = msg
    await update_game_message(db, game_id, msg.id)

    # guild=None short-circuits the accent-color/branding lookup entirely.
    inter = FakeInteraction(channel_id=4500, guild=None, channel=channel)

    await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

    assert len(msg.edits) == 1
    assert "embed" in msg.edits[0]
    reply = _reply(inter)
    assert "Added 2 fake player(s)" in reply
    assert "⚠️" not in reply


async def test_dev_fill_clapback_reports_stale_embed_when_message_missing(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    game_id = await create_game(
        db, 4600, 1, "clapback", state="joining",
        payload={"config": {"rounds": 3}, "players": []},
    )
    channel = FakeChannel()  # no message registered -> fetch_message raises
    await update_game_message(db, game_id, 12345)

    inter = FakeInteraction(channel_id=4600, guild=None, channel=channel)

    await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

    payload = await _payload(db, game_id)
    assert len(payload["players"]) == 2  # game state still updated
    reply = _reply(inter)
    assert "did **not** update" in reply


async def test_dev_fill_clapback_notes_message_not_posted_yet(sync_db_path):
    db = GamesDb(sync_db_path)
    cog = GamesDevCog(FakeBot(db))  # type: ignore[arg-type]

    # message_id still None -- mirrors the window between create_game() and
    # update_game_message() in _start_new_game().
    game_id = await create_game(
        db, 4700, 1, "clapback", state="joining",
        payload={"config": {"rounds": 3}, "players": []},
    )
    inter = FakeInteraction(channel_id=4700, guild=None, channel=FakeChannel())

    await cog.dev_fill.callback(cog, inter, count=2)  # type: ignore[arg-type,call-arg]

    payload = await _payload(db, game_id)
    assert len(payload["players"]) == 2
    assert "isn't posted yet" in _reply(inter)
