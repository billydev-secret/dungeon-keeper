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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_tz_offset_hours, open_db, open_db_immediate
from bot_modules.services.dm_branding import send_branded_dm
from bot_modules.services.survivor_service import SeasonError, get_season, update_config
from bot_modules.survivor import logic, reckoning
from bot_modules.survivor.views import SlatePickButton, swap_member_roles

log = logging.getLogger("dungeonkeeper.survivor")

WEDNESDAY, SATURDAY, TUESDAY = 2, 5, 1

# survivor-172: ``pick_week`` returns Week 1 from the moment the schedule
# ingests, which for a season created in August is weeks before any game.
# The slate and last call only fire once the week's own Tue–Mon frame has
# opened (``week_imminent``) — a state guard like the once-per-week keys,
# so it holds under ``force`` too (the dashboard button must not recreate
# the prod defect). It is the frame, not a day count, on purpose:
# ``past_weekly_moment`` treats every day after the target as "missed,
# catch up now", so a 7-day window that opened on a Thursday posted the
# slate that Thursday and DM'd the last call the Saturday before the
# opener, then burned both once-per-week keys (code review, 2026-09-04).

# survivor-185: role reconcile is drift repair, not a 60-second duty. Once
# an hour per season (plus whenever a decision fired or an admin forced the
# pass), and a member whose swap failed is left alone for an hour rather
# than producing a traceback and an API call every tick.
RECONCILE_INTERVAL = 3600.0
ROLE_FAILURE_BACKOFF = 3600.0
_reconciled_at: dict[int, float] = {}          # season id → last pass
_role_failures: dict[tuple[int, int], float] = {}  # (season, user) → when

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


def week_frame_opens(first_kickoff: float, offset_hours: float) -> float:
    """Epoch of the guild-local Tuesday midnight that opens the Tue–Mon
    frame (see ``past_weekly_moment``) containing ``first_kickoff``."""
    tz = timezone(timedelta(hours=offset_hours))
    local = datetime.fromtimestamp(first_kickoff, tz)
    back = (local.weekday() - TUESDAY) % 7
    start = (local - timedelta(days=back)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start.timestamp()


def week_imminent(
    conn: sqlite3.Connection, season: dict, week: int, now: float, offset: float
) -> bool:
    """``now`` is inside (or past) the weekly frame the week's first
    non-postponed kickoff falls in — the Wednesday slate and Saturday last
    call of *that* frame are the ones meant for it."""
    first = logic.week_first_kickoff(conn, season["season_year"], week)
    return first is not None and now >= week_frame_opens(first, offset)


def slate_due(
    conn: sqlite3.Connection, season: dict, now: float, offset: float,
    *, force: bool = False,
) -> int | None:
    week = logic.pick_week(conn, season["season_year"], now)
    if week is None or int(season["config"].get("last_slate_week") or 0) >= week:
        return None
    if not week_imminent(conn, season, week, now, offset):
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
    if not week_imminent(conn, season, week, now, offset):
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


# ── weekly clock (the dashboard's operator view) ───────────────────────

# Task key → (label, weekday, config hour key, config last-fired-week key).
# Order is the week's own order: Wednesday opens it, Saturday nudges,
# Tuesday closes it. It lives beside the due-functions so the clock the
# dashboard renders and the clock the loop runs on are the same clock.
WEEKLY_TASKS: dict[str, tuple[str, int, str, str]] = {
    "slate": ("Slate post", WEDNESDAY, "slate_hour", "last_slate_week"),
    "lastcall": ("Last call", SATURDAY, "lastcall_hour", "last_lastcall_week"),
    "reckoning": ("The Reckoning", TUESDAY, "reckoning_hour", "last_reckoned_week"),
}

# Only the two idempotent posts can be re-armed. The Reckoning pays
# weekly-win coins in the transaction that marks the week reckoned (spec
# §5), so resetting *its* week would pay everyone twice.
RESETTABLE_TASKS = frozenset({"slate", "lastcall"})


def next_weekly_moment(
    now: float, offset_hours: float, target_dow: int, target_hour: int
) -> float:
    """Epoch of the next (weekday, hour) in the guild's local clock, strictly
    after ``now`` — the "next due" a task shows when it isn't due yet."""
    tz = timezone(timedelta(hours=offset_hours))
    local = datetime.fromtimestamp(now, tz)
    candidate = local.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(target_dow - local.weekday()) % 7)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def weekly_clock_rows(
    config: dict, now: float, offset_hours: float, *, week: int | None,
    due: dict[str, int | None],
) -> list[dict]:
    """The Season card's read-only Weekly clock, one row per task: which
    week it last fired for, whether the poll loop would fire it on its next
    tick (``due``: this module's own due decisions), and otherwise the next
    guild-local moment its gate opens. This is the operator's only view of
    the clock on a real season — the force-run button lives on the
    Simulator card by design (first-look #4)."""
    rows = []
    for key, (label, dow, hour_key, fired_key) in WEEKLY_TASKS.items():
        hour = int(config.get(hour_key) or 0)
        fired_week = int(config.get(fired_key) or 0)
        rows.append({
            "task": key,
            "label": label,
            "hour": hour,
            "weekday": dow,
            "fired_week": fired_week,
            # Already spent for the current pick week — the Week 1 shape the
            # dashboard could not show before (2026-09-02 review).
            "spent": week is not None and fired_week >= week,
            "due_week": due.get(key),
            "next_ts": next_weekly_moment(now, offset_hours, dow, hour),
            "resettable": key in RESETTABLE_TASKS,
        })
    return rows


def weekly_clock(
    conn: sqlite3.Connection, season: dict, now: float, offset: float,
) -> tuple[list[dict], int | None]:
    """``(rows, pick_week)`` for the dashboard's Weekly clock — the rows
    from :func:`weekly_clock_rows`, fed by the same due-functions the poll
    loop runs, so what the panel says is due is what the next tick fires."""
    week = logic.pick_week(conn, season["season_year"], now)
    due = {
        "slate": slate_due(conn, season, now, offset),
        "lastcall": lastcall_due(conn, season, now, offset),
        "reckoning": reckoning_due(conn, season, now, offset),
    }
    return weekly_clock_rows(season["config"], now, offset, week=week, due=due), week


def rearm_weekly_task(
    conn: sqlite3.Connection, season: dict, task: str, now: float,
) -> tuple[int, int, int]:
    """Re-arm one weekly post for the current pick week, so the slate or the
    last call fires again at its next gate (or on the next tick if the gate
    is already open). Returns ``(week, last-fired before, last-fired after)``;
    the re-arm rule is ``max(week - 1, 0)``, the week before the one being
    re-armed. Raises :class:`SeasonError` for the Reckoning (it pays coins)
    or an unknown task, and when the task hasn't fired for the current week
    (nothing to reset). Does not commit."""
    if task not in RESETTABLE_TASKS:
        raise SeasonError("Only the slate and the last call can be re-armed.")
    label, _dow, _hour_key, fired_key = WEEKLY_TASKS[task]
    week = logic.pick_week(conn, season["season_year"], now)
    before = int(season["config"].get(fired_key) or 0)
    if week is None or before < week:
        raise SeasonError(
            f"{label} hasn't fired for the current week — nothing to reset."
        )
    after = max(week - 1, 0)
    update_config(conn, season["id"], {fired_key: after})
    return week, before, after


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
    week = logic.pick_week(conn, year, now)
    if week is None:
        return f"no open week in {year} right now — the season is between weeks"
    offset = get_tz_offset_hours(conn, season["guild_id"])
    if not week_imminent(conn, season, week, now, offset):
        first = logic.week_first_kickoff(conn, year, week) or now
        days = int((first - now) // 86400)
        return (
            f"Week {week}'s first kickoff is {days} days out — the slate and "
            "last call wait for the Tuesday that opens its week"
        )
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
            await reconcile_roles(
                bot, db_path, season, now, force=force or bool(fired),
            )
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


async def reconcile_roles(
    bot, db_path: Path, season: dict, now: float | None = None,
    *, force: bool = True,
) -> None:
    """Life-state role repair: alive players hold the Survivor role, ghosts
    the Ghost role (2026-08-18, Billy's #10).

    swap_member_roles is idempotent and checks the gateway role cache before
    calling Discord, so a no-drift pass costs zero API calls — this exists
    for the drift cases: a join that crashed after charging but before its
    grant (the a41e70e2 bug left exactly that), a mod removing a role by
    hand, a member rejoining after a leave. Best-effort per member; a
    failure never blocks the pass.

    Cadence (survivor-185): once per ``RECONCILE_INTERVAL`` per season
    unless ``force`` (a decision fired, or an admin pressed run-now). A
    member whose swap failed — a role above the bot, Manage Roles lost — is
    skipped for ``ROLE_FAILURE_BACKOFF`` and warned about once per backoff,
    instead of a traceback and an API call every 60-second tick. The memory
    is in-process; a restart just retries once, which is harmless."""
    now = time.time() if now is None else now
    last = _reconciled_at.get(season["id"], 0.0)
    if not force and now - last < RECONCILE_INTERVAL:
        return
    _reconciled_at[season["id"]] = now

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
        key = (season["id"], user_id)
        failed_at = _role_failures.get(key)
        if failed_at is not None and now - failed_at < ROLE_FAILURE_BACKOFF:
            continue
        note = await swap_member_roles(
            bot, season["guild_id"], season["config"], user_id,
            to_ghost=player_status != "alive",
        )
        if note and note.startswith("role swap failed"):
            _role_failures[key] = now
            log.warning(
                "survivor: role reconcile for %s in season %s: %s — "
                "retrying in an hour", user_id, season["id"], note,
            )
        else:
            _role_failures.pop(key, None)


async def confirm_leavers(bot, guild, suspects: list[int]) -> set[int]:
    """§6.14 with a cache guard (survivor-174): a suspected leaver dies only
    when the member cache is trustworthy — the bot is ready and the guild
    fully chunked — AND ``fetch_member`` says NotFound. A partial cache
    after a re-IDENTIFY, or any other API answer, keeps the player."""
    if not suspects or not bot.is_ready() or not getattr(guild, "chunked", False):
        return set()
    gone: set[int] = set()
    for user_id in suspects:
        try:
            await guild.fetch_member(user_id)
        except discord.NotFound:
            gone.add(user_id)
        except discord.HTTPException as exc:
            log.warning(
                "survivor: could not confirm whether %s left (%s) — kept",
                user_id, exc,
            )
    return gone


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

    def _suspects():
        with open_db(db_path) as conn:
            return reckoning.suspected_leavers(conn, season, present)

    gone = await confirm_leavers(bot, guild, await asyncio.to_thread(_suspects))

    def _apply(conn: sqlite3.Connection) -> dict:
        """The week's writes: leavers (§6.14), the weekly prize, the mark.
        Run twice on purpose — once rolled back to build the post, once
        for real after Discord accepted it (survivor-173)."""
        reckoning.eliminate_leavers(conn, season, week, present, confirmed=gone)
        paid = reckoning.pay_weekly_wins(conn, season, week)
        data = reckoning.build_reckoning_data(conn, season, week, now)
        if paid:
            data["weekly_win"] = {"count": len(paid), "amount": paid[0][1]}
        update_config(conn, season["id"], {
            "last_reckoned_week": week,
            "last_reckoned_at": int(now),
        })
        return data

    def _preview():
        with open_db(db_path) as conn:
            data = _apply(conn)
            from bot_modules.services.economy_service import load_econ_settings

            settings = load_econ_settings(conn, season["guild_id"])
            # Nothing is paid or marked until the post is up: the send comes
            # first, and a Forbidden used to lose the week's post forever
            # (retrying by resetting the mark would have double-paid).
            conn.rollback()
        return data, settings

    def _commit() -> bool:
        # The write lock first, then the mark re-read under it: the loop's
        # tick and the dashboard's run-now share no lock, and between
        # ``reckoning_due`` and here sit an API call per suspect and the
        # send itself — long enough for a second pass to reach the same
        # point. Whichever lands second finds the week marked and pays
        # nothing (code review, 2026-09-04).
        with open_db_immediate(db_path) as conn:
            fresh = get_season(conn, season["id"])
            marked = int((fresh or season)["config"].get("last_reckoned_week") or 0)
            if marked >= week:
                return False
            _apply(conn)
        return True

    data, settings = await asyncio.to_thread(_preview)

    def name_of(user_id: int) -> str:
        member = guild.get_member(user_id)
        return (
            discord.utils.escape_markdown(member.display_name)
            if member else f"soul {user_id}"
        )

    color = await safe_resolve_accent(db_path, guild, log_label="survivor")
    embed = reckoning.build_reckoning_embed(
        data, name_of, season_name=season["name"], settings=settings,
        color=color,
    )
    content, allowed = _pings(bot, season)
    try:
        await channel.send(
            content=content or None, embed=embed, allowed_mentions=allowed
        )
    except discord.HTTPException as exc:
        # Blocked, not fired: the week stays unreckoned and unpaid, so the
        # next pass retries the post — and the run report says why.
        log.warning("survivor: Reckoning post failed for week %s: %s", week, exc)
        return False
    if not await asyncio.to_thread(_commit):
        log.warning(
            "survivor: week %s of season %s was reckoned by a parallel pass "
            "while this one was posting — the second card is a duplicate",
            week, season["id"],
        )

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
            # Through dm_branding (style guide: DM branding) — content-only
            # passes through unbranded, and a closed DM returns None; the
            # eulogy already said it publicly.
            await send_branded_dm(
                member, db_path=db_path, guild=guild,
                content=CONDOLENCE.replace("{week}", str(week)),
            )
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

    def _first_kickoff():
        with open_db(db_path) as conn:
            return logic.week_first_kickoff(conn, season["season_year"], week)

    # survivor-183: name the first kickoff relatively, so a short week (the
    # Wednesday-night opener locks three teams the same day) is obvious.
    first = await asyncio.to_thread(_first_kickoff)
    ping = f"Week {week} is open — pick a team to win. ⬇️"
    if first is not None and first > now:
        ping = (
            f"Week {week} is open — first kickoff <t:{int(first)}:R>. "
            "Pick a team to win. ⬇️"
        )
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
            early_lines = early_game_lines(conn, season, week, now, offset)
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
        # The pick button rides the DM (2026-08-18) — same persistent
        # DynamicItem as the channel panel, so the nudge IS the door, not
        # directions to one. pick_view builds it fresh per send; a view
        # instance can't be reused across messages. send_branded_dm returns
        # None on a closed DM (style guide: DM branding).
        sent = await send_branded_dm(
            member, db_path=db_path, guild=guild,
            content=text, view=pick_view(season["id"]),
        )
        if sent is None:
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


def early_game_lines(
    conn: sqlite3.Connection, season: dict, week: int, now: float, offset: float
) -> list[str]:
    """§6.3: the next three games still to kick, kept only when they kick
    before Sunday afternoon local — the international-morning trap. The
    future filter runs in SQL *before* the LIMIT (survivor-180): a kicked
    game still marked 'scheduled' (an ESPN outage, a Saturday game under
    way) used to steal a slot from the Sunday-morning game the DM exists
    to name. ``kickoff_utc`` is always ``isoformat()`` in UTC (ESPN ingest
    and the simulator both), so the string comparison is chronological."""
    now_iso = datetime.fromtimestamp(now, timezone.utc).replace(
        microsecond=0
    ).isoformat()
    early = conn.execute(
        "SELECT home, away, kickoff_utc FROM nfl_games "
        "WHERE season_year = ? AND week = ? AND status = 'scheduled' "
        "AND kickoff_utc > ? ORDER BY kickoff_utc LIMIT 3",
        (season["season_year"], week, now_iso),
    ).fetchall()
    lines = []
    for r in early:
        ts = logic.kickoff_ts(r["kickoff_utc"])
        dow, hour = local_parts(ts, offset)
        if now < ts and (dow == SATURDAY or (dow == 6 and hour < 13)):
            lines.append(f"{r['away']} @ {r['home']} kicks <t:{int(ts)}:R>")
    return lines


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
