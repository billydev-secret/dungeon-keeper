"""The shared launch guard — one refusal for every door into a party game.

``launch_refusal`` is the single owner of the pre-flight checks and their copy
(platform-18: no slash entry checked for a running game; platform-27: only
Clapback checked its bank). These pin each branch, its order, and the copy the
slash entries, Play Again buttons, scheduler and rotation will all send.
"""

from __future__ import annotations

import pytest

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils import launch_guard
from bot_modules.games.utils.game_manager import create_game, relaunch_refusal
from bot_modules.games.utils.launch_guard import (
    BANK_ONLY_TYPES,
    CHANNEL_NOT_ALLOWED_MSG,
    busy_message,
    disabled_message,
    empty_bank_message,
    jump_url,
    launch_refusal,
    no_tag_match_message,
)
from bot_modules.services.games_db import GamesDb

GUILD = 4242
CHAN = 777
MSG = 123456789


async def _allow(db: GamesDb) -> None:
    await db.execute(
        "INSERT INTO games_allowed_channels (channel_id, guild_id) VALUES (?, ?)",
        (CHAN, GUILD),
    )


async def _dial(db: GamesDb, game_type: str, enabled: bool) -> None:
    await db.execute(
        "INSERT INTO games_game_config (guild_id, game_type, enabled) VALUES (?, ?, ?)",
        (GUILD, game_type, int(enabled)),
    )


async def _bank_row(db: GamesDb, game_type: str, tags: str = "[]") -> None:
    await db.execute(
        "INSERT INTO games_question_bank (game_type, category, question_text, tags)"
        " VALUES (?, 'sfw', 'q?', ?)",
        (game_type, tags),
    )


@pytest.fixture
def db(sync_db_path) -> GamesDb:
    return GamesDb(sync_db_path)


# ── the branches, in order ───────────────────────────────────────────────────


async def test_a_channel_off_the_allowlist_is_refused_first(db):
    # Dial off and a live game too — the allowlist answer still wins.
    await _dial(db, "traditional", False)
    await create_game(db, CHAN, 5, "wyr", guild_id=GUILD)
    assert await launch_refusal(db, "traditional", CHAN, GUILD) == CHANNEL_NOT_ALLOWED_MSG


async def test_no_channel_at_all_is_refused(db):
    assert await launch_refusal(db, "traditional", None, GUILD) == CHANNEL_NOT_ALLOWED_MSG


@pytest.mark.parametrize("game_type", ["traditional", "clapback"])
async def test_a_disabled_dial_is_refused_by_name(db, game_type):
    await _allow(db)
    await _dial(db, game_type, False)
    msg = await launch_refusal(db, game_type, CHAN, GUILD)
    assert msg == disabled_message(game_type)
    assert msg == f"{GAME_NAMES[game_type]} is currently disabled on this server."


async def test_an_explicit_label_names_the_disabled_line(db):
    await _allow(db)
    await _dial(db, "price", False)
    msg = await launch_refusal(db, "price", CHAN, GUILD, label="The Price Game")
    assert msg == "The Price Game is currently disabled on this server."


async def test_a_running_game_is_refused_with_its_name_and_a_jump_link(db):
    await _allow(db)
    await create_game(db, CHAN, 5, "wyr", message_id=MSG, guild_id=GUILD)
    msg = await launch_refusal(db, "traditional", CHAN, GUILD)
    assert msg is not None
    assert "already a game running" in msg  # the substring the recap tests pin
    assert "Would You Rather" in msg
    assert f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}" in msg
    assert "/games end" in msg


async def test_a_running_game_without_an_anchor_message_has_no_link(db):
    await _allow(db)
    await create_game(db, CHAN, 5, "wyr", guild_id=GUILD)
    msg = await launch_refusal(db, "traditional", CHAN, GUILD)
    assert msg == busy_message("wyr")
    assert msg is not None and "jump" not in msg and "discord.com" not in msg


@pytest.mark.parametrize("game_type", sorted(BANK_ONLY_TYPES))
async def test_a_bank_only_game_with_an_empty_bank_is_refused(db, game_type):
    await _allow(db)
    msg = await launch_refusal(db, game_type, CHAN, GUILD)
    assert msg == empty_bank_message(game_type)
    assert msg is not None and "web dashboard" in msg


@pytest.mark.parametrize("game_type", sorted(BANK_ONLY_TYPES))
async def test_a_bank_only_game_with_a_prompt_may_launch(db, game_type):
    await _allow(db)
    await _bank_row(db, game_type)
    assert await launch_refusal(db, game_type, CHAN, GUILD) is None


async def test_the_bank_check_is_bank_only_types_only(db):
    """Truth or Dare has host-written questions; an empty bank is not a refusal."""
    await _allow(db)
    assert await launch_refusal(db, "traditional", CHAN, GUILD) is None
    assert "traditional" not in BANK_ONLY_TYPES


@pytest.mark.parametrize("game_type", ["rushmore", "price"])
async def test_host_supplied_games_are_not_bank_checked(db, game_type):
    """Both default to ``source: host`` — the bank is one optional mode."""
    await _allow(db)
    assert await launch_refusal(db, game_type, CHAN, GUILD) is None


async def test_host_supplied_material_skips_the_bank(db):
    """An MLT ``question:`` needs no bank row."""
    await _allow(db)
    assert await launch_refusal(db, "mlt", CHAN, GUILD, host_supplied=True) is None


async def test_tags_with_no_match_are_refused_with_the_tag_line(db):
    await _allow(db)
    await _bank_row(db, "wyr", tags='["spicy"]')
    msg = await launch_refusal(db, "wyr", CHAN, GUILD, tags=["wholesome"])
    assert msg == no_tag_match_message(["wholesome"])
    assert await launch_refusal(db, "wyr", CHAN, GUILD, tags=["spicy"]) is None


async def test_an_nsfw_only_bank_reads_as_empty_in_an_sfw_room(db):
    """The age-gate is the channel's, and a refusal must respect it the way the
    draw does — otherwise the guard passes a game that then has no question."""
    await _allow(db)
    await _bank_row(db, "nhie", tags='["nsfw"]')
    assert await launch_refusal(db, "nhie", CHAN, GUILD) == empty_bank_message("nhie")
    assert await launch_refusal(db, "nhie", CHAN, GUILD, allow_nsfw=True) is None


async def test_everything_clear_returns_none(db):
    await _allow(db)
    await _dial(db, "clapback", True)
    await _bank_row(db, "clapback")
    assert await launch_refusal(db, "clapback", CHAN, GUILD) is None


# ── the recap door is the same guard ─────────────────────────────────────────


async def test_relaunch_refusal_is_this_guard(db):
    """Play Again / Run Again delegate here: the running-game line now names
    the game and links to it, and the copy lives in one module."""
    await _allow(db)
    await create_game(db, CHAN, 5, "nhie", message_id=MSG, guild_id=GUILD)
    assert await relaunch_refusal(db, "price", CHAN, GUILD) == await launch_refusal(
        db, "price", CHAN, GUILD
    )
    msg = await relaunch_refusal(db, "price", CHAN, GUILD)
    assert msg is not None and "Never Have I Ever" in msg
    assert f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}" in msg


def test_jump_url_needs_every_part():
    assert jump_url(GUILD, CHAN, MSG) == f"https://discord.com/channels/{GUILD}/{CHAN}/{MSG}"
    assert jump_url(GUILD, CHAN, None) is None
    assert jump_url(0, CHAN, MSG) is None


def test_the_copy_has_one_owner():
    """game_manager no longer carries its own refusal literals."""
    from bot_modules.games.utils import game_manager

    assert not hasattr(game_manager, "CHANNEL_BUSY_MSG")
    assert not hasattr(game_manager, "CHANNEL_NOT_ALLOWED_MSG")
    assert launch_guard.CHANNEL_NOT_ALLOWED_MSG.startswith("This channel isn't set up for games")
