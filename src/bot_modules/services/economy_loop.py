"""Hourly day-roll detector: quest rotation, settlement, and (when enabled) an
optional nightly XP→currency conversion.

Each guild's local calendar day is tracked in ``econ_day_marks``. On the hour
we compare the current guild-local day to the stored mark; when it rolls
forward, and **only if the guild's ``xp_per_coin`` rate is positive** (the
faucet ships off), we sum the day-that-just-ended's ``xp_events`` per user and
convert each via :func:`economy_service.process_conversion` (idempotent per
user/day). The mark advances **last** — so a crash mid-batch simply replays
harmlessly on the next tick. First sight of a guild only records the mark (no
retroactive conversion), and disabled guilds are skipped entirely.

The same hourly tick also drives the quest surface (spec §4):

* **Daily rotation** — on any day roll, a rotate-tag daily pool advances one
  slot (:func:`economy_quests_service.rotate_pool`).
* **Weekly rotation + community settlement + metrics** — when the guild-local
  ISO week changes (``econ_day_marks.last_iso_week`` vs
  :func:`quests.iso_week_for`), the weekly pool advances, every
  completed-but-unsettled community quest is paid out, and
  :func:`economy_metrics_service.compute_weekly_rollup` snapshots the week that
  just closed. ``last_iso_week`` advances in the same trailing mark update as
  ``last_local_day`` so a crash before it replays the whole roll (settlement is
  reserve-row idempotent and the rollup is PK-idempotent, so the replay pays
  only the members it missed and recomputes no metrics).
* **Claim expiry** — every tick (roll or not), stale pending sign-off claims
  transition to ``expired`` and each claimant is DM'd once. Expiry is a single
  global sweep (``expire_stale_claims`` is not guild-scoped) run before the
  per-guild loop; a disabled guild's stale claims still expire + DM, which is
  harmless (at worst a late DM, never a double payout).

This module also hosts a **second, faster loop** — :func:`register_loop` — for
the register channel's transaction feed (see ``economy/register.py``). It drains
new ``econ_ledger`` rows every :data:`REGISTER_INTERVAL_SECONDS` rather than
hourly, because a "you just got paid" entry is only useful while it is news.
Draining the ledger (rather than hooking payout call sites) means it catches
every currency movement, dashboard grants included, with nothing to forget.

The tick also refreshes each guild's **leaderboard panel** in place
(:func:`run_guild_leaderboard` — the ``/bank post-leaderboard`` embed; a 404
on the stored message clears its ids so a deleted panel stops the refresh).
Between ticks, :func:`leaderboard_live_loop` (a separate startup task) gives
the panel its near-real-time cadence: economy writes mark their guild dirty
via :mod:`bot_modules.economy.live_signal`, and the live loop repaints each
dirty panel at most once per :data:`LIVE_MIN_INTERVAL` seconds — the hourly
pass stays as the restart/rollback backstop.

The same tick also drives the **rental billing pass** (spec §6) per enabled
guild, after the day roll. Each pass has three phases, mirroring the loop's
"sync body, async effects" shape:

1. **Feature-gate reads (async, pre-transaction).** For the two feature-gated
   perks (role_icon / role_gradient) that actually have a live rental, ask
   :func:`perk_actions.feature_gate_ok` whether the guild still supports them.
   These are Discord reads, so they run before the transaction opens.
2. **Sweep + bill (sync, one transaction).** :func:`run_guild_rental_billing`
   suspends/resumes rentals whose feature gate flipped (freezing billing while
   suspended — the clock resumes via ``set_rental_suspended``) and then bills
   every live rental via :func:`economy_rentals_service.bill_rental`. The sweep
   runs BEFORE billing so a just-suspended rental is not charged this tick.
3. **Effects (async, post-commit).** DMs on grace entry / lapse / suspension
   transitions, ``revoke_role_perks`` for the beneficiary on lapse/cancel, and
   ``apply_role_perks`` to re-project a resumed rental. Every effect is
   fail-safe: a Discord outage can never corrupt billing state, and the
   projector is idempotent so a missed revoke self-heals on the next call.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.economy import live_signal, logic, quests
from bot_modules.economy.leaderboard import (
    build_leaderboard_embed,
    collect_leaderboard_data,
)
from bot_modules.economy.quest_views import QuestBoardView
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    revoke_role_perks,
)
from bot_modules.economy.register import (
    RegisterEntry,
    build_register_embed,
    collect_register_entries,
)
from bot_modules.economy.rentals import GRACE_SECONDS, BillingAction
from bot_modules.services.voice_master_service import (
    DEFAULT_NAME_TEMPLATE,
    get_owned_channel,
    load_voice_master_config,
    resolve_channel_name,
)
from bot_modules.services import economy_emoji_service as emoji_svc
from bot_modules.services import economy_demurrage_service as demurrage_svc
from bot_modules.services import economy_bounty_service as bounty_svc
from bot_modules.services import economy_pin_service as pin_svc
from bot_modules.services import economy_raffle_service as raffle_svc
from bot_modules.services.economy_qotd_sponsor_service import (
    expire_stale_submissions,
)
from bot_modules.services.economy_quests_service import (
    activate_community_weekly,
    active_member_ids,
    auto_size_community_target,
    community_contrib_summary,
    expire_stale_claims,
    get_quest,
    list_active_community_kind_quests,
    list_active_pool_ids,
    spotlight_kind,
    list_settleable_community_quests,
    next_community_weekly,
    prune_kind_activity,
    rotate_pool,
    settle_community_quest,
    settle_community_weekly,
)
from bot_modules.services.economy_metrics_service import compute_weekly_rollup
from bot_modules.services.economy_rentals_service import (
    BillingResult,
    bill_rental,
    list_rentals,
    set_rental_suspended,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    member_is_booster,
    notify_member,
    process_conversion,
    save_econ_settings,
)
from bot_modules.services.message_store import get_known_users_bulk

# The perks whose billing is gated on a guild feature (role icon / gradient +
# holographic role colors). Only these are swept each tick — the sweep asks
# Discord whether the feature still exists, so it is kept to the perks that can
# actually lose it.
_FEATURE_GATED_PERKS = ("role_icon", "role_gradient", "role_holographic")

# Grace-window length in whole hours, for the "payment failed" DM copy.
_GRACE_HOURS = int(GRACE_SECONDS // 3600)

# ── register feed cadence ──────────────────────────────────────────────
# How often the ledger is drained to the register channel. A feed wants to
# read as live, but each entry is its own message, so this also paces sends
# well inside Discord's per-channel rate limit.
REGISTER_INTERVAL_SECONDS = 30
# Ceiling on entries posted per guild per drain. A burst (a community-quest
# settlement paying dozens of members at once) spills into the next ticks
# instead of hammering the channel.
REGISTER_MAX_PER_TICK = 8
# Entries older than this are skipped rather than posted. A register is a
# live feed — replaying a day-old backlog after downtime is noise, not news.
REGISTER_STALE_SECONDS = 3600.0

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


@dataclass(frozen=True)
class CommunityBeat:
    """One community-weekly beat sheet to DM the host after commit.

    ``text`` is the fully rendered sheet (numbers + suggested copy) — the
    host posts it publicly in their own voice; the bot never does.
    """

    guild_id: int
    text: str


@dataclass(frozen=True)
class DayRollResult:
    """What a guild's day roll produced, for the post-commit effects."""

    beats: tuple[CommunityBeat, ...] = ()
    week_rolled: bool = False
    # The raffle draw for the week that just closed (sinks round 3, stage 5);
    # None when the raffle is off, the week was already drawn (a replay), or
    # no week rolled.
    raffle: raffle_svc.DrawResult | None = None


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
) -> DayRollResult:
    """Detect and process a guild-local day (and ISO-week) roll.

    First sight of a guild just records both marks — nothing is converted,
    rotated, or settled retroactively. On a day roll, when the guild's
    ``xp_per_coin`` rate is positive (the faucet is off by default), every user
    with ``xp_events`` on the day that just ended is converted (booster ceil per
    member); the daily rotate-tag pool advances one slot regardless. When the ISO week
    also changed, the weekly pool advances and completed community quests are
    settled. Both marks advance **last**, together — because conversion and
    settlement are idempotent, a crash before the mark update replays without
    double-crediting.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return DayRollResult()

    offset = get_tz_offset_hours(conn, guild_id)
    today = logic.local_day_for(now_ts, offset)
    this_week = quests.iso_week_for(today)

    row = conn.execute(
        "SELECT last_local_day, last_iso_week, last_community_week "
        "FROM econ_day_marks WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()

    if row is None:
        conn.execute(
            "INSERT OR IGNORE INTO econ_day_marks "
            "(guild_id, last_local_day, last_iso_week) VALUES (?, ?, ?)",
            (guild_id, today, this_week),
        )
        return DayRollResult()

    repair_beats = _repair_orphaned_community_quests(conn, guild_id, this_week, today)

    last_day = row["last_local_day"]
    if last_day == today:
        return DayRollResult(beats=tuple(repair_beats))
    beats: list[CommunityBeat] = list(repair_beats)
    week_rolled = False

    # ── day roll: convert the day that just ended, advance daily pool ──
    # The XP→coin faucet is off when the rate is 0 (the default): skip it
    # entirely so nothing is written or accumulated. Re-enabling (a positive
    # rate on the dashboard) then resumes from that day, not a backlog.
    if settings.xp_per_coin > 0:
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
    prune_kind_activity(conn, guild_id, today)

    # ── week roll: advance weekly pool + settle community quests ──
    # ``last_iso_week`` is NULL for pre-064 mark rows; treat that as a backfill
    # (record the week, don't settle) rather than a spurious week change.
    last_week = row["last_iso_week"]
    community_week = row["last_community_week"]
    raffle_result: raffle_svc.DrawResult | None = None
    if last_week is not None and last_week != this_week:
        week_rolled = True
        rotate_pool(conn, guild_id, "weekly")
        _settle_completed_community(bot, conn, settings, guild_id)
        week_beats, community_week = _roll_community_weekly(
            bot, conn, settings, guild_id,
            closed_week=last_week,
            new_week=this_week,
            community_week=community_week,
            local_day=today,
        )
        beats.extend(week_beats)
        beats.extend(_roll_community_weekly_slot2(
            bot, conn, settings, guild_id,
            closed_week=last_week,
            new_week=this_week,
            local_day=today,
        ))
        # Roll up metrics for the week that JUST closed (idempotent via PK —
        # a replay before the marks advance recomputes nothing).
        compute_weekly_rollup(
            conn, settings, guild_id, last_week, offset_hours=offset, now=now_ts
        )
        # Draw the closed week's raffle. Exactly-once via the draws PK — a
        # crash-and-replay of this roll gets None and stays quiet.
        if raffle_svc.raffle_enabled(settings):
            raffle_result = raffle_svc.draw_raffle(
                conn, guild_id, last_week, now=now_ts
            )
        # Collect the hoard tax for the week that just closed. Exactly-once
        # via the sweeps PK (same pattern) — the register feed narrates each
        # taxed member's ledger row, so no announcement happens here.
        if demurrage_svc.demurrage_enabled(settings):
            demurrage_svc.run_sweep(
                conn, settings, guild_id, last_week, now=now_ts
            )

    # Marks advance LAST (both columns together) so any crash above replays the
    # whole roll on the next tick.
    conn.execute(
        "UPDATE econ_day_marks SET last_local_day = ?, last_iso_week = ?, "
        "last_community_week = ? WHERE guild_id = ?",
        (today, this_week, community_week, guild_id),
    )
    return DayRollResult(
        beats=tuple(beats), week_rolled=week_rolled, raffle=raffle_result
    )


def _roll_community_weekly(
    bot: discord.Client,
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    *,
    closed_week: str,
    new_week: str,
    community_week: str | None,
    local_day: str,
) -> tuple[list[CommunityBeat], str | None]:
    """Gap-week alternation at the ISO-week roll (quest-variety stage 3).

    One week on, one week off: a run settles at the roll that closes its
    week (tier payouts + resolution beat, quest deactivates), the next roll
    finds a full gap week behind it and activates the library's next
    community weekly (auto-sized target, kickoff beat). Returns the beats
    plus the updated ``last_community_week`` mark — the run week, advanced
    only at activation, so "gap over" is simply ``community_week !=
    closed_week``. First-ever roll (mark NULL) activates immediately.

    This is concurrency lane 1 of 2 (``community_slot``) — lane 2
    (``_roll_community_weekly_slot2``) runs the same weekly cadence with no
    gap week, so the board isn't fully dark during this lane's breather.
    """
    beats: list[CommunityBeat] = []
    active = list_active_community_kind_quests(conn, guild_id, slot=1)
    if active:
        member_ids = active_member_ids(conn, guild_id, days=30)
        boosters = {
            uid: member_is_booster(bot, guild_id, uid) for uid in member_ids
        }
        for quest in active:
            summary = settle_community_weekly(
                conn, settings, guild_id, quest, boosters
            )
            beats.append(
                CommunityBeat(guild_id, quests.beat_resolution(summary))
            )
        return beats, community_week

    if community_week is not None and community_week == closed_week:
        return beats, community_week  # the gap week — let the win breathe

    nxt = next_community_weekly(conn, guild_id)
    if nxt is None:
        return beats, community_week  # library has no community weeklies
    kind = str(nxt["trigger_kind"])
    scope = nxt["trigger_channel_id"]
    target = auto_size_community_target(
        conn, guild_id, kind, local_day,
        channel_id=int(scope) if scope is not None else None,
    )
    activate_community_weekly(
        conn, guild_id, int(nxt["id"]), target=target, week=new_week
    )
    beats.append(
        CommunityBeat(
            guild_id,
            quests.beat_kickoff(
                str(nxt["title"]),
                quests.TRIGGER_KINDS.get(kind, kind),
                target,
                new_week,
            ),
        )
    )
    return beats, new_week


def _roll_community_weekly_slot2(
    bot: discord.Client,
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    *,
    closed_week: str,
    new_week: str,
    local_day: str,
) -> list[CommunityBeat]:
    """Concurrency lane 2: same weekly cadence as lane 1, no gap week.

    2026-07-22 decision: lane 1 keeps its one-week-on/one-week-off breather
    untouched, but two lines with matched 1-week run/gap durations can only
    ever be in perfect sync (both active, both dark together) or perfect
    anti-phase (always exactly one active) — there's no offset that gives
    sustained double coverage. So lane 2 instead refills the instant it
    settles: the board shows 2 goals whenever lane 1 is mid-run, and drops
    to just lane 2 during lane 1's gap week, but is never fully empty.
    """
    beats: list[CommunityBeat] = []
    active = list_active_community_kind_quests(conn, guild_id, slot=2)
    if active:
        quest = active[0]
        if str(quest["last_run_week"]) != closed_week:
            return beats  # still mid-run
        member_ids = active_member_ids(conn, guild_id, days=30)
        boosters = {
            uid: member_is_booster(bot, guild_id, uid) for uid in member_ids
        }
        summary = settle_community_weekly(conn, settings, guild_id, quest, boosters)
        beats.append(CommunityBeat(guild_id, quests.beat_resolution(summary)))

    nxt = next_community_weekly(conn, guild_id)
    if nxt is None:
        return beats  # library has no more community weeklies free
    kind = str(nxt["trigger_kind"])
    scope = nxt["trigger_channel_id"]
    target = auto_size_community_target(
        conn, guild_id, kind, local_day,
        channel_id=int(scope) if scope is not None else None,
    )
    activate_community_weekly(
        conn, guild_id, int(nxt["id"]), target=target, week=new_week, slot=2
    )
    beats.append(
        CommunityBeat(
            guild_id,
            quests.beat_kickoff(
                str(nxt["title"]),
                quests.TRIGGER_KINDS.get(kind, kind),
                target,
                new_week,
            ),
        )
    )
    return beats


def _repair_orphaned_community_quests(
    conn: sqlite3.Connection, guild_id: int, this_week: str, local_day: str
) -> list[CommunityBeat]:
    """Self-heal community-kind quests stuck active outside the slot system.

    A quest only carries a real ``community_target`` once it passes through
    ``activate_community_weekly`` — a seed script bug (2026-07-22) flipped
    ``active=1`` directly on 11 library rows, leaving them targetless and
    outside both concurrency lanes. Runs every tick (cheap, guild-scoped) so
    a bad seed self-heals within the hour rather than waiting on the next
    week roll, and immediately fills whichever lanes it just freed up — but
    only in the same tick it actually found orphans, so it never overrides a
    legitimate, intentional gap week.
    """
    orphans = conn.execute(
        "SELECT id FROM econ_quests WHERE guild_id = ? AND qtype = 'community' "
        "AND trigger_kind != '' AND active = 1 AND community_target IS NULL",
        (guild_id,),
    ).fetchall()
    if not orphans:
        return []
    conn.execute(
        "UPDATE econ_quests SET active = 0 "
        "WHERE guild_id = ? AND qtype = 'community' AND trigger_kind != '' "
        "AND active = 1 AND community_target IS NULL",
        (guild_id,),
    )
    beats: list[CommunityBeat] = []
    for slot in (1, 2):
        if list_active_community_kind_quests(conn, guild_id, slot=slot):
            continue
        nxt = next_community_weekly(conn, guild_id)
        if nxt is None:
            return beats
        kind = str(nxt["trigger_kind"])
        scope = nxt["trigger_channel_id"]
        target = auto_size_community_target(
            conn, guild_id, kind, local_day,
            channel_id=int(scope) if scope is not None else None,
        )
        activate_community_weekly(
            conn, guild_id, int(nxt["id"]), target=target, week=this_week, slot=slot
        )
        beats.append(
            CommunityBeat(
                guild_id,
                quests.beat_kickoff(
                    str(nxt["title"]),
                    quests.TRIGGER_KINDS.get(kind, kind),
                    target,
                    this_week,
                ),
            )
        )
    return beats


def community_hourly_beats(
    conn: sqlite3.Connection,
    guild_id: int,
    now_ts: float,
) -> list[CommunityBeat]:
    """Every-tick beat detection for the running community weekly.

    Tier crossings compare the live counter against ``notified_tier`` (which
    advances here, same transaction, so a beat DMs once); the final-24h
    nudge fires when the guild-local ISO week has under a day left and the
    top tier is still open.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return []
    beats: list[CommunityBeat] = []
    offset = get_tz_offset_hours(conn, guild_id)
    today = logic.local_day_for(now_ts, offset)
    for quest in list_active_community_kind_quests(conn, guild_id):
        qid = int(quest["id"])
        target = int(quest["community_target"] or 0)
        current = int(quest["current"] or 0)
        crossed = quests.community_tiers_crossed(current, target)
        notified = int(quest["notified_tier"] or 0)
        if crossed > notified:
            conn.execute(
                "UPDATE econ_community_progress SET notified_tier = ? "
                "WHERE quest_id = ?",
                (crossed, qid),
            )
            contributors, _top = community_contrib_summary(conn, qid)
            beats.append(
                CommunityBeat(
                    guild_id,
                    quests.beat_tier(
                        str(quest["title"]), crossed, current, target,
                        contributors,
                    ),
                )
            )
        if (
            not quest["final_notice_sent"]
            and crossed < len(quests.COMMUNITY_TIERS)
            and _seconds_to_next_week_start(today, offset, now_ts) < 86400
        ):
            conn.execute(
                "UPDATE econ_community_progress SET final_notice_sent = 1 "
                "WHERE quest_id = ?",
                (qid,),
            )
            beats.append(
                CommunityBeat(
                    guild_id,
                    quests.beat_final24(str(quest["title"]), current, target),
                )
            )
    return beats


def _seconds_to_next_week_start(
    local_day: str, offset: float, now_ts: float
) -> float:
    """Seconds until the next guild-local ISO week (Monday 00:00) begins."""
    from datetime import date, timedelta

    day = date.fromisoformat(local_day)
    next_monday = day + timedelta(days=7 - day.weekday())
    start_ts, _end = logic.local_day_bounds(next_monday.isoformat(), offset)
    return max(0.0, start_ts - now_ts)


def flip_announcement_content(
    pool: int, spot_label: str | None, game_role_id: int
) -> tuple[str, int]:
    """The weekly flip body + the role id to ping (0 = none).

    Opted-in members hold the economy game role, so the flip pings it — the one
    recurring economy post that reaches them without a DM. The caller allow-lists
    exactly that role so a copied body can never mint an @everyone ping.
    """
    lines = [
        f"📋 **This week's quests are up!** {pool} weeklies in the pool — "
        f"`/quests` shows yours.",
    ]
    if spot_label:
        lines.append(f"⚡ **Spotlight:** {spot_label} pays **double** all week.")
    body = "\n".join(lines)
    if game_role_id:
        return f"<@&{game_role_id}>\n{body}", game_role_id
    return body, 0


async def _post_flip_announcement(
    bot: discord.Client, db_path: Path, guild_id: int, now_ts: float
) -> None:
    """Post "this week's quests are up" at the ISO-week roll (stage 5).

    Lands in the leaderboard panel's channel when one is posted, else the
    bank channel; silently skips guilds with neither. Reveals the ⚡
    spotlight kind — the week's featured activity paying double.
    """

    def _load():
        with open_db(db_path) as conn:
            settings = load_econ_settings(conn, guild_id)
            offset = get_tz_offset_hours(conn, guild_id)
            week = quests.iso_week_for(logic.local_day_for(now_ts, offset))
            spot = spotlight_kind(conn, guild_id, week)
            pool = len(list_active_pool_ids(conn, guild_id, "weekly"))
            return settings, spot, pool

    settings, spot, pool = await asyncio.to_thread(_load)
    channel_id = settings.leaderboard_channel_id or settings.bank_channel_id
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(channel_id) if guild else None
    if not isinstance(channel, discord.TextChannel):
        return
    spot_label = quests.TRIGGER_KINDS.get(spot, spot) if spot else None
    content, ping_role = flip_announcement_content(
        pool, spot_label, settings.game_role_id
    )
    mentions = (
        discord.AllowedMentions(roles=[discord.Object(id=ping_role)])
        if ping_role
        else discord.AllowedMentions.none()
    )
    try:
        await channel.send(content, allowed_mentions=mentions)
    except discord.HTTPException:
        log.warning("flip announcement failed to send in %s", channel_id)


async def _send_community_beats(
    bot: discord.Client, db_path: Path, beats: list[CommunityBeat]
) -> None:
    """DM beat sheets to each guild's community host (post-commit effect).

    Host = ``community_host_user_id`` when set, else the guild owner. A
    failed DM is logged and dropped — beats are advisory copy, never money.
    """
    for beat in beats:
        try:
            guild = bot.get_guild(beat.guild_id)
            if guild is None:
                continue

            def _load_host(gid: int = beat.guild_id) -> int:
                with open_db(db_path) as conn:
                    return load_econ_settings(conn, gid).community_host_user_id

            host_id = await asyncio.to_thread(_load_host)
            if not host_id:
                host_id = guild.owner_id or 0
            member = guild.get_member(int(host_id)) if host_id else None
            if member is None:
                log.warning(
                    "Community beat: no host resolvable for guild %s.",
                    beat.guild_id,
                )
                continue
            await member.send(beat.text)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Community beat DM failed for guild %s.", beat.guild_id
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


@dataclass(frozen=True)
class ExpiredSponsorNotice:
    """A sponsored question nobody reviewed in time (for the post-commit DM)."""

    guild_id: int
    user_id: int
    question: str
    refund: int
    unit: str


@dataclass(frozen=True)
class ExpiredEmojiNotice:
    """One expired emoji sponsorship to DM after the sweep commits."""

    guild_id: int
    user_id: int
    name: str
    refund: int
    unit: str


@dataclass(frozen=True)
class ExpiredPinNotice:
    """A pending pin nobody reviewed in time (for the post-commit refund DM)."""

    guild_id: int
    user_id: int
    message: str
    refund: int
    unit: str


@dataclass(frozen=True)
class PinSweep:
    """A tick's pin work: pending refunds to DM, live pins to unpin from Discord."""

    refunds: list[ExpiredPinNotice]
    # (channel_id, message_id) of live pins past their 24h — unpinned after commit.
    unpins: list[tuple[int, int]]


def run_pin_expiry(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> PinSweep:
    """Retire live pins past 24h and refund pending pins nobody reviewed.

    Live pins are *not* refunded (their day ran) — they only need their Discord
    message unpinned after the transaction commits. Pending pins are refunded
    exactly once and DMed. A disabled economy skips both.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return PinSweep(refunds=[], unpins=[])
    unpins = [
        (int(row["pin_channel_id"]), int(row["pin_message_id"]))
        for row in pin_svc.expire_live_pins(conn, guild_id, now=now_ts)
    ]
    refunds = [
        ExpiredPinNotice(
            guild_id=guild_id,
            user_id=int(row["user_id"]),
            message=str(row["message"]),
            refund=int(row["price"]),
            unit=settings.currency_plural or "coins",
        )
        for row in pin_svc.expire_stale_pending(conn, settings, guild_id, now=now_ts)
    ]
    return PinSweep(refunds=refunds, unpins=unpins)


@dataclass(frozen=True)
class ExpiredBountyNotice:
    """An expired bounty for the post-commit card refresh + contributor DMs."""

    guild_id: int
    bounty_id: int
    title: str
    card_channel_id: int
    card_message_id: int
    refunded_user_ids: list[int]


def run_bounty_expiry(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> list[ExpiredBountyNotice]:
    """Expire and refund open bounties nobody awarded within the window.

    Refunds every contributor exactly once here; the caller re-renders each card
    and DMs the refunded members after the transaction commits.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return []
    return [
        ExpiredBountyNotice(
            guild_id=guild_id,
            bounty_id=int(exp.bounty["id"]),
            title=str(exp.bounty["title"]),
            card_channel_id=int(exp.bounty["card_channel_id"]),
            card_message_id=int(exp.bounty["card_message_id"]),
            refunded_user_ids=exp.refunded_user_ids,
        )
        for exp in bounty_svc.expire_bounties(conn, settings, guild_id, now=now_ts)
    ]


def run_emoji_expiry(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> list[ExpiredEmojiNotice]:
    """Expire and refund pending emoji sponsorships staff never got to.

    Mirrors :func:`run_sponsor_expiry` — pending only; approved rows are
    mid-upload (or a limbo a human should look at) and live rows are running
    rentals.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return []
    return [
        ExpiredEmojiNotice(
            guild_id=guild_id,
            user_id=int(row["user_id"]),
            name=str(row["name"]),
            refund=int(row["price"]),
            unit=settings.currency_plural or "coins",
        )
        for row in emoji_svc.expire_stale_submissions(
            conn, now_ts, expire_days=int(settings.emoji_sponsor_expire_days)
        )
        if int(row["guild_id"]) == guild_id
    ]


def run_sponsor_expiry(
    conn: sqlite3.Connection, guild_id: int, now_ts: float
) -> list[ExpiredSponsorNotice]:
    """Expire and refund pending sponsored questions staff never got to.

    Guild-scoped (unlike the claim sweep) because the timeout is a per-guild
    setting. ``expire_stale_submissions`` refunds exactly once and only touches
    'pending' — an *approved* question is waiting on a mod to run `/qotd post`,
    and timing that out would punish the member for staff latency.
    """
    settings = load_econ_settings(conn, guild_id)
    if not settings.enabled:
        return []
    return [
        ExpiredSponsorNotice(
            guild_id=guild_id,
            user_id=int(row["user_id"]),
            question=str(row["question"]),
            refund=int(row["price"]),
            unit=settings.currency_plural or "coins",
        )
        for row in expire_stale_submissions(conn, settings, guild_id, now=now_ts)
    ]


# ── rental billing pass ────────────────────────────────────────────────


@dataclass(frozen=True)
class SuspensionNotice:
    """A rental whose feature gate flipped this tick (for the post-commit DM).

    ``suspended`` is the NEW state: True when a required guild feature vanished
    (billing frozen, DM the owner), False when it returned (billing resumed,
    DM the owner AND re-project the beneficiary's role).
    """

    user_id: int
    beneficiary_id: int
    perk: str
    suspended: bool


@dataclass
class RentalTickOutcome:
    """Everything the sync billing body produced, for post-commit effects."""

    suspensions: list[SuspensionNotice] = field(default_factory=list)
    billing: list[BillingResult] = field(default_factory=list)


def run_guild_rental_billing(
    conn: sqlite3.Connection,
    settings: EconSettings,
    guild_id: int,
    gate_ok: dict[str, bool],
    now_ts: float,
) -> RentalTickOutcome:
    """Suspension sweep + billing for one guild, in the caller's transaction.

    ``gate_ok`` maps each feature-gated perk that has a live rental to whether
    the guild currently supports it (computed by the async caller). The sweep
    suspends a rental whose feature vanished and resumes one whose feature
    returned — recording only the *transitions* for the post-commit DMs — and
    always runs BEFORE billing so a rental suspended this tick returns ``none``
    from :func:`bill_rental` (its clock is frozen) rather than being charged.
    Rows are re-read after the sweep because ``set_rental_suspended`` mutates
    them. This body writes no Discord side effects; it only reports them.
    """
    outcome = RentalTickOutcome()

    for row in list_rentals(conn, guild_id, states=("active", "grace")):
        perk = str(row["perk"])
        if perk not in gate_ok:
            continue
        desired_suspended = not gate_ok[perk]
        if desired_suspended == bool(row["suspended"]):
            continue  # no transition — DM/re-project only on the edge
        set_rental_suspended(conn, int(row["id"]), desired_suspended, now=now_ts)
        outcome.suspensions.append(
            SuspensionNotice(
                user_id=int(row["user_id"]),
                beneficiary_id=int(row["beneficiary_id"]),
                perk=perk,
                suspended=desired_suspended,
            )
        )

    # Re-read: the sweep may have flipped ``suspended``/``next_bill_at`` above.
    for row in list_rentals(conn, guild_id, states=("active", "grace")):
        outcome.billing.append(bill_rental(conn, settings, row, now_ts))

    return outcome


async def _gather_feature_gates(
    bot: discord.Client, guild_id: int, live: list[sqlite3.Row]
) -> dict[str, bool]:
    """Ask Discord whether each feature-gated perk with a live rental is usable.

    Only queries a perk's gate when a live rental of that perk exists — the gate
    check can be a real Discord call (attempt-and-catch for gradient roles), so
    it is never paid when there is nothing to gate.
    """
    gate_ok: dict[str, bool] = {}
    for perk in _FEATURE_GATED_PERKS:
        if any(str(r["perk"]) == perk for r in live):
            gate_ok[perk] = await feature_gate_ok(bot, guild_id, perk)
    return gate_ok


async def run_guild_rentals(
    bot: discord.Client, db_path: Path, guild_id: int, now_ts: float
) -> None:
    """One guild's rental pass: feature gates → sweep+bill → post-commit effects.

    A disabled guild is left completely untouched. The billing transaction
    commits before any Discord effect runs, and each effect is fail-safe so an
    outage cannot corrupt billing state.
    """
    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
        if not settings.enabled:
            return
        live = list_rentals(conn, guild_id, states=("active", "grace"))

    if not live:
        return

    gate_ok = await _gather_feature_gates(bot, guild_id, live)

    with open_db(db_path) as conn:
        settings = load_econ_settings(conn, guild_id)
        outcome = run_guild_rental_billing(conn, settings, guild_id, gate_ok, now_ts)

    await _dispatch_rental_effects(bot, db_path, guild_id, outcome)


async def _safe_dm(
    bot: discord.Client, db_path: Path, guild_id: int, user_id: int, content: str
) -> None:
    """DM a member post-commit, isolating Discord failures from billing state."""
    try:
        await notify_member(bot, db_path, guild_id, user_id, content=content)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Economy loop: failed to DM rental notice to user %s.", user_id)


async def revert_voice_style(
    bot: discord.Client, db_path: Path, guild_id: int, member_id: int
) -> None:
    """Best-effort de-style of a live temp channel when the lease ends.

    The saved profile stays stored (dormant); only the LIVE channel is
    walked back to the template name and default limit. Failures are
    swallowed — the entitlement is already gone, so the next spawn is
    clean either way. The rename burns one of Discord's 2-per-10-minutes
    channel-rename slots; acceptable for a lapse.
    """
    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    def _load():
        with open_db(db_path) as conn:
            return (
                get_owned_channel(conn, guild_id, member_id),
                load_voice_master_config(conn, guild_id),
            )

    row, cfg = await asyncio.to_thread(_load)
    if row is None:
        return
    channel = guild.get_channel(int(row.channel_id))
    if not isinstance(channel, discord.VoiceChannel):
        return
    member = guild.get_member(member_id)
    name, _fell_back = resolve_channel_name(
        saved_name=None,
        template=cfg.default_name_template or DEFAULT_NAME_TEMPLATE,
        display_name=member.display_name if member else "member",
        username=member.name if member else "member",
        blocklist_patterns=[],
    )
    try:
        await channel.edit(
            name=name,
            user_limit=cfg.default_user_limit,
            reason="Economy: voice-style lease ended",
        )
    except (discord.Forbidden, discord.HTTPException):
        log.warning(
            "Economy loop: voice-style revert failed for channel %s.", channel.id,
        )


async def _delete_sponsored_emoji(
    bot: discord.Client, db_path: Path, guild_id: int, rental_id: int
) -> None:
    """Take the emoji down when its rental ends (lapse or cancel).

    State first: the live submission row is closed in its own transaction
    (freeing the member's slot and the name claim) before the Discord
    delete, so a crash can't leave the ledger thinking the emoji is paid
    for. A delete failure just logs — the emoji lingers until a mod
    removes it by hand, but nobody is billed for it.
    """

    def _close():
        with open_db(db_path) as conn:
            return emoji_svc.mark_lapsed(conn, rental_id)

    row = await asyncio.to_thread(_close)
    if row is None or row["emoji_id"] is None:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    emoji = guild.get_emoji(int(row["emoji_id"]))
    if emoji is None:
        return
    try:
        await emoji.delete(reason="Economy: emoji sponsorship ended")
    except (discord.Forbidden, discord.HTTPException):
        log.warning(
            "Economy loop: failed to delete lapsed sponsored emoji %s.",
            row["emoji_id"],
        )


async def revoke_perk_effect(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    perk: str,
    rental_id: int,
    beneficiary_id: int,
) -> None:
    """Strip a perk's Discord-side effect when its rental ends.

    Dispatches per perk kind: ``voice_style`` walks back the live temp
    channel (no personal role involved), ``emoji`` deletes the sponsored
    emoji, everything else re-projects/deletes the personal role via
    ``revoke_role_perks``. Shared by the billing loop (lapse / period-end
    cancel) and the member self-service refund flow, which must strip the
    perk immediately rather than waiting for period end.
    """
    if perk == "voice_style":
        await revert_voice_style(bot, db_path, guild_id, beneficiary_id)
    elif perk == "emoji":
        await _delete_sponsored_emoji(bot, db_path, guild_id, rental_id)
    else:
        await revoke_role_perks(bot, db_path, guild_id, beneficiary_id)


async def _dispatch_rental_effects(
    bot: discord.Client, db_path: Path, guild_id: int, outcome: RentalTickOutcome
) -> None:
    """Run a rental tick's post-commit Discord effects (DMs, revoke, re-project).

    Suspension transitions DM the owner (and re-project the beneficiary on
    resume). Billing outcomes: ``enter_grace`` DMs the owner once (subsequent
    grace ticks report ``retry`` — silent); ``revoke`` revokes the beneficiary's
    perk, DMs the owner, and courtesy-DMs the beneficiary of a lapsed *gift*;
    ``cancel_period_end`` revokes the beneficiary silently (member-initiated);
    ``charge`` (renewal or grace-recovery) and ``retry`` are silent with NO
    re-projection — grace never revoked the perk, so nothing needs rebuilding.
    """
    for notice in outcome.suspensions:
        if notice.suspended:
            await _safe_dm(
                bot, db_path, guild_id, notice.user_id,
                "Your perk is paused — the server lost the feature it needs, so "
                "billing is paused too. It resumes automatically when the "
                "feature returns.",
            )
        else:
            await _safe_dm(
                bot, db_path, guild_id, notice.user_id,
                "Your perk resumed — the server has the feature again and "
                "billing has restarted.",
            )
            try:
                await apply_role_perks(bot, db_path, guild_id, notice.beneficiary_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to re-project resumed perk for user %s.",
                    notice.beneficiary_id,
                )

    for res in outcome.billing:
        if res.action == BillingAction.ENTER_GRACE.value:
            await _safe_dm(
                bot, db_path, guild_id, res.user_id,
                f"Payment for your **{res.perk}** perk failed — you have "
                f"{_GRACE_HOURS}h of grace. I'll retry hourly; add funds to keep "
                "the perk.",
            )
        elif res.action in (
            BillingAction.REVOKE.value,
            BillingAction.CANCEL_PERIOD_END.value,
        ):
            try:
                await revoke_perk_effect(
                    bot, db_path, guild_id, res.perk, res.rental_id,
                    res.beneficiary_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to revoke perk for beneficiary %s.",
                    res.beneficiary_id,
                )
            if res.action == BillingAction.REVOKE.value:
                await _safe_dm(
                    bot, db_path, guild_id, res.user_id,
                    "Your perk lapsed — re-rent anytime from `/bank shop`.",
                )
                if res.beneficiary_id != res.user_id:
                    await _safe_dm(
                        bot, db_path, guild_id, res.beneficiary_id,
                        "A perk gifted to you has lapsed.",
                    )
        # charge / retry / none → silent (no DM, no re-projection).


async def run_guild_leaderboard(
    bot: discord.Client, db_path: Path, guild_id: int, now_ts: float
) -> None:
    """Hourly in-place refresh of the ``/bank post-leaderboard`` panel.

    Skips guilds without a posted panel (or with the economy off). A deleted
    panel message (404) clears the stored ids so the loop stops retrying —
    deleting the message is how staff retire the panel; any other Discord
    error leaves the ids for the next tick.
    """

    def _load():
        with open_db(db_path) as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled or not settings.leaderboard_message_id:
                return settings, None, {}
            data = collect_leaderboard_data(conn, guild_id, now_ts)
            known = get_known_users_bulk(
                conn, guild_id, [uid for uid, _ in data.top_earners]
            )
            return settings, data, known

    settings, data, known = await asyncio.to_thread(_load)
    if data is None:
        return
    guild = bot.get_guild(guild_id)
    if guild is None:
        return
    channel = guild.get_channel(settings.leaderboard_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    def _name(uid: int) -> str:
        member = guild.get_member(uid)
        if member:
            return member.display_name
        return known.get(uid) or f"User {uid}"

    accent = await resolve_accent_color(db_path, guild)
    embed = build_leaderboard_embed(
        settings, data, _name, now_ts=now_ts, color=accent
    )
    try:
        message = await channel.fetch_message(settings.leaderboard_message_id)
        await message.edit(embed=embed, view=QuestBoardView())
    except discord.NotFound:
        # The bottom-sticky repost deletes the old panel and posts a new one, so
        # a 404 here may just mean it moved. Re-read the id: if it changed, the
        # panel is alive under a new message — don't retire it.
        def _current_id() -> int:
            with open_db(db_path) as conn:
                return load_econ_settings(conn, guild_id).leaderboard_message_id

        if await asyncio.to_thread(_current_id) != settings.leaderboard_message_id:
            return

        def _clear() -> None:
            with open_db(db_path) as conn:
                save_econ_settings(
                    conn,
                    guild_id,
                    {"leaderboard_channel_id": 0, "leaderboard_message_id": 0},
                )

        await asyncio.to_thread(_clear)
        log.info(
            "Economy loop: leaderboard panel for guild %s is gone — "
            "cleared its ids.",
            guild_id,
        )
    except discord.HTTPException:
        log.warning(
            "Economy loop: leaderboard refresh failed for guild %s.", guild_id
        )


async def run_guild_register(
    bot: discord.Client, db_path: Path, guild_id: int, now_ts: float
) -> int:
    """Drain new ledger rows to the guild's register channel. Returns rows posted.

    Skips guilds with the economy off or no register channel (the picker is the
    toggle). A cursor of -1 is a first enable: it seeds to the ledger's current
    MAX(id) and posts nothing, so switching the feed on never replays history.
    Rows older than ``REGISTER_STALE_SECONDS`` are skipped (cursor still
    advances) — after long downtime, or a channel re-enabled weeks later, a
    stale backlog is noise, and draining it would burn the channel's rate limit.

    The cursor advances only over rows we actually posted, and only after the
    sends land, so a crash mid-drain replays the un-posted tail rather than
    losing it (at worst a duplicate entry, never a silent gap).
    """

    def _load():
        with open_db(db_path) as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled or not settings.register_channel_id:
                return settings, None
            if settings.register_cursor_id < 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) AS max_id FROM econ_ledger "
                    "WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                save_econ_settings(
                    conn, guild_id, {"register_cursor_id": int(row["max_id"])}
                )
                return settings, None
            entries = collect_register_entries(
                conn,
                guild_id,
                settings.register_cursor_id,
                REGISTER_MAX_PER_TICK,
            )
            known = get_known_users_bulk(
                conn, guild_id, _register_name_ids(entries)
            )
            return settings, (entries, known)

    settings, loaded = await asyncio.to_thread(_load)
    if not loaded:
        return 0
    entries, known = loaded
    if not entries:
        return 0

    guild = bot.get_guild(guild_id)
    if guild is None:
        return 0
    channel = guild.get_channel(settings.register_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return 0

    def _name(uid: int) -> str:
        member = guild.get_member(uid)
        if member:
            return member.display_name
        return known.get(uid) or f"User {uid}"

    cutoff = now_ts - REGISTER_STALE_SECONDS
    posted = 0
    highest = settings.register_cursor_id
    for entry in entries:
        if entry.created_at < cutoff:
            highest = entry.ledger_id  # skipped, but never re-examined
            continue
        member = guild.get_member(entry.user_id)
        embed = build_register_embed(
            entry,
            settings,
            _name,
            avatar_url=member.display_avatar.url if member else None,
        )
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning(
                "Economy register: no permission to post in guild %s — "
                "leaving the cursor for a retry.",
                guild_id,
            )
            break
        except discord.HTTPException:
            log.warning(
                "Economy register: send failed for guild %s ledger row %s.",
                guild_id,
                entry.ledger_id,
            )
            break
        highest = entry.ledger_id
        posted += 1

    if highest > settings.register_cursor_id:

        def _advance() -> None:
            with open_db(db_path) as conn:
                save_econ_settings(conn, guild_id, {"register_cursor_id": highest})

        await asyncio.to_thread(_advance)
    return posted


def _register_name_ids(entries: list[RegisterEntry]) -> list[int]:
    """Every user id a register batch's embeds might need a display name for."""
    ids: set[int] = set()
    for entry in entries:
        ids.add(entry.user_id)
        if entry.actor_id:
            ids.add(entry.actor_id)
        for key in ("to", "from"):
            try:
                counterparty = int(entry.meta.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if counterparty:
                ids.add(counterparty)
    return list(ids)


async def register_tick(bot: discord.Client, db_path: Path, now_ts: float) -> None:
    """One register drain across every guild, isolating per-guild failures."""
    for guild in list(bot.guilds):
        try:
            await run_guild_register(bot, db_path, guild.id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy register: drain failed for guild %s.", guild.id
            )


async def register_loop(bot: discord.Client, db_path: Path) -> None:
    """The transaction feed's own cadence — the hourly tick is far too slow.

    Separate from :func:`economy_loop` because a register entry is only useful
    while it is still news; the hourly tick would batch a day's activity into
    lumps an hour apart.
    """
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(REGISTER_INTERVAL_SECONDS)
        try:
            await register_tick(bot, db_path, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Economy register: tick failed.")


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
        beats: list[CommunityBeat] = []
        sponsor_notices: list[ExpiredSponsorNotice] = []
        week_rolled = False
        raffle_draw: raffle_svc.DrawResult | None = None
        try:
            with open_db(db_path) as conn:
                roll = run_guild_day_roll(bot, conn, guild.id, now_ts)
                beats.extend(roll.beats)
                week_rolled = roll.week_rolled
                raffle_draw = roll.raffle
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Economy loop: unhandled error for guild %s.", guild.id)

        # Separate transaction: a day-roll failure must not swallow refunds
        # members are owed, and vice versa. Both lists are (re)set before the
        # try so a failed sweep can't NameError below or leak the previous
        # guild's notices.
        sponsor_notices, emoji_notices = [], []
        pin_sweep = PinSweep(refunds=[], unpins=[])
        bounty_notices: list[ExpiredBountyNotice] = []
        try:
            with open_db(db_path) as conn:
                sponsor_notices = run_sponsor_expiry(conn, guild.id, now_ts)
                emoji_notices = run_emoji_expiry(conn, guild.id, now_ts)
                pin_sweep = run_pin_expiry(conn, guild.id, now_ts)
                bounty_notices = run_bounty_expiry(conn, guild.id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: sponsor-expiry sweep failed for guild %s.", guild.id
            )

        for notice in sponsor_notices:
            try:
                await notify_member(
                    bot,
                    db_path,
                    notice.guild_id,
                    notice.user_id,
                    content=(
                        f"Nobody got to your sponsored question in time, so "
                        f"you've had your {notice.refund} {notice.unit} back.\n"
                        f"> {notice.question}"
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to DM expired sponsor to user %s.",
                    notice.user_id,
                )

        for notice in emoji_notices:
            try:
                await notify_member(
                    bot,
                    db_path,
                    notice.guild_id,
                    notice.user_id,
                    content=(
                        f"Nobody got to your sponsored emoji :{notice.name}: "
                        f"in time, so you've had your {notice.refund} "
                        f"{notice.unit} back."
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to DM expired emoji sponsor to %s.",
                    notice.user_id,
                )

        # Pin of the Day: unpin the live cards whose 24h ran out (the rows are
        # already retired in DB), then DM anyone whose pending pin expired
        # unreviewed and was refunded.
        if pin_sweep.unpins:
            from bot_modules.economy.pin_views import unpin_and_delete

            for channel_id, message_id in pin_sweep.unpins:
                try:
                    await unpin_and_delete(bot, channel_id, message_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "Economy loop: failed to unpin expired pin %s.", message_id
                    )
        for notice in pin_sweep.refunds:
            try:
                await notify_member(
                    bot,
                    db_path,
                    notice.guild_id,
                    notice.user_id,
                    content=(
                        f"No mod got to your pinned message in time, so you've "
                        f"had your {notice.refund} {notice.unit} back.\n"
                        f"> {notice.message}"
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to DM expired pin to user %s.",
                    notice.user_id,
                )

        # Community bounties that expired unawarded: refresh each board card to
        # its "expired" state (rows already refunded in DB) and DM every
        # contributor whose stake came back.
        if bounty_notices:
            from bot_modules.economy.bounty_views import refresh_card_by_id

            for notice in bounty_notices:
                try:
                    await refresh_card_by_id(
                        bot, guild, notice.card_channel_id,
                        notice.card_message_id, notice.bounty_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "Economy loop: failed to refresh expired bounty card %s.",
                        notice.bounty_id,
                    )
                for uid in notice.refunded_user_ids:
                    try:
                        await notify_member(
                            bot, db_path, notice.guild_id, uid,
                            content=(
                                f"The bounty **{notice.title}** expired unawarded, "
                                "so your stake is back in your wallet."
                            ),
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception(
                            "Economy loop: failed to DM bounty refund to %s.", uid
                        )

        if raffle_draw is not None and raffle_draw.winner_id is not None:
            # The draw row is already committed — the DM is best-effort and
            # gated on the opt-in role like every recurring economy DM.
            try:
                await notify_member(
                    bot,
                    db_path,
                    guild.id,
                    raffle_draw.winner_id,
                    content=(
                        f"🎟️ You won the {raffle_draw.iso_week} raffle "
                        f"({raffle_draw.tickets} tickets in the draw)! Your "
                        "prize: the next weekly perk payment from your wallet "
                        "— a renewal or a brand-new rent — is free. It's "
                        "applied automatically and keeps for 28 days."
                    ),
                    require_game_role=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: failed to DM raffle winner %s.",
                    raffle_draw.winner_id,
                )

        if week_rolled:
            try:
                await _post_flip_announcement(bot, db_path, guild.id, now_ts)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Economy loop: flip announcement failed for guild %s.",
                    guild.id,
                )

        try:
            with open_db(db_path) as conn:
                beats.extend(community_hourly_beats(conn, guild.id, now_ts))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: community beat check failed for guild %s.",
                guild.id,
            )

        try:
            await _send_community_beats(bot, db_path, beats)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: community beat send failed for guild %s.",
                guild.id,
            )

        try:
            await run_guild_rentals(bot, db_path, guild.id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: rental pass failed for guild %s.", guild.id
            )

        try:
            await run_guild_leaderboard(bot, db_path, guild.id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Economy loop: leaderboard refresh failed for guild %s.", guild.id
            )


async def economy_loop(bot: discord.Client, db_path: Path) -> None:
    await bot.wait_until_ready()

    while not bot.is_closed():
        sleep_secs = _seconds_until_next_hour()
        await asyncio.sleep(sleep_secs)
        await run_tick(bot, db_path, time.time())


# ── live leaderboard refresh ───────────────────────────────────────────


# How often the live loop looks for dirty guilds, and the per-guild floor
# between panel edits. 120 s keeps a busy hour to ≤30 edits per guild —
# far inside Discord's edit limits — while a burst of quest activity still
# lands on the panel within a couple of minutes.
LIVE_POLL_SECONDS = 20.0
LIVE_MIN_INTERVAL = 120.0


async def run_live_tick(
    bot: discord.Client, db_path: Path, now_ts: float
) -> None:
    """Refresh the leaderboard panel of every guild whose debounce is up.

    Economy writes (:func:`economy_service.apply_credit`, community bumps,
    dashboard progress edits) mark their guild dirty in
    :mod:`bot_modules.economy.live_signal`; this consumes the marks.
    Guilds without a posted panel exit :func:`run_guild_leaderboard` after
    one settings read, and per-guild failures never stall the rest.
    """
    for guild_id in live_signal.take_ready(now_ts, LIVE_MIN_INTERVAL):
        try:
            await run_guild_leaderboard(bot, db_path, guild_id, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "Live leaderboard refresh failed for guild %s.", guild_id
            )


async def leaderboard_live_loop(bot: discord.Client, db_path: Path) -> None:
    """The near-real-time companion to the hourly tick (live leaderboard).

    Cheap when idle (an empty-set check every ``LIVE_POLL_SECONDS``); the
    hourly :func:`run_guild_leaderboard` pass remains the backstop for
    restarts, rollbacks, and marks lost in-process.
    """
    await bot.wait_until_ready()

    while not bot.is_closed():
        await asyncio.sleep(LIVE_POLL_SECONDS)
        await run_live_tick(bot, db_path, time.time())
