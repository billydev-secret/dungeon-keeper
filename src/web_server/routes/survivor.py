"""Survivor dashboard API — the feature's entire admin surface.

Survivor's configuration is dashboard-managed per the 2026-08-17 decision
(spec §3): season lifecycle, every §5 dial, the flavor corpus, and roster
eliminate/revive all live here, admin-gated. The only Discord-side admin
surface is the two live mod actions (`/survivor admin settle`,
`preview-reckoning`), which arrive with the settle engine in stage 4.

Every mutation mirrors to the DK mod-log channel (spec §3: "all admin
actions → DK mod-log"). Snowflakes go out as strings — JS ``Number`` can't
hold a full Discord id.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.services import survivor_espn as espn
from bot_modules.services import survivor_service as svc
from bot_modules.services.moderation import write_audit
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import mirror_admin_action_to_mod_log

log = logging.getLogger("web.survivor")

router = APIRouter()
_ADMIN = Depends(require_perms({"admin"}))

# The three roles the bot manages (spec §3.3), paired with the config key
# that stores each one's id.
MANAGED_ROLES = (
    ("role_survivor_id", "🏈 Survivor"),
    ("role_ghost_id", "👻 Ghost"),
    ("role_sole_survivor_id", "🏈 Sole Survivor"),
)

_ID_KEYS = frozenset(
    {"channel_id", "role_survivor_id", "role_ghost_id", "role_sole_survivor_id"}
)


class CreateSeasonBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    season_year: int = Field(ge=2020, le=2100)


class FlavorBody(BaseModel):
    category: str
    line: str = Field(min_length=1, max_length=500)


class FlavorUpdateBody(BaseModel):
    line: str | None = Field(default=None, min_length=1, max_length=500)
    active: bool | None = None


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
    """Best-effort Survivor↔Ghost swap for an admin eliminate/revive — the
    same swap the settle engine performs on a game death (spec §1.7), so the
    weekly role pings match the roster the panel shows. Returns a note when
    the swap was skipped or failed, for the mod-log mirror; never raises."""
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return "role swap skipped — bot offline"
    member = guild.get_member(user_id)
    if member is None:
        return "role swap skipped — member not in guild"
    survivor = guild.get_role(int(config.get("role_survivor_id") or 0))
    ghost = guild.get_role(int(config.get("role_ghost_id") or 0))
    add, remove = (ghost, survivor) if to_ghost else (survivor, ghost)
    if add is None and remove is None:
        return "role swap skipped — roles not configured"
    try:
        if remove is not None and remove in member.roles:
            await member.remove_roles(remove, reason="Survivor: web admin action")
        if add is not None and add not in member.roles:
            await member.add_roles(add, reason="Survivor: web admin action")
    except (discord.Forbidden, discord.HTTPException):
        log.exception("survivor: role swap failed for %s", user_id)
        return "role swap failed — check Manage Roles"
    return None


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
            flavor = svc.list_flavor(conn, guild_id, include_inactive=True)
            archived = [
                s for s in svc.list_seasons(conn, guild_id) if s["status"] == "complete"
            ]
        return season, players, flavor, archived

    season, players, flavor, archived = await run_query(_q)
    return {
        "season": _season_json(season) if season else None,
        "players": [
            {**p, "user_id": str(p["user_id"])} for p in players
        ],
        "flavor": flavor,
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
            seeded = svc.seed_default_flavor(conn, guild_id)
            # Durable audit row in the same transaction (the Discord mirror
            # is best-effort and log.txt is wiped every boot).
            write_audit(
                conn, guild_id=guild_id, action="survivor_season_create",
                actor_id=int(user.user_id),
                extra={"season_id": season_id, "name": body.name,
                       "year": body.season_year, "via": "web"},
            )
            conn.commit()
        return season_id, seeded

    season_id, seeded = await _service_call(run_query(_create))

    # Roles only after the season exists (spec §3.3): create any of the three
    # that are missing and store their ids in the season's config. Degraded
    # path — no bot, no guild, or no Manage Roles — leaves ids unset and
    # reports it; the panel surfaces the warning.
    role_ids: dict[str, int] = {}
    role_report: list[str] = []
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is not None:
        existing = {r.name: r for r in guild.roles}
        for key, role_name in MANAGED_ROLES:
            role = existing.get(role_name)
            if role is None:
                try:
                    role = await guild.create_role(
                        name=role_name, reason="Survivor season setup"
                    )
                    role_report.append(f"created {role_name}")
                except discord.Forbidden:
                    role_report.append(
                        f"couldn't create {role_name} — missing Manage Roles"
                    )
                    continue
                except discord.HTTPException:
                    log.exception("survivor: role create failed for %s", role_name)
                    role_report.append(f"couldn't create {role_name}")
                    continue
            role_ids[key] = role.id
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

    # Full-season schedule ingest (spec §4.2), best-effort like the roles:
    # 18 weeks of ESPN fetches, with failed weeks reported and healed by the
    # daily refresh once the polling loop (stage 4) is running.
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
        "flavor_seeded": seeded,
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


# ── flavor corpus ─────────────────────────────────────────────────────


@router.post("/survivor/flavor")
async def add_flavor(
    request: Request, body: FlavorBody, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            flavor_id = svc.add_flavor(conn, guild_id, body.category, body.line)
            conn.commit()
        return flavor_id

    flavor_id = await _service_call(run_query(_q))
    await _mirror_mod_log(
        ctx, guild_id,
        action="flavor added",
        summary=f"{body.category}: {body.line[:120]}",
        user=user,
    )
    return {"id": flavor_id}


@router.put("/survivor/flavor/{flavor_id}")
async def update_flavor(
    request: Request,
    flavor_id: int,
    body: FlavorUpdateBody,
    user: AuthenticatedUser = _ADMIN,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            changed = svc.update_flavor(
                conn, guild_id, flavor_id, line=body.line, active=body.active
            )
            conn.commit()
        return changed

    changed = await _service_call(run_query(_q))
    if not changed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such flavor line.")
    await _mirror_mod_log(
        ctx, guild_id, action="flavor updated", summary=f"line #{flavor_id}", user=user
    )
    return {"ok": True}


@router.delete("/survivor/flavor/{flavor_id}")
async def delete_flavor(
    request: Request, flavor_id: int, user: AuthenticatedUser = _ADMIN
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            deleted = svc.delete_flavor(conn, guild_id, flavor_id)
            conn.commit()
        return deleted

    deleted = await run_query(_q)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such flavor line.")
    await _mirror_mod_log(
        ctx, guild_id, action="flavor deleted", summary=f"line #{flavor_id}", user=user
    )
    return {"ok": True}


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
    await _mirror_mod_log(
        ctx, guild_id,
        action="player revived",
        summary=f"<@{user_id}> walks again" + (f" ({note})" if note else ""),
        user=user,
    )
    return {"ok": True, "role_note": note}
