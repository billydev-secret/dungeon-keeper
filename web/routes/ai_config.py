"""AI config endpoints — view/edit models, prompts, and run prompt tests."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from web.auth import AuthenticatedUser
from web.deps import get_ctx, require_perms, run_query

if TYPE_CHECKING:
    pass

router = APIRouter()


# ── GET: full AI config snapshot ──────────────────────────────────────

@router.get("/ai")
async def get_ai_config(
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.ai_config import (
            KNOWN_MODELS,
            get_command_model_with_source,
            get_mod_model,
            get_prompt_with_source,
            get_wellness_model,
            list_prompts,
        )

        with ctx.open_db() as conn:
            prompts = []
            for info in list_prompts():
                text, is_override = get_prompt_with_source(conn, info.key)
                cmd_model, model_is_override = get_command_model_with_source(conn, info.key)
                prompts.append({
                    "key": info.key,
                    "label": info.label,
                    "description": info.description,
                    "text": text,
                    "is_override": is_override,
                    "model": cmd_model,
                    "model_is_override": model_is_override,
                })

            return {
                "mod_model": get_mod_model(conn),
                "wellness_model": get_wellness_model(conn),
                "known_models": KNOWN_MODELS,
                "has_api_key": bool(os.getenv("ANTHROPIC_API_KEY")),
                "prompts": prompts,
            }

    return await run_query(_q)


# ── PUT: update models ────────────────────────────────────────────────

class ModelUpdate(BaseModel):
    mod_model: str | None = None
    wellness_model: str | None = None


@router.put("/ai/models")
async def update_models(
    request: Request,
    body: ModelUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.ai_config import set_mod_model, set_wellness_model

        with ctx.open_db() as conn:
            if body.mod_model is not None:
                set_mod_model(conn, body.mod_model.strip())
            if body.wellness_model is not None:
                set_wellness_model(conn, body.wellness_model.strip())
        return {"ok": True}

    return await run_query(_q)


# ── PUT: update per-command model ─────────────────────────────────────

class CommandModelUpdate(BaseModel):
    model: str  # empty string clears the override


@router.put("/ai/prompts/{prompt_key}/model")
async def update_command_model(
    prompt_key: str,
    request: Request,
    body: CommandModelUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.ai_config import get_prompt_info, set_command_model

        if get_prompt_info(prompt_key) is None:
            return {"ok": False, "detail": f"Unknown prompt key: {prompt_key}"}
        with ctx.open_db() as conn:
            set_command_model(conn, prompt_key, body.model.strip())
        return {"ok": True}

    return await run_query(_q)


# ── PUT: update a single prompt ───────────────────────────────────────

class PromptUpdate(BaseModel):
    text: str


@router.put("/ai/prompts/{prompt_key}")
async def update_prompt(
    prompt_key: str,
    request: Request,
    body: PromptUpdate,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.ai_config import get_prompt_info, set_prompt

        if get_prompt_info(prompt_key) is None:
            return {"ok": False, "detail": f"Unknown prompt key: {prompt_key}"}
        with ctx.open_db() as conn:
            set_prompt(conn, prompt_key, body.text)
        return {"ok": True}

    return await run_query(_q)


# ── DELETE: reset a prompt to its default ─────────────────────────────

@router.delete("/ai/prompts/{prompt_key}")
async def reset_prompt(
    prompt_key: str,
    request: Request,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    ctx = get_ctx(request)

    def _q():
        from services.ai_config import get_prompt_info, reset_prompt as _reset

        if get_prompt_info(prompt_key) is None:
            return {"ok": False, "detail": f"Unknown prompt key: {prompt_key}"}
        with ctx.open_db() as conn:
            _reset(conn, prompt_key)
        return {"ok": True}

    return await run_query(_q)


# ── POST: test-run a prompt ───────────────────────────────────────────

class PromptTest(BaseModel):
    system: str
    user_input: str
    model: str | None = None
    max_tokens: int = 1024


@router.post("/ai/test")
async def test_prompt(
    request: Request,
    body: PromptTest,
    _: AuthenticatedUser = Depends(require_perms({"admin"})),
):
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "detail": "ANTHROPIC_API_KEY is not set."}

    ctx = get_ctx(request)

    # Determine which model to use
    model = body.model
    if not model:
        def _get_model():
            from services.ai_config import get_mod_model
            with ctx.open_db() as conn:
                return get_mod_model(conn)
        model = await run_query(_get_model)

    from anthropic import AsyncAnthropic
    from anthropic.types import TextBlock

    assert model is not None
    client = AsyncAnthropic(api_key=api_key)
    try:
        async with client.messages.stream(
            model=model,
            system=body.system,
            messages=[{"role": "user", "content": body.user_input}],
            max_tokens=body.max_tokens,
        ) as stream:
            message = await stream.get_final_message()

        parts: list[str] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
        text = "".join(parts).strip()

        return {
            "ok": True,
            "response": text,
            "model": model,
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
