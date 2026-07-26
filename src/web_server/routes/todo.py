"""Todo endpoints — shared server todo list, sticky board, recurring tasks."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bot_modules.core.db_utils import get_tz_offset_hours, open_db_immediate
from bot_modules.services.todo_recurring_service import (
    RecurringValidationError,
    create_recurring,
    delete_recurring,
    describe_cadence,
    get_recurring,
    list_recurring,
    run_now,
    set_status,
    update_recurring,
)
from bot_modules.services.todo_service import (
    TASK_MAX_LEN,
    complete_todo,
    create_todo,
    get_board,
    list_todos,
)
from web_server.helpers import resolve_names as _resolve_names
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

# Tasks and recurring definitions are a moderator worklist end to end. Board
# *placement* is admin — it makes the bot post into an arbitrary channel, which
# is server configuration rather than worklist curation.
_MOD = Depends(require_perms({"moderator"}))
_ADMIN = Depends(require_perms({"admin"}))


class TodoCreateBody(BaseModel):
    task: str


class BoardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = "0"


class RecurringBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    description: str | None = None
    recurrence: str = "daily"
    time_of_day: int = 0
    recur_days: list[int] = []


def _recurring_dict(task, *, tz_offset: float) -> dict:
    return {
        "id": task.id,
        "task": task.task,
        "description": task.description,
        "recurrence": task.recurrence,
        "time_of_day": task.time_of_day,
        "recur_days": list(task.recur_days),
        "status": task.status,
        "next_run_at": task.next_run_at,
        "last_run_at": task.last_run_at,
        "last_status": task.last_status,
        "created_by": str(task.created_by),
        "created_at": task.created_at,
        "cadence": describe_cadence(task),
    }


@router.get("/todos")
async def list_todos_endpoint(
    request: Request,
    status: Optional[str] = None,
    user: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = list_todos(conn, guild_id, status=status)
            board = get_board(conn, guild_id)

        todos = [
            {
                "id": r["id"],
                "added_by": str(r["added_by"]),
                "added_by_name": "",
                "task": r["task"],
                "description": r["description"],
                "source_message_url": r["source_message_url"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
                "completed_by": str(r["completed_by"]) if r["completed_by"] else None,
                "completed_by_name": "",
                "recurring_id": r["recurring_id"],
            }
            for r in rows
        ]
        pending = sum(1 for t in todos if t["completed_at"] is None)
        return {
            "pending_count": pending,
            "completed_count": len(todos) - pending,
            "todos": todos,
            "board": {
                "channel_id": str(board.channel_id),
                "message_id": str(board.message_id),
                "posted": board.posted,
                "updated_at": board.updated_at,
            },
        }

    result = await run_query(_q)

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    await _resolve_names(
        ctx, guild, result["todos"],
        ("added_by", "added_by_name"),
        ("completed_by", "completed_by_name"),
    )
    result["can_manage_board"] = "admin" in user.perms
    if result["board"]["posted"] and guild is not None:
        result["board"]["jump_url"] = (
            f"https://discord.com/channels/{guild_id}"
            f"/{result['board']['channel_id']}/{result['board']['message_id']}"
        )
    return result


@router.post("/todos")
async def create_todo_endpoint(
    request: Request,
    body: TodoCreateBody,
    user: AuthenticatedUser = _MOD,
):
    task = (body.task or "").strip()
    if not task:
        raise HTTPException(status_code=400, detail="Task cannot be empty.")
    if len(task) > TASK_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Task must be {TASK_MAX_LEN} characters or fewer.",
        )

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return create_todo(conn, guild_id, user.user_id, task)

    todo_id = await run_query(_q)
    await _refresh_board(ctx, guild_id)
    return {"ok": True, "id": todo_id}


@router.post("/todos/{todo_id}/complete")
async def complete_todo_endpoint(
    request: Request,
    todo_id: int,
    user: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return complete_todo(conn, todo_id, guild_id, user.user_id)

    updated = await run_query(_q)
    if not updated:
        raise HTTPException(status_code=404, detail="Todo not found or already completed.")
    await _refresh_board(ctx, guild_id)
    return {"ok": True}


# ── Board placement ─────────────────────────────────────────────────────────


def _todo_cog(ctx):
    bot = getattr(ctx, "bot", None)
    return bot.get_cog("TodoCog") if bot else None


async def _refresh_board(ctx, guild_id: int) -> None:
    """Best-effort in-place board repaint after a dashboard mutation.

    A failure here is never fatal to the request — the 60s loop repaints
    anyway, so a missing bot or a Discord hiccup just means the board is up to
    a minute stale rather than the task silently not being saved.
    """
    cog = _todo_cog(ctx)
    if cog is None:
        return
    try:
        await cog.refresh_board(guild_id)
    except Exception:  # pragma: no cover - defensive
        pass


@router.put("/todos/board")
async def set_board(
    request: Request,
    body: BoardBody,
    _: AuthenticatedUser = _ADMIN,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    try:
        channel_id = int(body.channel_id or "0")
    except ValueError:
        raise HTTPException(status_code=400, detail="Pick a valid channel.") from None

    cog = _todo_cog(ctx)
    if cog is None:
        raise HTTPException(
            status_code=503,
            detail="The bot isn't connected right now — try again in a moment.",
        )
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(status_code=503, detail="The bot isn't in this server.")

    if not channel_id:
        await cog.unpost_board(guild)
        return {"ok": True, "posted": False}

    channel = guild.get_channel(channel_id)
    if channel is None or not hasattr(channel, "send"):
        raise HTTPException(status_code=400, detail="That channel doesn't exist here.")

    message = await cog.place_board(guild, channel)
    if message is None:
        raise HTTPException(
            status_code=400,
            detail="I can't post in that channel — check my Send Messages and Embed Links permissions.",
        )
    return {"ok": True, "posted": True, "message_id": str(message.id)}


# ── Recurring tasks ─────────────────────────────────────────────────────────


@router.get("/todos/recurring")
async def list_recurring_endpoint(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            items = [_recurring_dict(t, tz_offset=tz) for t in list_recurring(conn, guild_id)]
        return {"items": items, "tz_offset_hours": tz}

    return await run_query(_q)


@router.post("/todos/recurring")
async def create_recurring_endpoint(
    request: Request,
    body: RecurringBody,
    user: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            return create_recurring(
                conn,
                guild_id,
                task=body.task,
                description=body.description,
                recurrence=body.recurrence,
                time_of_day=body.time_of_day,
                recur_days=body.recur_days,
                created_by=user.user_id,
                offset_hours=tz,
                now_ts=time.time(),
            )

    try:
        new_id = await run_query(_q)
    except RecurringValidationError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    return {"ok": True, "id": new_id}


@router.put("/todos/recurring/{recurring_id}")
async def update_recurring_endpoint(
    request: Request,
    recurring_id: int,
    body: RecurringBody,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            return update_recurring(
                conn,
                recurring_id,
                guild_id,
                task=body.task,
                description=body.description,
                recurrence=body.recurrence,
                time_of_day=body.time_of_day,
                recur_days=body.recur_days,
                offset_hours=tz,
                now_ts=time.time(),
            )

    try:
        ok = await run_query(_q)
    except RecurringValidationError as err:
        raise HTTPException(status_code=400, detail=str(err)) from None
    if not ok:
        raise HTTPException(status_code=404, detail="That recurring task no longer exists.")
    return {"ok": True}


@router.delete("/todos/recurring/{recurring_id}")
async def delete_recurring_endpoint(
    request: Request,
    recurring_id: int,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return delete_recurring(conn, recurring_id, guild_id)

    if not await run_query(_q):
        raise HTTPException(status_code=404, detail="That recurring task no longer exists.")
    return {"ok": True}


@router.post("/todos/recurring/{recurring_id}/{action}")
async def recurring_action(
    request: Request,
    recurring_id: int,
    action: str,
    _: AuthenticatedUser = _MOD,
):
    if action not in ("pause", "resume", "run-now"):
        raise HTTPException(status_code=404, detail="Unknown action.")

    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        # "Run now" reads (is the last instance still open?) then writes, so it
        # takes the write lock up front — otherwise it and the background
        # spawner could both act on the same snapshot and insert two copies.
        opener = open_db_immediate(ctx.db_path) if action == "run-now" else ctx.open_db()
        with opener as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            if action == "run-now":
                if get_recurring(conn, recurring_id, guild_id) is None:
                    return None
                result = run_now(
                    conn,
                    recurring_id,
                    guild_id,
                    now_ts=time.time(),
                    offset_hours=tz,
                )
                return {"spawned": result.status == "spawned" if result else False}
            ok = set_status(
                conn,
                recurring_id,
                guild_id,
                "paused" if action == "pause" else "active",
                offset_hours=tz,
                now_ts=time.time(),
            )
            return {"ok": True} if ok else None

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="That recurring task no longer exists.")
    if action == "run-now":
        await _refresh_board(ctx, guild_id)
        if not result["spawned"]:
            return {
                "ok": True,
                "spawned": False,
                "detail": "That task is already on the list — nothing new added.",
            }
    return {"ok": True, **result}
