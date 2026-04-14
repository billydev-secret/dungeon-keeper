"""Moderation endpoints — jails, tickets, warnings, audit log."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from services.message_store import get_known_users_bulk
from services.moderation import get_transcript
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web.schemas import (
    AuditLogResponse,
    JailsResponse,
    ModerationStatsResponse,
    PolicyTicketsResponse,
    TicketsResponse,
    TranscriptResponse,
    WarningsResponse,
)

router = APIRouter()


def _resolve_names(ctx, guild, entries, *id_name_pairs):
    if not entries:
        return
    _guild_id = guild.id if guild else 0
    unresolved: set[int] = set()
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            uid = entry.get(id_field)
            if uid:
                if guild:
                    member = guild.get_member(int(uid))
                    if member:
                        entry[name_field] = member.display_name
                        continue
                unresolved.add(int(uid))
    if unresolved:
        with ctx.open_db() as conn:
            known = get_known_users_bulk(conn, _guild_id, list(unresolved))
        for entry in entries:
            for id_field, name_field in id_name_pairs:
                if entry.get(name_field):
                    continue
                uid = entry.get(id_field)
                if uid and int(uid) in known:
                    entry[name_field] = known[int(uid)]
    for entry in entries:
        for id_field, name_field in id_name_pairs:
            if entry.get(name_field):
                continue
            uid = entry.get(id_field)
            if uid:
                entry[name_field] = f"User {uid}"


# ── Summary stats ─────────────────────────────────────────────────────────


@router.get("/moderation/stats", response_model=ModerationStatsResponse)
async def moderation_stats(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    one_week_ago = time.time() - 7 * 86400

    def _q():
        with ctx.open_db() as conn:

            def r(sql, *a):
                return conn.execute(sql, a).fetchone()[0]

            return {
                "active_jails": r(
                    "SELECT COUNT(*) FROM jails WHERE guild_id = ? AND status = 'active'",
                    guild_id,
                ),
                "total_jails": r(
                    "SELECT COUNT(*) FROM jails WHERE guild_id = ?", guild_id
                ),
                "open_tickets": r(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'open'",
                    guild_id,
                ),
                "closed_tickets": r(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'closed'",
                    guild_id,
                ),
                "total_tickets": r(
                    "SELECT COUNT(*) FROM tickets WHERE guild_id = ?", guild_id
                ),
                "active_warnings": r(
                    "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND revoked = 0",
                    guild_id,
                ),
                "total_warnings": r(
                    "SELECT COUNT(*) FROM warnings WHERE guild_id = ?", guild_id
                ),
                "recent_actions": r(
                    "SELECT COUNT(*) FROM audit_log WHERE guild_id = ? AND created_at >= ?",
                    guild_id,
                    one_week_ago,
                ),
            }

    return await run_query(_q)


# ── Jails ─────────────────────────────────────────────────────────────────


@router.get("/moderation/jails", response_model=JailsResponse)
async def list_jails(
    request: Request,
    status: str | None = None,
    user_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if status:
                clauses.append("status = ?")
                params.append(status)
            if user_id:
                clauses.append("user_id = ?")
                params.append(int(user_id))
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM jails WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()
            jails = []
            for r in rows:
                jails.append(
                    {
                        "id": r["id"],
                        "user_id": str(r["user_id"]),
                        "moderator_id": str(r["moderator_id"]),
                        "reason": r["reason"],
                        "status": r["status"],
                        "created_at": r["created_at"],
                        "expires_at": r["expires_at"],
                        "released_at": r["released_at"],
                        "release_reason": r["release_reason"],
                        "channel_id": str(r["channel_id"]) if r["channel_id"] else "",
                    }
                )
            active = sum(1 for j in jails if j["status"] == "active")
            return {"active_count": active, "total_count": len(jails), "jails": jails}

    result = await run_query(_q)
    _resolve_names(
        ctx,
        guild,
        result["jails"],
        ("user_id", "user_name"),
        ("moderator_id", "moderator_name"),
    )
    return result


# ── Tickets ───────────────────────────────────────────────────────────────


@router.get("/moderation/tickets", response_model=TicketsResponse)
async def list_tickets(
    request: Request,
    status: str | None = None,
    user_id: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if status == "closed":
                clauses.append("status IN ('closed', 'deleted')")
            elif status:
                clauses.append("status = ?")
                params.append(status)
            else:
                clauses.append("status != 'deleted'")
            if user_id:
                clauses.append("user_id = ?")
                params.append(int(user_id))
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM tickets WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()
            tickets = []
            for r in rows:
                tickets.append(
                    {
                        "id": r["id"],
                        "user_id": str(r["user_id"]),
                        "description": r["description"],
                        "status": r["status"],
                        "claimer_id": str(r["claimer_id"]) if r["claimer_id"] else None,
                        "escalated": bool(r["escalated"]),
                        "created_at": r["created_at"],
                        "closed_at": r["closed_at"],
                        "closed_by": str(r["closed_by"]) if r["closed_by"] else None,
                        "close_reason": r["close_reason"],
                        "channel_id": str(r["channel_id"]) if r["channel_id"] else "",
                    }
                )
            open_c = sum(1 for t in tickets if t["status"] == "open")
            closed_c = sum(1 for t in tickets if t["status"] == "closed")
            return {
                "open_count": open_c,
                "closed_count": closed_c,
                "total_count": len(tickets),
                "tickets": tickets,
            }

    result = await run_query(_q)
    _resolve_names(
        ctx,
        guild,
        result["tickets"],
        ("user_id", "user_name"),
        ("claimer_id", "claimer_name"),
        ("closed_by", "closer_name"),
    )
    return result


# ── Warnings ──────────────────────────────────────────────────────────────


@router.get("/moderation/warnings", response_model=WarningsResponse)
async def list_warnings(
    request: Request,
    user_id: str | None = None,
    active_only: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if user_id:
                clauses.append("user_id = ?")
                params.append(int(user_id))
            if active_only:
                clauses.append("revoked = 0")
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM warnings WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()
            warnings = []
            for r in rows:
                warnings.append(
                    {
                        "id": r["id"],
                        "user_id": str(r["user_id"]),
                        "moderator_id": str(r["moderator_id"]),
                        "reason": r["reason"],
                        "created_at": r["created_at"],
                        "revoked": bool(r["revoked"]),
                        "revoked_at": r["revoked_at"],
                        "revoked_by": str(r["revoked_by"]) if r["revoked_by"] else None,
                        "revoke_reason": r["revoke_reason"],
                    }
                )
            active = sum(1 for w in warnings if not w["revoked"])
            return {
                "active_count": active,
                "total_count": len(warnings),
                "warnings": warnings,
            }

    result = await run_query(_q)
    _resolve_names(
        ctx,
        guild,
        result["warnings"],
        ("user_id", "user_name"),
        ("moderator_id", "moderator_name"),
        ("revoked_by", "revoker_name"),
    )
    return result


# ── Policy Tickets ────────────────────────────────────────────────────────


@router.get("/moderation/policy-tickets", response_model=PolicyTicketsResponse)
async def list_policy_tickets(
    request: Request,
    status: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if status:
                clauses.append("status = ?")
                params.append(status)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"SELECT * FROM policy_tickets WHERE {where} ORDER BY created_at DESC LIMIT 200",
                params,
            ).fetchall()
            tickets = []
            for r in rows:
                tickets.append(
                    {
                        "id": r["id"],
                        "creator_id": str(r["creator_id"]),
                        "title": r["title"],
                        "description": r["description"],
                        "status": r["status"],
                        "vote_text": r["vote_text"],
                        "channel_id": str(r["channel_id"]) if r["channel_id"] else "",
                        "created_at": r["created_at"],
                        "vote_started_at": r["vote_started_at"],
                        "vote_ended_at": r["vote_ended_at"],
                    }
                )
            open_c = sum(1 for t in tickets if t["status"] == "open")
            voting_c = sum(1 for t in tickets if t["status"] == "voting")
            closed_c = sum(1 for t in tickets if t["status"] == "closed")
            return {
                "open_count": open_c,
                "voting_count": voting_c,
                "closed_count": closed_c,
                "total_count": len(tickets),
                "policy_tickets": tickets,
            }

    result = await run_query(_q)
    _resolve_names(ctx, guild, result["policy_tickets"], ("creator_id", "creator_name"))
    return result


# ── Transcript ────────────────────────────────────────────────────────────

_VALID_RECORD_TYPES = ("ticket", "jail", "policy_ticket")


@router.get("/moderation/transcript", response_model=TranscriptResponse)
async def transcript(
    request: Request,
    record_type: str,
    record_id: int,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    if record_type not in _VALID_RECORD_TYPES:
        raise HTTPException(
            status_code=400, detail=f"Invalid record_type: {record_type}"
        )

    ctx = get_ctx(request)
    get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return {"transcript": get_transcript(conn, record_type, record_id)}

    return await run_query(_q)


# ── Audit log ─────────────────────────────────────────────────────────────


@router.get("/moderation/audit", response_model=AuditLogResponse)
async def audit_log(
    request: Request,
    limit: int = 50,
    action: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    limit = min(limit, 200)

    def _q():
        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if action:
                clauses.append("action = ?")
                params.append(action)
            where = " AND ".join(clauses)
            total = conn.execute(
                f"SELECT COUNT(*) FROM audit_log WHERE {where}",
                params,
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM audit_log WHERE {where} ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            entries = []
            for r in rows:
                entries.append(
                    {
                        "id": r["id"],
                        "action": r["action"],
                        "actor_id": str(r["actor_id"]),
                        "target_id": str(r["target_id"]) if r["target_id"] else None,
                        "extra": json.loads(r["extra"]) if r["extra"] else {},
                        "created_at": r["created_at"],
                    }
                )
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("actor_id", "actor_name"),
        ("target_id", "target_name"),
    )
    return result
