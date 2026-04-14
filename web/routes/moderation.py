"""Moderation endpoints — jails, tickets, warnings, audit log."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request

from services.message_store import get_known_users_bulk
from services.moderation import (
    claim_ticket,
    close_ticket,
    create_jail,
    create_warning,
    escalate_ticket,
    fmt_duration,
    get_transcript,
    parse_duration,
    reopen_ticket,
    write_audit,
)
from web.auth import AuthenticatedUser
from web.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web.schemas import (
    AuditLogResponse,
    JailsResponse,
    ModerationStatsResponse,
    PolicyTicketsResponse,
    TicketActionResult,
    TicketDetailSchema,
    TicketJailBody,
    TicketNoteBody,
    TicketReasonBody,
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
    if guild:
        for t in result["tickets"]:
            cid = t.get("channel_id")
            if not cid:
                continue
            try:
                ch = guild.get_channel(int(cid))
            except (TypeError, ValueError):
                ch = None
            if ch is not None:
                t["channel_name"] = ch.name
    return result


@router.get("/moderation/tickets/{ticket_id}", response_model=TicketDetailSchema)
async def get_ticket_detail(
    request: Request,
    ticket_id: int,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT * FROM tickets WHERE guild_id = ? AND id = ?",
                (guild_id, ticket_id),
            ).fetchone()
            if row is None:
                return None
            ticket = {
                "id": row["id"],
                "user_id": str(row["user_id"]),
                "description": row["description"],
                "status": row["status"],
                "claimer_id": str(row["claimer_id"]) if row["claimer_id"] else None,
                "escalated": bool(row["escalated"]),
                "created_at": row["created_at"],
                "closed_at": row["closed_at"],
                "closed_by": str(row["closed_by"]) if row["closed_by"] else None,
                "close_reason": row["close_reason"],
                "channel_id": str(row["channel_id"]) if row["channel_id"] else "",
            }
            user_id_int = int(row["user_id"])
            warn_active = conn.execute(
                "SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ? AND revoked = 0",
                (guild_id, user_id_int),
            ).fetchone()[0]
            jail_total = conn.execute(
                "SELECT COUNT(*) FROM jails WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id_int),
            ).fetchone()[0]
            warn_rows = conn.execute(
                "SELECT id, reason, moderator_id, created_at, revoked "
                "FROM warnings WHERE guild_id = ? AND user_id = ? "
                "ORDER BY created_at DESC LIMIT 20",
                (guild_id, user_id_int),
            ).fetchall()
            jail_rows = conn.execute(
                "SELECT id, reason, moderator_id, created_at, expires_at "
                "FROM jails WHERE guild_id = ? AND user_id = ? "
                "ORDER BY created_at DESC LIMIT 20",
                (guild_id, user_id_int),
            ).fetchall()
            history: list[dict] = []
            for w in warn_rows:
                body = w["reason"] or ("Warning revoked" if w["revoked"] else "Warning issued")
                if w["revoked"]:
                    body = f"{body} (revoked)"
                history.append(
                    {
                        "kind": "warn",
                        "body": body,
                        "actor_id": str(w["moderator_id"]) if w["moderator_id"] else "",
                        "actor_name": "",
                        "date": w["created_at"],
                    }
                )
            for j in jail_rows:
                dur_s = (
                    int(j["expires_at"] - j["created_at"])
                    if j["expires_at"] and j["created_at"]
                    else 0
                )
                dur_label = fmt_duration(dur_s) if dur_s > 0 else "indefinite"
                body = f"{dur_label}"
                if j["reason"]:
                    body = f"{dur_label} · {j['reason']}"
                history.append(
                    {
                        "kind": "jail",
                        "body": body,
                        "actor_id": str(j["moderator_id"]) if j["moderator_id"] else "",
                        "actor_name": "",
                        "date": j["created_at"],
                    }
                )
            history.sort(key=lambda e: e["date"], reverse=True)
            history = history[:20]
            return {
                "ticket": ticket,
                "subject": {
                    "user_id": str(user_id_int),
                    "user_name": "",
                    "joined_at": None,
                    "warn_count_active": warn_active,
                    "jail_count_total": jail_total,
                },
                "history": history,
            }

    data = await run_query(_q)
    if data is None:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket = data["ticket"]
    subject = data["subject"]
    history = data["history"]

    _resolve_names(
        ctx,
        guild,
        [ticket],
        ("user_id", "user_name"),
        ("claimer_id", "claimer_name"),
        ("closed_by", "closer_name"),
    )
    subject["user_name"] = ticket.get("user_name", "")

    if guild:
        cid = ticket.get("channel_id")
        if cid:
            try:
                ch = guild.get_channel(int(cid))
            except (TypeError, ValueError):
                ch = None
            if ch is not None:
                ticket["channel_name"] = ch.name
        member = guild.get_member(int(subject["user_id"]))
        if member and member.joined_at:
            subject["joined_at"] = member.joined_at.timestamp()

    _resolve_names(ctx, guild, history, ("actor_id", "actor_name"))

    return {**ticket, "subject": subject, "history": history}


# ── Ticket mutations ─────────────────────────────────────────────────────


def _fetch_ticket_row(conn, guild_id: int, ticket_id: int):
    row = conn.execute(
        "SELECT * FROM tickets WHERE guild_id = ? AND id = ?",
        (guild_id, ticket_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return row


@router.post(
    "/moderation/tickets/{ticket_id}/claim",
    response_model=TicketActionResult,
)
async def ticket_claim(
    request: Request,
    ticket_id: int,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            if row["status"] != "open":
                raise HTTPException(
                    status_code=409, detail="Only open tickets can be claimed"
                )
            claim_ticket(conn, ticket_id, user.user_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_claim",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": "open",
                "message": "Ticket claimed",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/close",
    response_model=TicketActionResult,
)
async def ticket_close(
    request: Request,
    ticket_id: int,
    body: TicketReasonBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip() or "Closed from dashboard"

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            if row["status"] != "open":
                raise HTTPException(
                    status_code=409, detail="Only open tickets can be closed"
                )
            close_ticket(conn, ticket_id, closed_by=user.user_id, reason=reason)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_close",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id, "reason": reason},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": "closed",
                "message": "Ticket closed",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/reopen",
    response_model=TicketActionResult,
)
async def ticket_reopen(
    request: Request,
    ticket_id: int,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            if row["status"] != "closed":
                raise HTTPException(
                    status_code=409, detail="Only closed tickets can be reopened"
                )
            reopen_ticket(conn, ticket_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_reopen",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": "open",
                "message": "Ticket reopened",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/dismiss",
    response_model=TicketActionResult,
)
async def ticket_dismiss(
    request: Request,
    ticket_id: int,
    body: TicketReasonBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    tail = (body.reason or "").strip()
    reason = f"Dismissed: {tail}" if tail else "Dismissed"

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            if row["status"] != "open":
                raise HTTPException(
                    status_code=409, detail="Only open tickets can be dismissed"
                )
            close_ticket(conn, ticket_id, closed_by=user.user_id, reason=reason)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_dismiss",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id, "reason": reason},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": "closed",
                "message": "Ticket dismissed",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/escalate",
    response_model=TicketActionResult,
)
async def ticket_escalate(
    request: Request,
    ticket_id: int,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            if row["status"] != "open":
                raise HTTPException(
                    status_code=409, detail="Only open tickets can be escalated"
                )
            if row["escalated"]:
                return {
                    "ok": True,
                    "ticket_id": ticket_id,
                    "status": row["status"],
                    "message": "Already escalated",
                }
            escalate_ticket(conn, ticket_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_escalate",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": "open",
                "message": "Ticket escalated",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/warn",
    response_model=TicketActionResult,
)
async def ticket_warn(
    request: Request,
    ticket_id: int,
    body: TicketReasonBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A reason is required for a warning")

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            subject_id = int(row["user_id"])
            warning_id = create_warning(
                conn,
                guild_id=guild_id,
                user_id=subject_id,
                moderator_id=user.user_id,
                reason=reason,
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_warn",
                actor_id=user.user_id,
                target_id=subject_id,
                extra={
                    "ticket_id": ticket_id,
                    "warning_id": warning_id,
                    "reason": reason,
                },
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": row["status"],
                "message": f"Warning #{warning_id} issued",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/jail",
    response_model=TicketActionResult,
)
async def ticket_jail(
    request: Request,
    ticket_id: int,
    body: TicketJailBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip()
    duration_s = parse_duration(body.duration or "")
    if duration_s is None:
        raise HTTPException(
            status_code=400,
            detail="Could not parse duration (use e.g. '30m', '24h', '7d')",
        )

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            subject_id = int(row["user_id"])
            jail_id = create_jail(
                conn,
                guild_id=guild_id,
                user_id=subject_id,
                moderator_id=user.user_id,
                reason=reason,
                stored_roles=[],
                channel_id=0,
                duration_seconds=duration_s,
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_jail",
                actor_id=user.user_id,
                target_id=subject_id,
                extra={
                    "ticket_id": ticket_id,
                    "jail_id": jail_id,
                    "duration_seconds": duration_s,
                    "reason": reason,
                    "dashboard_only": True,
                },
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": row["status"],
                "message": f"Jail #{jail_id} recorded ({fmt_duration(duration_s)})",
            }

    return await run_query(_q)


@router.post(
    "/moderation/tickets/{ticket_id}/note",
    response_model=TicketActionResult,
)
async def ticket_note(
    request: Request,
    ticket_id: int,
    body: TicketNoteBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    note_body = (body.body or "").strip()
    if not note_body:
        raise HTTPException(status_code=400, detail="Note body is required")

    def _q():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_note",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"ticket_id": ticket_id, "body": note_body},
            )
            return {
                "ok": True,
                "ticket_id": ticket_id,
                "status": row["status"],
                "message": "Note added",
            }

    return await run_query(_q)


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
