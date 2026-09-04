"""The one launch guard every door into a party game shares.

A game has four front doors — its ``/games play`` slash entry, the recap card's
Play Again / Run Again button, the dashboard scheduler and the feature-rotation
launcher — and until 2026-09-02 each ran its own subset of the pre-flight checks
in its own copy of the refusal copy. The slash entries checked the channel and
the enabled dial but never whether a game was already running (platform-18:
sixteen of seventeen would launch a second board on top of a live one); only
Clapback pre-checked its bank (platform-27: a bare ``/games play mlt`` filled a
lobby and died at Start on an empty bank).

:func:`launch_refusal` runs every check in one place and **owns the copy**.
It returns the refusal message, or ``None`` when the launch may go ahead, and
never sends anything — the caller sends it ephemerally (a slash entry), edits it
into the card (Play Again) or logs it (a headless launch). The checks, in order:

1. the channel is on the games allowlist;
2. the game's enabled dial is on for this guild;
3. no game is already running in the channel — the refusal names the running
   game and links to its board when the anchor message is known;
4. for a bank-only game launched without host-supplied material, the bank has
   at least one prompt the requested tags and the channel's age-gate allow —
   refused with the dashboard hint, as Clapback already did.

``game_manager.relaunch_refusal`` (the Play Again buttons' door) delegates here,
so the recap cards got the bank check and the jump link the day this landed;
the slash entries, scheduler and rotation are wired through it by later waves
(common-lib A.1) and import the refusal strings from this module rather than
carrying their own.
"""

from __future__ import annotations

from collections.abc import Sequence

from bot_modules.games.constants import GAME_NAMES
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    check_game_enabled,
    get_active_game,
)
from bot_modules.games.utils.question_source import has_matching_questions

# The games whose launch draws its first prompt from the question bank and
# has nothing else to fall back on: an empty bank means no game — Clapback
# refuses at the slash entry, WYR and NHIE post a notice and unwind, MLT fills
# a lobby and dies at Start. Mt. Rushmore Draft and Name Your Price read the
# bank only in their ``source: bank`` mode and default to host-supplied
# material, so they are deliberately not here; an empty bank is an in-game
# notice there, as it is for the per-round bank draws of photo, traditional
# and ffa. A caller launching MLT with a host ``question:`` passes
# ``host_supplied=True`` and the bank is not consulted.
BANK_ONLY_TYPES = frozenset({"clapback", "wyr", "nhie", "mlt"})

CHANNEL_NOT_ALLOWED_MSG = (
    "This channel isn't set up for games. An admin can enable it from the web dashboard."
)


def game_name(game_type: str, label: str | None = None) -> str:
    """The display name a refusal uses — an explicit *label* wins."""
    return label or GAME_NAMES.get(game_type, game_type)


def disabled_message(game_type: str, label: str | None = None) -> str:
    return f"{game_name(game_type, label)} is currently disabled on this server."


def jump_url(guild_id: int, channel_id: int, message_id: int | None) -> str | None:
    """Discord deep link to a game's anchor message, or None without one."""
    if not message_id or not channel_id or not guild_id:
        return None
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


def busy_message(
    running_type: str, *, link: str | None = None, label: str | None = None,
) -> str:
    """The channel already has a live game. Names it and links to its board
    when the anchor message is known, so a second host can find the thing in
    the way instead of scrolling for it."""
    name = game_name(running_type, label)
    where = f"**{name}** ([jump to it]({link}))" if link else f"**{name}**"
    return (
        f"❌ There's already a game running in this channel — {where}. "
        "Wait for it to finish, or the host or a mod can `/games end` it first."
    )


def empty_bank_message(game_type: str, label: str | None = None) -> str:
    return (
        f"No prompts in the bank for {game_name(game_type, label)}. "
        "Add some from the Games question bank on the web dashboard."
    )


def no_tag_match_message(tags: Sequence[str]) -> str:
    return f"No questions match tags: {', '.join(tags)} for this game."


async def launch_refusal(
    db,
    game_type: str,
    channel_id: int | None,
    guild_id: int,
    *,
    label: str | None = None,
    tags: Sequence[str] | None = None,
    allow_nsfw: bool = False,
    host_supplied: bool = False,
) -> str | None:
    """Why *game_type* must not launch in *channel_id* right now, or None.

    ``label`` overrides the display name in the refusal (defaults to
    ``GAME_NAMES``). ``tags`` is the host's tag filter for the bank check;
    ``allow_nsfw`` is the channel's own age-gate (``channel_allows_nsfw``), so
    a bank holding only NSFW rows reads as empty in an SFW room. A caller
    launching with host-supplied material (an MLT ``question:``) passes
    ``host_supplied=True`` and the bank is not consulted.
    """
    if not await check_allowed_channel(db, channel_id):
        return CHANNEL_NOT_ALLOWED_MSG
    if not await check_game_enabled(db, game_type, guild_id):
        return disabled_message(game_type, label)
    running = await get_active_game(db, channel_id)
    if running is not None:
        link = jump_url(guild_id, int(running["channel_id"]), running["message_id"])
        return busy_message(running["game_type"], link=link)
    if game_type in BANK_ONLY_TYPES and not host_supplied:
        tag_list = [t for t in (tags or []) if t]
        if not await has_matching_questions(db, game_type, tag_list, allow_nsfw=allow_nsfw):
            if tag_list:
                return no_tag_match_message(tag_list)
            return empty_bank_message(game_type, label)
    return None
