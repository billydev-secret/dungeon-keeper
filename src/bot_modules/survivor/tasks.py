"""Survivor weekly tasks — slate, last call, THE RECKONING (spec §2.3, §2.5).

The cadence, guild-local via ``tz_offset_hours`` (§6.6, the house pattern):
Wednesday ``slate_hour`` the slate posts; Saturday ``lastcall_hour`` the
pickless get their DM; Tuesday ``reckoning_hour`` the meadow gathers. Each
task fires **at or after** its hour and catches up if the bot slept through
it — per-week state in the season config guarantees exactly-once per week.
The roles get pinged exactly twice a week (slate + Reckoning); restraint is
the brand.

Pure due-decisions up top (tested directly); async posting glue below,
called from the poll loop's tick.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours, open_db
from bot_modules.services.survivor_service import update_config
from bot_modules.survivor import logic, reckoning
from bot_modules.survivor.views import SlatePickButton, swap_member_roles

log = logging.getLogger("dungeonkeeper.survivor")

WEDNESDAY, SATURDAY, TUESDAY = 2, 5, 1

CONDOLENCE = (
    "Your run ended in Week {week}. 🪦\n"
    "-# You can keep playing: Ghost Streak is live — the longest win streak "
    "after elimination takes the side pot. `/survivor pick` still works. 👻"
)
LAST_CALL = (
    "You haven't picked for Week {week}. Make your pick below — or I'll "
    "pick for you, and I have terrible taste. 🌙"
)


def local_parts(now: float, offset_hours: float) -> tuple[int, int]:
    """(weekday 0=Mon, hour) in the guild's local clock."""
    local = datetime.fromtimestamp(
        now, timezone(timedelta(hours=offset_hours))
    )
    return local.weekday(), local.hour


def past_weekly_moment(
    now: float, offset_hours: float, target_dow: int, target_hour: int
) -> bool:
    """At or after (target_dow, target_hour) in the week's own frame.

    The frame anchors on Tuesday (the week's picks open after Monday Night
    Football), so a bot asleep through the moment catches up later in the
    week — and **Monday (frame 6) is excluded entirely**: the next week
    becomes "current" the moment MNF kicks on Monday night, and without the
    exclusion every task would fire immediately at frame 6 instead of
    waiting for its own day (the Reckoning firing mid-MNF was the bug the
    tests caught). A task missed all the way to Monday just waits a day.
    """
    dow, hour = local_parts(now, offset_hours)
    frame = (dow - TUESDAY) % 7          # Tue=0 … Mon=6
    target = (target_dow - TUESDAY) % 7
    if frame < target or frame > 5:
        return False
    return frame > target or hour >= target_hour


def slate_due(
    conn: sqlite3.Connection, season: dict, now: float, offset: float,
    *, force: bool = False,
) -> int | None:
    week = logic.pick_week(conn, season["season_year"], now)
    if week is None or int(season["config"].get("last_slate_week") or 0) >= week:
        return None
    hour = int(season["config"]["slate_hour"])
    if force:
        return week
    return week if past_weekly_moment(now, offset, WEDNESDAY, hour) else None


def lastcall_due(
    conn: sqlite3.Connection, season: dict, now: float, offset: float,
    *, force: bool = False,
) -> int | None:
    week = logic.pick_week(conn, season["season_year"], now)
    if week is None or int(season["config"].get("last_lastcall_week") or 0) >= week:
        return None
    hour = int(season["config"]["lastcall_hour"])
    if force:
        return week
    return week if past_weekly_moment(now, offset, SATURDAY, hour) else None


def reckoning_due(
    conn: sqlite3.Connection, season: dict, now: float, offset: float,
    *, force: bool = False,
) -> int | None:
    week = reckoning.next_reckoning_week(conn, season, now)
    if week is None:
        return None
    hour = int(season["config"]["reckoning_hour"])
    if force:
        return week
    return week if past_weekly_moment(now, offset, TUESDAY, hour) else None


# ── posting glue ───────────────────────────────────────────────────────


def _channel(bot, season: dict) -> discord.TextChannel | None:
    guild = bot.get_guild(season["guild_id"])
    channel = (
        guild.get_channel(int(season["config"]["channel_id"] or 0))
        if guild else None
    )
    return channel if isinstance(channel, discord.TextChannel) else None


def _pings(bot, season: dict) -> tuple[str, discord.AllowedMentions]:
    """Both roles, twice a week, nothing else ever (§2.3 as decided)."""
    guild = bot.get_guild(season["guild_id"])
    mentions = []
    roles = []
    for key in ("role_survivor_id", "role_ghost_id"):
        role = guild.get_role(int(season["config"][key] or 0)) if guild else None
        if role is not None:
            mentions.append(role.mention)
            roles.append(role)
    return " ".join(mentions), discord.AllowedMentions(
        everyone=False, users=False, roles=roles
    )


def idle_reason(conn: sqlite3.Connection, season: dict, now: float) -> str:
    """Why a season with nothing due has nothing due — the sentence the
    dashboard's run-now button shows instead of claiming success.

    The gates all route through the week lookup, so an un-ingested schedule
    is indistinguishable from a quiet week unless we say so (2026-08-18:
    a season on a year ESPN has no schedule for looked identical to one
    that had already posted, and the button reported success for both).
    """
    year = season["season_year"]
    games = conn.execute(
        "SELECT COUNT(*) FROM nfl_games WHERE season_year = ?", (year,)
    ).fetchone()[0]
    if not games:
        return (
            f"no schedule ingested for {year} — generate or ingest one "
            "before any weekly task can be due"
        )
    if logic.pick_week(conn, year, now) is None:
        return f"no open week in {year} right now — the season is between weeks"
    return "already run for this week — the once-per-week state still holds"


async def run_weekly_tasks(
    bot,
    db_path: Path,
    now: float,
    *,
    force: bool = False,
    guild_id: int | None = None,
) -> list[dict]:
    """One decision pass for every live season. Cheap when nothing is due.

    ``force`` skips the guild-local day/hour gates — the dashboard's
    run-now button and the simulator use it. The per-week state keys still
    guarantee once-per-week, so forcing can never double-post.

    ``guild_id`` scopes the pass to one guild. The dashboard passes it: a
    forced run is an admin acting on *their* server, and without the filter
    one admin's button press dragged every other guild's season past its
    clock gates too (2026-08-18).

    Returns one record per season considered — ``fired`` lists the tasks
    that posted, ``reason`` explains an empty ``fired`` — so a caller can
    report what actually happened instead of a blind success.
    """

    def _seasons():
        with open_db(db_path) as conn:
            # Enrolling seasons included (self-review 2026-08-18): the season
            # only turns 'active' at the FIRST KICKOFF, so an active-only
            # filter would silently skip week 1's Wednesday panel ping and
            # Saturday last call. The Reckoning gates on elapsed weeks anyway.
            if guild_id is None:
                rows = conn.execute(
                    "SELECT DISTINCT guild_id FROM survivor_seasons "
                    "WHERE status != 'complete'"
                ).fetchall()
                guild_ids = [int(r["guild_id"]) for r in rows]
            else:
                guild_ids = [int(guild_id)]
            from bot_modules.services.survivor_service import get_active_season

            out = []
            for gid in guild_ids:
                season = get_active_season(conn, gid)
                if season is None:
                    continue
                if not int(season["config"]["channel_id"] or 0):
                    out.append((season, 0.0, None, None, None, "no channel configured"))
                    continue
                offset = get_tz_offset_hours(conn, season["guild_id"])
                reck = reckoning_due(conn, season, now, offset, force=force)
                slate = slate_due(conn, season, now, offset, force=force)
                last = lastcall_due(conn, season, now, offset, force=force)
                reason = (
                    idle_reason(conn, season, now)
                    if reck is None and slate is None and last is None
                    else ""
                )
                out.append((season, offset, reck, slate, last, reason))
            return out

    report: list[dict] = []
    for season, offset, reck_wk, slate_wk, lastcall_wk, reason in (
        await asyncio.to_thread(_seasons)
    ):
        fired: list[str] = []
        blocked: list[str] = []
        failed = ""
        try:
            if reck_wk is not None:
                ok = await post_reckoning(bot, db_path, season, reck_wk, now)
                (fired if ok else blocked).append("reckoning")
            if slate_wk is not None:
                ok = await post_slate(bot, db_path, season, slate_wk, now)
                (fired if ok else blocked).append("slate")
            if lastcall_wk is not None:
                ok = await send_last_call(
                    bot, db_path, season, lastcall_wk, now
                )
                (fired if ok else blocked).append("last call")
            await reconcile_roles(bot, db_path, season)
        except Exception as exc:
            failed = str(exc) or exc.__class__.__name__
            log.exception(
                "survivor weekly task failed for season %s", season["id"]
            )
        if blocked and not reason:
            reason = (
                f"{', '.join(blocked)} was due but couldn't post — check the "
                "Survivor channel and the bot's permissions there"
            )
        report.append({
            "season_id": season["id"],
            "guild_id": season["guild_id"],
            "name": season["name"],
            "week": reck_wk or slate_wk or lastcall_wk,
            "fired": fired,
            "blocked": blocked,
            "reason": reason,
            "error": failed,
        })
    return report


async def reconcile_roles(bot, db_path: Path, season: dict) -> None:
    """Life-state role repair, every decision pass: alive players hold the
    Survivor role, ghosts the Ghost role (2026-08-18, Billy's #10).

    swap_member_roles is idempotent and checks the gateway role cache before
    calling Discord, so a no-drift pass costs zero API calls — this exists
    for the drift cases: a join that crashed after charging but before its
    grant (the a41e70e2 bug left exactly that), a mod removing a role by
    hand, a member rejoining after a leave. Best-effort per member; a
    failure is logged by the swap itself and never blocks the pass."""

    def _q():
        with open_db(db_path) as conn:
            return [
                (int(r["user_id"]), r["status"])
                for r in conn.execute(
                    "SELECT user_id, status FROM survivor_players "
                    "WHERE season_id = ?",
                    (season["id"],),
                ).fetchall()
            ]

    for user_id, player_status in await asyncio.to_thread(_q):
        await swap_member_roles(
            bot, season["guild_id"], season["config"], user_id,
            to_ghost=player_status != "alive",
        )


async def post_reckoning(
    bot, db_path: Path, season: dict, week: int, now: float
) -> bool:
    """Returns True when the Reckoning actually posted — the run report
    must not claim a task fired when its channel was unreachable."""
    channel = _channel(bot, season)
    if channel is None:
        return False
    guild = channel.guild
    present = {m.id for m in guild.members}

    def _q():
        with open_db(db_path) as conn:
            # §6.14: leavers die at the Reckoning, so this post reports them.
            reckoning.eliminate_leavers(conn, season, week, present)
            # The weekly prize pays in the same transaction that marks the
            # week reckoned — once per week structurally, never in a preview.
            paid = reckoning.pay_weekly_wins(conn, season, week)
            data = reckoning.build_reckoning_data(conn, season, week, now)
            if paid:
                data["weekly_win"] = {
                    "count": len(paid), "amount": paid[0][1],
                }
            update_config(conn, season["id"], {
                "last_reckoned_week": week,
                "last_reckoned_at": int(now),
            })
            conn.commit()
        return data

    data = await asyncio.to_thread(_q)

    def name_of(user_id: int) -> str:
        member = guild.get_member(user_id)
        return (
            discord.utils.escape_markdown(member.display_name)
            if member else f"soul {user_id}"
        )

    color = await resolve_accent_color(db_path, guild)
    embed = reckoning.build_reckoning_embed(
        data, name_of, season_name=season["name"], color=color
    )
    content, allowed = _pings(bot, season)
    await channel.send(content=content or None, embed=embed, allowed_mentions=allowed)

    # §2.6 as amended 2026-08-18: standings live on the ONE panel, which
    # gets refreshed here instead of posting a separate board.
    from bot_modules.survivor.views import refresh_panel

    await refresh_panel(bot, db_path, season["id"])

    # Ghost roles applied on post (§2.5) + one warm condolence DM (§1.7).
    for entry in data["deaths"]:
        await swap_member_roles(
            bot, season["guild_id"], season["config"], entry["user_id"],
            to_ghost=True,
        )
        member = guild.get_member(entry["user_id"])
        if member is not None:
            try:
                await member.send(CONDOLENCE.replace("{week}", str(week)))
            except (discord.Forbidden, discord.HTTPException):
                pass  # closed DMs: the eulogy already said it publicly
    return True


async def post_slate(
    bot, db_path: Path, season: dict, week: int, now: float
) -> bool:
    """Wednesday's moment (§2.3 as amended 2026-08-18): repost the ONE
    channel panel to the bottom with the week-open ping — the panel already
    carries the slate, the standings line, and both buttons. In between
    Wednesdays the panel only gets edited, never reposted."""
    from bot_modules.survivor.views import PanelError, repost_panel

    content, allowed = _pings(bot, season)
    ping = f"Week {week} is open — pick a team to win. ⬇️"
    if content:
        ping = f"{content} {ping}"

    def _mark():
        with open_db(db_path) as conn:
            update_config(conn, season["id"], {"last_slate_week": week})
            conn.commit()

    try:
        await repost_panel(
            bot, db_path, season["id"],
            content=ping, allowed_mentions=allowed,
        )
    except PanelError as exc:
        log.warning("survivor: weekly panel repost failed: %s", exc)
        return False
    await asyncio.to_thread(_mark)
    return True


async def send_last_call(
    bot, db_path: Path, season: dict, week: int, now: float
) -> bool:
    """Saturday's nudge: DM only the pickless alive (§2.3); closed DMs fall
    back to one channel mention. Early-window games get named (§6.3).

    Returns True when the week's last call was handled — including the
    everyone-has-picked case, which is a real outcome, not a failure."""
    channel = _channel(bot, season)
    guild = channel.guild if channel else bot.get_guild(season["guild_id"])
    if guild is None:
        return False

    def _q():
        with open_db(db_path) as conn:
            pickless = [
                int(r["user_id"])
                for r in conn.execute(
                    "SELECT p.user_id FROM survivor_players p "
                    "WHERE p.season_id = ? AND p.status = 'alive' "
                    "AND NOT EXISTS (SELECT 1 FROM survivor_picks k WHERE "
                    "k.season_id = p.season_id AND k.user_id = p.user_id "
                    "AND k.week = ?)",
                    (season["id"], week),
                ).fetchall()
            ]
            offset = get_tz_offset_hours(conn, season["guild_id"])
            early = conn.execute(
                "SELECT home, away, kickoff_utc FROM nfl_games "
                "WHERE season_year = ? AND week = ? AND status = 'scheduled' "
                "ORDER BY kickoff_utc LIMIT 3",
                (season["season_year"], week),
            ).fetchall()
            # §6.3: name games that kick before Sunday afternoon local —
            # the international-morning trap.
            early_lines = []
            for r in early:
                ts = logic.kickoff_ts(r["kickoff_utc"])
                dow, hour = local_parts(ts, offset)
                if now < ts and (dow == SATURDAY or (dow == 6 and hour < 13)):
                    early_lines.append(
                        f"{r['away']} @ {r['home']} kicks <t:{int(ts)}:R>"
                    )
            update_config(conn, season["id"], {"last_lastcall_week": week})
            conn.commit()
        return pickless, early_lines

    pickless, early_lines = await asyncio.to_thread(_q)
    if not pickless:
        return True  # everybody picked — the nudge was due and is now done
    text = LAST_CALL.replace("{week}", str(week))
    if early_lines:
        text += "\n-# Early games this week: " + " · ".join(early_lines)
    fallback: list[int] = []
    for user_id in pickless:
        member = guild.get_member(user_id)
        if member is None:
            continue
        try:
            # The pick button rides the DM (2026-08-18) — same persistent
            # DynamicItem as the channel panel, so the nudge IS the door,
            # not directions to one. pick_view builds it fresh per send;
            # a view instance can't be reused across messages.
            await member.send(text, view=pick_view(season["id"]))
        except (discord.Forbidden, discord.HTTPException):
            fallback.append(user_id)
    if fallback and channel is not None:
        mentions = " ".join(f"<@{uid}>" for uid in fallback)
        await channel.send(
            f"🌙 Last call, {mentions} — no pick yet for Week {week}. "
            "Make your pick below.",
            view=pick_view(season["id"]),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False,
                users=[discord.Object(id=uid) for uid in fallback],
            ),
        )
    return True


def pick_view(season_id: int) -> discord.ui.View:
    """One 🏈 Make your pick button — the last-call DM's door. Persistent
    (DynamicItem), so it keeps working across restarts like every panel
    button."""
    view = discord.ui.View(timeout=None)
    view.add_item(SlatePickButton(season_id))
    return view


async def post_addenda(bot, db_path: Path, reports: dict) -> None:
    """Post-Reckoning straggler grades (§4.2 as clarified): when a settle
    sweep touches an already-reckoned week, say so — never silently."""

    def _q():
        with open_db(db_path) as conn:
            from bot_modules.services.survivor_service import get_season

            out = []
            for season_id, report in reports.items():
                season = get_season(conn, season_id)
                if season is None:
                    continue
                reckoned = int(season["config"].get("last_reckoned_week") or 0)
                late_weeks = sorted(
                    w for w in report.graded_weeks if w <= reckoned
                )
                if late_weeks:
                    out.append((season, late_weeks, list(report.recomputed)))
            return out

    for season, weeks, recomputed in await asyncio.to_thread(_q):
        channel = _channel(bot, season)
        if channel is None:
            continue
        names = ", ".join(f"<@{uid}>" for uid in recomputed[:10])
        await channel.send(
            f"⏳ **Results update** — pending Week "
            f"{', '.join(map(str, weeks))} result(s) are now final"
            + (f", affecting {names}" if names else "")
            + ". Standings updated.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
