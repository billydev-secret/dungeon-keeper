"""Bot-synced intake procedure reference (the #welcome-procedure content).

The procedure text + question lists live on the dashboard as an ordered
list of **blocks** (config value ``intake_reference_blocks``); the bot keeps
a configured channel in sync with them:

* a ``text`` block renders as one message (chunked if very long);
* a ``questions`` block renders as an optional bold header message plus
  **one message per question**, so a greeter can Copy Text on exactly the
  question they need.

Sync is a position-wise diff against ``intake_reference_messages``
(migration 116): unchanged positions are left alone, changed ones are
edited in place (message ids — and any links to them — stay stable),
extras are posted, surplus messages deleted. The bot only ever touches
messages it tracks; human posts in the channel are ignored.

A one-time **import** turns the channel's existing human-posted history
into draft text blocks so this guild's real content seeds the editor
without retyping (generic: any guild, any channel).

Pure logic (parse / validate / render / diff) is all unit-testable without
Discord; only :func:`sync_channel` and :func:`import_channel` touch it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_config_value, set_config_value

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger(__name__)

BLOCKS_KEY = "intake_reference_blocks"
CHANNEL_KEY = "intake_reference_channel_id"

KIND_TEXT = "text"
KIND_QUESTIONS = "questions"
KINDS = (KIND_TEXT, KIND_QUESTIONS)

#: Discord's message cap is 2000; leave headroom for markdown we add.
_CHUNK_LIMIT = 1900
_IMPORT_HISTORY_LIMIT = 200


@dataclass(frozen=True)
class Block:
    kind: str
    title: str = ""
    body: str = ""


# ---------------------------------------------------------------------------
# Parse / validate
# ---------------------------------------------------------------------------


def parse_blocks(raw: str) -> list[Block]:
    """Stored-config parser — tolerant; invalid entries drop individually."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    blocks: list[Block] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "")
        if kind not in KINDS or not (title or body.strip()):
            continue
        blocks.append(Block(kind, title, body))
    return blocks


def validate_blocks(entries: list[dict]) -> str:
    """Dashboard-save validator — strict where :func:`parse_blocks` is
    tolerant, so the editor hears about a bad block instead of losing it.
    Returns the canonical JSON to store; raises ``ValueError`` with a
    user-facing message otherwise.
    """
    if len(entries) > 100:
        raise ValueError("At most 100 blocks.")
    out = []
    for i, e in enumerate(entries, start=1):
        kind = str(e.get("kind") or "").strip()
        title = str(e.get("title") or "").strip()
        body = str(e.get("body") or "")
        if kind not in KINDS:
            raise ValueError(f"Block {i}: unknown kind '{kind}'.")
        if not (title or body.strip()):
            raise ValueError(f"Block {i}: needs a title or some content.")
        if kind == KIND_QUESTIONS and not _question_lines(body):
            raise ValueError(f"Block {i}: a question list needs at least one question.")
        out.append({"kind": kind, "title": title, "body": body})
    return json.dumps(out)


def blocks_config(conn: sqlite3.Connection, guild_id: int) -> list[Block]:
    return parse_blocks(get_config_value(conn, BLOCKS_KEY, "", guild_id))


def reference_channel_id(conn: sqlite3.Connection, guild_id: int) -> int:
    try:
        return int(get_config_value(conn, CHANNEL_KEY, "0", guild_id))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _question_lines(body: str) -> list[str]:
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _chunk_text(content: str) -> list[str]:
    """Split a long text into ≤ limit messages, preferring paragraph then
    line boundaries; a single oversized line hard-splits as a last resort."""
    if len(content) <= _CHUNK_LIMIT:
        return [content]
    chunks: list[str] = []
    current = ""
    for para in content.split("\n\n"):
        pieces = [para] if len(para) <= _CHUNK_LIMIT else para.splitlines()
        for piece in pieces:
            while len(piece) > _CHUNK_LIMIT:  # pathological single line
                chunks.append(piece[:_CHUNK_LIMIT])
                piece = piece[_CHUNK_LIMIT:]
            joined = f"{current}\n\n{piece}" if current else piece
            if len(joined) > _CHUNK_LIMIT:
                chunks.append(current)
                current = piece
            else:
                current = joined
    if current:
        chunks.append(current)
    return chunks


def render_blocks(blocks: list[Block]) -> list[str]:
    """The full channel as an ordered list of message contents."""
    messages: list[str] = []
    for b in blocks:
        if b.kind == KIND_QUESTIONS:
            if b.title:
                messages.append(f"**{b.title}**")
            messages.extend(_question_lines(b.body))
        else:
            content = f"**{b.title}**\n{b.body}".strip() if b.title else b.body.strip()
            messages.extend(_chunk_text(content))
    return messages


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Diff + mapping
# ---------------------------------------------------------------------------


def diff_messages(
    rendered: list[str], stored: list[tuple[int, str]]
) -> tuple[list[tuple[str, int, str]], list[int]]:
    """Position-wise sync plan.

    ``stored`` is ``(message_id, content_hash)`` per position. Returns
    ``(ops, deletes)`` where each op is ``("keep"|"edit", message_id,
    content)`` for positions that already have a message and ``("post", 0,
    content)`` for new tail positions; ``deletes`` are surplus message ids.
    Unchanged positions are kept untouched, so ids stay stable across
    wording edits and appends.
    """
    ops: list[tuple[str, int, str]] = []
    for i, content in enumerate(rendered):
        if i < len(stored):
            mid, stored_hash = stored[i]
            if stored_hash == content_hash(content):
                ops.append(("keep", mid, content))
            else:
                ops.append(("edit", mid, content))
        else:
            ops.append(("post", 0, content))
    deletes = [mid for mid, _ in stored[len(rendered):]]
    return ops, deletes


def stored_messages(conn: sqlite3.Connection, guild_id: int) -> list[tuple[int, str]]:
    return [
        (int(r["message_id"]), str(r["content_hash"]))
        for r in conn.execute(
            "SELECT message_id, content_hash FROM intake_reference_messages "
            "WHERE guild_id = ? ORDER BY position",
            (guild_id,),
        ).fetchall()
    ]


def replace_mapping(
    conn: sqlite3.Connection, guild_id: int, mapping: list[tuple[int, str]]
) -> None:
    conn.execute(
        "DELETE FROM intake_reference_messages WHERE guild_id = ?", (guild_id,)
    )
    conn.executemany(
        "INSERT INTO intake_reference_messages "
        "(guild_id, position, message_id, content_hash) VALUES (?, ?, ?, ?)",
        [(guild_id, i, mid, h) for i, (mid, h) in enumerate(mapping)],
    )


# ---------------------------------------------------------------------------
# Import (seed the editor from a channel's existing content)
# ---------------------------------------------------------------------------


def blocks_from_messages(contents: list[str]) -> list[dict]:
    """Draft blocks from raw channel history: one text block per message.

    The admin splits question lists out in the editor afterwards — guessing
    which walls of text are question lists is their call, not ours.
    """
    return [
        {"kind": KIND_TEXT, "title": "", "body": c.strip()}
        for c in contents
        if c.strip()
    ]


# ---------------------------------------------------------------------------
# Discord side
# ---------------------------------------------------------------------------


async def sync_channel(ctx: AppContext, guild: discord.Guild) -> dict:
    """Reconcile the reference channel with the configured blocks.

    Returns counts for the dashboard's save feedback. A tracked message
    someone hand-deleted is reposted (the 404 edit falls back to a send);
    only tracked messages are ever edited or deleted.
    """

    def _load():
        with ctx.open_db() as conn:
            return (
                reference_channel_id(conn, guild.id),
                blocks_config(conn, guild.id),
                stored_messages(conn, guild.id),
            )

    channel_id, blocks, stored = await asyncio.to_thread(_load)
    if channel_id <= 0:
        return {"synced": False, "reason": "no channel configured"}
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return {"synced": False, "reason": "channel not found"}

    ops, deletes = diff_messages(render_blocks(blocks), stored)
    mapping: list[tuple[int, str]] = []
    edited = posted = deleted = 0
    for op, mid, content in ops:
        h = content_hash(content)
        if op == "keep":
            mapping.append((mid, h))
            continue
        if op == "edit":
            try:
                await channel.get_partial_message(mid).edit(content=content)
                mapping.append((mid, h))
                edited += 1
                continue
            except discord.NotFound:
                pass  # someone deleted it by hand — fall through to repost
            except discord.HTTPException:
                log.warning("intake reference: edit failed in guild %s", guild.id)
                mapping.append((mid, h))  # keep tracking; retry next save
                continue
        try:
            sent = await channel.send(
                content, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.warning("intake reference: post failed in guild %s", guild.id)
            break  # keep mapping consistent with what actually exists
        mapping.append((sent.id, h))
        posted += 1
    for mid in deletes:
        try:
            await channel.get_partial_message(mid).delete()
            deleted += 1
        except discord.HTTPException:
            log.debug("intake reference: delete failed", exc_info=True)

    def _store():
        with ctx.open_db() as conn:
            replace_mapping(conn, guild.id, mapping)

    await asyncio.to_thread(_store)
    return {"synced": True, "edited": edited, "posted": posted, "deleted": deleted}


async def import_channel(
    ctx: AppContext, guild: discord.Guild, channel_id: int
) -> list[dict]:
    """Seed draft blocks from a channel's existing messages (oldest first).

    Raises ``ValueError`` (user-facing message) when the channel is missing
    or the editor already has content — import never overwrites work.
    """

    def _existing():
        with ctx.open_db() as conn:
            return blocks_config(conn, guild.id)

    if await asyncio.to_thread(_existing):
        raise ValueError("The editor already has blocks — import won't overwrite them.")
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ValueError("That channel isn't available to the bot.")
    contents: list[str] = []
    try:
        async for msg in channel.history(
            limit=_IMPORT_HISTORY_LIMIT, oldest_first=True
        ):
            contents.append(msg.content)
    except discord.HTTPException as exc:
        raise ValueError("Couldn't read the channel history.") from exc
    blocks = blocks_from_messages(contents)
    if not blocks:
        raise ValueError("No text content found in that channel.")

    def _store():
        with ctx.open_db() as conn:
            set_config_value(conn, BLOCKS_KEY, json.dumps(blocks), guild.id)

    await asyncio.to_thread(_store)
    return blocks
