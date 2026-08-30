"""AI config and query endpoints for the dashboard."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from bot_modules.core.bot_exclusion import bot_ids_subquery
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

router = APIRouter()

# The AI panel is primary-guild-only and its prompts apply bot-wide, so they are
# stored at guild_id=0 — the row every guild's reader resolves through
# ``get_config_value``'s legacy fallback. Writing them at the active guild's id
# (what this router used to do) left a second guild silently running whatever
# stale guild-0 row happened to exist, with no surface anywhere to see or edit
# it, and made "Restore Original" a no-op whenever the only row was the legacy
# one. Saves and resets also clear the active guild's own row so an override
# written by the old, guild-scoped behaviour can never shadow the global value.
GLOBAL_GUILD_ID = 0


def _require_primary_guild(ctx, guild_id: int) -> None:
    """Refuse a bot-global AI write from anywhere but the primary guild.

    ``require_perms({"admin"})`` only proves the caller administers the guild
    they currently have selected, and every value this panel touches (the
    prompts at guild 0, the model source, the loaded model) is shared by every
    guild the bot serves. Without this, a second guild's admin could rewrite
    the moderation prompts every other guild runs — a prompt that never emits
    the verdict block makes every message read as fine. The panel is already
    primary-guild-only in the nav, but that flag is client-side.
    """
    if int(guild_id) != int(ctx.guild_id):
        raise HTTPException(
            403, "AI settings are bot-global — edit them from the primary guild"
        )


# ── GET /config/ai ─────────────────────────────────────────────────────────────


@router.get("/config/ai")
async def get_ai_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    from bot_modules.services import ollama_client
    from bot_modules.services.ai_config import get_prompt_with_source, list_prompts

    ctx = get_ctx(request)
    # Read is gated the same way the writes below are: the payload carries the
    # host's model path and every shared prompt, and this panel is primary-guild
    # only. Its sole caller is config-ai.js, which the nav never mounts
    # elsewhere — so this closes the disclosure without costing anyone a page.
    _require_primary_guild(ctx, get_active_guild_id(request))

    def _q():
        from bot_modules.core.db_utils import get_config_value
        with ctx.open_db() as conn:
            model_path = get_config_value(conn, "llm_model_path", "")
            hf_repo    = get_config_value(conn, "llm_hf_repo", "")
            hf_file    = get_config_value(conn, "llm_hf_file", "")
            prompts = []
            for p in list_prompts():
                text, is_override = get_prompt_with_source(
                    conn, p.key, GLOBAL_GUILD_ID
                )
                prompts.append({
                    "key": p.key,
                    "label": p.label,
                    "description": p.description,
                    "text": text,
                    "is_override": is_override,
                })
        return {
            "llm_status": ollama_client.status(),
            "llm_model_path": model_path or os.getenv("LLAMA_MODEL_PATH", ""),
            "llm_hf_repo": hf_repo or os.getenv("LLAMA_HF_REPO", ""),
            "llm_hf_file": hf_file or os.getenv("LLAMA_HF_FILE", ""),
            "prompts": prompts,
        }

    return await run_query(_q)


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
    from bot_modules.services.ai_config import reset_prompt, set_prompt

    ctx = get_ctx(request)
    _require_primary_guild(ctx, guild_id)

    def _q():
        with ctx.open_db() as conn:
            set_prompt(conn, key, body.text, GLOBAL_GUILD_ID)
            if guild_id:
                reset_prompt(conn, key, guild_id)

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
    _require_primary_guild(ctx, guild_id)

    def _q():
        with ctx.open_db() as conn:
            reset_prompt(conn, key, GLOBAL_GUILD_ID)
            if guild_id:
                reset_prompt(conn, key, guild_id)

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
    from bot_modules.services.ai_config import get_prompt

    ctx = get_ctx(request)
    _require_primary_guild(ctx, guild_id)

    if not ollama_client.is_available():
        raise HTTPException(503, "LLM is not configured.")

    def _q():
        with ctx.open_db() as conn:
            return get_prompt(conn, key, GLOBAL_GUILD_ID)

    system = await run_query(_q)

    result = await ollama_client.chat(
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
    include_bots: bool = False


@router.post("/messages/ai-query")
async def messages_ai_query(
    request: Request,
    body: AiQueryBody,
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
):
    from bot_modules.services import ollama_client
    from bot_modules.services.ai_config import get_prompt
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

            # Without this, asking the assistant "who posts most here?" is
            # answered from a corpus that is ~21% bot output. An explicit author
            # filter overrides, so you can still ask about a specific bot.
            if not body.include_bots and not authors:
                where.append(f"author_id NOT IN ({bot_ids_subquery()})")
                params.append(guild_id)

            rows = conn.execute(
                "SELECT message_id, author_id, content, reply_to_id, ts, channel_id "
                f"FROM messages WHERE {' AND '.join(where)} ORDER BY ts ASC LIMIT 500",
                params,
            ).fetchall()

        return rows, system

    rows, system = await run_query(_q)

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
        system=system,
        user_content=prompt,
        max_tokens=4096,
    )
    return {"result": result or "No analysis returned.", "message_count": len(rows)}


# ── GET /config/ai/model-status ───────────────────────────────────────────────


@router.get("/config/ai/model-status")
async def get_model_status(
    _: AuthenticatedUser = Depends(require_perms({"moderator"})),
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
    _require_primary_guild(ctx, guild_id)

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
    guild_id: int = Depends(get_active_guild_id),
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    from bot_modules.services import ollama_client

    ctx = get_ctx(request)
    _require_primary_guild(ctx, guild_id)
    if not ollama_client.is_available(ctx.db_path):
        raise HTTPException(400, "No model source configured — set model path and HuggingFace details first.")

    ollama_client.reload(ctx.db_path)
    return {"ok": True, "message": "Model reload started. Poll /api/config/ai/model-status for progress."}
