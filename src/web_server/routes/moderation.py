"""Moderation endpoints — jails, tickets, warnings, audit log."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from web_server.helpers import resolve_names as _resolve_names
from bot_modules.jail.apply import apply_jail
from bot_modules.commands.jail_commands import _do_unjail, resolve_release_target
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
from bot_modules.services.anon_audit_service import (
    EVENT_REPLY_POSTED,
    FEATURE_CONFESSIONS,
    KNOWN_FEATURES,
    count_events,
    feature_label,
    get_retention_days,
    list_events,
    set_retention_days,
)
from web_server.schemas import (
    AnonAuditLogResponse,
    AnonAuditRetentionBody,
    AnonAuditRetentionResponse,
    AuditLogResponse,
    ConfessionsAuditLogResponse,
    DMAuditLogResponse,
    JailsResponse,
    ModerationStatsResponse,
    NsfwBlocksResponse,
    NsfwTagsResponse,
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
    # Presence is computed live rather than stored: a jailed member can leave
    # and rejoin freely (the hold re-applies on return), so any column recording
    # "they left" would be stale the moment they came back. The panel uses this
    # to warn that releasing them drops their stored roles for good.
    for j in result["jails"]:
        j["in_guild"] = (
            guild.get_member(int(j["user_id"])) is not None
            if guild is not None
            else None
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
    transcript, channel cleanup, DM, audit) — same behavior as the ``/unjail``
    slash command, for departed members too. This used to close the row out
    directly when the member had left, which skipped the transcript and left
    their jail channel orphaned in the category.
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
    if bot is None or guild is None:
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

    target = await resolve_release_target(bot, guild, int(jail["user_id"]))
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

    # Discord has no such user at all (deleted account, or an id that never
    # existed). Nothing to transcript or notify — close the row out so it stops
    # showing as an active hold on someone unreachable.
    def _release_record():
        with ctx.open_db() as conn:
            release_jail(
                conn,
                jail_id,
                reason=reason or "Released from dashboard (user unreachable)",
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="jail_release",
                actor_id=user.user_id,
                target_id=int(jail["user_id"]),
                extra={
                    "jail_id": jail_id,
                    "reason": reason,
                    "note": "user_unresolvable",
                },
            )

    await run_query(_release_record)
    return {
        "ok": True,
        "message": "That account no longer exists on Discord — jail record marked released.",
    }


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


# ── Confessions audit ─────────────────────────────────────────────────────────
#
# Reads `anon_audit_log`, NOT `confession_threads`. The operational table keeps
# only a seven-day TTL (it is purged hourly so thread identity and reply routing
# stay bounded), which made this panel a rolling seven-day window — fine while a
# Discord mod-log channel held the permanent copy, wrong once that channel became
# optional. The audit rows live for the guild's anon-audit retention window
# instead, so the two lifetimes are independent.
#
# Content is LEFT JOINed from `messages` rather than stored, so it is present
# only at guild storage level 'all'. See migration 145.


@router.get("/moderation/confessions-audit", response_model=ConfessionsAuditLogResponse)
async def confessions_audit_log(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    # Clamped at both ends: SQLite reads a negative LIMIT as "no limit", so an
    # unclamped `?limit=-1` would dump every de-anonymising row in one response.
    limit = max(1, min(limit, 200))
    offset = max(offset, 0)

    def _q():
        with ctx.open_db() as conn:
            total = count_events(conn, guild_id, feature=FEATURE_CONFESSIONS)
            events = list_events(
                conn, guild_id,
                feature=FEATURE_CONFESSIONS,
                limit=limit, offset=offset, with_content=True,
            )
            entries = [
                {
                    "id": e.id,
                    "message_id": str(e.message_id) if e.message_id else None,
                    "author_id": str(e.actor_id),
                    "channel_id": str(e.channel_id) if e.channel_id else None,
                    # "confession" or "reply" — the panel labels rows with this
                    # rather than calling every row a confession, which is what
                    # it did while reading the undifferentiated thread table.
                    "kind": "reply" if e.event == EVENT_REPLY_POSTED else "confession",
                    # Already a string in `extra` (snowflake precision), so it
                    # passes straight through.
                    "root_message_id": e.extra.get("root_message_id"),
                    "replied_to_id": str(e.target_id) if e.target_id else None,
                    "content": e.content,
                    "created_at": e.created_at,
                }
                for e in events
            ]
            return {"total": total, "entries": entries}

    result = await run_query(_q)
    await _resolve_names(
        ctx, guild, result["entries"],
        ("author_id", "author_name"),
        ("replied_to_id", "replied_to_name"),
    )
    return result


# ── Anonymous-features audit ──────────────────────────────────────────────────
#
# Covers the games-suite anonymous surfaces (AMA, FFA, Hot Takes, Fantasies,
# Clapback, WYR, Compliment) plus Confessions, which has its own panel above
# reading the same table filtered to its slug. Whisper and Guess stay out —
# their panels read their own load-bearing tables.
#
# Content is LEFT JOINed from `messages` rather than stored, so it is present
# only at guild storage level 'all' and absent for events that never produced a
# guild message. See migration 145 for why that trade was made deliberately.


@router.get("/moderation/anon-audit", response_model=AnonAuditLogResponse)
async def anon_audit_log(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    feature: str | None = None,
    actor_id: int | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    # Clamped at both ends: SQLite reads a negative LIMIT as "no limit", so an
    # unclamped `?limit=-1` would dump every de-anonymising row in one response.
    limit = max(1, min(limit, 200))
    offset = max(offset, 0)

    def _q():
        with ctx.open_db() as conn:
            total = count_events(
                conn, guild_id, feature=feature, actor_id=actor_id
            )
            events = list_events(
                conn, guild_id,
                feature=feature, actor_id=actor_id,
                limit=limit, offset=offset, with_content=True,
            )
            entries = [
                {
                    "id": e.id,
                    "feature": e.feature,
                    # Canonical display name, so this panel calls each game
                    # what the rest of the dashboard calls it.
                    "feature_label": feature_label(e.feature),
                    "event": e.event,
                    # Derived server-side from MOD_EVENTS: the panel styles on
                    # this flag rather than keeping its own copy of which
                    # event names mean "a mod acted on someone's anon post".
                    "is_mod_action": e.is_mod_action,
                    "actor_id": str(e.actor_id),
                    "target_id": str(e.target_id) if e.target_id else None,
                    "game_id": e.game_id,
                    "message_id": str(e.message_id) if e.message_id else None,
                    "channel_id": str(e.channel_id) if e.channel_id else None,
                    "content": e.content,
                    "extra": e.extra,
                    "created_at": e.created_at,
                }
                for e in events
            ]
            # Filter options come from the constant, not a DISTINCT over the
            # table: SQLite has no loose index scan, so that query walked the
            # guild's whole partition on every page load to return ≤7 strings.
            features = [
                {"value": f, "label": feature_label(f)}
                for f in KNOWN_FEATURES
            ]
            return {"total": total, "entries": entries, "features": features}

    result = await run_query(_q)
    await _resolve_names(
        ctx,
        guild,
        result["entries"],
        ("actor_id", "actor_name"),
        ("target_id", "target_name"),
    )
    return result


@router.get(
    "/moderation/anon-audit/retention", response_model=AnonAuditRetentionResponse
)
async def get_anon_audit_retention(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return {"retention_days": get_retention_days(conn, guild_id)}

    return await run_query(_q)


@router.put(
    "/moderation/anon-audit/retention", response_model=AnonAuditRetentionResponse
)
async def put_anon_audit_retention(
    request: Request,
    body: AnonAuditRetentionBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            set_retention_days(conn, guild_id, body.retention_days)
            conn.commit()
            return {"retention_days": get_retention_days(conn, guild_id)}

    return await run_query(_q)


# ── NSFW image reports ───────────────────────────────────────────────────
#
# Both are admin-gated. These rows describe members' uploads and are never
# surfaced more widely — see docs/nsfw_classifier_spec.md.


@router.get("/moderation/nsfw-tags", response_model=NsfwTagsResponse)
async def nsfw_tags_report(
    request: Request,
    days: int = 30,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """What the tagger saw, and where it disagreed with the verdict engine.

    Age-gated channels only — that is the sole scope NudeNet runs in, so it is
    the sole scope this can describe.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    window = max(1, min(int(days), 365))

    def _q():
        with ctx.open_db() as conn:
            since = int(time.time()) - window * 86400
            scope = "guild_id = ? AND created_at >= ? AND marqo_score IS NOT NULL"
            params = (guild_id, since)
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS classified,
                       COALESCE(SUM(verdict), 0) AS explicit,
                       COALESCE(SUM(top_label IS NOT NULL), 0) AS tagged,
                       COALESCE(SUM(verdict = 1 AND top_label IS NULL), 0)
                           AS explicit_untagged,
                       COALESCE(SUM(verdict = 0 AND top_label IS NOT NULL), 0)
                           AS tagged_not_explicit,
                       COALESCE(AVG(inference_ms), 0) AS avg_ms
                FROM nsfw_classifications WHERE {scope}
                """,
                params,
            ).fetchone()
            labels = conn.execute(
                f"""
                SELECT top_label AS label, COUNT(*) AS n,
                       COALESCE(AVG(marqo_score), 0) AS avg_score
                FROM nsfw_classifications
                WHERE {scope} AND top_label IS NOT NULL
                GROUP BY top_label ORDER BY n DESC
                """,
                params,
            ).fetchall()
            # Ten fixed 0.1-wide buckets. A score of exactly 1.0 would land in
            # a non-existent eleventh, so it is folded into the top one.
            scores = conn.execute(
                f"""
                SELECT MIN(CAST(marqo_score * 10 AS INTEGER), 9) AS bucket,
                       COUNT(*) AS n,
                       COALESCE(SUM(verdict), 0) AS explicit
                FROM nsfw_classifications WHERE {scope}
                GROUP BY bucket ORDER BY bucket
                """,
                params,
            ).fetchall()
        return {
            "days": window,
            "classified": int(totals["classified"]),
            "explicit": int(totals["explicit"]),
            "tagged": int(totals["tagged"]),
            "explicit_untagged": int(totals["explicit_untagged"]),
            "tagged_not_explicit": int(totals["tagged_not_explicit"]),
            "avg_inference_ms": round(float(totals["avg_ms"]), 1),
            "labels": [
                {
                    "label": r["label"],
                    "count": int(r["n"]),
                    "avg_score": round(float(r["avg_score"]), 3),
                }
                for r in labels
            ],
            "scores": [
                {
                    "floor": round(int(r["bucket"]) / 10, 1),
                    "count": int(r["n"]),
                    "explicit": int(r["explicit"]),
                }
                for r in scores
            ],
        }

    return await run_query(_q)


@router.get("/moderation/nsfw-blocks", response_model=NsfwBlocksResponse)
async def nsfw_blocks_report(
    request: Request,
    days: int = 30,
    limit: int = 100,
    surface: str | None = None,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    """Every image a gate destroyed, so a false positive is reviewable.

    Unlike the tag report this covers **every** channel: the places a deletion
    is most likely to be a mistake are exactly the ones no classification row
    is written for.
    """
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    window = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 500))
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None

    def _q():
        with ctx.open_db() as conn:
            since = int(time.time()) - window * 86400
            clauses = ["guild_id = ?", "created_at >= ?"]
            params: list = [guild_id, since]
            if surface:
                clauses.append("surface = ?")
                params.append(surface)
            where = " AND ".join(clauses)
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COALESCE(SUM(action = 'removed'), 0) AS removed,
                       COALESCE(SUM(action = 'logged'), 0) AS logged
                FROM nsfw_blocks WHERE {where}
                """,
                params,
            ).fetchone()
            by_surface = conn.execute(
                f"SELECT surface, COUNT(*) AS n FROM nsfw_blocks "
                f"WHERE {where} GROUP BY surface",
                params,
            ).fetchall()
            rows = conn.execute(
                f"SELECT * FROM nsfw_blocks WHERE {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return {
            "days": window,
            "total": int(totals["total"]),
            "removed": int(totals["removed"]),
            "logged": int(totals["logged"]),
            "by_surface": {r["surface"]: int(r["n"]) for r in by_surface},
            "entries": [
                {
                    # Snowflakes cross to the browser as strings so JS float
                    # math can't round them into non-existent members.
                    "message_id": str(r["message_id"]),
                    "channel_id": str(r["channel_id"]),
                    "author_id": str(r["author_id"]),
                    "filename": r["filename"],
                    "score": r["marqo_score"],
                    "surface": r["surface"],
                    "action": r["action"],
                    "created_at": int(r["created_at"]),
                }
                for r in rows
            ],
        }

    result = await run_query(_q)
    await _resolve_names(ctx, guild, result["entries"], ("author_id", "author_name"))
    for entry in result["entries"]:
        # get_channel_or_thread, not get_channel: both gates fire on messages in
        # threads (is_age_gated_channel deliberately resolves through a thread's
        # parent), and get_channel returns None for them — which rendered every
        # thread-hosted block as a bare numeric id in the report.
        channel = (
            guild.get_channel_or_thread(int(entry["channel_id"])) if guild else None
        )
        entry["channel_name"] = getattr(channel, "name", "") or ""
    return result
