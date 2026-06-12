"""Pure decision logic for starboard reaction handling.

All functions here take and return plain Python values so they're unit-
testable without spinning up Discord. The cog calls these to decide
whether a reaction event should proceed, whether reposting would leak
NSFW, and whether an emoji string is a valid reaction target.
"""

from __future__ import annotations

from collections.abc import Iterable

import discord


def should_process_reaction(
    *,
    cfg_enabled: bool,
    cfg_channel_id: int,
    cfg_emoji: str,
    payload_emoji: str,
    payload_channel_id: int,
    excluded_channel_ids: Iterable[int],
) -> bool:
    """Return True if a raw reaction event should advance to the starboard flow.

    Combines all the early-exit checks that both ``on_raw_reaction_add`` and
    ``on_raw_reaction_remove`` apply: starboard must be enabled and have a
    channel configured, the reaction emoji must match, the reaction must NOT
    be on the starboard channel itself (otherwise stars on starboard posts
    would compound), and the source channel must not be excluded.
    """
    if not cfg_enabled:
        return False
    if not cfg_channel_id:
        return False
    if payload_emoji != cfg_emoji:
        return False
    if payload_channel_id == cfg_channel_id:
        return False
    if payload_channel_id in set(excluded_channel_ids):
        return False
    return True


def nsfw_leak_blocked(*, source_nsfw: bool, starboard_nsfw: bool) -> bool:
    """Return True when reposting source → starboard would leak NSFW.

    Discord's age-gate only applies to NSFW channels. Reposting an
    age-restricted message into a non-NSFW starboard would let members
    who lack access to the source see the content, so we refuse.
    """
    return source_nsfw and not starboard_nsfw


def validate_emoji(emoji_str: str) -> tuple[bool, str | None]:
    """Validate that ``emoji_str`` parses as a Discord reaction emoji.

    Returns ``(ok, error_message)``. On success ``error_message`` is None;
    on failure it carries the user-facing message the cog should ephemeral-
    reply with. Refuses empty strings (mods can clear an emoji elsewhere)
    and refuses plain text that PartialEmoji would silently treat as
    unparseable, which would otherwise leave the starboard listener
    quietly broken until someone notices it never triggered.
    """
    s = emoji_str.strip()
    if not s:
        return False, "Emoji cannot be empty."

    try:
        parsed: discord.PartialEmoji | None = discord.PartialEmoji.from_str(s)
    except Exception:  # noqa: BLE001 — defensive: any parser bug must reject
        parsed = None

    if parsed is None or (parsed.id is None and not parsed.name):
        return False, (
            "That doesn't look like a reaction emoji. Use a unicode emoji "
            "(e.g. ⭐) or a server custom emoji (e.g. <:name:123456>)."
        )
    return True, None
