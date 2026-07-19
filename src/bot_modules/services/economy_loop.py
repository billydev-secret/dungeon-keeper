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

The tick also refreshes each guild's **leaderboard panel** in place
(:func:`run_guild_leaderboard` — the ``/bank post-leaderboard`` embed; a 404
on the stored message clears its ids so a deleted panel stops the refresh).

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
from bot_modules.economy import logic, quests
from bot_modules.economy.leaderboard import (
    build_leaderboard_embed,
    collect_leaderboard_data,
)
from bot_modules.economy.perk_actions import (
    apply_role_perks,
    feature_gate_ok,
    revoke_role_perks,
)
from bot_modules.economy.rentals import GRACE_SECONDS, BillingAction
from bot_modules.services.economy_quests_service import (
    activate_community_weekly,
    active_member_ids,
    auto_size_community_target,
    community_contrib_summary,
    expire_stale_claims,
    get_quest,
    list_active_community_kind_quests,
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

# The two perks whose billing is gated on a guild feature (role icon / gradient
# role colors). Only these are swept each tick — the sweep asks Discord whether
# the feature still exists, so it is kept to the perks that can actually lose it.
_FEATURE_GATED_PERKS = ("role_icon", "role_gradient")

# Grace-window length in whole hours, for the "payment failed" DM copy.
_GRACE_HOURS = int(GRACE_SECONDS // 3600)

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
) -> list[CommunityBeat]:
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
        return []

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
        return []

    last_day = row["last_local_day"]
    if last_day == today:
        return []
    beats: list[CommunityBeat] = []

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
    prune_kind_activity(conn, guild_id, today)

    # ── week roll: advance weekly pool + settle community quests ──
    # ``last_iso_week`` is NULL for pre-064 mark rows; treat that as a backfill
    # (record the week, don't settle) rather than a spurious week change.
    last_week = row["last_iso_week"]
    community_week = row["last_community_week"]
    if last_week is not None and last_week != this_week:
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
        # Roll up metrics for the week that JUST closed (idempotent via PK —
        # a replay before the marks advance recomputes nothing).
        compute_weekly_rollup(
            conn, settings, guild_id, last_week, offset_hours=offset, now=now_ts
        )

    # Marks advance LAST (both columns together) so any crash above replays the
    # whole roll on the next tick.
    conn.execute(
        "UPDATE econ_day_marks SET last_local_day = ?, last_iso_week = ?, "
        "last_community_week = ? WHERE guild_id = ?",
        (today, this_week, community_week, guild_id),
    )
    return beats


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
    """
    beats: list[CommunityBeat] = []
    active = list_active_community_kind_quests(conn, guild_id)
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
    target = auto_size_community_target(conn, guild_id, kind, local_day)
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
                await revoke_role_perks(bot, db_path, guild_id, res.beneficiary_id)
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
                        "The custom color gifted to you has lapsed.",
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
        await message.edit(embed=embed)
    except discord.NotFound:

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
        try:
            with open_db(db_path) as conn:
                beats.extend(run_guild_day_roll(bot, conn, guild.id, now_ts))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Economy loop: unhandled error for guild %s.", guild.id)

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
