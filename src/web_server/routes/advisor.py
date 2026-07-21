"""Billy-bot endpoint — powers the Help panel's "Ask Billy-bot" box.

Grounded, member-facing help (not admin config), so it's open to any
authenticated dashboard user. The heavy lifting lives in
``bot_modules.services.advisor_service``; this is thin glue. Rate limiting is
handled by the ``ai`` tier in ``server.py`` (see ``_TIER_ROUTES``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from web_server.auth import AuthenticatedUser
from web_server.deps import require_perms

router = APIRouter()


class AdvisorBody(BaseModel):
    question: str
    # Prior [{role, content}] turns for a multi-message chat; sanitized service-side.
    history: list[dict] | None = None


@router.post("/help/advisor")
async def help_advisor(
    body: AdvisorBody,
    # Empty perm set = "any authenticated user" (help is not admin config).
    _: AuthenticatedUser = Depends(require_perms(set())),
):
    from bot_modules.services.advisor_service import answer_advisor

    result = await answer_advisor(body.question, body.history)
    return {"ok": result.ok, "answer": result.answer}
