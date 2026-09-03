"""Shared display-name resolution for embeds — no Discord API calls.

A ``<@id>`` mention is only resolved into a name by the *reading* client, from
its own cache. Discord's servers do nothing to it, so inside an embed it
degrades to a bare numeric id for any viewer who hasn't seen that user before.
Anywhere a name is meant to simply render, resolve it here and emit plain text.

The chain, in order:

1. ``guild.get_member(uid).display_name`` — ``intents.members`` is enabled, so
   this cache is complete for present members and updates the moment somebody
   changes their nickname.
2. ``known_users.display_name``, then ``known_users.username`` — persistent, and
   the only source covering members who have **left** the guild.
3. ``<@id>`` — last resort for a user neither source knows.

The live cache leads because a ``known_users`` row is only as fresh as that
user's last recorded activity: someone who renamed and hasn't spoken since keeps
a stale name on file indefinitely. The table then covers exactly what the cache
structurally cannot.

:func:`resolve_name_from` is the pure, synchronous core, safe to call from an
embed builder. :func:`build_name_fn` does the async prefetch and hands back a
``NameFn`` closed over the results — and only queries for ids the member cache
misses, so a roster of present members costs no I/O at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import open_db
from bot_modules.services.message_store import get_known_user_names_bulk

if TYPE_CHECKING:
    import discord

# A NameFn turns a user id into embed-ready text: an escaped display name, or a
# raw <@id> mention for a user we genuinely cannot name.
NameFn = Callable[[int], str]


def mention(user_id: int) -> str:
    """The last-resort rendering for a user we can't name.

    Also the default ``NameFn`` for embed builders whose caller doesn't inject
    a resolver, which keeps their pre-resolver output unchanged.
    """
    return f"<@{user_id}>"


def named_or_anonymous(user_id: int, name_fn: NameFn, *, fallback: str = "a member") -> str:
    """``name_fn(user_id)``, or ``fallback`` when there is no user to name.

    A user id of 0 is not an unknown member — it is the absence of one, which
    is how an erasure detaches a still-running purchase from the person who
    bought it (``economy_theme_service.anonymise_live_theme``). Feeding that 0
    to a resolver would render the literal ``<@0>``, so every embed that can
    outlive its buyer routes through here instead of repeating the check.
    """
    return name_fn(user_id) if user_id else fallback


def resolve_name_from(
    user_id: int,
    *,
    guild: "discord.Guild | None",
    table_names: Mapping[int, str] | None = None,
) -> str:
    """Resolve one user id to embed-ready text.

    The chain is live member cache -> ``known_users`` (``table_names``) ->
    ``<@id>``. ``table_names`` is a prefetched mapping rather than a live
    connection so this stays synchronous and safe to call from an embed
    builder; see :func:`build_name_fn` for the async prefetch.

    The result is markdown-escaped, since a display name containing ``*`` or
    ``_`` would otherwise mangle the surrounding embed copy.
    """
    import discord  # local: keeps module import cheap for non-Discord callers

    member = guild.get_member(user_id) if guild is not None else None
    for candidate in (
        member.display_name if member is not None else None,
        table_names.get(user_id) if table_names else None,
    ):
        name = (candidate or "").strip()
        if name:
            return discord.utils.escape_markdown(name)

    return mention(user_id)


async def build_name_fn(
    *,
    guild: "discord.Guild | None",
    db_path: Path,
    guild_id: int,
    user_ids: list[int],
) -> NameFn:
    """Prefetch names for ``user_ids`` and return a sync resolver over them.

    Only ids the live member cache can't answer are looked up in the database,
    so a guild full of present members costs no query at all. The single
    batched read runs in a worker thread to keep sqlite off the event loop.
    """
    misses = sorted(
        {
            uid for uid in user_ids
            if uid and (guild is None or guild.get_member(uid) is None)
        }
    )

    table_names: dict[int, str] = {}
    if misses:
        def _lookup() -> dict[int, str]:
            with open_db(db_path) as conn:
                return get_known_user_names_bulk(conn, guild_id, misses)

        table_names = await asyncio.to_thread(_lookup)

    def resolve(uid: int) -> str:
        return resolve_name_from(uid, guild=guild, table_names=table_names)

    return resolve
