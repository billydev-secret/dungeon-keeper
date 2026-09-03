"""Survivor dashboard API — the feature's entire admin surface.

Survivor's configuration is dashboard-managed per the 2026-08-17 decision
(spec §3): season lifecycle, every §5 dial, and roster
eliminate/revive all live here, admin-gated. There is NO Discord-side admin
surface at all (decided 2026-08-18) — manual settle and the Reckoning
preview arrive on this panel with the settle engine in stage 4.

Every mutation mirrors to the DK mod-log channel (spec §3: "all admin
actions → DK mod-log"). Snowflakes go out as strings — JS ``Number`` can't
hold a full Discord id.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.core.role_provision import (
    ensure_feature_role,
    provenance_recorder,
)
from bot_modules.services import feature_roles as fr
from bot_modules.services import survivor_espn as espn
from bot_modules.services import survivor_service as svc
from bot_modules.services.moderation import write_audit
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import mirror_admin_action_to_mod_log
from web_server.routes.panel_posting import sticky_conflict

log = logging.getLogger("web.survivor")

router = APIRouter()
_ADMIN = Depends(require_perms({"admin"}))

# The three roles the bot manages (spec §3.3), paired with the config key that
# stores each one's id. Read from the shared registry rather than spelled out
# here: `bot-roles` can only list a role the registry knows about, and a name
# that lived at one call site was a role nothing could audit.
MANAGED_ROLES = tuple(
    (entry.key, entry.spec.name)
    for entry in fr.MANAGED_ROLES
    if entry.source == fr.SOURCE_SURVIVOR
)

_ID_KEYS = frozenset(
    {
        "channel_id", "role_survivor_id", "role_ghost_id",
        "role_sole_survivor_id", "announcement_message_id",
        "announcement_channel_id",
    }
)


class CreateSeasonBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    season_year: int = Field(ge=2020, le=2100)




class EliminateBody(BaseModel):
    week: int = Field(ge=1, le=18)


def _config_json(config: dict) -> dict:
    """Config for the wire: id-valued keys become strings."""
    out: dict[str, Any] = {}
    for key, value in config.items():
        out[key] = str(value) if key in _ID_KEYS else value
    return out


def _season_json(season: dict) -> dict:
    return {
        "id": season["id"],
        "name": season["name"],
        "season_year": season["season_year"],
        "status": season["status"],
        "config": _config_json(season["config"]),
    }


def _coerce_config_body(body: dict) -> dict:
    """Undo the wire's stringly ids before the service validates types."""
    coerced = dict(body)
    for key in _ID_KEYS & set(coerced):
        try:
            coerced[key] = int(coerced[key] or 0)
        except (TypeError, ValueError):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"{key} must be an id."
            ) from None
    return coerced


async def _mirror_mod_log(
    ctx: Any, guild_id: int, *, action: str, summary: str, user: AuthenticatedUser
) -> None:
    """Post the admin action to the guild's mod-log channel, best-effort —
    the shared mirror in ``web_server.helpers``, branded for Survivor."""
    await mirror_admin_action_to_mod_log(
        ctx, guild_id,
        domain="🏈 Survivor",
        action=action,
        summary=summary,
        user=user,
        log=log,
    )


async def _swap_member_roles(
    ctx: Any, guild_id: int, config: dict, user_id: int, *, to_ghost: bool
) -> str | None:
    """Thin delegate: the swap lives once in survivor/views.py, shared with
    the Reckoning's death march (stage 6a)."""
    from bot_modules.survivor.views import swap_member_roles

    return await swap_member_roles(
        getattr(ctx, "bot", None), guild_id, config, user_id, to_ghost=to_ghost
    )


async def _service_call(coro):
    """Await a query, translating service errors to a 422."""
    try:
        return await coro
    except svc.SeasonError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


# ── overview ──────────────────────────────────────────────────────────


@router.get("/survivor/overview")
async def overview(request: Request, _: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            players = svc.list_players(conn, season["id"]) if season else []
            archived = [
                s for s in svc.list_seasons(conn, guild_id) if s["status"] == "complete"
            ]
        return season, players, archived

    season, players, archived = await run_query(_q)
    return {
        "season": _season_json(season) if season else None,
        "players": [
            {**p, "user_id": str(p["user_id"])} for p in players
        ],
        "archived_seasons": [
            {"id": s["id"], "name": s["name"], "season_year": s["season_year"]}
            for s in archived
        ],
    }


# ── season lifecycle ──────────────────────────────────────────────────


@router.post("/survivor/season")
async def create_season(
    request: Request, body: CreateSeasonBody, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    # The DB create runs FIRST: it owns the one-live-season rule (service
    # check + schema backstop), and a refused create must leave the guild
    # untouched — creating roles before validation left orphans behind a 422.
    def _create():
        with ctx.open_db() as conn:
            season_id = svc.create_season(conn, guild_id, body.name, body.season_year)
            # Durable audit row in the same transaction (the Discord mirror
            # is best-effort and log.txt is wiped every boot).
            write_audit(
                conn, guild_id=guild_id, action="survivor_season_create",
                actor_id=int(user.user_id),
                extra={"season_id": season_id, "name": body.name,
                       "year": body.season_year, "via": "web"},
            )
            conn.commit()
        return season_id

    season_id = await _service_call(run_query(_create))

    # Roles only after the season exists (spec §3.3): create any of the three
    # that are missing and store their ids in the season's config. Degraded
    # path — no bot, no guild, or no Manage Roles — leaves ids unset and
    # reports it; the panel surfaces the warning.
    role_ids: dict[str, int] = {}
    role_report: list[str] = []
    created: set[str] = set()
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is not None:
        for key, role_name in MANAGED_ROLES:

            async def _mark_created(_role, key=key) -> None:
                created.add(key)

            role = await ensure_feature_role(
                guild,
                fr.spec_for(key),
                # The bot swaps these three on members every week, so a
                # same-named role above its own top role is no use and must
                # not be adopted.
                assigns=True,
                on_provision=provenance_recorder(ctx, guild_id, key),
                # A brand-new season starts with no ids of its own; the helper's
                # adopt-by-name step is what reuses the roles a previous season
                # left behind instead of stacking up a second @Ghost each year.
                load=lambda: 0,
                store=lambda rid, key=key: role_ids.__setitem__(key, rid),
                on_create=_mark_created,
                feature="Survivor",
            )
            if role is None:
                role_report.append(
                    f"couldn't create {role_name} — check Manage Roles"
                )
                continue
            role_report.append(
                f"created {role_name}" if key in created
                else f"using existing {role_name}"
            )
    else:
        role_report.append("bot offline — roles not created; set them later")

    def _finish():
        with ctx.open_db() as conn:
            if role_ids:
                svc.update_config(conn, season_id, role_ids)
            season = svc.get_season(conn, season_id)
            conn.commit()
        return season

    season = await _service_call(run_query(_finish))
    assert season is not None

    # Full-season schedule ingest (spec §4.2), best-effort like the roles.
    # Synthetic seasons (the testing rig, year >= SIM_YEAR_MIN) skip ESPN
    # entirely — their schedule comes from the Simulator card.
    from bot_modules.survivor.sim import is_sim_year

    if is_sim_year(body.season_year):
        schedule_report = (
            "synthetic season — generate a schedule from the Simulator card"
        )
    else:
        schedule_report = await _ingest_season_schedule(ctx, body.season_year)

    await _mirror_mod_log(
        ctx,
        guild_id,
        action="season created",
        summary=f"**{season['name']}** ({season['season_year']}) — enrolling; "
        + schedule_report,
        user=user,
    )
    return {
        "season": _season_json(season),
        "role_report": role_report,
        "schedule_report": schedule_report,
    }


async def _ingest_season_schedule(ctx: Any, season_year: int) -> str:
    """Fetch and ingest the season's schedule; returns a one-line report."""
    try:
        async with aiohttp.ClientSession() as session:
            games, skipped, failed_weeks = await espn.fetch_season(
                session, season_year
            )
    except Exception:  # noqa: BLE001 — unversioned API, never fail the create
        log.exception("survivor: season schedule fetch failed outright")
        return "schedule ingest failed — the daily refresh will retry"
    if not games:
        return "schedule ingest got no games — the daily refresh will retry"

    def _q():
        with ctx.open_db() as conn:
            counts = espn.ingest_games(conn, season_year, games)
            conn.commit()
        return counts

    counts = await run_query(_q)
    total = counts["inserted"] + counts["updated"]
    report = f"schedule ingested ({total} games, {counts['inserted']} new)"
    if failed_weeks:
        report += f"; weeks failed, refresh will heal: {failed_weeks}"
    if skipped:
        report += f"; {skipped} malformed events skipped"
    return report


@router.post("/survivor/season/end")
async def end_season(request: Request, user: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season to end.")
            svc.end_season(conn, season["id"])
            write_audit(
                conn, guild_id=guild_id, action="survivor_season_end",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"], "via": "web"},
            )
            conn.commit()
        return season

    season = await _service_call(run_query(_q))
    await _mirror_mod_log(
        ctx,
        guild_id,
        action="season ended",
        summary=f"**{season['name']}** archived; history stays queryable",
        user=user,
    )
    return {"ok": True}


# ── this week's games + manual settle ─────────────────────────────────


class SettleBody(BaseModel):
    game_id: str = Field(min_length=1, max_length=32)
    outcome: str = Field(min_length=1, max_length=8)  # abbr | TIE | VOID


@router.get("/survivor/week")
async def week_card(request: Request, _: AuthenticatedUser = _ADMIN):
    """The panel's This Week's Games card: the current pick week's slate plus
    any earlier still-unsettled games (the ones the settle buttons exist for),
    with pick-count context."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        import time

        from bot_modules.survivor.logic import kickoff_ts, pick_week

        now = time.time()
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            week = pick_week(conn, season["season_year"], now)
            rows = conn.execute(
                "SELECT week, game_id, home, away, kickoff_utc, status, winner,"
                " favorite FROM nfl_games WHERE season_year = ? AND ("
                "  week = ? OR (status != 'final' AND week < ?)"
                ") ORDER BY week, kickoff_utc",
                (
                    season["season_year"],
                    week if week is not None else -1,
                    week if week is not None else 99,
                ),
            ).fetchall()
            alive = conn.execute(
                "SELECT COUNT(*) FROM survivor_players "
                "WHERE season_id = ? AND status = 'alive'",
                (season["id"],),
            ).fetchone()[0]
            picked = conn.execute(
                "SELECT COUNT(DISTINCT p.user_id) FROM survivor_picks p "
                "JOIN survivor_players pl ON pl.season_id = p.season_id "
                " AND pl.user_id = p.user_id "
                "WHERE p.season_id = ? AND p.week = ? AND pl.status = 'alive'",
                (season["id"], week if week is not None else -1),
            ).fetchone()[0]
            games = [
                {
                    "week": int(r["week"]),
                    "game_id": r["game_id"],
                    "home": r["home"],
                    "away": r["away"],
                    "kickoff_ts": kickoff_ts(r["kickoff_utc"]),
                    "status": r["status"],
                    "winner": r["winner"],
                    "favorite": r["favorite"],
                    "kicked": kickoff_ts(r["kickoff_utc"]) <= now,
                }
                for r in rows
            ]
        return week, games, int(alive), int(picked)

    week, games, alive, picked = await _service_call(run_query(_q))
    return {"week": week, "games": games, "alive": alive, "picked": picked}


@router.post("/survivor/settle")
async def settle_game(
    request: Request, body: SettleBody, user: AuthenticatedUser = _ADMIN
):
    """The panel's manual settle: record (or correct) a result by hand and
    re-grade every live season sharing the schedule. Grading is derived, so
    a correction unwinds strikes and resurrects the wrongly dead."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    outcome = body.outcome.strip().upper()

    def _q():
        from bot_modules.survivor.settle import manual_settle

        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            # nfl_games is league truth shared by every guild — re-grade all
            # live seasons on this schedule, not just the caller's.
            guilds = conn.execute(
                "SELECT DISTINCT guild_id FROM survivor_seasons "
                "WHERE status != 'complete'"
            ).fetchall()
            live = [
                s
                for r in guilds
                if (s := svc.get_active_season(conn, int(r["guild_id"])))
            ]
            try:
                out = manual_settle(
                    conn, season["season_year"], body.game_id, outcome, live
                )
            except ValueError as exc:
                raise svc.SeasonError(str(exc)) from exc
            write_audit(
                conn, guild_id=guild_id, action="survivor_manual_settle",
                actor_id=int(user.user_id),
                extra={
                    "game_id": body.game_id, "outcome": outcome,
                    "old_winner": out["old_winner"],
                    "old_status": out["old_status"], "via": "web",
                },
            )
            conn.commit()
        return out, season["id"]

    out, season_id = await _service_call(run_query(_q))
    # Standings changed — the channel panel's line should track (self-review
    # 2026-08-18). Best-effort, like every panel touch.
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        from bot_modules.survivor.views import refresh_panel

        await refresh_panel(bot, ctx.db_path, season_id)
    correction = out["old_winner"] is not None and out["old_winner"] != outcome
    await _mirror_mod_log(
        ctx, guild_id,
        action="game settled by hand",
        summary=f"`{body.game_id}` → **{outcome}**"
        + (f" (was {out['old_winner']})" if correction else ""),
        user=user,
    )
    return {
        "ok": True,
        "old_winner": out["old_winner"],
        "correction": correction,
        "changes": {
            str(season_id): {
                "graded": r.graded, "voided": r.voided,
                "recomputed": [str(u) for u in r.recomputed],
            }
            for season_id, r in out["reports"].items()
        },
    }


@router.get("/survivor/reckoning-preview")
async def reckoning_preview(request: Request, _: AuthenticatedUser = _ADMIN):
    """Render the next Reckoning exactly as Tuesday will post it (rotation
    is deterministic), without posting, mutating, or marking anything —
    leaver detection is deliberately skipped here."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        import time

        from bot_modules.survivor import reckoning as reck

        now = time.time()
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            week = reck.next_reckoning_week(conn, season, now)
            pending = week is None
            if pending:
                # Nothing reckonable yet: preview the upcoming week's state
                # anyway so the button always shows something honest.
                week = int(season["config"].get("last_reckoned_week") or 0) + 1
            data = reck.build_reckoning_data(conn, season, week, now)
            from bot_modules.services.economy_service import load_econ_settings

            settings = load_econ_settings(conn, guild_id)
        return season, data, pending, settings

    season, data, pending, settings = await _service_call(run_query(_q))

    from bot_modules.survivor.reckoning import build_reckoning_embed

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def name_of(user_id: int) -> str:
        member = guild.get_member(user_id) if guild else None
        return member.display_name if member else f"soul {user_id}"

    embed = build_reckoning_embed(
        data, name_of, season_name=season["name"], settings=settings,
    )
    return {
        "pending": pending,
        "week": data["week"],
        "title": embed.title,
        "description": embed.description,
        "fields": [
            {"name": f.name, "value": f.value} for f in embed.fields
        ],
    }


# ── simulator (synthetic seasons only) + force-run tasks ──────────────


class SimScheduleBody(BaseModel):
    weeks: int = Field(ge=1, le=18)
    minutes_per_week: int = Field(ge=2, le=1440)


class SimSettleBody(BaseModel):
    mode: str = Field(pattern="^(chalk|random|upset)$")


@router.post("/survivor/sim/schedule")
async def sim_schedule(
    request: Request, body: SimScheduleBody, user: AuthenticatedUser = _ADMIN
):
    """Lay down a compressed synthetic schedule. Refuses real season years —
    the rig can never touch a real schedule."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        import time

        from bot_modules.survivor.sim import SimError, generate_schedule

        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            try:
                created = generate_schedule(
                    conn, season, weeks=body.weeks,
                    minutes_per_week=body.minutes_per_week, now=time.time(),
                )
            except SimError as exc:
                raise svc.SeasonError(str(exc)) from exc
            write_audit(
                conn, guild_id=guild_id, action="survivor_sim_schedule",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"], "weeks": body.weeks,
                       "minutes_per_week": body.minutes_per_week, "via": "web"},
            )
            conn.commit()
        return season, created

    season, created = await _service_call(run_query(_q))
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        from bot_modules.survivor.views import refresh_panel

        await refresh_panel(bot, ctx.db_path, season["id"])
    return {"ok": True, "games": created}


@router.post("/survivor/sim/settle")
async def sim_settle(
    request: Request, body: SimSettleBody, user: AuthenticatedUser = _ADMIN
):
    """Settle every kicked synthetic game through the real pipeline."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        import time

        from bot_modules.survivor.sim import SimError, settle_kicked

        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            try:
                settled = settle_kicked(
                    conn, season, mode=body.mode, now=time.time(),
                    live_seasons=[season],
                )
            except SimError as exc:
                raise svc.SeasonError(str(exc)) from exc
            write_audit(
                conn, guild_id=guild_id, action="survivor_sim_settle",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"], "mode": body.mode,
                       "settled": len(settled), "via": "web"},
            )
            conn.commit()
        return season, settled

    season, settled = await _service_call(run_query(_q))
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        from bot_modules.survivor.views import refresh_panel

        await refresh_panel(bot, ctx.db_path, season["id"])
    return {
        "ok": True,
        "settled": [{"game_id": g, "outcome": o} for g, o in settled],
    }


@router.post("/survivor/tasks/run")
async def run_tasks_now(request: Request, user: AuthenticatedUser = _ADMIN):
    """Force the weekly tasks past their clock gates — the Reckoning if a
    week is reckonable, the panel repost, last call. Once-per-week state
    still holds, so this can never double-post; audited like everything."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Bot offline.")

    def _audit():
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id, action="survivor_tasks_run",
                actor_id=int(user.user_id), extra={"via": "web"},
            )
            conn.commit()

    await run_query(_audit)
    import time

    from bot_modules.survivor.tasks import run_weekly_tasks

    report = await run_weekly_tasks(
        bot, ctx.db_path, time.time(), force=True, guild_id=guild_id
    )
    # Scoped to this guild: a forced run is an admin acting on their own
    # server, and the unscoped call dragged every other guild's season past
    # its clock gates too (2026-08-18).
    fired = [task for row in report for task in row["fired"]]
    await _mirror_mod_log(
        ctx, guild_id, action="weekly tasks forced",
        summary=(
            f"clock gates bypassed; posted {', '.join(fired)}"
            if fired else "clock gates bypassed; nothing was due"
        ),
        user=user,
    )
    return {"ok": True, "report": report}


# ── weekly clock ──────────────────────────────────────────────────────

# Task key → (label, weekday, config hour key, config last-fired-week key).
# Order is the week's own order: Wednesday opens it, Saturday nudges,
# Tuesday closes it.
WEEKLY_TASKS: dict[str, tuple[str, int, str, str]] = {
    "slate": ("Slate post", 2, "slate_hour", "last_slate_week"),
    "lastcall": ("Last call", 5, "lastcall_hour", "last_lastcall_week"),
    "reckoning": ("The Reckoning", 1, "reckoning_hour", "last_reckoned_week"),
}

# Only the two idempotent posts can be re-armed from the panel. The
# Reckoning pays weekly-win coins in the transaction that marks the week
# reckoned (spec §5), so resetting *its* week would pay everyone twice.
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
    tick (``due``: the tasks module's own due decisions), and otherwise the
    next guild-local moment its gate opens. This is the operator's only view
    of the clock on a real season — the force-run button lives on the
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


@router.get("/survivor/clock")
async def weekly_clock(request: Request, _: AuthenticatedUser = _ADMIN):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        import time

        from bot_modules.core.db_utils import get_tz_offset_hours
        from bot_modules.survivor import tasks
        from bot_modules.survivor.logic import pick_week

        now = time.time()
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            offset = get_tz_offset_hours(conn, guild_id)
            week = pick_week(conn, season["season_year"], now)
            due = {
                "slate": tasks.slate_due(conn, season, now, offset),
                "lastcall": tasks.lastcall_due(conn, season, now, offset),
                "reckoning": tasks.reckoning_due(conn, season, now, offset),
            }
        return weekly_clock_rows(
            season["config"], now, offset, week=week, due=due,
        ), week, offset

    rows, week, offset = await _service_call(run_query(_q))
    return {"week": week, "offset_hours": offset, "tasks": rows}


@router.post("/survivor/tasks/{task}/reset")
async def reset_weekly_task(
    request: Request, task: str, user: AuthenticatedUser = _ADMIN
):
    """Re-arm one weekly post for the current pick week: the slate or the
    last call fires again at its next gate (or on the next tick if the gate
    is already open). The Reckoning is refused — it pays coins."""
    if task not in RESETTABLE_TASKS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Only the slate and the last call can be re-armed.",
        )
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    label, _dow, _hour_key, fired_key = WEEKLY_TASKS[task]

    def _q():
        import time

        from bot_modules.survivor.logic import pick_week

        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            week = pick_week(conn, season["season_year"], time.time())
            before = int(season["config"].get(fired_key) or 0)
            if week is None or before < week:
                raise svc.SeasonError(
                    f"{label} hasn't fired for the current week — nothing to reset."
                )
            after = max(week - 1, 0)
            svc.update_config(conn, season["id"], {fired_key: after})
            write_audit(
                conn, guild_id=guild_id, action="survivor_task_reset",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"], "task": task, "week": week,
                       "was": before, "via": "web"},
            )
            conn.commit()
        return week, before, after

    week, before, after = await _service_call(run_query(_q))
    await _mirror_mod_log(
        ctx, guild_id, action="weekly task re-armed",
        summary=f"{label} will fire again for week {week} "
        f"(last-fired week {before} → {after})",
        user=user,
    )
    return {"ok": True, "task": task, "week": week, "fired_week": after}


# ── announcement ──────────────────────────────────────────────────────


@router.post("/survivor/announcement")
async def post_announcement(request: Request, user: AuthenticatedUser = _ADMIN):
    """Post (or repost) THE channel panel — the one updating message
    (decided 2026-08-18): season pitch, current week's slate, standings
    line, join + pick buttons. Reposting retires the previous copy; weekly
    reposts with the ping are the Wednesday task's job. Never pinned: the
    panel is sticky, and a pin lasted only until the next chat message
    (2026-09-02)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season — create one first.")
            if not int(season["config"]["channel_id"] or 0):
                raise svc.SeasonError(
                    "Set the Survivor channel in Wiring first."
                )
        return season

    season = await _service_call(run_query(_q))

    bot = getattr(ctx, "bot", None)
    if bot is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Bot offline."
        )

    # The panel goes to the configured Survivor channel, and it re-sticks under
    # the bot's own posts — so anything else already holding that channel's
    # bottom is a collision worth naming before the panel lands on top of it.
    warning = await sticky_conflict(
        ctx,
        guild_id,
        int(season["config"]["channel_id"] or 0),
        excluding=("survivor", "survivor-pending"),
    )

    from bot_modules.survivor.views import PanelError, repost_panel

    try:
        message, retired = await repost_panel(
            ctx.bot, ctx.db_path, season["id"]
        )
    except PanelError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)
        ) from exc

    def _audit():
        with ctx.open_db() as conn:
            write_audit(
                conn, guild_id=guild_id, action="survivor_announcement_post",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"], "message_id": str(message.id),
                       "retired_previous": retired, "via": "web"},
            )
            conn.commit()

    await run_query(_audit)
    await _mirror_mod_log(
        ctx, guild_id,
        action="panel posted",
        summary=f"in <#{message.channel.id}>"
        + (" · previous copy retired" if retired else ""),
        user=user,
    )
    return {
        "ok": True,
        "message_id": str(message.id),
        "retired_previous": retired,
        "warning": warning,
    }


# ── config ────────────────────────────────────────────────────────────


@router.put("/survivor/config")
async def update_config(
    request: Request, body: dict, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    coerced = _coerce_config_body(body)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season — create one first.")
            merged = svc.update_config(conn, season["id"], coerced)
            write_audit(
                conn, guild_id=guild_id, action="survivor_config_update",
                actor_id=int(user.user_id),
                extra={"season_id": season["id"],
                       "keys": sorted(coerced), "via": "web"},
            )
            conn.commit()
        return merged

    merged = await _service_call(run_query(_q))
    await _mirror_mod_log(
        ctx,
        guild_id,
        action="config updated",
        summary=f"changed: {', '.join(sorted(coerced)) or '(nothing)'}",
        user=user,
    )
    return {"config": _config_json(merged)}


# ── roster ────────────────────────────────────────────────────────────


@router.post("/survivor/player/{user_id}/eliminate")
async def eliminate_player(
    request: Request,
    user_id: int,
    body: EliminateBody,
    user: AuthenticatedUser = _ADMIN,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            done = svc.eliminate_player(conn, season["id"], user_id, body.week)
            if done:
                write_audit(
                    conn, guild_id=guild_id, action="survivor_eliminate",
                    actor_id=int(user.user_id), target_id=user_id,
                    extra={"season_id": season["id"], "week": body.week,
                           "via": "web"},
                )
            conn.commit()
        return done, season

    done, season = await _service_call(run_query(_q))
    if not done:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That member isn't alive in this season."
        )
    note = await _swap_member_roles(
        ctx, guild_id, season["config"], user_id, to_ghost=True
    )
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        from bot_modules.survivor.views import refresh_panel

        await refresh_panel(bot, ctx.db_path, season["id"])
    await _mirror_mod_log(
        ctx, guild_id,
        action="player eliminated",
        summary=f"<@{user_id}> marked dead in week {body.week}"
        + (f" ({note})" if note else ""),
        user=user,
    )
    return {"ok": True, "role_note": note}


@router.post("/survivor/player/{user_id}/revive")
async def revive_player(
    request: Request, user_id: int, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            season = svc.get_active_season(conn, guild_id)
            if season is None:
                raise svc.SeasonError("No live season.")
            done = svc.revive_player(conn, season["id"], user_id)
            if done:
                write_audit(
                    conn, guild_id=guild_id, action="survivor_revive",
                    actor_id=int(user.user_id), target_id=user_id,
                    extra={"season_id": season["id"], "via": "web"},
                )
            conn.commit()
        return done, season

    done, season = await _service_call(run_query(_q))
    if not done:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That member isn't a ghost in this season."
        )
    note = await _swap_member_roles(
        ctx, guild_id, season["config"], user_id, to_ghost=False
    )
    bot = getattr(ctx, "bot", None)
    if bot is not None:
        from bot_modules.survivor.views import refresh_panel

        await refresh_panel(bot, ctx.db_path, season["id"])
    await _mirror_mod_log(
        ctx, guild_id,
        action="player revived",
        summary=f"<@{user_id}> walks again" + (f" ({note})" if note else ""),
        user=user,
    )
    return {"ok": True, "role_note": note}
