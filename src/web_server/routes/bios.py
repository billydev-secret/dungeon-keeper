"""Bios admin endpoints — template fields, question pool, scalar config.

All endpoints are admin-gated. Reads/writes are scoped to the active
guild via ``get_active_guild_id(request)``. Field writes lazily create
the guild's `bio_templates` row on first call and bump
``bio_templates.version`` on every successful mutation.
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from bot_modules.bios import db as bios_db
from bot_modules.bios.trigger import post_trigger_button as _post_trigger_button
from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.services.embeds import BIOS_PRIMARY
from web_server.auth import AuthenticatedUser
from web_server.deps import (
    get_active_guild_id,
    get_ctx,
    require_perms,
    run_query,
)

log = logging.getLogger("dungeonkeeper.web.bios")

router = APIRouter()


FieldTypeLit = Literal["short", "paragraph", "choice"]


# ── Pydantic bodies ────────────────────────────────────────────────────


class BiosConfigBody(BaseModel):
    bios_channel_id: str | None = None
    wizard_category_id: str | None = None
    questions_per_bio: int | None = Field(default=None, ge=1, le=10)
    embed_color: str | None = None  # hex, e.g. "#C8763E" or "C8763E"
    wizard_timeout: int | None = Field(default=None, ge=1, le=120)
    archive_grace: int | None = Field(default=None, ge=0, le=3600)


class FieldCreateBody(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    field_type: FieldTypeLit
    choices: list[str] = Field(default_factory=list)
    required: bool = False
    is_headline: bool = False
    max_len: int = Field(default=1024, ge=1, le=4096)

    @field_validator("choices")
    @classmethod
    def _trim_choices(cls, v: list[str]) -> list[str]:
        return [c.strip() for c in v if c and c.strip()]


class FieldUpdateBody(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    field_type: FieldTypeLit | None = None
    choices: list[str] | None = None
    required: bool | None = None
    is_headline: bool | None = None
    max_len: int | None = Field(default=None, ge=1, le=4096)
    active: bool | None = None

    @field_validator("choices")
    @classmethod
    def _trim_choices(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [c.strip() for c in v if c and c.strip()]


class FieldReorderBody(BaseModel):
    ordered_ids: list[int]


class QuestionCreateBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=512)
    weight: int = Field(default=1, ge=1, le=1000)


class QuestionUpdateBody(BaseModel):
    prompt: str | None = Field(default=None, min_length=1, max_length=512)
    weight: int | None = Field(default=None, ge=1, le=1000)
    active: bool | None = None


# ── Config GET/PUT ─────────────────────────────────────────────────────


@router.get("/config")
async def get_bios_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return {
                "bios_channel_id": get_config_value(conn, "bios_channel_id", "0", guild_id),
                "wizard_category_id": get_config_value(
                    conn, "bios_wizard_category_id", "0", guild_id
                ),
                "questions_per_bio": int(
                    get_config_value(conn, "bios_questions_per_bio", "3", guild_id) or "3"
                ),
                "embed_color": get_config_value(
                    conn, "bios_embed_color", f"#{BIOS_PRIMARY:06X}", guild_id
                ),
                "wizard_timeout": int(
                    get_config_value(conn, "bios_wizard_timeout", "15", guild_id) or "15"
                ),
                "archive_grace": int(
                    get_config_value(conn, "bios_archive_grace", "60", guild_id) or "60"
                ),
            }

    return await run_query(_q)


@router.put("/config")
async def update_bios_config(
    request: Request,
    body: BiosConfigBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if body.bios_channel_id is not None:
                set_config_value(conn, "bios_channel_id", body.bios_channel_id, guild_id)
            if body.wizard_category_id is not None:
                set_config_value(
                    conn, "bios_wizard_category_id", body.wizard_category_id, guild_id
                )
            if body.questions_per_bio is not None:
                set_config_value(
                    conn,
                    "bios_questions_per_bio",
                    str(body.questions_per_bio),
                    guild_id,
                )
            if body.embed_color is not None:
                set_config_value(conn, "bios_embed_color", body.embed_color, guild_id)
            if body.wizard_timeout is not None:
                set_config_value(
                    conn, "bios_wizard_timeout", str(body.wizard_timeout), guild_id
                )
            if body.archive_grace is not None:
                set_config_value(
                    conn, "bios_archive_grace", str(body.archive_grace), guild_id
                )
        return {"ok": True}

    result = await run_query(_q)
    ctx.invalidate_guild_config(guild_id)
    return result


# ── Fields ─────────────────────────────────────────────────────────────


@router.get("/fields")
async def list_fields(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            if tmpl is None:
                return {
                    "template_version": 0,
                    "fields": [],
                    "headline_warning": False,
                }
            rows = bios_db.list_fields_admin(conn, tmpl.id)
            has_headline = any(r["is_headline"] and r["active"] for r in rows)
            has_active = any(r["active"] for r in rows)
            return {
                "template_version": tmpl.version,
                "fields": rows,
                "headline_warning": has_active and not has_headline,
            }

    return await run_query(_q)


@router.post("/fields")
async def create_field(
    request: Request,
    body: FieldCreateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.is_headline and body.field_type != "short":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Headline must be a short field.",
        )
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tmpl = bios_db.get_or_create_template(conn, guild_id)
            field_id = bios_db.create_field(
                conn,
                tmpl.id,
                label=body.label,
                field_type=body.field_type,
                choices=body.choices,
                required=body.required,
                is_headline=body.is_headline,
                max_len=body.max_len,
            )
            bios_db.bump_template_version(conn, tmpl.id)
        return {"id": field_id, "ok": True}

    return await run_query(_q)


@router.put("/fields/{field_id}")
async def update_field(
    field_id: int,
    request: Request,
    body: FieldUpdateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    if body.is_headline is True and body.field_type is not None and body.field_type != "short":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Headline must be a short field.",
        )
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            if tmpl is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="No template yet."
                )
            owner_check = conn.execute(
                "SELECT field_type FROM bio_fields WHERE id = ? AND template_id = ?",
                (field_id, tmpl.id),
            ).fetchone()
            if owner_check is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Field not found."
                )
            # If flipping to headline but the (unchanged) stored type isn't 'short',
            # reject — keeps the CHECK constraint happy.
            if body.is_headline is True and body.field_type is None:
                if owner_check["field_type"] != "short":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Headline must be a short field.",
                    )
            bios_db.update_field(
                conn,
                field_id,
                template_id=tmpl.id,
                label=body.label,
                field_type=body.field_type,
                choices=body.choices,
                required=body.required,
                is_headline=body.is_headline,
                max_len=body.max_len,
                active=body.active,
            )
            bios_db.bump_template_version(conn, tmpl.id)
        return {"ok": True}

    return await run_query(_q)


@router.delete("/fields/{field_id}")
async def soft_retire_field(
    field_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            if tmpl is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="No template yet."
                )
            owner_check = conn.execute(
                "SELECT 1 FROM bio_fields WHERE id = ? AND template_id = ?",
                (field_id, tmpl.id),
            ).fetchone()
            if owner_check is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Field not found."
                )
            bios_db.soft_retire_field(conn, field_id)
            bios_db.bump_template_version(conn, tmpl.id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/fields/reorder")
async def reorder_fields(
    request: Request,
    body: FieldReorderBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            tmpl = bios_db.get_template(conn, guild_id)
            if tmpl is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="No template yet."
                )
            bios_db.reorder_fields(conn, tmpl.id, body.ordered_ids)
            bios_db.bump_template_version(conn, tmpl.id)
        return {"ok": True}

    return await run_query(_q)


# ── Questions ──────────────────────────────────────────────────────────


@router.get("/questions")
async def list_questions(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            return bios_db.list_questions_admin(conn, guild_id)

    return await run_query(_q)


@router.post("/questions")
async def create_question(
    request: Request,
    body: QuestionCreateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            qid = bios_db.create_question(
                conn, guild_id, prompt=body.prompt, weight=body.weight
            )
        return {"id": qid, "ok": True}

    return await run_query(_q)


@router.put("/questions/{question_id}")
async def update_question(
    question_id: int,
    request: Request,
    body: QuestionUpdateBody,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM bio_questions WHERE id = ? AND guild_id = ?",
                (question_id, guild_id),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Question not found."
                )
            bios_db.update_question(
                conn,
                question_id,
                prompt=body.prompt,
                weight=body.weight,
                active=body.active,
            )
        return {"ok": True}

    return await run_query(_q)


@router.delete("/questions/{question_id}")
async def soft_retire_question(
    question_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM bio_questions WHERE id = ? AND guild_id = ?",
                (question_id, guild_id),
            ).fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Question not found."
                )
            bios_db.soft_retire_question(conn, question_id)
        return {"ok": True}

    return await run_query(_q)


# ── Post the trigger button into the bios channel ──────────────────────


@router.post("/post-trigger-button")
async def post_trigger_button(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _read_channel_id() -> int:
        with ctx.open_db() as conn:
            raw = get_config_value(conn, "bios_channel_id", "0", guild_id)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    channel_id = await run_query(_read_channel_id)
    if channel_id == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configure the bios channel before posting the trigger button.",
        )

    if ctx.bot is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot client is not available.",
        )

    channel = ctx.bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured bios channel is no longer available.",
        )

    msg = await _post_trigger_button(ctx, channel, embed_color=BIOS_PRIMARY)
    return {"message_id": str(msg.id), "channel_id": str(channel.id)}
