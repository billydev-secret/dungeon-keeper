"""Every ``/games play`` slash entry runs the one shared launch guard.

``launch_guard.launch_refusal`` owns the pre-flight checks and their copy
(allowed channel, enabled dial, busy channel, empty bank — pinned in
``tests/test_launch_guard.py``). Until 2026-09-04 each slash entry carried its
own copy of the first two checks and none ran the busy check, so a double-tap
or a second host launched a second board on top of a live one (platform-18,
trivia-tail-88: Truth or Dare double-started four times in prod), and only
Clapback pre-checked its bank (platform-27).

One wiring row per cog: with another game live in the channel the entry
refuses ephemerally with the guard's busy line and never reaches ``launch``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_modules.cogs.games_ama_cog import AMACog
from bot_modules.cogs.games_clapback_cog import ClapbackCog
from bot_modules.cogs.games_compliment_cog import ComplimentCog
from bot_modules.cogs.games_ffa_cog import FFACog
from bot_modules.cogs.games_legitlibs import LegitLibsCog
from bot_modules.cogs.games_mfk_cog import MFKCog
from bot_modules.cogs.games_price_cog import PriceCog
from bot_modules.cogs.games_rushmore_cog import RushmoreCog
from bot_modules.cogs.games_story_cog import StoryCog
from bot_modules.cogs.games_traditional_cog import TraditionalCog
from bot_modules.cogs.games_ttl_cog import TTLCog
from bot_modules.games.utils import game_manager
from bot_modules.games.utils.game_manager import create_game
from bot_modules.games.utils.launch_guard import empty_bank_message
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 780
HOST = 1
MSG = 999_001


def _bot(db_path):
    return SimpleNamespace(
        games_db=GamesDb(db_path), active_views={},
        ctx=SimpleNamespace(db_path=db_path), get_cog=lambda name: None,
    )


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=HOST, display_name="Host"),
        guild=None,
        guild_id=GUILD,
        channel_id=CHAN,
        channel=SimpleNamespace(
            id=CHAN, name="games", guild=None, is_nsfw=lambda: False, send=AsyncMock(),
        ),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
        client=None,
    )


async def _invoke(cog, entry: str, interaction) -> None:
    if entry == "start_ffa":
        await cog.start_ffa(interaction)  # the ffa / ffa_banner commands' shared body
        return
    await getattr(type(cog), entry).callback(cog, interaction)  # type: ignore[attr-defined]


# game_type, cog class, the slash entry's attribute
ENTRIES = [
    pytest.param("clapback", ClapbackCog, "clapback", id="clapback"),
    pytest.param("ttl", TTLCog, "twotruths", id="ttl"),
    pytest.param("traditional", TraditionalCog, "traditional", id="traditional"),
    pytest.param("ama", AMACog, "ama", id="ama"),
    pytest.param("compliment", ComplimentCog, "compliment", id="compliment"),
    pytest.param("mfk", MFKCog, "mfk", id="mfk"),
    pytest.param("story", StoryCog, "story", id="story"),
    pytest.param("price", PriceCog, "price_cmd", id="price"),
    pytest.param("rushmore", RushmoreCog, "rushmore_cmd", id="rushmore"),
    pytest.param("ffa", FFACog, "start_ffa", id="ffa"),
    pytest.param("legitlibs", LegitLibsCog, "legitlibs", id="legitlibs"),
]


@pytest.mark.parametrize("game_type, cog_cls, entry", ENTRIES)
async def test_a_busy_channel_refuses_the_slash_entry(sync_db_path, game_type, cog_cls, entry):
    bot = _bot(sync_db_path)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    # A bank row so the bank-only entries are refused for the busy channel,
    # not for an empty bank — the busy check comes first either way.
    await cog.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text)"
        " VALUES (?, 'sfw', 'p?')",
        (game_type,),
    )
    # Another game — deliberately not the same type, which is what LegitLibs'
    # old private guard alone let through — is live in this channel.
    await create_game(cog.db, CHAN, 7, "wyr", message_id=MSG, guild_id=GUILD)
    interaction = _interaction()

    await _invoke(cog, entry, interaction)

    launch.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert "already a game running" in args[0]
    assert "Would You Rather" in args[0]
    assert f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}" in args[0]


async def test_clapback_empty_bank_is_the_guards_line(sync_db_path):
    """Clapback's own pre-check moved into the shared guard; the copy is the
    guard's, and the entry never reaches launch."""
    bot = _bot(sync_db_path)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    interaction = _interaction()

    await ClapbackCog.clapback.callback(cog, interaction)  # type: ignore[attr-defined]

    launch.assert_not_awaited()
    args, kwargs = interaction.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert args[0] == empty_bank_message("clapback")


@pytest.mark.parametrize("game_type, cog_cls, entry", ENTRIES)
async def test_a_clear_channel_reaches_launch(
    sync_db_path, game_type, cog_cls, entry, monkeypatch
):
    """The other half: with nothing in the way the entry defers and launches."""
    monkeypatch.setattr(game_manager, "sign_off_game_chore", AsyncMock())
    bot = _bot(sync_db_path)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    launch = AsyncMock(return_value="new-gid")
    cog.launch = launch  # type: ignore[method-assign]
    if hasattr(cog, "launch_banner"):
        cog.launch_banner = launch  # type: ignore[attr-defined]
    await cog.db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )
    await cog.db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text)"
        " VALUES (?, 'sfw', 'p?')",
        (game_type,),
    )
    interaction = _interaction()

    await _invoke(cog, entry, interaction)

    interaction.response.defer.assert_awaited_once()
    launch.assert_awaited_once()
