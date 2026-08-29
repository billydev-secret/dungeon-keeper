"""Todo endpoints — shared server todo list, sticky board, recurring tasks."""

from __future__ import annotations

import time
from typing import Optional

import discord
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bot_modules.core.db_utils import get_tz_offset_hours, open_db_immediate
from bot_modules.core.utils import jump_url
from bot_modules.services.todo_recurring_service import (
    RecurringValidationError,
    create_recurring,
    delete_recurring,
    describe_cadence,
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
from web_server.routes.panel_posting import sticky_conflict

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


def _board_dict(board) -> dict:
    return {
        "channel_id": str(board.channel_id),
        "message_id": str(board.message_id),
        "posted": board.posted,
        "updated_at": board.updated_at,
    }


def _recurring_dict(task) -> dict:
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
                "missed_at": r["missed_at"],
            }
            for r in rows
        ]
        # A row the daily reset wrote off is closed, not outstanding — the
        # dashboard's pending count has to agree with the board's.
        pending = sum(
            1
            for t in todos
            if t["completed_at"] is None and t["missed_at"] is None
        )
        return {
            "pending_count": pending,
            "completed_count": sum(
                1 for t in todos if t["completed_at"] is not None
            ),
            "missed_count": sum(1 for t in todos if t["missed_at"] is not None),
            "todos": todos,
            "board": _board_dict(board),
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
    if guild is not None:
        board = result["board"]
        if board["posted"]:
            board["jump_url"] = jump_url(
                guild_id, int(board["channel_id"]), int(board["message_id"])
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
    """Best-effort in-place repaint of **both** boards after a dashboard mutation.

    A failure here is never fatal to the request — the 60s loop repaints
    anyway, so a missing bot or a Discord hiccup just means a board is up to a
    minute stale rather than the task silently not being saved.

    Also the path a changed *definition* takes. The board is one row per
    definition, so adding, renaming, deleting, pausing or resuming one changes
    what it shows even though no todo row moved — and the 60s loop is not a
    backstop there, since it only repaints guilds where a spawn or a write-off
    happened. Without this an added chore would stay invisible and a deleted
    one leave a ghost row until the next scheduled fire, up to a day away for a
    daily and a week for a weekly. The repaint is signature-guarded, so a call
    that changed nothing costs a DB read and no API call.
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
    if not isinstance(channel, discord.TextChannel):
        # Threads and voice channels are filtered out of the picker; a duck-type
        # check here would admit types place_board is not typed to accept.
        raise HTTPException(status_code=400, detail="That channel doesn't exist here.")

    # There is only one todo board since migration 180, so the sibling-board
    # collision this used to refuse with a 409 no longer exists. Every *other*
    # sticky panel still would bury it — the casino hub and the Survivor panel
    # repaint on their own schedule — so the general guard stays.
    warning = await sticky_conflict(ctx, guild_id, channel_id, excluding="todo-board")

    message = await cog.place_board(guild, channel)
    if message is None:
        raise HTTPException(
            status_code=400,
            detail="I can't post in that channel — check my Send Messages and Embed Links permissions.",
        )
    return {
        "ok": True,
        "posted": True,
        "message_id": str(message.id),
        "warning": warning,
    }


# ── Recurring tasks ─────────────────────────────────────────────────────────


@router.get("/todos/recurring")
async def list_recurring_endpoint(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tz = get_tz_offset_hours(conn, guild_id)
            items = [_recurring_dict(t) for t in list_recurring(conn, guild_id)]
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
    await _refresh_board(ctx, guild_id)
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
    await _refresh_board(ctx, guild_id)
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
    await _refresh_board(ctx, guild_id)
    return {"ok": True}


_NOT_FOUND = "That recurring task no longer exists."


async def _set_recurring_status(request: Request, recurring_id: int, status: str):
    """Shared body for pause/resume — the only thing that differs is the status."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return set_status(
                conn,
                recurring_id,
                guild_id,
                status,
                offset_hours=get_tz_offset_hours(conn, guild_id),
                now_ts=time.time(),
            )

    if not await run_query(_q):
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    await _refresh_board(ctx, guild_id)
    return {"ok": True}


@router.post("/todos/recurring/{recurring_id}/pause")
async def pause_recurring(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    return await _set_recurring_status(request, recurring_id, "paused")


@router.post("/todos/recurring/{recurring_id}/resume")
async def resume_recurring(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    return await _set_recurring_status(request, recurring_id, "active")


@router.post("/todos/recurring/{recurring_id}/run-now")
async def run_recurring_now(
    request: Request, recurring_id: int, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        # Read-then-write (is the last instance still open?), so take the write
        # lock up front — otherwise this and the background spawner could act
        # on the same snapshot and insert two copies.
        with open_db_immediate(ctx.db_path) as conn:
            return run_now(
                conn,
                recurring_id,
                guild_id,
                now_ts=time.time(),
                offset_hours=get_tz_offset_hours(conn, guild_id),
            )

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    await _refresh_board(ctx, guild_id)
    if result.status != "spawned":
        return {
            "ok": True,
            "spawned": False,
            "detail": "That task is already on the list — nothing new added.",
        }
    return {"ok": True, "spawned": True}
