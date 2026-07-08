"""Docs endpoints — author single-source markdown, render as embeds anywhere.

Authoring surface for the ``docs`` system. Because the dashboard shares the
bot's event loop, a save (``PUT``) re-renders every channel the doc is posted in
*immediately* — the whole point of "maintain in one place".
"""

from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import discord
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import BaseModel

from bot_modules.docs import db as docs_db
from bot_modules.docs import sync as docs_sync
from bot_modules.docs.render import render_doc
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query
from web_server.helpers import public_base_url

router = APIRouter()

_MOD = Depends(require_perms({"moderator"}))

# Mod-uploaded images are served unauthenticated from the public /static mount,
# so keep the surface tight: raster only (no SVG — that's a stored-XSS vector on
# our own domain), 8 MB ceiling (Discord's display limit), uuid filenames.
_IMAGE_DIR = Path(__file__).resolve().parent.parent / "static" / "doc-images"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _sniff_image_ext(data: bytes) -> str | None:
    """Return a safe extension from magic bytes, or None if unsupported."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class DocCreateBody(BaseModel):
    doc_key: str
    title: str = ""
    body_md: str = ""
    accent: str = ""


class DocUpdateBody(BaseModel):
    title: str = ""
    body_md: str = ""
    accent: str = ""


class PreviewBody(BaseModel):
    title: str = ""
    body_md: str = ""


class PlacementBody(BaseModel):
    channel_id: str


class PinBody(BaseModel):
    pinned: bool


def _sync_json(r: docs_sync.SyncResult) -> dict:
    return {
        "channel_id": str(r.channel_id),
        "status": r.status,
        "created": r.created,
        "edited": r.edited,
        "deleted": r.deleted,
        "message_count": len(r.message_ids),
        "detail": r.detail,
        "pinned": r.pinned,
        "pin_detail": r.pin_detail,
    }


def _doc_json(doc: dict, placements: list[dict], *, include_body: bool) -> dict:
    out = {
        "doc_key": doc["doc_key"],
        "title": doc["title"],
        "accent": doc["accent"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
        "updated_by": str(doc["updated_by"]),
        "body_len": len(doc["body_md"] or ""),
        "placements": [
            {
                "channel_id": str(p["channel_id"]),
                "message_count": p["message_count"],
                "pinned": bool(p.get("pinned")),
            }
            for p in placements
        ],
    }
    if include_body:
        out["body_md"] = doc["body_md"]
    return out


def _require_guild(ctx, guild_id: int) -> discord.Guild:
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot is not connected to this server right now.",
        )
    return guild


# ── list / read ─────────────────────────────────────────────────────

@router.get("/docs")
async def list_docs(request: Request, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            docs = docs_db.list_docs(conn, guild_id)
            return [
                _doc_json(d, docs_db.list_placements(conn, d["id"]), include_body=False)
                for d in docs
            ]

    return {"docs": await run_query(_q)}


@router.get("/docs/{doc_key}")
async def get_doc(request: Request, doc_key: str, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            doc = docs_db.get_doc(conn, guild_id, doc_key)
            if doc is None:
                return None
            return _doc_json(doc, docs_db.list_placements(conn, doc["id"]), include_body=True)

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=404, detail="Doc not found.")
    return result


@router.post("/docs/preview")
async def preview_doc(request: Request, body: PreviewBody, _: AuthenticatedUser = _MOD):
    """Render markdown to embed specs without saving (live editor preview)."""
    specs = render_doc(body.title, body.body_md)
    return {
        "embeds": [
            {"description": s.description, "image_url": s.image_url} for s in specs
        ]
    }


@router.post("/docs/images")
async def upload_image(
    request: Request, file: UploadFile = File(...), _: AuthenticatedUser = _MOD
):
    """Store an uploaded image and return the markdown to drop into a doc."""
    data = await file.read(_MAX_IMAGE_BYTES + 1)
    if len(data) > _MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image must be 8 MB or smaller.")
    ext = _sniff_image_ext(data)
    if ext is None:
        raise HTTPException(
            status_code=400, detail="Unsupported image (use PNG, JPG, GIF, or WEBP)."
        )
    _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{uuid4().hex}.{ext}"
    (_IMAGE_DIR / fname).write_bytes(data)
    url = f"{public_base_url()}/static/doc-images/{fname}"
    return {"url": url, "markdown": f"![image]({url})"}


# ── create / update / delete ────────────────────────────────────────

@router.post("/docs")
async def create_doc(request: Request, body: DocCreateBody, user: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    key = docs_db.slugify_key(body.doc_key)
    if not key:
        raise HTTPException(status_code=400, detail="Key must contain letters or digits.")
    if len(body.title) > docs_db.TITLE_MAX_LEN:
        raise HTTPException(status_code=400, detail="Title is too long.")
    if len(body.body_md) > docs_db.BODY_MAX_LEN:
        raise HTTPException(status_code=400, detail="Document body is too long.")

    def _q():
        now = time.time()
        with ctx.open_db() as conn:
            if docs_db.get_doc(conn, guild_id, key):
                return None
            docs_db.create_doc(
                conn, guild_id, key, body.title.strip(), body.body_md,
                body.accent.strip(), user.user_id, now,
            )
            doc = docs_db.get_doc(conn, guild_id, key)
            assert doc is not None  # just inserted
            return _doc_json(doc, [], include_body=True)

    result = await run_query(_q)
    if result is None:
        raise HTTPException(status_code=409, detail=f"A doc named '{key}' already exists.")
    return result


@router.put("/docs/{doc_key}")
async def update_doc(
    request: Request, doc_key: str, body: DocUpdateBody, user: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    if len(body.title) > docs_db.TITLE_MAX_LEN:
        raise HTTPException(status_code=400, detail="Title is too long.")
    if len(body.body_md) > docs_db.BODY_MAX_LEN:
        raise HTTPException(status_code=400, detail="Document body is too long.")

    def _save():
        now = time.time()
        with ctx.open_db() as conn:
            doc = docs_db.get_doc(conn, guild_id, doc_key)
            if doc is None:
                return None
            docs_db.update_doc(
                conn, doc["id"], title=body.title.strip(), body_md=body.body_md,
                accent=body.accent.strip(), user_id=user.user_id, now=now,
            )
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_save)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    # Re-render every placement live.
    sync_results: list[docs_sync.SyncResult] = []
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is not None:
        sync_results = await docs_sync.sync_doc(ctx, guild, doc)

    def _read():
        with ctx.open_db() as conn:
            return docs_db.list_placements(conn, doc["id"])

    placements = await run_query(_read)
    return {
        "ok": True,
        "doc": _doc_json(doc, placements, include_body=True),
        "sync": [_sync_json(r) for r in sync_results],
    }


@router.delete("/docs/{doc_key}")
async def delete_doc(request: Request, doc_key: str, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _load():
        with ctx.open_db() as conn:
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_load)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    # Best-effort: pull the doc's messages down from every channel first.
    def _placements():
        with ctx.open_db() as conn:
            return docs_db.list_placements(conn, doc["id"])

    for p in await run_query(_placements):
        await docs_sync.unpost_doc(ctx, doc, p["channel_id"])

    def _del():
        with ctx.open_db() as conn:
            docs_db.delete_doc(conn, doc["id"])

    await run_query(_del)
    return {"ok": True}


# ── placements ──────────────────────────────────────────────────────

@router.post("/docs/{doc_key}/placements")
async def add_placement(
    request: Request, doc_key: str, body: PlacementBody, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)
    try:
        channel_id = int(body.channel_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid channel.")

    def _load():
        with ctx.open_db() as conn:
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_load)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    result = await docs_sync.post_doc(ctx, guild, doc, channel_id)
    if result.status == "missing_channel":
        raise HTTPException(status_code=400, detail="That channel isn't available.")
    return {"ok": result.status == "ok", "sync": _sync_json(result)}


@router.put("/docs/{doc_key}/placements/{channel_id}/pin")
async def set_placement_pin(
    request: Request,
    doc_key: str,
    channel_id: int,
    body: PinBody,
    _: AuthenticatedUser = _MOD,
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)

    def _load():
        with ctx.open_db() as conn:
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_load)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    result = await docs_sync.set_pin(ctx, guild, doc, channel_id, body.pinned)
    if result is None:
        raise HTTPException(status_code=404, detail="Doc isn't posted in that channel.")
    return {"ok": result.status == "ok" and not result.pin_detail, "sync": _sync_json(result)}


@router.delete("/docs/{doc_key}/placements/{channel_id}")
async def remove_placement(
    request: Request, doc_key: str, channel_id: int, _: AuthenticatedUser = _MOD
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _load():
        with ctx.open_db() as conn:
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_load)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    removed = await docs_sync.unpost_doc(ctx, doc, channel_id)
    return {"ok": removed}


@router.post("/docs/{doc_key}/sync")
async def sync_doc_endpoint(request: Request, doc_key: str, _: AuthenticatedUser = _MOD):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    guild = _require_guild(ctx, guild_id)

    def _load():
        with ctx.open_db() as conn:
            return docs_db.get_doc(conn, guild_id, doc_key)

    doc = await run_query(_load)
    if doc is None:
        raise HTTPException(status_code=404, detail="Doc not found.")

    results = await docs_sync.sync_doc(ctx, guild, doc)
    return {"ok": True, "sync": [_sync_json(r) for r in results]}
