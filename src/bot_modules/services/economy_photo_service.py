"""Photo Challenge payouts — DB layer for posting an image in the photo channel.

Two independent payouts, each once per guild-local day:

* a flat **participation award** (``reward_photo_post``) on the post itself,
  no quest required, deduped on ``econ_photo_rewards``; and
* the **photo_post quest** bonus stacked on top, deduped on its own claim
  occurrence (``photo_post:<local_day>``).

Both ride one transaction, so concurrent posts pay each side at most once
(the same INSERT OR IGNORE anchor pattern as the login faucet).

``econ_photo_rewards`` (migration 101) had no service owner: its only reader
and writer in the repo was the cog's ``on_message`` listener, which also
reached into ``games_game_config`` and ``games_scheduled`` — another feature's
tables — to find the channel. Pure sync SQLite here; the listener that decides
*when* to call this, and the reaction it adds afterwards, stay in the cog.
"""

from __future__ import annotations

import json
import sqlite3

from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.economy.logic import local_day_for
from bot_modules.services.economy_quests_service import (
    fire_trigger_quests,
    source_enabled,
)
from bot_modules.services.economy_service import (
    EconSettings,
    apply_credit,
    load_econ_settings,
)


def read_photo_channel(conn: sqlite3.Connection, guild_id: int) -> int:
    """The configured Photo Challenge channel id, or 0 when unset.

    0 means the admin hasn't picked a Photo Challenge channel — the listener
    no-ops then, so the mechanic is dormant until one is set. Read from
    ``games_game_config`` (game_type 'photo'), the same ``channel_id`` the
    standalone Photo Challenge Setup panel owns. When that config carries no
    channel but an **active photo schedule** does (a schedule created without
    the Setup panel ever being saved, which leaves the config row empty), fall
    back to the schedule's channel so posts there still earn instead of
    silently paying nothing.
    """
    row = conn.execute(
        "SELECT options FROM games_game_config"
        " WHERE guild_id = ? AND game_type = 'photo'",
        (guild_id,),
    ).fetchone()
    opts: dict = {}
    if row and row[0]:
        try:
            opts = json.loads(row[0])
        except (ValueError, TypeError):
            opts = {}
    try:
        channel_id = int(str(opts.get("channel_id")).strip() or 0)
    except (ValueError, TypeError):
        channel_id = 0
    if channel_id > 0:
        return channel_id
    # Config has no channel — recover the channel from an active photo
    # schedule so a schedule-only setup isn't silently unpaid.
    sched = conn.execute(
        "SELECT channel_id FROM games_scheduled"
        " WHERE guild_id = ? AND game_type = 'photo' AND status = 'active'"
        " ORDER BY id ASC LIMIT 1",
        (guild_id,),
    ).fetchone()
    if sched and sched[0]:
        try:
            return int(sched[0])
        except (ValueError, TypeError):
            return 0
    return 0


def payout_possible(conn: sqlite3.Connection, guild_id: int) -> bool:
    """True when a photo payout is possible in this guild right now.

    Economy enabled and the photo_post income source on, plus at least one
    thing to pay: a positive flat participation award (``reward_photo_post``)
    or ≥1 active photo_post quest. Gates the per-post write so a channel with
    nothing to pay never opens a write transaction.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return False
    if not source_enabled(conn, guild_id, "photo_post"):
        return False
    if settings.reward_photo_post > 0:
        return True
    row = conn.execute(
        "SELECT 1 FROM econ_quests WHERE guild_id = ? AND active = 1"
        " AND trigger_kind = 'photo_post' LIMIT 1",
        (guild_id,),
    ).fetchone()
    return row is not None


def award_photo_post(
    conn: sqlite3.Connection,
    guild_id: int,
    user_id: int,
    *,
    channel_id: int,
    booster: bool,
    now: float,
) -> tuple[EconSettings, int, list] | None:
    """Pay for one photo post: ``(settings, participation, fired)``.

    ``None`` when the economy is off or the photo_post income source is
    disabled — the caller has nothing to announce. ``participation`` is the
    credited flat award (0 when already paid today or the award is unpriced);
    ``fired`` is the quest outcomes that also completed, which the caller
    reacts to.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return None
    if not source_enabled(conn, guild_id, "photo_post"):
        return None
    offset = get_tz_offset_hours(conn, guild_id)
    day = local_day_for(now, offset)
    # Flat participation award — once per local day. The INSERT OR IGNORE
    # anchor rides this transaction, so concurrent posts pay it at most once
    # (mirrors the login faucet).
    participation = 0
    if settings.reward_photo_post > 0:
        cur = conn.execute(
            "INSERT OR IGNORE INTO econ_photo_rewards"
            " (guild_id, user_id, local_day) VALUES (?, ?, ?)",
            (guild_id, user_id, day),
        )
        if (cur.rowcount or 0) == 1:
            participation = apply_credit(
                conn,
                guild_id,
                user_id,
                settings.reward_photo_post,
                "photo_post",
                meta={"day": day},
                booster=booster,
                multiplier=settings.booster_multiplier,
            )
    # The photo_post quest bonus stacks on top (once/day by occurrence;
    # fire_trigger_quests re-checks the source toggle).
    fired = fire_trigger_quests(
        conn,
        settings,
        guild_id,
        "photo_post",
        user_id,
        local_day=day,
        occurrence=day,
        booster=booster,
        channel_ids=(channel_id,),
    )
    return settings, participation, fired
