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
from bot_modules.survivor.embeds import build_board_embed
from bot_modules.survivor.views import SlatePickButton, swap_member_roles

log = logging.getLogger("dungeonkeeper.survivor")

WEDNESDAY, SATURDAY, TUESDAY = 2, 5, 1

CONDOLENCE = (
    "Your run ended in Week {week}. 🪦\n"
    "-# You can keep playing: Ghost Streak is live — the longest win streak "
    "after elimination takes the side pot. `/survivor pick` still works. 👻"
)
LAST_CALL = (
    "You haven't picked for Week {week}. `/survivor pick` — or I'll pick "
    "for you, and I have terrible taste. 🌙"
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


def slate_due(conn: sqlite3.Connection, season: dict, now: float, offset: float) -> int | None:
    week = logic.pick_week(conn, season["season_year"], now)
    if week is None or int(season["config"].get("last_slate_week") or 0) >= week:
        return None
    hour = int(season["config"]["slate_hour"])
    return week if past_weekly_moment(now, offset, WEDNESDAY, hour) else None


def lastcall_due(conn: sqlite3.Connection, season: dict, now: float, offset: float) -> int | None:
    week = logic.pick_week(conn, season["season_year"], now)
    if week is None or int(season["config"].get("last_lastcall_week") or 0) >= week:
        return None
    hour = int(season["config"]["lastcall_hour"])
    return week if past_weekly_moment(now, offset, SATURDAY, hour) else None


def reckoning_due(conn: sqlite3.Connection, season: dict, now: float, offset: float) -> int | None:
    week = reckoning.next_reckoning_week(conn, season, now)
    if week is None:
        return None
    hour = int(season["config"]["reckoning_hour"])
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


async def run_weekly_tasks(bot, db_path: Path, now: float) -> None:
    """One decision pass for every live season. Cheap when nothing is due."""

    def _seasons():
        with open_db(db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT guild_id FROM survivor_seasons "
                "WHERE status = 'active'"
            ).fetchall()
            from bot_modules.services.survivor_service import get_active_season

            out = []
            for r in rows:
                season = get_active_season(conn, int(r["guild_id"]))
                if season and int(season["config"]["channel_id"] or 0):
                    offset = get_tz_offset_hours(conn, season["guild_id"])
                    out.append((
                        season,
                        offset,
                        reckoning_due(conn, season, now, offset),
                        slate_due(conn, season, now, offset),
                        lastcall_due(conn, season, now, offset),
                    ))
            return out

    for season, offset, reck_wk, slate_wk, lastcall_wk in await asyncio.to_thread(_seasons):
        try:
            if reck_wk is not None:
                await post_reckoning(bot, db_path, season, reck_wk, now)
            if slate_wk is not None:
                await post_slate(bot, db_path, season, slate_wk, now)
            if lastcall_wk is not None:
                await send_last_call(bot, db_path, season, lastcall_wk, now)
        except Exception:
            log.exception(
                "survivor weekly task failed for season %s", season["id"]
            )


async def post_reckoning(bot, db_path: Path, season: dict, week: int, now: float) -> None:
    channel = _channel(bot, season)
    if channel is None:
        return
    guild = channel.guild
    present = {m.id for m in guild.members}

    def _q():
        with open_db(db_path) as conn:
            # §6.14: leavers die at the Reckoning, so this post reports them.
            reckoning.eliminate_leavers(conn, season, week, present)
            data = reckoning.build_reckoning_data(conn, season, week, now)
            update_config(conn, season["id"], {
                "last_reckoned_week": week,
                "last_reckoned_at": int(now),
            })
            from bot_modules.survivor.logic import board_data

            board = board_data(conn, season, now)
            conn.commit()
        return data, board

    data, board = await asyncio.to_thread(_q)

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

    # The board auto-posts after each Reckoning (§2.6).
    await channel.send(embed=build_board_embed(
        board, name_of, season_name=season["name"],
        strikes_allowed=int(season["config"]["strikes"]), color=color,
    ))

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


async def post_slate(bot, db_path: Path, season: dict, week: int, now: float) -> None:
    channel = _channel(bot, season)
    if channel is None:
        return

    def _q():
        with open_db(db_path) as conn:
            rows = conn.execute(
                "SELECT game_id, home, away, kickoff_utc FROM nfl_games "
                "WHERE season_year = ? AND week = ? AND status != 'postponed' "
                "ORDER BY kickoff_utc",
                (season["season_year"], week),
            ).fetchall()
            games = [
                {
                    "home": r["home"], "away": r["away"],
                    "kickoff_ts": logic.kickoff_ts(r["kickoff_utc"]),
                }
                for r in rows
            ]
            alive = conn.execute(
                "SELECT COUNT(*) FROM survivor_players "
                "WHERE season_id = ? AND status = 'alive'",
                (season["id"],),
            ).fetchone()[0]
            entrants = conn.execute(
                "SELECT COUNT(*) FROM survivor_players WHERE season_id = ?",
                (season["id"],),
            ).fetchone()[0]
            picked = conn.execute(
                "SELECT COUNT(DISTINCT p.user_id) FROM survivor_picks p "
                "JOIN survivor_players pl ON pl.season_id = p.season_id "
                "AND pl.user_id = p.user_id "
                "WHERE p.season_id = ? AND p.week = ? AND pl.status = 'alive'",
                (season["id"], week),
            ).fetchone()[0]
            pot = logic.pot_totals(conn, season)["main"]
            gauntlet_mode = bool(
                logic.elapsed_weeks(conn, season["season_year"], now)
            )
            update_config(conn, season["id"], {"last_slate_week": week})
            conn.commit()
        return games, int(alive), int(entrants), int(picked), pot, gauntlet_mode

    games, alive, entrants, picked, pot, gauntlet_mode = await asyncio.to_thread(_q)
    if not games:
        return
    guild = channel.guild
    color = await resolve_accent_color(db_path, guild)
    config = season["config"]
    embed = reckoning.build_slate_embed(
        games, week=week, picked=picked, alive=alive,
        season_name=season["name"],
        entrants=entrants, pot=pot, buyin=int(config["buyin_coins"]),
        late_entry=str(config["late_entry"]), gauntlet_mode=gauntlet_mode,
        color=color,
    )
    # The slate doubles as the weekly mini-announcement (2026-08-18): the
    # Join button rides alongside the pick button — unless entry is closed.
    view = discord.ui.View(timeout=None)
    view.add_item(SlatePickButton(season["id"]))
    join_line = reckoning.slate_join_line(
        buyin=int(config["buyin_coins"]),
        late_entry=str(config["late_entry"]),
        gauntlet_mode=gauntlet_mode,
    )
    if join_line is not None:
        from bot_modules.survivor.views import JoinSeasonButton

        view.add_item(JoinSeasonButton(season["id"]))
    content, allowed = _pings(bot, season)
    await channel.send(
        content=content or None, embed=embed, view=view, allowed_mentions=allowed
    )


async def send_last_call(bot, db_path: Path, season: dict, week: int, now: float) -> None:
    """Saturday's nudge: DM only the pickless alive (§2.3); closed DMs fall
    back to one channel mention. Early-window games get named (§6.3)."""
    channel = _channel(bot, season)
    guild = channel.guild if channel else bot.get_guild(season["guild_id"])
    if guild is None:
        return

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
        return
    text = LAST_CALL.replace("{week}", str(week))
    if early_lines:
        text += "\n-# Early games this week: " + " · ".join(early_lines)
    fallback: list[int] = []
    for user_id in pickless:
        member = guild.get_member(user_id)
        if member is None:
            continue
        try:
            await member.send(text)
        except (discord.Forbidden, discord.HTTPException):
            fallback.append(user_id)
    if fallback and channel is not None:
        mentions = " ".join(f"<@{uid}>" for uid in fallback)
        await channel.send(
            f"🌙 Last call, {mentions} — no pick yet for Week {week}. "
            "`/survivor pick`",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False,
                users=[discord.Object(id=uid) for uid in fallback],
            ),
        )


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
