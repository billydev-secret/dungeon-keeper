"""Nightly XP→currency conversion, driven by an hourly day-roll detector.

Each guild's local calendar day is tracked in ``econ_day_marks``. On the hour
we compare the current guild-local day to the stored mark; when it rolls
forward we sum the day-that-just-ended's ``xp_events`` per user and convert each
via :func:`economy_service.process_conversion` (idempotent per user/day), then
advance the mark **last** — so a crash mid-batch simply replays harmlessly on
the next tick. First sight of a guild only records the mark (no retroactive
conversion), and disabled guilds are skipped entirely.

The same hourly tick also drives the quest surface (spec §4):

* **Daily rotation** — on any day roll, a rotate-tag daily pool advances one
  slot (:func:`economy_quests_service.rotate_pool`).
* **Weekly rotation + community settlement** — when the guild-local ISO week
  changes (``econ_day_marks.last_iso_week`` vs :func:`quests.iso_week_for`),
  the weekly pool advances and every completed-but-unsettled community quest is
  paid out. ``last_iso_week`` advances in the same trailing mark update as
  ``last_local_day`` so a crash before it replays the whole roll (settlement is
  reserve-row idempotent, so the replay pays only the members it missed).
* **Claim expiry** — every tick (roll or not), stale pending sign-off claims
  transition to ``expired`` and each claimant is DM'd once. Expiry is a single
  global sweep (``expire_stale_claims`` is not guild-scoped) run before the
  per-guild loop; a disabled guild's stale claims still expire + DM, which is
  harmless (at worst a late DM, never a double payout).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import logic, quests
from bot_modules.services.economy_quests_service import (
    active_member_ids,
    expire_stale_claims,
    get_quest,
    list_settleable_community_quests,
    rotate_pool,
    settle_community_quest,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    member_is_booster,
    notify_member,
    process_conversion,
)

log = logging.getLogger("dungeonkeeper.economy_loop")


def _seconds_until_next_hour() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (nxt - now).total_seconds()


@dataclass(frozen=True)
class ExpiredClaimNotice:
    """One expired sign-off claim to DM after the expiry transaction commits."""

    guild_id: int
    user_id: int
    quest_id: int
    quest_title: str


def _settle_completed_community(
    bot: discord.Client,
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
) -> None:
    """Pay out every completed-but-unsettled community quest for a guild.

    ``list_settleable_community_quests`` already excludes sign-off quests — those
    settle only via the dashboard's manual path — so the auto-sweep never pays a
    quest awaiting human approval. Members are the last-30-day active roster;
    each payout is booster-ceiled and reserve-row idempotent.
    """
    settleable = list_settleable_community_quests(conn, guild_id)
    if not settleable:
        return
    member_ids = active_member_ids(conn, guild_id, days=30)
    member_boosters = {uid: member_is_booster(bot, guild_id, uid) for uid in member_ids}
    for quest in settleable:
        settle_community_quest(
            conn, settings, guild_id, int(quest["id"]), member_boosters
        )


def run_guild_day_roll(
    bot: discord.Client,
    conn: sqlite3.Connection,
    guild_id: int,
    now_ts: float,
) -> None:
    """Detect and process a guild-local day (and ISO-week) roll.

    First sight of a guild just records both marks — nothing is converted,
    rotated, or settled retroactively. On a day roll, every user with
    ``xp_events`` on the day that just ended is converted (booster ceil per
    member) and the daily rotate-tag pool advances one slot. When the ISO week
    also changed, the weekly pool advances and completed community quests are
    settled. Both marks advance **last**, together — because conversion and
    settlement are idempotent, a crash before the mark update replays without
    double-crediting.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return

    offset = get_tz_offset_hours(conn, guild_id)
    today = logic.local_day_for(now_ts, offset)
    this_week = quests.iso_week_for(today)

    row = conn.execute(
        "SELECT last_local_day, last_iso_week FROM econ_day_marks WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO econ_day_marks "
            "(guild_id, last_local_day, last_iso_week) VALUES (?, ?, ?)",
            (guild_id, today, this_week),
        )
        return

    last_day = row["last_local_day"]
    if last_day == today:
        return

    # ── day roll: convert the day that just ended, advance daily pool ──
    start, end = logic.local_day_bounds(last_day, offset)
    rows = conn.execute(
        """
        SELECT user_id, SUM(amount) AS xp
        FROM xp_events
        WHERE guild_id = ? AND created_at >= ? AND created_at < ?
        GROUP BY user_id
        """,
        (guild_id, start, end),
    ).fetchall()
    for r in rows:
        user_id = int(r["user_id"])
        xp = float(r["xp"] or 0.0)
        booster = member_is_booster(bot, guild_id, user_id)
        process_conversion(
            conn,
            settings,
            guild_id,
            user_id,
            local_day=last_day,
            xp=xp,
            booster=booster,
        )

    rotate_pool(conn, guild_id, "daily")

    # ── week roll: advance weekly pool + settle community quests ──
    # ``last_iso_week`` is NULL for pre-064 mark rows; treat that as a backfill
    # (record the week, don't settle) rather than a spurious week change.
    last_week = row["last_iso_week"]
    if last_week is not None and last_week != this_week:
        rotate_pool(conn, guild_id, "weekly")
        _settle_completed_community(bot, conn, settings, guild_id)

    # Marks advance LAST (both columns together) so any crash above replays the
    # whole roll on the next tick.
    conn.execute(
        "UPDATE econ_day_marks SET last_local_day = ?, last_iso_week = ? "
        "WHERE guild_id = ?",
        (today, this_week, guild_id),
    )


def run_claim_expiry(
    conn: sqlite3.Connection, now_ts: float
) -> list[ExpiredClaimNotice]:
    """Expire stale pending sign-off claims and collect their DM notices.

    ``expire_stale_claims`` transitions each row out of 'pending' as it returns
    it (atomic UPDATE ... RETURNING), so a claimant is only ever notified once.
    Runs against the whole DB (not one guild) — each notice carries its own
    guild_id for the after-commit DM.
    """
    notices: list[ExpiredClaimNotice] = []
    for claim in expire_stale_claims(conn, now_ts):
        gid = int(claim["guild_id"])
        quest = get_quest(conn, gid, int(claim["quest_id"]))
        title = quest["title"] if quest is not None else "a quest"
        notices.append(
            ExpiredClaimNotice(
                guild_id=gid,
                user_id=int(claim["user_id"]),
                quest_id=int(claim["quest_id"]),
                quest_title=title,
            )
        )
    return notices


async def run_tick(bot: discord.Client, db_path: Path, now_ts: float) -> None:
    """One hourly tick: global claim expiry (+ DMs), then per-guild rolls.

    The expiry sweep commits before any DM is sent — ``notify_member`` is async
    Discord I/O, so rows are collected inside the transaction and notified after
    it commits. Per-guild roll failures are logged and isolated so one guild
    never stalls the rest.
    """
    try:
        with open_db(db_path) as conn:
            notices = run_claim_expiry(conn, now_ts)
    except Exception:
        log.exception("Economy loop: claim-expiry sweep failed.")
        notices = []

    for notice in notices:
        try:
            await notify_member(
                bot,
                db_path,
                notice.guild_id,
                notice.user_id,
                content=(
                    f"Your claim on **{notice.quest_title}** expired — "
                    "you can re-claim it."
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: failed to DM expired claim to user %s.",
                notice.user_id,
            )

    for guild in list(bot.guilds):
        try:
            with open_db(db_path) as conn:
                run_guild_day_roll(bot, conn, guild.id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Economy loop: unhandled error for guild %s.", guild.id)


async def economy_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        sleep_secs = _seconds_until_next_hour()
        await asyncio.sleep(sleep_secs)
        await run_tick(bot, db_path, time.time())
