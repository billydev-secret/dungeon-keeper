"""One-shot startup reconcile for the ``boost`` trigger.

The boost quest is credited by the live ``on_member_update`` transition where a
member's ``premium_since`` flips from unset to set (``EconomyCog._on_boost_started``).
That listener only ever sees a *change*, so it misses anyone who was already
boosting when the quest — or the listener itself — shipped: the state
transition already happened, and boosting is a one-shot state (you never
"start boosting" a second time), so those members would never be credited.

On startup we replay the boost trigger once for every member currently boosting
each guild, keyed on their ``premium_since`` timestamp — the exact occurrence
key the live listener derives (``str(int(premium_since.timestamp()))``). The
per-occurrence claim collision in ``claim_quest`` makes this idempotent: a
member the listener already paid collides and is skipped, so no one is
double-credited, and re-running on every restart is safe.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_quests_service import fire_trigger_inline

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)


class _BoostMember(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def premium_since(self) -> datetime | None: ...


class _BoostGuild(Protocol):
    """Structural view of the guild fields the reconcile touches.

    A real ``discord.Guild`` satisfies this (``premium_subscribers`` is its
    boosting members), and so can a lightweight test double. Read-only
    properties keep ``premium_subscribers`` covariant, so a plain
    ``list[Member]`` matches.
    """

    @property
    def id(self) -> int: ...
    @property
    def premium_subscribers(self) -> Sequence[_BoostMember]: ...


def reconcile_guild_boosters(
    conn: sqlite3.Connection, guild: _BoostGuild
) -> int:
    """Credit the boost trigger to every current booster of ``guild``.

    Returns the number of boost claims filed this pass (0 when every booster
    was already credited, or the guild has none). Rides the passed connection.
    """
    fired_total = 0
    for member in guild.premium_subscribers:
        since = member.premium_since
        if since is None:
            continue
        occurrence = str(int(since.timestamp()))
        fired = fire_trigger_inline(
            conn,
            guild.id,
            "boost",
            member.id,
            occurrence=occurrence,
            booster=True,
        )
        fired_total += len(fired)
    return fired_total


async def reconcile_boosters(bot: "Bot", db_path: "Path") -> None:
    """Startup task: replay the boost trigger for existing boosters, once.

    Waits for the member cache to populate, then walks each guild's current
    boosters. Idempotent via the boost claim's per-occurrence key, so it is
    harmless to re-run on every boot — a fully-reconciled guild files nothing.
    """
    await bot.wait_until_ready()
    for guild in bot.guilds:
        try:
            with open_db(db_path) as conn:
                filed = reconcile_guild_boosters(conn, guild)
        except Exception:
            log.exception("boost reconcile failed for guild %s", guild.id)
            continue
        if filed:
            log.info(
                "boost reconcile: filed %s boost claim(s) for existing "
                "boosters in guild %s",
                filed,
                guild.id,
            )
