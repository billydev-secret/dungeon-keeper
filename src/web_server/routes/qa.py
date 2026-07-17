"""QA Tracker dashboard endpoints — board, void, archive, config, top testers.

Every route is admin-only (``require_perms({"admin"})``); paths mount under
``/api/qa``. The board folds each test's verdicts (voided included, for
audit) into the row plus a computed Discord jump link.

Void and archive are DB-first: the qa_service write commits, then a
best-effort re-render of the Discord card runs through ``ctx.bot`` (the bot
and web server share one process and loop — the same seam the quest sign-off
cards use). A Discord hiccup never rolls back the DB change; the response
just carries ``card_updated: false`` and the card self-heals on the next
button click.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.qa.cards import build_card_embed
from bot_modules.services import qa_service
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
_log = logging.getLogger("dungeonkeeper.web.qa")

require_admin = require_perms({"admin"})

_STATUSES = ("pending", "passed", "failed", "blocked", "archived")


# ── request bodies ────────────────────────────────────────────────────


class SettingsBody(BaseModel):
    """Full settings payload — the panel always saves every knob."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    role_id: int = Field(ge=0)
    channel_id: int = Field(ge=0)
    reward: int = Field(ge=0, le=10_000)
    daily_cap: int = Field(ge=0, le=1_000)


# ── serialization ─────────────────────────────────────────────────────


def _verdict_dict(row: sqlite3.Row) -> dict:
    return {
        "id": int(row["id"]),
        # Snowflakes stringified: they overflow JS number precision.
        "user_id": str(row["user_id"]),
        "verdict": row["verdict"],
        "note": row["note"],
        "paid_amount": int(row["paid_amount"]),
        "voided": row["voided_at"] is not None,
        "voided_by": str(row["voided_by"]) if row["voided_by"] is not None else None,
        "voided_at": row["voided_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _test_dict(row: sqlite3.Row, verdicts: list[sqlite3.Row], guild_id: int) -> dict:
    jump_url = None
    if row["channel_id"] and row["message_id"]:
        jump_url = (
            f"https://discord.com/channels/{guild_id}"
            f"/{int(row['channel_id'])}/{int(row['message_id'])}"
        )
    return {
        "id": int(row["id"]),
        "entry_key": row["entry_key"],
        "title": row["title"],
        "status": row["status"],
        "commit_sha": row["commit_sha"],
        "commit_subject": row["commit_subject"],
        "verified_by": str(row["verified_by"]) if row["verified_by"] else None,
        "verified_at": row["verified_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "jump_url": jump_url,
        "verdicts": [_verdict_dict(v) for v in verdicts],
    }


# ── board ─────────────────────────────────────────────────────────────


@router.get("/qa/tests")
async def list_tests(
    request: Request,
    status: str | None = None,
    _: AuthenticatedUser = Depends(require_admin),
):
    if status is not None and status not in _STATUSES:
        raise HTTPException(422, f"unknown status: {status!r}")
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = qa_service.list_tests(conn, guild_id, status)
            return [
                _test_dict(r, qa_service.list_verdicts(conn, int(r["id"])), guild_id)
                for r in rows
            ]

    return {"tests": await run_query(_q)}


# ── settings ──────────────────────────────────────────────────────────


def _settings_dict(settings: qa_service.QASettings) -> dict:
    return {
        "enabled": settings.enabled,
        "role_id": str(settings.role_id),
        "channel_id": str(settings.channel_id),
        "reward": settings.reward,
        "daily_cap": settings.daily_cap,
    }


@router.get("/qa/settings")
async def get_settings(
    request: Request,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return qa_service.load_qa_settings(conn, guild_id)

    return _settings_dict(await run_query(_q))


@router.put("/qa/settings")
async def put_settings(
    request: Request,
    body: SettingsBody,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            qa_service.save_qa_settings(
                conn,
                guild_id,
                {
                    "enabled": body.enabled,
                    "role_id": body.role_id,
                    "channel_id": body.channel_id,
                    "reward": body.reward,
                    "daily_cap": body.daily_cap,
                },
            )
            return qa_service.load_qa_settings(conn, guild_id)

    return _settings_dict(await run_query(_q))


# ── card re-render (best effort, never fatal) ─────────────────────────


async def _rerender_card(
    request: Request, test_id: int, *, strip_components: bool = False
) -> bool:
    """Re-render the Discord card from fresh rows after a dashboard change.

    Edits through ``ctx.bot`` — the bot runs in this process, so no separate
    REST client or token plumbing is needed. Archive passes
    ``strip_components`` to drop the verdict buttons (``view=None``); a void
    leaves the components untouched. Every failure is logged and swallowed:
    the DB change already committed and the cog re-renders the card on the
    next button click anyway.
    """
    ctx = get_ctx(request)
    bot = getattr(ctx, "bot", None)
    if bot is None or not bot.is_ready():
        return False

    def _read():
        with ctx.open_db() as conn:
            test = qa_service.get_test(conn, test_id)
            if test is None:
                return None
            verdicts = [dict(v) for v in qa_service.list_verdicts(conn, test_id)]
            return dict(test), verdicts

    loaded = await run_query(_read)
    if loaded is None:
        return False
    test, verdicts = loaded
    if not test.get("channel_id") or not test.get("message_id"):
        return False

    import discord

    try:
        channel = bot.get_channel(int(test["channel_id"]))
        if not isinstance(channel, discord.abc.Messageable):
            return False
        message = await channel.fetch_message(int(test["message_id"]))
        embed = discord.Embed.from_dict(build_card_embed(test, verdicts))
        if strip_components:
            await message.edit(embed=embed, view=None)
        else:
            await message.edit(embed=embed)
        return True
    except discord.DiscordException:
        _log.warning("qa card re-render failed (test %s)", test_id, exc_info=True)
        return False


# ── void + archive ────────────────────────────────────────────────────


@router.post("/qa/verdicts/{verdict_id}/void")
async def void_verdict(
    request: Request,
    verdict_id: int,
    user: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT guild_id FROM qa_verdicts WHERE id = ?", (verdict_id,)
            ).fetchone()
            if row is None or int(row["guild_id"]) != guild_id:
                return None
            return qa_service.void_verdict(conn, verdict_id, user.user_id)

    outcome = await run_query(_q)
    if outcome is None:
        raise HTTPException(404, "verdict not found or already voided")

    card_updated = await _rerender_card(request, outcome.test_id)
    return {
        "ok": True,
        "clawed": outcome.clawed,
        "shortfall": outcome.shortfall,
        "status": outcome.status,
        "card_updated": card_updated,
    }


@router.post("/qa/tests/{test_id}/archive")
async def archive_test(
    request: Request,
    test_id: int,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = qa_service.get_test(conn, test_id)
            if row is None or int(row["guild_id"]) != guild_id:
                return False
            qa_service.archive_test(conn, test_id)
            return True

    if not await run_query(_q):
        raise HTTPException(404, "test not found")

    card_updated = await _rerender_card(request, test_id, strip_components=True)
    return {"ok": True, "status": "archived", "card_updated": card_updated}


# ── top testers ───────────────────────────────────────────────────────


@router.get("/qa/top-testers")
async def top_testers(
    request: Request,
    _: AuthenticatedUser = Depends(require_admin),
):
    """Un-voided verdict counts per tester, joined with gross ``qa_reward``
    coins from the ledger (a void's clawback is a separate ``qa_void`` debit,
    so "earned" here stays gross — the scoreboard rewards showing up)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            rows = conn.execute(
                """
                SELECT v.user_id,
                       COUNT(*) AS verdicts,
                       COALESCE((
                           SELECT SUM(l.amount) FROM econ_ledger l
                           WHERE l.guild_id = v.guild_id
                             AND l.user_id = v.user_id
                             AND l.kind = 'qa_reward'
                       ), 0) AS coins
                FROM qa_verdicts v
                WHERE v.guild_id = ? AND v.voided_at IS NULL
                GROUP BY v.user_id
                ORDER BY verdicts DESC, coins DESC, v.user_id
                LIMIT 20
                """,
                (guild_id,),
            ).fetchall()
            return [
                {
                    "user_id": str(r["user_id"]),
                    "verdicts": int(r["verdicts"]),
                    "coins": int(r["coins"]),
                }
                for r in rows
            ]

    return {"testers": await run_query(_q)}
