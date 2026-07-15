"""Chat Revive dashboard API — the feature's management surface.

Chat Revive is dashboard-managed (no slash commands): guild settings, the
per-channel dials, the question bank, the scoreboard, plus the two
Discord-side actions (manual fire, posting the opt-in button) which need the
live bot from ``ctx.bot``. All DB work runs through ``run_query``'s
threadpool; time enters as ``time.time()`` at the route boundary.
"""

from __future__ import annotations

import random
import time
from dataclasses import asdict, replace

import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from bot_modules.chat_revive.actions import (
    ReviveOptInButton,
    channel_is_busy,
    send_revive,
)
from bot_modules.chat_revive.logic import FLOURISHES, band_label, should_ping
from bot_modules.games.utils.question_source import channel_allows_nsfw
from bot_modules.services.chat_revive_loop import ReviveInFlight, send_guard
from bot_modules.services.chat_revive_service import (
    KNOWN_CATEGORIES,
    ChannelConfig,
    GuildConfig,
    add_question,
    bulk_add_questions,
    delete_channel_config,
    evaluate,
    get_guild_config,
    list_channel_configs,
    list_questions,
    pick_question,
    record_event,
    retire_question,
    revive_stats,
    save_channel_config,
    save_guild_config,
    seed_starter_pack,
)
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()
_MOD = Depends(require_perms({"moderator"}))

OPTIN_PITCH = (
    "🔥 **Chat Revive** — take the role and get summoned (rarely — a few "
    "times a week at most) when a favorite channel needs a spark. "
    "Tap to join or leave any time."
)


class GuildConfigBody(BaseModel):
    enabled: bool
    role_id: int | None = None
    quiet_start: int = Field(0, ge=0, le=23)
    quiet_end: int = Field(8, ge=0, le=23)
    daily_budget: int = Field(3, ge=1, le=10)
    guild_gap_minutes: int = Field(90, ge=10, le=720)
    flourish_enabled: bool = True


class ChannelBody(BaseModel):
    enabled: bool = True
    categories: list[str] = Field(default_factory=list)
    ping_enabled: bool = False
    role_id_override: int | None = None
    rest_hours: float = Field(8.0, ge=1.0, le=72.0)
    fire_multiplier: float = Field(4.0, ge=2.0, le=10.0)


class QuestionBody(BaseModel):
    text: str
    category: str = "general"
    nsfw: bool = False


class BulkBody(BaseModel):
    lines: str


class ChannelActionBody(BaseModel):
    channel_id: int


def _guild_cfg_json(cfg: GuildConfig) -> dict:
    """Stringify snowflakes — JS `Number` can't hold a full Discord ID."""
    d = asdict(cfg)
    d["guild_id"] = str(d["guild_id"])
    if d.get("role_id") is not None:
        d["role_id"] = str(d["role_id"])
    return d


def _channel_cfg_json(cfg: ChannelConfig) -> dict:
    d = asdict(cfg)
    d["guild_id"] = str(d["guild_id"])
    d["channel_id"] = str(d["channel_id"])
    if d.get("role_id_override") is not None:
        d["role_id_override"] = str(d["role_id_override"])
    return d


def _require_channel(ctx, guild_id: int, channel_id: int) -> discord.TextChannel:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Bot is not connected to this guild."
        )
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such text channel.")
    return channel


def _clean_categories(raw: list[str]) -> tuple[str, ...]:
    tokens = [t.strip().lower() for t in raw if t.strip()]
    bad = [t for t in tokens if not t.isalpha()]
    if bad:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Categories must be single words: {', '.join(bad)}",
        )
    return tuple(dict.fromkeys(tokens))


# ── config ────────────────────────────────────────────────────────────


@router.get("/chat-revive/overview")
async def overview(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            cfg = get_guild_config(conn, guild_id)
            channels = list_channel_configs(conn, guild_id)
            bank_size = len(list_questions(conn, guild_id))
        return cfg, channels, bank_size

    cfg, channels, bank_size = await run_query(_q)
    return {
        "config": _guild_cfg_json(cfg),
        "channels": [_channel_cfg_json(c) for c in channels],
        "bank_size": bank_size,
        "categories": list(KNOWN_CATEGORIES),
    }


@router.put("/chat-revive/config")
async def put_config(
    request: Request, body: GuildConfigBody, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            cfg = replace(
                get_guild_config(conn, guild_id),
                enabled=body.enabled,
                role_id=body.role_id,
                quiet_start=body.quiet_start,
                quiet_end=body.quiet_end,
                daily_budget=body.daily_budget,
                guild_gap_minutes=body.guild_gap_minutes,
                flourish_enabled=body.flourish_enabled,
            )
            save_guild_config(conn, cfg)
            seeded = seed_starter_pack(conn, guild_id, now) if body.enabled else 0
        return cfg, seeded

    cfg, seeded = await run_query(_q)
    return {"config": _guild_cfg_json(cfg), "seeded": seeded}


@router.put("/chat-revive/channels/{channel_id}")
async def put_channel(
    request: Request,
    channel_id: int,
    body: ChannelBody,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    categories = _clean_categories(body.categories)

    def _q():
        with ctx.open_db() as conn:
            cfg = ChannelConfig(
                guild_id=guild_id,
                channel_id=channel_id,
                enabled=body.enabled,
                categories=categories,
                ping_enabled=body.ping_enabled,
                role_id_override=body.role_id_override,
                rest_hours=body.rest_hours,
                fire_multiplier=body.fire_multiplier,
            )
            save_channel_config(conn, cfg)
        return cfg

    return {"channel": _channel_cfg_json(await run_query(_q))}


@router.delete("/chat-revive/channels/{channel_id}")
async def remove_channel(
    request: Request, channel_id: int, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return delete_channel_config(conn, guild_id, channel_id)

    if not await run_query(_q):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Channel wasn't configured.")
    return {"ok": True}


# ── question bank ─────────────────────────────────────────────────────


@router.get("/chat-revive/questions")
async def questions(
    request: Request,
    category: str | None = None,
    include_retired: bool = False,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return list_questions(
                conn,
                guild_id,
                category=category.lower().strip() if category else None,
                include_retired=include_retired,
            )

    return {"questions": [asdict(q) for q in await run_query(_q)]}


@router.post("/chat-revive/questions")
async def create_question(
    request: Request, body: QuestionBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            return add_question(
                conn,
                guild_id,
                body.text,
                category=body.category.lower().strip() or "general",
                nsfw=body.nsfw,
                created_by=user.user_id,
                now_ts=now,
            )

    qid = await run_query(_q)
    if qid is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Duplicate (or blank) question."
        )
    return {"id": qid}


@router.post("/chat-revive/questions/bulk")
async def bulk_questions(
    request: Request, body: BulkBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()
    lines = body.lines.splitlines()
    if len(lines) > 1000:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Too many lines.")

    def _q():
        with ctx.open_db() as conn:
            return bulk_add_questions(
                conn, guild_id, lines, created_by=user.user_id, now_ts=now
            )

    added, skipped = await run_query(_q)
    return {"added": added, "skipped": skipped}


@router.post("/chat-revive/questions/{question_id}/retire")
async def retire(request: Request, question_id: int, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return retire_question(conn, guild_id, question_id)

    if not await run_query(_q):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such question.")
    return {"ok": True}


# ── scoreboard & the brain preview ────────────────────────────────────


@router.get("/chat-revive/stats")
async def stats(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            return revive_stats(conn, guild_id, now_ts=now)

    d = asdict(await run_query(_q))
    for c in d["channels"]:
        c["channel_id"] = str(c["channel_id"])
    return d


@router.get("/chat-revive/check/{channel_id}")
async def check(request: Request, channel_id: int, _: AuthenticatedUser = _MOD):
    """The trust-builder: would it fire right now, and why (not)?"""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    channel = guild.get_channel(channel_id) if guild else None
    live = isinstance(channel, discord.TextChannel)
    busy = await channel_is_busy(bot, channel_id) if live and bot else False
    slowmode = channel.slowmode_delay or 0 if live else 0
    allow_nsfw = channel_allows_nsfw(channel) if live else False

    def _q():
        with ctx.open_db() as conn:
            ev = evaluate(
                conn,
                guild_id,
                channel_id,
                now_ts=now,
                busy=busy,
                slowmode_delay=slowmode,
            )
            cats = ev.channel_cfg.categories if ev.channel_cfg else ()
            q = pick_question(
                conn, guild_id, categories=cats, allow_nsfw=allow_nsfw, now_ts=now
            )
        return ev, q

    ev, q = await run_query(_q)
    v = ev.verdict
    role_id = (
        ev.channel_cfg.role_id_override if ev.channel_cfg else None
    ) or ev.guild_cfg.role_id
    would_ping = bool(
        ev.channel_cfg
        and ev.channel_cfg.ping_enabled
        and role_id
        and should_ping(ev.freq.last_ping_ts, now)
    )
    return {
        "would_fire": v.fire,
        "reason": v.reason,
        "mode": v.mode,
        "band": band_label(v.band) if v.band is not None else None,
        "silence_minutes": round(v.silence_s / 60) if v.silence_s else None,
        "threshold_minutes": round(v.threshold_s / 60) if v.threshold_s else None,
        "history_days": round(ev.inputs.history_days, 1),
        "would_ask": q.text if q else None,
        "would_ping": would_ping,
        "live_channel": live,
    }


@router.post("/chat-revive/fire")
async def fire(request: Request, body: ChannelActionBody, _: AuthenticatedUser = _MOD):
    """Manual revive: skips lull detection, keeps ping scarcity."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()
    channel = _require_channel(ctx, guild_id, body.channel_id)
    allow_nsfw = channel_allows_nsfw(channel)

    def _q():
        with ctx.open_db() as conn:
            ev = evaluate(
                conn, guild_id, body.channel_id, now_ts=now, busy=False,
                slowmode_delay=0,
            )
            cats = ev.channel_cfg.categories if ev.channel_cfg else ()
            q = pick_question(
                conn, guild_id, categories=cats, allow_nsfw=allow_nsfw, now_ts=now
            )
        return ev, q

    ev, q = await run_query(_q)
    if not ev.guild_cfg.enabled:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Chat Revive is disabled for this guild."
        )
    if q is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "No eligible question — bank empty, filtered too narrowly, or "
            "everything was used within the last month.",
        )
    role_id = (
        ev.channel_cfg.role_id_override if ev.channel_cfg else None
    ) or ev.guild_cfg.role_id
    ping = bool(
        ev.channel_cfg
        and ev.channel_cfg.ping_enabled
        and role_id
        and should_ping(ev.freq.last_ping_ts, now)
    )
    flourish = random.choice(FLOURISHES) if ev.guild_cfg.flourish_enabled else None
    try:
        async with send_guard(channel.id):
            msg = await send_revive(
                channel,
                question_text=q.text,
                role_id=role_id if ping else None,
                flourish=flourish,
            )
    except ReviveInFlight as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A revive is already being posted there."
        ) from exc
    except discord.HTTPException as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Couldn't post in that channel."
        ) from exc

    def _rec():
        with ctx.open_db() as conn:
            record_event(
                conn,
                guild_id,
                body.channel_id,
                question_id=q.id,
                message_id=msg.id,
                trigger_kind="manual",
                pinged=ping,
                now_ts=now,
                offset_hours=ev.offset_hours,
            )

    await run_query(_rec)
    return {"ok": True, "question": q.text, "pinged": ping}


@router.post("/chat-revive/optin-post")
async def optin_post(
    request: Request, body: ChannelActionBody, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    channel = _require_channel(ctx, guild_id, body.channel_id)

    def _q():
        with ctx.open_db() as conn:
            return get_guild_config(conn, guild_id)

    cfg = await run_query(_q)
    if cfg.role_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "No opt-in role configured yet."
        )
    view = discord.ui.View(timeout=None)
    view.add_item(ReviveOptInButton(cfg.role_id))
    try:
        await channel.send(OPTIN_PITCH, view=view)
    except discord.HTTPException as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Couldn't post in that channel."
        ) from exc
    return {"ok": True}
