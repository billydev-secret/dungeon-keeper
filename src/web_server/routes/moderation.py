"""Moderation endpoints — jails, tickets, warnings, audit log."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from web_server.helpers import resolve_names as _resolve_names
from bot_modules.jail.apply import apply_jail
from bot_modules.commands.jail_commands import _do_unjail
from bot_modules.services.moderation import (
    claim_ticket,
    close_ticket,
    create_warning,
    escalate_ticket,
    fmt_duration,
    get_active_warning_count,
    get_transcript,
    parse_duration,
    release_jail,
    reopen_ticket,
    revoke_warning,
    write_audit,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.schemas import (
    AuditLogResponse,
    ConfessionsAuditLogResponse,
    DMAuditLogResponse,
    JailsResponse,
    ModerationStatsResponse,
    PolicyTicketsResponse,
    SimpleActionResult,
    TicketActionResult,
    TicketDetailSchema,
    TicketJailBody,
    TicketNoteBody,
    TicketReasonBody,
    TicketsResponse,
    TranscriptResponse,
    WarningsResponse,
    WhisperAuditLogResponse,
)

router = APIRouter()

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
    await _resolve_names(
        ctx,
        guild,
        result["jails"],
        ("user_id", "user_name"),
        ("moderator_id", "moderator_name"),
    )
    return result


@router.post("/moderation/jails/{jail_id}/release", response_model=SimpleActionResult)
async def jail_release_route(
    request: Request,
    jail_id: int,
    body: TicketReasonBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Release a jail from the dashboard.

    Routes through the canonical :func:`_do_unjail` flow (role restore,
    transcript, channel cleanup, DM, audit) — same behavior as the
    ``/unjail`` slash command. If the member has already left the guild,
    the DB record is released directly since there is no role to remove.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip()

    def _lookup():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT * FROM jails WHERE id = ? AND guild_id = ?",
                (jail_id, guild_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Jail not found")
            return dict(row)

    jail = await run_query(_lookup)
    if jail["status"] != "active":
        raise HTTPException(status_code=409, detail="Jail is not active")

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status_code=503,
            detail="Bot is not connected to this guild — cannot release jail.",
        )
    moderator = guild.get_member(int(user.user_id))
    if moderator is None:
        raise HTTPException(
            status_code=403,
            detail="Your account isn't a member of this guild (cannot moderate).",
        )

    target = guild.get_member(int(jail["user_id"]))
    if target is not None:
        message = await _do_unjail(ctx, guild, target, reason=reason, actor=moderator)

        def _status_now():
            with ctx.open_db() as conn:
                r = conn.execute(
                    "SELECT status FROM jails WHERE id = ?", (jail_id,)
                ).fetchone()
                return r["status"] if r else "missing"

        if await run_query(_status_now) != "released":
            # _do_unjail reports failures as a status string, not an exception.
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    # Member left the guild — there's no role to remove; close out the record.
    def _release_record():
        with ctx.open_db() as conn:
            release_jail(
                conn,
                jail_id,
                reason=reason or "Released from dashboard (user left guild)",
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="jail_release",
                actor_id=user.user_id,
                target_id=int(jail["user_id"]),
                extra={"jail_id": jail_id, "reason": reason, "note": "user_left_guild"},
            )

    await run_query(_release_record)
    return {"ok": True, "message": "User has left the server — jail record marked released."}


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
    await _resolve_names(
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

    await _resolve_names(
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

    await _resolve_names(ctx, guild, history, ("actor_id", "actor_name"))

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
    """Apply a real moderation hold from the dashboard.

    Routes through the canonical :func:`apply_jail` flow so the user actually
    gets the Jailed role applied, a private jail channel created, and a DM
    notification — same behavior as the ``/jail`` slash command. Returns 503
    if the bot can't reach the live guild (without a live connection there's
    no way to apply the role).
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip()

    raw_duration = (body.duration or "").strip()
    duration_s = parse_duration(raw_duration) if raw_duration else None
    if raw_duration and duration_s is None:
        raise HTTPException(
            status_code=400,
            detail="Could not parse duration (use e.g. '30m', '24h', '7d')",
        )

    # Verify the ticket exists and resolve the subject before going to Discord.
    def _ticket_lookup():
        with ctx.open_db() as conn:
            row = _fetch_ticket_row(conn, guild_id, ticket_id)
            return int(row["user_id"]), row["status"]

    subject_id, ticket_status = await run_query(_ticket_lookup)

    # Resolve guild + members from the live bot cache. If the bot isn't
    # connected we refuse — the dashboard must not silently no-op when an
    # admin clicks "Jail user" expecting the role to apply.
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status_code=503,
            detail="Bot is not connected to this guild — cannot apply jail.",
        )

    target = guild.get_member(subject_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail="Target user is no longer a member of this guild.",
        )

    moderator = guild.get_member(int(user.user_id))
    if moderator is None:
        raise HTTPException(
            status_code=403,
            detail="Your account isn't a member of this guild (cannot moderate).",
        )

    result = await apply_jail(
        ctx,
        guild,
        target,
        moderator,
        reason=reason,
        duration_seconds=duration_s,
        source="dashboard",
        source_extra={"ticket_id": ticket_id},
    )

    if not result.ok:
        # Precondition rejections (bot/self/admin/mod/already_jailed) come
        # back as 409 since they're a conflict with the target's current
        # state. Permission failures are 500 because they're bot-config
        # issues for the operator to fix.
        client_errors = {
            "bot_target",
            "self_target",
            "admin_target",
            "mod_target",
            "already_jailed",
        }
        status_code = 409 if result.error_kind in client_errors else 500
        raise HTTPException(
            status_code=status_code,
            detail=result.error_message or "Could not apply jail.",
        )

    duration_text = fmt_duration(duration_s) if duration_s else "Indefinite"
    return {
        "ok": True,
        "ticket_id": ticket_id,
        "status": ticket_status,
        "message": f"Jail #{result.jail_id} applied ({duration_text})",
    }


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
    await _resolve_names(
        ctx,
        guild,
        result["warnings"],
        ("user_id", "user_name"),
        ("moderator_id", "moderator_name"),
        ("revoked_by", "revoker_name"),
    )
    return result


# ── Policy Tickets ────────────────────────────────────────────────────────


@router.post("/moderation/warnings/{warning_id}/revoke", response_model=SimpleActionResult)
async def warning_revoke_route(
    request: Request,
    warning_id: int,
    body: TicketReasonBody,
    user: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    """Revoke a warning from the dashboard — mirrors the /revokewarn command."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    reason = (body.reason or "").strip()

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT * FROM warnings WHERE id = ? AND guild_id = ?",
                (warning_id, guild_id),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Warning not found")
            if row["revoked"]:
                raise HTTPException(status_code=409, detail="Warning is already revoked")
            if not revoke_warning(
                conn, warning_id, revoked_by=user.user_id, reason=reason
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Couldn't revoke — it may have just been revoked by someone else.",
                )
            count = get_active_warning_count(conn, guild_id, int(row["user_id"]))
            write_audit(
                conn,
                guild_id=guild_id,
                action="warning_revoke",
                actor_id=user.user_id,
                target_id=int(row["user_id"]),
                extra={"warning_id": warning_id, "reason": reason, "count": count},
            )
            return {
                "ok": True,
                "message": f"Warning #{warning_id} revoked — {count} active warning(s) remain.",
            }

    return await run_query(_q)


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
    await _resolve_names(ctx, guild, result["policy_tickets"], ("creator_id", "creator_name"))
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
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return {
                "transcript": get_transcript(conn, record_type, record_id, guild_id)
            }

    return await run_query(_q)


# ── Audit log ─────────────────────────────────────────────────────────────

# Cache audit_log COUNT(*) per (guild_id, action) for 60s — the table grows
# constantly and the panel polls; recomputing total on every poll is wasteful.
_AUDIT_TOTAL_CACHE: dict[tuple[int, str | None], tuple[float, int]] = {}
_AUDIT_TOTAL_TTL = 60.0


@router.get("/moderation/audit", response_model=AuditLogResponse)
async def audit_log(
    request: Request,
    limit: int = 50,
    action: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    limit = min(limit, 200)

    def _q():
        import time as _t

        with ctx.open_db() as conn:
            clauses = ["guild_id = ?"]
            params: list = [guild_id]
            if action:
                clauses.append("action = ?")
                params.append(action)
            where = " AND ".join(clauses)

            cache_key = (guild_id, action)
            now = _t.monotonic()
            cached = _AUDIT_TOTAL_CACHE.get(cache_key)
            if cached and now - cached[0] < _AUDIT_TOTAL_TTL:
                total = cached[1]
            else:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM audit_log WHERE {where}",
                    params,
                ).fetchone()[0]
                _AUDIT_TOTAL_CACHE[cache_key] = (now, total)

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
    await _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("actor_id", "actor_name"),
        ("target_id", "target_name"),
    )
    return result


@router.get("/moderation/dm-audit", response_model=DMAuditLogResponse)
async def dm_audit_log(
    request: Request,
    limit: int = 50,
    action: str | None = None,
    req_type: str | None = Query(None, alias="type"),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
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
            if req_type:
                clauses.append("notes = ?")
                params.append(f"type={req_type}")
            where = " AND ".join(clauses)
            total = conn.execute(
                f"SELECT COUNT(*) FROM dm_audit_log WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM dm_audit_log WHERE {where} ORDER BY timestamp DESC LIMIT ?",
                params + [limit],
            ).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "action": r["action"],
                    "actor_id": str(r["actor_id"]) if r["actor_id"] else None,
                    "user_a_id": str(r["user_a_id"]) if r["user_a_id"] else None,
                    "user_b_id": str(r["user_b_id"]) if r["user_b_id"] else None,
                    "notes": r["notes"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    await _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("actor_id", "actor_name"),
        ("user_a_id", "user_a_name"),
        ("user_b_id", "user_b_name"),
    )
    return result


@router.get("/moderation/whisper-audit", response_model=WhisperAuditLogResponse)
async def whisper_audit_log(
    request: Request,
    limit: int = 50,
    state: str | None = None,
    reported_only: bool = False,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    limit = min(limit, 200)

    def _q():
        with ctx.open_db() as conn:
            clauses = ["w.guild_id = ?"]
            params: list = [guild_id]
            if state:
                clauses.append("w.state = ?")
                params.append(state)
            if reported_only:
                clauses.append(
                    "EXISTS (SELECT 1 FROM whisper_reports wr WHERE wr.whisper_id = w.id)"
                )
            where = " AND ".join(clauses)
            total = conn.execute(
                f"SELECT COUNT(*) FROM whispers w WHERE {where}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT w.id, w.sender_id, w.target_id, w.state,
                       w.solved, w.exposed, w.created_at,
                       COUNT(wr.id) AS report_count
                FROM whispers w
                LEFT JOIN whisper_reports wr ON wr.whisper_id = w.id
                WHERE {where}
                GROUP BY w.id
                ORDER BY w.created_at DESC
                LIMIT ?
                """,
                params + [limit],
            ).fetchall()
            entries = [
                {
                    "id": r["id"],
                    "sender_id": str(r["sender_id"]),
                    "target_id": str(r["target_id"]),
                    "state": r["state"],
                    "solved": bool(r["solved"]),
                    "exposed": bool(r["exposed"]),
                    "report_count": r["report_count"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    await _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("sender_id", "sender_name"),
        ("target_id", "target_name"),
    )
    return result


@router.get("/moderation/confessions-audit", response_model=ConfessionsAuditLogResponse)
async def confessions_audit_log(
    request: Request,
    limit: int = 50,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    limit = min(limit, 200)

    def _q():
        with ctx.open_db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM confession_threads WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()[0]
            rows = conn.execute(
                """
                SELECT ct.message_id, ct.channel_id, ct.original_author_id,
                       ct.discord_thread_id, ct.created_at,
                       m.content
                FROM confession_threads ct
                LEFT JOIN messages m ON m.message_id = ct.message_id
                WHERE ct.guild_id = ?
                ORDER BY ct.created_at DESC
                LIMIT ?
                """,
                (guild_id, limit),
            ).fetchall()
            entries = [
                {
                    "message_id": str(r["message_id"]),
                    "author_id": str(r["original_author_id"]),
                    "channel_id": str(r["channel_id"]),
                    "thread_id": str(r["discord_thread_id"]) if r["discord_thread_id"] else None,
                    "content": r["content"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    await _resolve_names(ctx, guild, result["entries"], ("author_id", "author_name"))
    return result
