"""Timed announcements: dashboard-queued one-shot channel posts.

Three layers live here (same shape as scheduled_games_service):
  * Pure builders — ``compute_post_at`` / ``build_announcement_message`` (unit-tested, no I/O).
  * Sync CRUD over a sqlite3 connection — used by the web route via ``run_query``.
  * The async polling loop ``announcements_loop`` — registered as a bot startup task.

An announcement may also carry up to five self-assign role buttons
(``announcement_buttons``); the components themselves live in
``bot_modules.announcements.buttons``, which owns the click path and its
safety re-check.

Wall-clock fields (``post_date``, ``post_time_min``) are the source of truth;
``post_at`` is a derived UTC-epoch cache the loop polls (guild-local per the fixed
``tz_offset_hours``, no DST). ``post_at IS NULL`` means draft — invisible to the loop.

Crash safety: the loop atomically claims a row (``status='scheduled'`` → ``'sent'``,
checked via rowcount) *before* awaiting the send, so a crash mid-send can never
double-post; the worst case is a row marked sent with no message id. A row found
more than ``MAX_LATE_SECONDS`` past its slot is marked ``error`` instead of posting
stale news late.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

import discord

from bot_modules.announcements.buttons import (
    DEFAULT_STYLE,
    MAX_BUTTONS,
    build_announcement_view,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import open_db
from bot_modules.services.branding_service import DEFAULT_ACCENT

log = logging.getLogger(__name__)

VALID_MENTION_KINDS = ("none", "role", "everyone")
VALID_STATUSES = ("draft", "scheduled", "sent", "error")

# How far past its slot a scheduled announcement may still fire (covers restarts);
# beyond this it's marked error — a stale announcement is worse than none.
MAX_LATE_SECONDS = 2 * 3600
MISSED_ERROR = "Missed post window (bot was offline)"

_EPOCH = datetime(1970, 1, 1)


# ── Pure builders ───────────────────────────────────────────────────────────

def compute_post_at(post_date: str, post_time_min: int, offset_hours: float) -> float:
    """Guild-local date + minutes-since-midnight → UTC epoch (local = UTC + offset)."""
    d = datetime.strptime(post_date, "%Y-%m-%d")
    local = datetime(d.year, d.month, d.day) + timedelta(minutes=int(post_time_min))
    utc_naive = local - timedelta(hours=offset_hours)
    return (utc_naive - _EPOCH).total_seconds()


def build_announcement_message(
    row: Mapping, accent: discord.Color
) -> tuple[str | None, discord.Embed, discord.AllowedMentions]:
    """Compose (content, embed, allowed_mentions) for a row.

    The AllowedMentions object mirrors ``mention_kind`` exactly — nothing pings
    unless the admin explicitly picked it, whatever the text contains.
    """
    kind = row["mention_kind"]
    role_id = row["mention_role_id"]

    prefix = ""
    allowed = discord.AllowedMentions.none()
    if kind == "everyone":
        prefix = "@everyone"
        allowed = discord.AllowedMentions(everyone=True, users=False, roles=False)
    elif kind == "role" and role_id:
        prefix = f"<@&{int(role_id)}>"
        allowed = discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(id=int(role_id))]
        )

    plain = (row["plain_text"] or "").strip()
    content = " ".join(p for p in (prefix, plain) if p) or None

    color = accent
    if row["accent_hex"]:
        try:
            color = discord.Color(int(str(row["accent_hex"]).lstrip("#"), 16))
        except ValueError:
            pass

    embed = discord.Embed(
        title=row["title"] or None,
        description=row["body"] or None,
        color=color,
    )
    if row["image_url"]:
        embed.set_image(url=row["image_url"])
    return content, embed, allowed


# ── Sync CRUD (web route, via run_query) ────────────────────────────────────

_INSERT_COLS = (
    "guild_id", "channel_id", "title", "body", "image_url", "accent_hex",
    "plain_text", "mention_kind", "mention_role_id", "post_date", "post_time_min",
    "post_at", "status", "created_by", "created_at", "updated_at",
)

_UPDATABLE_COLS = {
    "channel_id", "title", "body", "image_url", "accent_hex", "plain_text",
    "mention_kind", "mention_role_id", "post_date", "post_time_min", "post_at",
    "status", "error",
}

# What a clone carries over — content only, never schedule/sent/error state.
# (Role buttons ride along too, copied separately in clone_announcement.)
_CLONE_COLS = (
    "channel_id", "title", "body", "image_url", "accent_hex",
    "plain_text", "mention_kind", "mention_role_id",
)

_BUTTON_COLS = ("role_id", "label", "emoji", "style", "position")


def list_buttons(conn: sqlite3.Connection, ann_id: int) -> list[sqlite3.Row]:
    """This announcement's role buttons, left to right."""
    return conn.execute(
        "SELECT * FROM announcement_buttons WHERE announcement_id = ? "
        "ORDER BY position ASC, id ASC",
        (ann_id,),
    ).fetchall()


def replace_buttons(
    conn: sqlite3.Connection, ann_id: int, buttons: list[dict]
) -> None:
    """Swap in a whole button set; array order becomes ``position``.

    Wholesale replacement (role menus' ``replace_options`` idiom) — the editor
    always submits the full list, so diffing rows would only add ways to drift.
    """
    conn.execute("DELETE FROM announcement_buttons WHERE announcement_id = ?", (ann_id,))
    for pos, btn in enumerate(buttons[:MAX_BUTTONS]):
        conn.execute(
            "INSERT INTO announcement_buttons "
            f"(announcement_id, {', '.join(_BUTTON_COLS)}) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ann_id, int(btn["role_id"]), btn.get("label", "") or "",
                btn.get("emoji", "") or "", btn.get("style") or DEFAULT_STYLE, pos,
            ),
        )


def create_announcement(conn: sqlite3.Connection, **fields) -> int:
    placeholders = ", ".join("?" for _ in _INSERT_COLS)
    cols = ", ".join(_INSERT_COLS)
    cur = conn.execute(
        f"INSERT INTO announcements ({cols}) VALUES ({placeholders})",
        tuple(fields[c] for c in _INSERT_COLS),
    )
    return int(cur.lastrowid or 0)


def list_announcements(conn: sqlite3.Connection, guild_id: int) -> list[sqlite3.Row]:
    """All rows: pending (draft/scheduled/error) by slot then age, sent last, newest first."""
    return conn.execute(
        "SELECT * FROM announcements WHERE guild_id = ? "
        "ORDER BY (status = 'sent') ASC, "
        "CASE WHEN status = 'sent' THEN -COALESCE(sent_at, 0) "
        "     ELSE COALESCE(post_at, 9e15) END ASC, id ASC",
        (guild_id,),
    ).fetchall()


def get_announcement(conn: sqlite3.Connection, ann_id: int, guild_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM announcements WHERE id = ? AND guild_id = ?",
        (ann_id, guild_id),
    ).fetchone()


def update_announcement(
    conn: sqlite3.Connection, ann_id: int, guild_id: int, fields: dict, now: float
) -> None:
    cols = [c for c in fields if c in _UPDATABLE_COLS]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)
    params = [fields[c] for c in cols] + [now, ann_id, guild_id]
    conn.execute(
        f"UPDATE announcements SET {assignments}, updated_at = ? WHERE id = ? AND guild_id = ?",
        params,
    )


def delete_announcement(conn: sqlite3.Connection, ann_id: int, guild_id: int) -> None:
    cur = conn.execute(
        "DELETE FROM announcements WHERE id = ? AND guild_id = ?", (ann_id, guild_id)
    )
    if cur.rowcount:
        # Foreign keys aren't enforced on this connection, so the child rows go
        # explicitly — and only once the guild-scoped delete actually matched.
        conn.execute(
            "DELETE FROM announcement_buttons WHERE announcement_id = ?", (ann_id,)
        )


def clone_announcement(
    conn: sqlite3.Connection, ann_id: int, guild_id: int, created_by: int, now: float
) -> int | None:
    """Copy a row's content into a fresh draft (no schedule, no sent state)."""
    src = get_announcement(conn, ann_id, guild_id)
    if src is None:
        return None
    fields = {c: src[c] for c in _CLONE_COLS}
    fields.update(
        guild_id=guild_id, post_date=None, post_time_min=None, post_at=None,
        status="draft", created_by=created_by, created_at=now, updated_at=now,
    )
    new_id = create_announcement(conn, **fields)
    replace_buttons(
        conn, new_id, [dict(b) for b in list_buttons(conn, ann_id)]
    )
    return new_id


def fetch_due(conn: sqlite3.Connection, now: float) -> list[sqlite3.Row]:
    """Due scheduled rows across all guilds (the loop is global)."""
    return conn.execute(
        "SELECT * FROM announcements "
        "WHERE status = 'scheduled' AND post_at IS NOT NULL AND post_at <= ?",
        (now,),
    ).fetchall()


def claim(conn: sqlite3.Connection, ann_id: int, now: float) -> bool:
    """Atomically move scheduled → sent; False means another pass (or an edit) won."""
    cur = conn.execute(
        "UPDATE announcements SET status = 'sent', sent_at = ?, updated_at = ? "
        "WHERE id = ? AND status = 'scheduled'",
        (now, now, ann_id),
    )
    return cur.rowcount == 1


def mark_sent(
    conn: sqlite3.Connection, ann_id: int, channel_id: int, message_id: int
) -> None:
    conn.execute(
        "UPDATE announcements SET sent_channel_id = ?, sent_message_id = ? WHERE id = ?",
        (channel_id, message_id, ann_id),
    )


def mark_error(conn: sqlite3.Connection, ann_id: int, error: str, now: float) -> None:
    conn.execute(
        "UPDATE announcements SET status = 'error', error = ?, sent_at = NULL, "
        "updated_at = ? WHERE id = ?",
        (error, now, ann_id),
    )


# ── Async polling loop ──────────────────────────────────────────────────────

def _db_call(db_path: Path, fn, *args):
    with open_db(db_path) as conn:
        return fn(conn, *args)


async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except Exception:
        return None


async def _process_due(bot, db_path: Path, row, now: float) -> None:
    ann_id = row["id"]

    if not await asyncio.to_thread(_db_call, db_path, claim, ann_id, now):
        return  # raced by a concurrent pass or an admin edit

    if now - row["post_at"] > MAX_LATE_SECONDS:
        log.warning("Announcement %s: %.0fs past its slot, marking missed", ann_id, now - row["post_at"])
        await asyncio.to_thread(_db_call, db_path, mark_error, ann_id, MISSED_ERROR, now)
        return

    channel = await _resolve_channel(bot, row["channel_id"])
    if channel is None:
        log.warning("Announcement %s: channel %s unreachable", ann_id, row["channel_id"])
        await asyncio.to_thread(_db_call, db_path, mark_error, ann_id, "Channel unreachable", now)
        return

    guild = bot.get_guild(row["guild_id"])
    if guild is not None:
        accent = await resolve_accent_color(db_path, guild)
    else:
        accent = discord.Color(DEFAULT_ACCENT)

    content, embed, allowed = build_announcement_message(row, accent)
    buttons = await asyncio.to_thread(_db_call, db_path, list_buttons, ann_id)
    view = build_announcement_view(buttons, guild)
    try:
        # view=None is what discord.py already means by "no components", so the
        # no-buttons case needs no branch here.
        msg = await channel.send(
            content=content, embed=embed, allowed_mentions=allowed, view=view
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("Announcement %s: send failed in channel %s: %s", ann_id, row["channel_id"], e)
        await asyncio.to_thread(_db_call, db_path, mark_error, ann_id, f"Send failed: {e}", now)
        return

    await asyncio.to_thread(_db_call, db_path, mark_sent, ann_id, channel.id, msg.id)
    log.info("Announcement %s posted to channel %s", ann_id, channel.id)


async def announcements_loop(bot, db_path: Path) -> None:
    """Poll every 60s and post due announcements. Registered as a bot startup task."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = time.time()
            rows = await asyncio.to_thread(_db_call, db_path, fetch_due, now)
            for row in rows:
                try:
                    await _process_due(bot, db_path, row, now)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("announcement %s failed to process", row["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("announcements_loop iteration error")
        await asyncio.sleep(60)
