"""Hot Takes' Start Voting button classifies its crash, like Clapback's loop.

``games_hottakes_cog.py`` carried the same blanket-``except Exception`` shape
that killed Clapback game 959cd749 on a one-second 503 (2026-09-10) — and a
worse version of it: its ``end_game(self.db, self.game_id)`` passes no
``payload``, no ``bot=`` and no ``player_ids=``, so a transient failure lost
the row *and* the roster's payout *and* wrote a blank history row.

The row is what ``recover_game`` resumes from — it re-drives voting from
``payload["results"]`` against ``payload["takes"]`` — so a transient failure
has to leave it alone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot_modules.cogs.games_hottakes_cog import HotTakesSubmitView
from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 779
HOST = 1

TAKES = [
    {"text": "pineapple belongs on pizza", "author_id": 2},
    {"text": "cereal is a soup", "author_id": 3},
]


def _server_error() -> discord.DiscordServerError:
    return discord.DiscordServerError(
        SimpleNamespace(status=503, reason="Service Unavailable"),
        {"code": 0, "message": "upstream connect error"},
    )


def _view(sync_db_path, gid, raises):
    bot = SimpleNamespace(
        games_db=GamesDb(sync_db_path), active_views={},
        ctx=SimpleNamespace(db_path=sync_db_path),
    )
    cog = SimpleNamespace(_run_voting=AsyncMock(side_effect=raises))
    view = HotTakesSubmitView(gid, HOST, bot.games_db, bot, cog)
    bot.active_views[gid] = view
    return view


def _interaction(channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild=SimpleNamespace(id=GUILD),
        guild_id=GUILD,
        channel=channel,
        channel_id=CHAN,
        message=SimpleNamespace(edit=AsyncMock()),
        response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock(), edit_message=AsyncMock()
        ),
    )


def _channel():
    return SimpleNamespace(
        id=CHAN, name="games", guild=SimpleNamespace(id=GUILD), send=AsyncMock(),
    )


async def _game(db, *, takes=TAKES, results=()):
    return await create_game(
        db, CHAN, HOST, "hottakes", state="playing", guild_id=GUILD,
        payload={"takes": takes, "results": list(results), "phase": "voting"},
    )


async def _row(db, gid):
    return await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (gid,))


async def test_a_transient_failure_leaves_the_game_row_for_recovery(sync_db_path):
    view = _view(sync_db_path, "placeholder", _server_error())
    gid = await _game(view.db)
    view.game_id = gid
    view.bot.active_views[gid] = view
    channel = _channel()

    await view.start_voting.callback(_interaction(channel))  # type: ignore[arg-type]

    row = await _row(view.db, gid)
    assert row is not None, "a transient 503 must not archive the game"
    # Both takes are still there for recover_game to re-drive voting on.
    assert len(json.loads(row["payload"])["takes"]) == 2
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list if c.args)
    assert "Game ended" not in said


async def test_a_real_bug_still_ends_the_game(sync_db_path):
    view = _view(sync_db_path, "placeholder", KeyError("takes"))
    gid = await _game(view.db)
    view.game_id = gid
    view.bot.active_views[gid] = view
    channel = _channel()

    await view.start_voting.callback(_interaction(channel))  # type: ignore[arg-type]

    assert await _row(view.db, gid) is None
    said = " ".join(str(c.args[0]) for c in channel.send.await_args_list if c.args)
    assert "Something went wrong" in said
