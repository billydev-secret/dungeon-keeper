"""Wellness Guardian background loops.

Three loops run as startup task factories:

1. wellness_tick_loop — every 60 seconds:
   - blackout transitions (post entry DM per spec §4.5)
   - lift expired slow mode
   - resume paused users whose paused_until ≤ now
   - credit clean-day streaks per user in their own timezone
   - nightly: GC old counter/overage rows, sweep opted-out users past retention.

2. wellness_weekly_report_loop — added in Phase F.

3. wellness_active_list_loop — hourly rebuild of #active-in-commitment pinned
   embed + milestone celebration posts.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
from datetime import timedelta
from pathlib import Path

import discord

from db_utils import open_db
from services.wellness_ai import generate_weekly_encouragement
from services.wellness_service import (
    MILESTONES,
    SETTINGS_RETENTION_SECONDS,
    WellnessBlackout,
    WellnessStreak,
    clear_blackout_active,
    compute_weekly_summary,
    gc_old_cap_data,
    gc_opted_out_users,
    get_wellness_config,
    has_clean_day_credit,
    has_weekly_report,
    increment_streak_day,
    insert_weekly_report,
    list_active_blackout_markers,
    list_active_users,
    list_blackouts,
    list_committed_users_with_streaks,
    list_expired_slow_mode,
    list_uncelebrated_milestones,
    lift_slow_mode,
    mark_badge_celebrated,
    mark_blackout_active,
    resume_user,
    upsert_wellness_config,
    user_now,
)

log = logging.getLogger("dungeonkeeper.wellness.scheduler")


async def _try_dm(user: discord.User | discord.Member, *, content: str | None = None,
                  embed: discord.Embed | None = None) -> bool:
    try:
        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed:
            kwargs["embed"] = embed
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def _format_minute(minute_of_day: int) -> str:
    h, m = divmod(minute_of_day, 60)
    return f"{h:02d}:{m:02d}"


async def _process_blackout_transitions(bot: discord.Client, db_path: Path) -> None:
    """Detect blackout entry/exit per active user and DM them on transitions."""
    for guild in bot.guilds:
        with open_db(db_path) as conn:
            users = list_active_users(conn, guild.id)
        for u in users:
            if u.is_paused:
                continue
            with open_db(db_path) as conn:
                blackouts = list_blackouts(conn, guild.id, u.user_id)
                active_marker_ids = set(list_active_blackout_markers(conn, guild.id, u.user_id))
            now_local = user_now(u.timezone)
            currently_active_ids: set[int] = set()
            newly_active: list[WellnessBlackout] = []
            for b in blackouts:
                if b.is_active_at(now_local):
                    currently_active_ids.add(b.id)
                    if b.id not in active_marker_ids:
                        newly_active.append(b)
            ended_ids = active_marker_ids - currently_active_ids

            with open_db(db_path) as conn:
                for b in newly_active:
                    if mark_blackout_active(conn, guild.id, u.user_id, b.id):
                        # New entry — DM user
                        member = guild.get_member(u.user_id)
                        if member:
                            asyncio.create_task(_send_blackout_entry_dm(member, b))
                for bid in ended_ids:
                    clear_blackout_active(conn, guild.id, u.user_id, bid)


async def _send_blackout_entry_dm(member: discord.Member, blackout: WellnessBlackout) -> None:
    embed = discord.Embed(
        title=f"🌙 {blackout.name} blackout started",
        description=(
            f"Slow mode is active until **{_format_minute(blackout.end_minute)}**.\n\n"
            "Sleep well! 💚"
        ),
        color=discord.Color.from_str("#7BC97B"),
    )
    await _try_dm(member, embed=embed)


async def _lift_expired_slow_mode(bot: discord.Client, db_path: Path) -> None:
    now = time.time()
    with open_db(db_path) as conn:
        expired = list_expired_slow_mode(conn, now)
        for state in expired:
            lift_slow_mode(conn, state.guild_id, state.user_id)


async def _resume_expired_pauses(bot: discord.Client, db_path: Path) -> None:
    """Auto-resume users whose paused_until has passed."""
    now = time.time()
    with open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT guild_id, user_id FROM wellness_users WHERE paused_until IS NOT NULL AND paused_until <= ?",
            (now,),
        ).fetchall()
        for r in rows:
            resume_user(conn, int(r["guild_id"]), int(r["user_id"]))


async def _nightly_maintenance(bot: discord.Client, db_path: Path) -> None:
    """Heavy maintenance: GC counter rows and sweep retired users."""
    with open_db(db_path) as conn:
        gc_old_cap_data(conn)
        gc_opted_out_users(conn, SETTINGS_RETENTION_SECONDS)


async def _credit_clean_days(bot: discord.Client, db_path: Path) -> None:
    """Per-tick streak crediting. Runs every 60s and credits each user once per
    day in their own timezone — zeroes in on the minute their `daily_reset_hour`
    rolls over. Dedup via wellness_streak_history's PRIMARY KEY."""
    for guild in bot.guilds:
        with open_db(db_path) as conn:
            users = list_active_users(conn, guild.id)
        for u in users:
            if u.is_paused:
                continue
            now_local = user_now(u.timezone)
            if now_local.hour != u.daily_reset_hour:
                continue
            today_iso = now_local.date().isoformat()
            with open_db(db_path) as conn:
                if has_clean_day_credit(conn, guild.id, u.user_id, today_iso):
                    continue
                row = conn.execute(
                    "SELECT last_violation_date FROM wellness_streaks WHERE guild_id = ? AND user_id = ?",
                    (guild.id, u.user_id),
                ).fetchone()
                if row and row["last_violation_date"] == today_iso:
                    continue
                increment_streak_day(conn, guild.id, u.user_id, today_iso)


# ---------------------------------------------------------------------------
# Public loop
# ---------------------------------------------------------------------------

async def wellness_tick_loop(bot: discord.Client, db_path: Path) -> None:
    """Background task — runs every 60 seconds."""
    await bot.wait_until_ready()
    last_nightly_day: str | None = None
    while not bot.is_closed():
        try:
            await _process_blackout_transitions(bot, db_path)
            await _lift_expired_slow_mode(bot, db_path)
            await _resume_expired_pauses(bot, db_path)
            await _credit_clean_days(bot, db_path)

            # Nightly maintenance — fire once per UTC day at minute 5
            now_utc = user_now("UTC")
            today = now_utc.date().isoformat()
            if last_nightly_day != today and now_utc.hour == 0 and now_utc.minute >= 5:
                await _nightly_maintenance(bot, db_path)
                last_nightly_day = today
        except Exception:
            log.exception("wellness_tick_loop error")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Active-in-Commitment loop (spec §5)
# ---------------------------------------------------------------------------

_ACTIVE_EMBED_COLOR = discord.Color.from_str("#7BC97B")
_ACTIVE_EMBED_TITLE = "💚 Active in Commitment"
_ACTIVE_MAX_ENTRIES = 25  # Discord embed description stays comfortable


def _build_active_embed(
    guild: discord.Guild, entries: list[tuple[int, WellnessStreak]],
) -> discord.Embed:
    if not entries:
        embed = discord.Embed(
            title=_ACTIVE_EMBED_TITLE,
            description=(
                "No one has opted in with public commitment yet. "
                "Turn it on in `/wellness settings` to show up here. 🌱"
            ),
            color=_ACTIVE_EMBED_COLOR,
        )
        embed.set_footer(text="Wellness Guardian — updated hourly")
        return embed

    lines: list[str] = []
    for user_id, streak in entries[:_ACTIVE_MAX_ENTRIES]:
        member = guild.get_member(user_id)
        name = member.display_name if member else f"User {user_id}"
        badge = streak.current_badge or "🌱"
        lines.append(f"{badge} **{name}** — {streak.current_days} day{'s' if streak.current_days != 1 else ''}")

    remainder = max(0, len(entries) - _ACTIVE_MAX_ENTRIES)
    desc = "\n".join(lines)
    if remainder:
        desc += f"\n\n*…and {remainder} more*"

    embed = discord.Embed(
        title=_ACTIVE_EMBED_TITLE,
        description=desc,
        color=_ACTIVE_EMBED_COLOR,
    )
    embed.set_footer(text="Wellness Guardian — updated hourly • cheering you on 💚")
    return embed


async def _rebuild_active_list_for_guild(bot: discord.Client, db_path: Path, guild: discord.Guild) -> None:
    with open_db(db_path) as conn:
        cfg = get_wellness_config(conn, guild.id)
        if cfg is None or not cfg.active_channel_id:
            return
        entries = list_committed_users_with_streaks(conn, guild.id)

    channel = guild.get_channel(cfg.active_channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None:
        return
    perms = channel.permissions_for(me)
    if not perms.send_messages:
        return

    embed = _build_active_embed(guild, entries)

    # Try to edit existing pinned embed, else post a new one and pin it
    message: discord.Message | None = None
    if cfg.active_list_message_id:
        try:
            message = await channel.fetch_message(cfg.active_list_message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is not None:
        try:
            await message.edit(embed=embed)
            return
        except (discord.Forbidden, discord.HTTPException):
            message = None  # fall through to re-post

    try:
        new_message = await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.warning("wellness: failed to post active list in guild %s", guild.id)
        return

    # Pin the new message if we can
    if perms.manage_messages:
        try:
            await new_message.pin(reason="Wellness Guardian active-in-commitment list")
        except (discord.Forbidden, discord.HTTPException):
            pass

    with open_db(db_path) as conn:
        upsert_wellness_config(conn, guild.id, active_list_message_id=new_message.id)


async def _post_milestone_celebrations(bot: discord.Client, db_path: Path, guild: discord.Guild) -> None:
    """Post celebration messages for users whose badge upgraded since last check."""
    with open_db(db_path) as conn:
        cfg = get_wellness_config(conn, guild.id)
        if cfg is None or not cfg.active_channel_id:
            return
        pending = list_uncelebrated_milestones(conn, guild.id)

    channel = guild.get_channel(cfg.active_channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return
    me = guild.me
    if me is None:
        return
    perms = channel.permissions_for(me)
    if not perms.send_messages:
        return

    for user_id, streak in pending:
        new_badge = streak.current_badge
        # Seed badge (🌱) on first creation is not a celebration — skip if seed and no days yet
        if not new_badge or (new_badge == "🌱" and streak.current_days == 0):
            with open_db(db_path) as conn:
                mark_badge_celebrated(conn, guild.id, user_id, new_badge or "")
            continue

        # Ignore downgrades (decay dropping badge tier) — only celebrate upgrades
        if streak.celebrated_badge and _badge_rank(new_badge) <= _badge_rank(streak.celebrated_badge):
            with open_db(db_path) as conn:
                mark_badge_celebrated(conn, guild.id, user_id, new_badge)
            continue

        member = guild.get_member(user_id)
        display = member.mention if member else f"<@{user_id}>"
        embed = discord.Embed(
            title=f"{new_badge} Milestone reached!",
            description=(
                f"{display} just hit a **{streak.current_days}-day streak** — "
                f"that's {new_badge} territory! 💚\n\n"
                "Keep showing up for yourself. We're cheering you on."
            ),
            color=_ACTIVE_EMBED_COLOR,
        )
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(
                users=[member] if member else False, roles=False, everyone=False,
            ))
        except (discord.Forbidden, discord.HTTPException):
            log.debug("wellness: failed to post milestone celebration in guild %s", guild.id)
            continue

        with open_db(db_path) as conn:
            mark_badge_celebrated(conn, guild.id, user_id, new_badge)


def _badge_rank(badge: str) -> int:
    """Return the MILESTONES index for a badge, or -1 if unknown."""
    for i, (_threshold, emoji) in enumerate(MILESTONES):
        if emoji == badge:
            return i
    return -1


async def wellness_active_list_loop(bot: discord.Client, db_path: Path) -> None:
    """Hourly rebuild of the active list + milestone celebrations."""
    await bot.wait_until_ready()
    # Stagger the first run so we don't spam on boot; wait ~30s for caches to warm up
    await asyncio.sleep(30)
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                try:
                    await _post_milestone_celebrations(bot, db_path, guild)
                    await _rebuild_active_list_for_guild(bot, db_path, guild)
                except Exception:
                    log.exception("wellness_active_list_loop: guild %s error", guild.id)
        except Exception:
            log.exception("wellness_active_list_loop top-level error")
        await asyncio.sleep(3600)  # hourly


# ---------------------------------------------------------------------------
# Weekly AI report loop (spec §6)
# ---------------------------------------------------------------------------

_WEEKLY_REPORT_COLOR = discord.Color.from_str("#7BC97B")


def _build_weekly_report_embed(
    user_display: str, summary: dict, ai_text: str,
) -> discord.Embed:
    badge = summary.get("badge") or "🌱"
    week_start = summary.get("week_start", "")
    week_end = summary.get("week_end", "")
    clean = summary.get("clean_days", 0)
    pct = summary.get("compliance_pct", 0)
    cur = summary.get("current_days", 0)
    pb = summary.get("personal_best", 0)
    is_pb = summary.get("is_personal_best", False)

    body = (
        f"**Week of {week_start} → {week_end}**\n\n"
        f"{badge} Current streak: **{cur} days**" + (" *(personal best!)*" if is_pb else "") + "\n"
        f"💚 Personal best: **{pb} days**\n"
        f"🌿 Clean days this week: **{clean}/7** ({pct}%)\n\n"
        f"{ai_text}"
    )
    embed = discord.Embed(
        title=f"💚 Your wellness summary, {user_display}",
        description=body,
        color=_WEEKLY_REPORT_COLOR,
    )
    embed.set_footer(text="Sent on Sunday mornings • turn off in /wellness settings")
    return embed


def _iso_week_for(now_local) -> tuple[int, int, str]:
    """Return (iso_year, iso_week, week_start_iso) for the user's now."""
    iso_year, iso_week, _iso_weekday = now_local.isocalendar()
    # Compute Monday of this ISO week
    week_start = (now_local.date() - timedelta(days=now_local.weekday()))
    return iso_year, iso_week, week_start.isoformat()


async def _generate_and_send_weekly_report(
    bot: discord.Client, db_path: Path, guild: discord.Guild, user,
) -> bool:
    """Compute, generate, DM, and archive a single user's weekly report.

    Returns True if a report was actually sent (or attempted), False if skipped.
    """
    now_local = user_now(user.timezone)
    # Sunday morning gate: weekday 6 (Mon=0..Sun=6) AND hour ≥ 9
    if now_local.weekday() != 6 or now_local.hour < 9:
        return False

    iso_year, iso_week, _ = _iso_week_for(now_local)

    with open_db(db_path) as conn:
        if has_weekly_report(conn, guild.id, user.user_id, iso_year, iso_week):
            return False
        # Use the START of the user's current ISO week (Monday) as the window
        week_start_date = now_local.date() - timedelta(days=now_local.weekday())
        summary = compute_weekly_summary(conn, guild.id, user.user_id, week_start_date)

    # AI encouragement (will fall back if no API key configured)
    ai_text = await generate_weekly_encouragement(
        streak_days=int(summary["current_days"]),
        is_personal_best=bool(summary["is_personal_best"]),
        compliance_pct=int(summary["compliance_pct"]),
    )

    member = guild.get_member(user.user_id)
    if member is None:
        return False
    embed = _build_weekly_report_embed(member.display_name, summary, ai_text)

    # DM with reservation: insert FIRST so a parallel run can't dup, then DM
    with open_db(db_path) as conn:
        inserted = insert_weekly_report(
            conn,
            guild.id,
            user.user_id,
            iso_year=iso_year,
            iso_week=iso_week,
            week_start=summary["week_start"],
            report_json=_json.dumps(summary),
            ai_text=ai_text,
        )
    if not inserted:
        return False  # raced with another tick — skip silently

    try:
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        log.info("wellness_weekly_report: DM closed for user %s", user.user_id)

    return True


async def wellness_weekly_report_loop(bot: discord.Client, db_path: Path) -> None:
    """Background task — every 5 minutes, look for users who should receive
    their weekly report (Sunday morning local, hour ≥ 9, not yet archived)."""
    await bot.wait_until_ready()
    # Stagger initial run by a minute so the bot is fully settled
    await asyncio.sleep(60)
    while not bot.is_closed():
        try:
            for guild in bot.guilds:
                with open_db(db_path) as conn:
                    users = list_active_users(conn, guild.id)
                for u in users:
                    if u.is_paused:
                        continue
                    try:
                        await _generate_and_send_weekly_report(bot, db_path, guild, u)
                    except Exception:
                        log.exception(
                            "wellness_weekly_report_loop: failed for guild=%s user=%s",
                            guild.id, u.user_id,
                        )
        except Exception:
            log.exception("wellness_weekly_report_loop top-level error")
        await asyncio.sleep(300)  # 5 minutes
