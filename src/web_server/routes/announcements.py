"""Timed announcements endpoints — queue, edit, schedule, and clone channel posts.

Admin-only. The route computes ``post_at`` (UTC epoch) from guild-local
date/time via the guild's fixed ``tz_offset_hours``; the bot-side loop in
``announcements_service`` does the actual posting. Snowflakes are stringified
in responses (they overflow JS number precision).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from bot_modules.announcements.buttons import (
    BUTTON_STYLES,
    DEFAULT_STYLE,
    MAX_BUTTONS,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_tz_offset_hours
from bot_modules.core.role_safety import role_block_reason
from bot_modules.core.utils import get_bot_member
from bot_modules.services.announcements_service import (
    VALID_MENTION_KINDS,
    clone_announcement,
    compute_post_at,
    create_announcement,
    delete_announcement,
    get_announcement,
    list_announcements,
    list_buttons,
    replace_buttons,
    update_announcement,
)
from bot_modules.services.branding_service import DEFAULT_ACCENT
from web_server.auth import AuthenticatedUser
from web_server.deps import get_active_guild_id, get_ctx, require_perms, run_query

log = logging.getLogger("dungeonkeeper.web.announcements")

router = APIRouter()

require_admin = require_perms({"admin"})

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# Discord's own custom-emoji form, matching PartialEmoji.from_str's regex.
_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[a-zA-Z0-9_]{1,32}:[0-9]{15,20}>$")


# ── Pydantic models ──────────────────────────────────────────────────────────

class ButtonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: str
    label: str = Field(default="", max_length=80)
    emoji: str = Field(default="", max_length=64)
    style: str = DEFAULT_STYLE


class AnnouncementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    title: str = Field(default="", max_length=256)
    body: str = Field(default="", max_length=4096)
    image_url: Optional[str] = Field(default=None, max_length=1024)
    accent_hex: Optional[str] = None
    plain_text: Optional[str] = Field(default=None, max_length=300)
    mention_kind: str = "none"
    mention_role_id: Optional[str] = None
    post_date: Optional[str] = None       # guild-local "YYYY-MM-DD"
    post_time: Optional[str] = None       # guild-local "HH:MM"
    buttons: list[ButtonBody] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_time_of_day(raw: str) -> int:
    """Parse 'HH:MM' into minutes since local midnight (0..1439)."""
    try:
        hh, mm = raw.split(":")
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="post_time must be 'HH:MM'")
    if not 0 <= minutes < 24 * 60:
        raise HTTPException(status_code=400, detail="post_time out of range")
    return minutes


def _validate(body: AnnouncementBody) -> dict:
    """Validate body shape; return normalized column values (sans post_at)."""
    try:
        channel_id = int(body.channel_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="channel_id must be numeric")

    if not body.title.strip() and not body.body.strip():
        raise HTTPException(status_code=400, detail="Give it a title or a body")

    if body.mention_kind not in VALID_MENTION_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid mention_kind: {body.mention_kind}")
    mention_role_id: int | None = None
    if body.mention_kind == "role":
        try:
            mention_role_id = int(body.mention_role_id or "")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Pick a role to mention")

    image_url = (body.image_url or "").strip() or None
    if image_url and not image_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="image_url must be http(s)")

    accent_hex = (body.accent_hex or "").strip() or None
    if accent_hex:
        if not _HEX_RE.match(accent_hex):
            raise HTTPException(status_code=400, detail="accent_hex must be 6 hex digits")
        accent_hex = accent_hex.lstrip("#").upper()

    if (body.post_date is None) != (body.post_time is None):
        raise HTTPException(status_code=400, detail="Set both a date and a time, or neither")
    post_time_min: int | None = None
    post_date: str | None = None
    if body.post_date is not None:
        post_time_min = _parse_time_of_day(body.post_time or "")
        try:
            time.strptime(body.post_date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="post_date must be 'YYYY-MM-DD'")
        post_date = body.post_date

    return {
        "channel_id": channel_id,
        "title": body.title.strip(),
        "body": body.body,
        "image_url": image_url,
        "accent_hex": accent_hex,
        "plain_text": (body.plain_text or "").strip() or None,
        "mention_kind": body.mention_kind,
        "mention_role_id": mention_role_id,
        "post_date": post_date,
        "post_time_min": post_time_min,
    }


def _validate_emoji(raw: str) -> str:
    """Reject text that only *looks* like an emoji to discord.py.

    ``PartialEmoji.from_str`` never fails: it wraps anything unmatched as a
    unicode emoji named after the raw string, so "notanemoji" sails through
    here and comes back as a 400 from Discord at post time — after the
    announcement is already marked sent-with-an-error. Catching it on save
    turns a surprise into a form error.
    """
    if not raw or _CUSTOM_EMOJI_RE.match(raw):
        return raw
    # Real unicode emoji carry no ASCII; this also passes flags, skin-tone
    # modifiers and ZWJ sequences without enumerating them.
    if any(ord(ch) < 128 for ch in raw):
        raise HTTPException(
            status_code=400,
            detail=f"“{raw}” isn't an emoji — paste one, or leave the box empty.",
        )
    return raw


def _validate_buttons(body: AnnouncementBody) -> list[dict]:
    """Shape-check the role buttons; return normalized rows in submitted order.

    Guild-side safety (does the role exist, can we grant it, is it dangerous)
    needs the gateway and happens in ``_check_buttons_against_guild``.
    """
    if len(body.buttons) > MAX_BUTTONS:
        raise HTTPException(
            status_code=400,
            detail=f"An announcement can carry at most {MAX_BUTTONS} role buttons",
        )
    out: list[dict] = []
    seen: set[int] = set()
    for btn in body.buttons:
        try:
            role_id = int(btn.role_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Pick a role for every button")
        if role_id <= 0:
            raise HTTPException(status_code=400, detail="Pick a role for every button")
        if role_id in seen:
            # Two buttons for one role would toggle each other — always a mistake.
            raise HTTPException(
                status_code=400, detail="Two buttons can't offer the same role"
            )
        seen.add(role_id)
        if btn.style not in BUTTON_STYLES:
            raise HTTPException(status_code=400, detail=f"Invalid button style: {btn.style}")
        out.append({
            "role_id": role_id,
            "label": btn.label.strip(),
            "emoji": _validate_emoji(btn.emoji.strip()),
            "style": btn.style,
        })
    return out


def _check_buttons_against_guild(ctx, guild_id: int, buttons: list[dict]) -> None:
    """Refuse any role we can't safely let members self-assign.

    Unlike role menus there is no elevated-permission override: an announcement
    is a public, permanent post, so a role carrying mod powers is simply not
    allowed on one. The bot re-checks this at click time too — this is the
    early, legible failure.
    """
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is None:
        return  # off-gateway (e.g. tests) — the click-path check still stands
    bot_member = get_bot_member(guild)
    for btn in buttons:
        reason = role_block_reason(guild.get_role(btn["role_id"]), bot_member)
        if reason is not None:
            raise HTTPException(status_code=400, detail=f"Can't add that button — {reason}.")


def _channel_in_guild(ctx, guild_id: int, channel_id: int) -> bool:
    """True if the live bot can see this channel in the active guild (best-effort)."""
    bot = getattr(ctx, "bot", None)
    if bot is None:
        return True  # bot not attached (e.g. tests) — skip the guard
    guild = bot.get_guild(guild_id)
    if guild is None:
        return True
    return guild.get_channel(channel_id) is not None


def _button_dict(row: sqlite3.Row) -> dict:
    return {
        "role_id": str(row["role_id"]),
        "label": row["label"],
        "emoji": row["emoji"],
        "style": row["style"],
    }


def _ann_dict(row: sqlite3.Row, guild_id: int, buttons: list[sqlite3.Row]) -> dict:
    jump_url = None
    if row["sent_channel_id"] and row["sent_message_id"]:
        jump_url = (
            f"https://discord.com/channels/{guild_id}"
            f"/{int(row['sent_channel_id'])}/{int(row['sent_message_id'])}"
        )
    return {
        "id": int(row["id"]),
        "channel_id": str(row["channel_id"]),
        "title": row["title"],
        "body": row["body"],
        "image_url": row["image_url"],
        "accent_hex": row["accent_hex"],
        "plain_text": row["plain_text"],
        "mention_kind": row["mention_kind"],
        "mention_role_id": str(row["mention_role_id"]) if row["mention_role_id"] else None,
        "post_date": row["post_date"],
        "post_time_min": row["post_time_min"],
        "post_at": row["post_at"],
        "status": row["status"],
        "sent_at": row["sent_at"],
        "error": row["error"],
        "jump_url": jump_url,
        "buttons": [_button_dict(b) for b in buttons],
        "created_by": str(row["created_by"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _compute_schedule(conn, guild_id: int, fields: dict, now: float) -> tuple[float | None, str]:
    """Derive (post_at, status) from validated wall-clock fields; 400 on past times."""
    if fields["post_date"] is None:
        return None, "draft"
    offset = get_tz_offset_hours(conn, guild_id)
    post_at = compute_post_at(fields["post_date"], fields["post_time_min"], offset)
    if post_at < now:
        raise HTTPException(status_code=400, detail="That date/time is in the past")
    return post_at, "scheduled"


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("")
async def list_all(
    request: Request,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    default_accent = DEFAULT_ACCENT
    bot = getattr(ctx, "bot", None)
    guild = bot.get_guild(guild_id) if bot else None
    if guild is not None:
        default_accent = (await resolve_accent_color(ctx.db_path, guild)).value

    def _q():
        with ctx.open_db() as conn:
            rows = list_announcements(conn, guild_id)
            return {
                "items": [
                    _ann_dict(r, guild_id, list_buttons(conn, int(r["id"])))
                    for r in rows
                ],
                "max_buttons": MAX_BUTTONS,
                "tz_offset_hours": get_tz_offset_hours(conn, guild_id),
                "default_accent_hex": f"{default_accent:06X}",
                "guild_id": str(guild_id),
            }

    return await run_query(_q)


@router.post("")
async def create(
    request: Request,
    body: AnnouncementBody,
    user: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    fields = _validate(body)
    buttons = _validate_buttons(body)

    if not _channel_in_guild(ctx, guild_id, fields["channel_id"]):
        raise HTTPException(status_code=400, detail="Channel is not in this server")
    _check_buttons_against_guild(ctx, guild_id, buttons)

    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            post_at, status = _compute_schedule(conn, guild_id, fields, now)
            ann_id = create_announcement(
                conn,
                guild_id=guild_id,
                post_at=post_at,
                status=status,
                created_by=int(user.user_id),
                created_at=now,
                updated_at=now,
                **fields,
            )
            replace_buttons(conn, ann_id, buttons)
        return {"ok": True, "id": ann_id, "status": status, "post_at": post_at}

    return await run_query(_q)


@router.put("/{ann_id}")
async def update(
    ann_id: int,
    request: Request,
    body: AnnouncementBody,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    fields = _validate(body)
    buttons = _validate_buttons(body)

    if not _channel_in_guild(ctx, guild_id, fields["channel_id"]):
        raise HTTPException(status_code=400, detail="Channel is not in this server")
    _check_buttons_against_guild(ctx, guild_id, buttons)

    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            row = get_announcement(conn, ann_id, guild_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Announcement not found")
            if row["status"] == "sent":
                raise HTTPException(status_code=409, detail="Already sent — clone it instead")
            post_at, status = _compute_schedule(conn, guild_id, fields, now)
            # Editing always re-derives status: time set → scheduled, cleared →
            # draft; either way a stale error is wiped.
            if not update_announcement(
                conn, ann_id, guild_id,
                {**fields, "post_at": post_at, "status": status, "error": None},
                now, expected_status=row["status"],
            ):
                # The send loop claimed the row between our read and our write.
                raise HTTPException(
                    status_code=409, detail="Just went out — reload and clone it instead"
                )
            replace_buttons(conn, ann_id, buttons)
        return {"ok": True, "status": status, "post_at": post_at}

    return await run_query(_q)


@router.delete("/{ann_id}")
async def delete(
    ann_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)

    def _q():
        with ctx.open_db() as conn:
            if get_announcement(conn, ann_id, guild_id) is None:
                raise HTTPException(status_code=404, detail="Announcement not found")
            delete_announcement(conn, ann_id, guild_id)
        return {"ok": True}

    return await run_query(_q)


@router.post("/{ann_id}/post-now")
async def post_now(
    ann_id: int,
    request: Request,
    _: AuthenticatedUser = Depends(require_admin),
):
    """Arm the announcement to fire on the loop's next poll (≤ ~1 minute)."""
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            row = get_announcement(conn, ann_id, guild_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Announcement not found")
            if row["status"] == "sent":
                raise HTTPException(status_code=409, detail="Already sent — clone it instead")
            if not update_announcement(
                conn, ann_id, guild_id,
                {"status": "scheduled", "post_at": now, "error": None},
                now, expected_status=row["status"],
            ):
                raise HTTPException(
                    status_code=409, detail="Just went out — reload and clone it instead"
                )
        return {"ok": True}

    return await run_query(_q)


@router.post("/{ann_id}/clone")
async def clone(
    ann_id: int,
    request: Request,
    user: AuthenticatedUser = Depends(require_admin),
):
    ctx = get_ctx(request)
    guild_id = get_active_guild_id(request)
    now = time.time()

    def _q():
        with ctx.open_db() as conn:
            new_id = clone_announcement(conn, ann_id, guild_id, int(user.user_id), now)
            if new_id is None:
                raise HTTPException(status_code=404, detail="Announcement not found")
        return {"ok": True, "id": new_id}

    return await run_query(_q)
