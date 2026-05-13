"""Display-name resolution that prefers local DB over Discord API.

The cogs that render charts of "who interacts with whom" need a display
name for every user ID in the data. The naive approach hits Discord:
``guild.query_members(...)`` for batches and ``bot.fetch_user(...)`` for
left-server members. Both are gateway/HTTP API calls subject to rate
limits, and our own ``known_users`` table already caches every user we've
ever seen.

This helper resolves names DB-first, then live guild cache, only hitting
Discord for users neither knows about.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from bot_modules.core.db_utils import open_db
from bot_modules.services.message_store import get_known_users_bulk

if TYPE_CHECKING:
    import discord


async def resolve_display_names(
    *,
    bot: "discord.Client",
    guild: "discord.Guild",
    db_path: Path,
    user_ids: list[int],
) -> dict[int, str]:
    """Resolve display names for ``user_ids`` with minimal Discord API use.

    Strategy:
      1. Bulk DB lookup against ``known_users``.
      2. For any miss, the live ``guild.get_member`` cache (in-memory).
      3. For users still missing (left the server, never recorded a
         message), one ``bot.fetch_user`` per remaining id.
    """
    import discord  # local to avoid TYPE_CHECKING-only import at module load

    if not user_ids:
        return {}

    name_map: dict[int, str] = {}

    def _db_lookup() -> dict[int, str]:
        with open_db(db_path) as conn:
            return get_known_users_bulk(conn, guild.id, user_ids)

    name_map.update(await asyncio.to_thread(_db_lookup))

    for uid in user_ids:
        if uid in name_map and name_map[uid]:
            continue
        member = guild.get_member(uid)
        if member is not None:
            name_map[uid] = member.display_name

    missing = [uid for uid in user_ids if not name_map.get(uid)]
    for uid in missing:
        try:
            user = await bot.fetch_user(uid)
        except (discord.NotFound, discord.HTTPException):
            continue
        name_map[uid] = user.display_name

    return name_map
