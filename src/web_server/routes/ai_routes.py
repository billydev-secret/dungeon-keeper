"""AI config and query endpoints for the dashboard."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()


# ── GET /config/ai ─────────────────────────────────────────────────────────────


@router.get("/config/ai")
async def get_ai_config(
    request: Request,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services import ollama_client
    from bot_modules.services.ai_config import (
        KNOWN_MODELS,
        get_command_model_with_source,
        get_mod_model,
        get_prompt_with_source,
        get_wellness_model,
        list_prompts,
    )

    ctx = get_ctx(request)

    def _q():
        from bot_modules.core.db_utils import get_config_value
        with ctx.open_db() as conn:
            mod_model = get_mod_model(conn, guild_id)
            wellness_model = get_wellness_model(conn, guild_id)
            model_path = get_config_value(conn, "llm_model_path", "")
            hf_repo    = get_config_value(conn, "llm_hf_repo", "")
            hf_file    = get_config_value(conn, "llm_hf_file", "")
            prompts = []
            for p in list_prompts():
                text, is_override = get_prompt_with_source(conn, p.key, guild_id)
                model, model_is_override = get_command_model_with_source(
                    conn, p.key, guild_id
                )
                prompts.append({
                    "key": p.key,
                    "label": p.label,
                    "description": p.description,
                    "text": text,
                    "is_override": is_override,
                    "model": model,
                    "model_is_override": model_is_override,
                })
        return {
            "llm_status": ollama_client.status(),
            "llm_model_path": model_path or os.getenv("LLAMA_MODEL_PATH", ""),
            "llm_hf_repo": hf_repo or os.getenv("LLAMA_HF_REPO", ""),
            "llm_hf_file": hf_file or os.getenv("LLAMA_HF_FILE", ""),
            "known_models": KNOWN_MODELS,
            "mod_model": mod_model,
            "wellness_model": wellness_model,
            "prompts": prompts,
        }

    return await run_query(_q)


# ── PUT /config/ai/models ──────────────────────────────────────────────────────


class ModelsBody(BaseModel):
    mod_model: str
    wellness_model: str


@router.put("/config/ai/models")
async def put_ai_models(
    request: Request,
    body: ModelsBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.ai_config import set_mod_model, set_wellness_model

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_mod_model(conn, body.mod_model, guild_id)
            set_wellness_model(conn, body.wellness_model, guild_id)

    await run_query(_q)
    return {"ok": True}


# ── PUT /config/ai/prompts/{key} ───────────────────────────────────────────────


class PromptBody(BaseModel):
    text: str


@router.put("/config/ai/prompts/{key}")
async def put_ai_prompt(
    request: Request,
    key: str,
    body: PromptBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.ai_config import set_prompt

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_prompt(conn, key, body.text, guild_id)

    try:
        await run_query(_q)
    except KeyError:
        raise HTTPException(404, f"Unknown prompt key: {key}")
    return {"ok": True}


@router.delete("/config/ai/prompts/{key}")
async def reset_ai_prompt(
    request: Request,
    key: str,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.ai_config import reset_prompt

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            reset_prompt(conn, key, guild_id)

    try:
        await run_query(_q)
    except KeyError:
        raise HTTPException(404, f"Unknown prompt key: {key}")
    return {"ok": True}


# ── PUT /config/ai/prompts/{key}/model ────────────────────────────────────────


class PromptModelBody(BaseModel):
    model: str


@router.put("/config/ai/prompts/{key}/model")
async def put_ai_prompt_model(
    request: Request,
    key: str,
    body: PromptModelBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services.ai_config import set_command_model

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_command_model(conn, key, body.model, guild_id)

    try:
        await run_query(_q)
    except KeyError:
        raise HTTPException(404, f"Unknown prompt key: {key}")
    return {"ok": True}


# ── POST /config/ai/prompts/{key}/test ────────────────────────────────────────


class PromptTestBody(BaseModel):
    user_input: str


@router.post("/config/ai/prompts/{key}/test")
async def test_ai_prompt(
    request: Request,
    key: str,
    body: PromptTestBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services import ollama_client
    from bot_modules.services.ai_config import get_command_model, get_prompt

    if not ollama_client.is_available():
        raise HTTPException(503, "LLM is not configured.")

    ctx = get_ctx(request)

    with ctx.open_db() as conn:
        system = get_prompt(conn, key, guild_id)
        model = get_command_model(conn, key, guild_id)

    result = await ollama_client.chat(
        model=model,
        system=system,
        user_content=body.user_input,
        max_tokens=512,
    )
    return {"result": result}


# ── POST /messages/ai-query ───────────────────────────────────────────────────


class AiQueryBody(BaseModel):
    question: str
    author: str | list[str] | None = None
    channel: str | list[str] | None = None
    days: int = 7


@router.post("/messages/ai-query")
async def messages_ai_query(
    request: Request,
    body: AiQueryBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    from bot_modules.services import ollama_client
    from bot_modules.services.ai_config import get_command_model, get_prompt
    from bot_modules.services.ai_moderation_service import (
        _MAX_MSG_CHARS,
        _channel_label,
        _resolve_name,
        _ts_fmt,
    )
    from datetime import datetime, timedelta, timezone

    if not ollama_client.is_available():
        raise HTTPException(503, "LLM is not configured.")

    ctx = get_ctx(request)
    guild = ctx.bot.get_guild(guild_id) if ctx.bot else None
    if not guild:
        raise HTTPException(503, "Guild not available")

    authors = [body.author] if isinstance(body.author, str) else (body.author or [])
    channels = [body.channel] if isinstance(body.channel, str) else (body.channel or [])

    cutoff_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=body.days)).timestamp()
    )

    def _q():
        with ctx.open_db() as conn:
            system = get_prompt(conn, "ai_prompt_query_user", guild_id)
            model = get_command_model(conn, "ai_prompt_query_user", guild_id)

            where = ["guild_id = ?", "ts >= ?", "content IS NOT NULL"]
            params: list = [guild_id, cutoff_ts]

            if authors:
                placeholders = ",".join("?" * len(authors))
                where.append(f"author_id IN ({placeholders})")
                params.extend(int(a) for a in authors)

            if channels:
                placeholders = ",".join("?" * len(channels))
                where.append(f"channel_id IN ({placeholders})")
                params.extend(int(c) for c in channels)

            rows = conn.execute(
                "SELECT message_id, author_id, content, reply_to_id, ts, channel_id "
                f"FROM messages WHERE {' AND '.join(where)} ORDER BY ts ASC LIMIT 500",
                params,
            ).fetchall()

        return rows, system, model

    rows, system, model = await run_query(_q)

    if not rows:
        return {"result": "No messages found for the specified filters.", "message_count": 0}

    name_cache: dict[int, str] = {}
    lines = []
    for r in rows:
        author_id, content, ts, channel_id = r[1], r[2], r[4], r[5]
        name = _resolve_name(guild, name_cache, author_id)
        ch_label = _channel_label(guild, channel_id)
        content_str = (content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
        lines.append(f"[{_ts_fmt(ts)}] #{ch_label} | {name}: {content_str}")

    prompt = (
        f"Moderator question: {body.question}\n\n"
        f"Message log (last {body.days} days):\n\n" + "\n".join(lines)
    )

    result = await ollama_client.chat(
        model=model,
        system=system,
        user_content=prompt,
        max_tokens=4096,
    )
    return {"result": result or "No analysis returned.", "message_count": len(rows)}


# ── GET /config/ai/model-status ───────────────────────────────────────────────


@router.get("/config/ai/model-status")
async def get_model_status(
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services import ollama_client
    return ollama_client.status()


# ── PUT /config/ai/model-source ───────────────────────────────────────────────


class ModelSourceBody(BaseModel):
    model_path: str
    hf_repo: str
    hf_file: str


@router.put("/config/ai/model-source")
async def put_model_source(
    request: Request,
    body: ModelSourceBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.core.db_utils import set_config_value

    ctx = get_ctx(request)

    def _q():
        with ctx.open_db() as conn:
            set_config_value(conn, "llm_model_path", body.model_path.strip())
            set_config_value(conn, "llm_hf_repo",    body.hf_repo.strip())
            set_config_value(conn, "llm_hf_file",    body.hf_file.strip())

    await run_query(_q)
    return {"ok": True}


# ── POST /config/ai/model-reload ──────────────────────────────────────────────


@router.post("/config/ai/model-reload")
async def post_model_reload(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services import ollama_client

    ctx = get_ctx(request)
    if not ollama_client.is_available(ctx.db_path):
        raise HTTPException(400, "No model source configured — set model path and HuggingFace details first.")

    ollama_client.reload(ctx.db_path)
    return {"ok": True, "message": "Model reload started. Poll /api/config/ai/model-status for progress."}
