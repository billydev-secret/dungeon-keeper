"""Bot-synced intake procedure reference (the #welcome-procedure content).

The procedure text + question lists live on the dashboard as an ordered
list of **blocks** (config value ``intake_reference_blocks``); the bot keeps
a configured channel in sync with them:

A block's title is always its own bold message, so the content below it is
copy-pasteable as-is (Discord's Copy Text takes the whole message):

* a ``text`` block renders as an optional bold header message plus its body
  as one message (chunked if very long);
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
#: Backstop on the per-sync existence sweep (~1 request per 100 messages).
#: The sweep stops as soon as every tracked message is accounted for, so
#: this only bites when something really is missing *and* the channel has a
#: lot of untracked posts. A sweep that hits it reports ``incomplete``
#: rather than quietly declaring the unread tail intact.
_SWEEP_LIMIT = 500


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
        if kind == KIND_QUESTIONS:
            lines = _question_lines(body)
            if not lines:
                raise ValueError(
                    f"Block {i}: a question list needs at least one question."
                )
            # Each question posts as its own message, so an over-long line
            # would 400 mid-sync and wedge the reconcile. Reject on save.
            over = next((ln for ln in lines if len(ln) > _CHUNK_LIMIT), None)
            if over is not None:
                raise ValueError(
                    f"Block {i}: a question is {len(over)} characters — the "
                    f"limit is {_CHUNK_LIMIT}. Split it into shorter lines."
                )
        if len(title) > _CHUNK_LIMIT:
            raise ValueError(f"Block {i}: the title is too long.")
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
    """Split a long text into ≤ limit messages on line boundaries.

    A flat greedy accumulator over lines: paragraph breaks survive because a
    blank line is just an empty element, and lines are rejoined with the
    ``\\n`` they were split on, so the rendered text always matches the
    editor. A single oversized line hard-splits as a last resort — flushing
    the accumulator **first** so message order is never shuffled.
    """
    if len(content) <= _CHUNK_LIMIT:
        return [content]
    chunks: list[str] = []
    current = ""

    def flush() -> None:
        # rstrip: a chunk boundary that lands on a paragraph break would
        # otherwise post a message ending in blank lines.
        nonlocal current
        trimmed = current.rstrip()
        if trimmed:
            chunks.append(trimmed)
        current = ""

    for line in content.split("\n"):
        while len(line) > _CHUNK_LIMIT:  # pathological single line
            flush()  # keep earlier text ahead of the split pieces
            chunks.append(line[:_CHUNK_LIMIT])
            line = line[_CHUNK_LIMIT:]
        joined = f"{current}\n{line}" if current else line
        if len(joined) > _CHUNK_LIMIT:
            flush()
            current = line
        else:
            current = joined
    flush()
    return chunks


def render_blocks(blocks: list[Block]) -> list[str]:
    """The full channel as an ordered list of message contents.

    A title always posts as its **own** message, never as a first line of the
    body — most text blocks are canned messages a greeter copy-pastes, and
    Discord's Copy Text takes the whole message, so a heading sharing the
    message means trimming it off every single paste. Both block kinds render
    the same way now: heading message, then content message(s).
    """
    messages: list[str] = []
    for b in blocks:
        if b.title:
            messages.append(f"**{b.title}**")
        if b.kind == KIND_QUESTIONS:
            messages.extend(_question_lines(b.body))
        elif b.body.strip():
            messages.extend(_chunk_text(b.body.strip()))
    return messages


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Diff + mapping
# ---------------------------------------------------------------------------


def missing_message_ids(
    stored: list[tuple[int, str]], seen: set[int], horizon: int | None
) -> set[int]:
    """Tracked ids the existence sweep *proved* are gone.

    ``horizon`` is the highest id the sweep actually reached, or ``None``
    when it read the whole window. Ids past a truncated sweep are treated
    as present: calling one missing on incomplete evidence would delete and
    re-send a stretch of real messages, so the fail-safe is to do nothing.
    """
    return {
        mid
        for mid, _ in stored
        if mid not in seen and (horizon is None or mid <= horizon)
    }


def rebuild_gap(
    stored: list[tuple[int, str]],
    missing: set[int] | frozenset[int],
    rendered_count: int,
) -> int | None:
    """First position the channel has to be rebuilt from, or ``None``.

    A gap at or past the rendered range is ``None``: those messages are
    surplus the delete pass removes anyway, so there is nothing to restore
    and no reason to churn the positions above it.
    """
    gap = next((i for i, (mid, _) in enumerate(stored) if mid in missing), None)
    return None if gap is None or gap >= rendered_count else gap


def diff_messages(
    rendered: list[str],
    stored: list[tuple[int, str]],
    missing: set[int] | frozenset[int] = frozenset(),
) -> tuple[list[tuple[str, int, str]], list[int]]:
    """Position-wise sync plan.

    ``stored`` is ``(message_id, content_hash)`` per position. Returns
    ``(ops, deletes)`` where each op is ``("keep"|"edit", message_id,
    content)`` for positions that already have a message and ``("post", 0,
    content)`` for new tail positions; ``deletes`` are surplus message ids.
    Unchanged positions are kept untouched, so ids stay stable across
    wording edits and appends.

    ``missing`` are tracked ids someone deleted by hand. Discord can't
    insert a message into the middle of a channel, so restoring reading
    order means re-sending from the **first** gap onward and deleting the
    stale copies — positions above it still keep their ids. A gap that
    lands past the rendered range is ignored: those messages are surplus
    the delete pass removes anyway.
    """
    gap = rebuild_gap(stored, missing, len(rendered))
    reusable = len(stored) if gap is None else gap
    ops: list[tuple[str, int, str]] = []
    for i, content in enumerate(rendered):
        if i < reusable:
            mid, stored_hash = stored[i]
            if stored_hash == content_hash(content):
                ops.append(("keep", mid, content))
            else:
                ops.append(("edit", mid, content))
        else:
            ops.append(("post", 0, content))
    cut = len(rendered) if gap is None else gap
    deletes = [mid for mid, _ in stored[cut:] if mid not in missing]
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


async def _sweep_existing(
    channel: discord.TextChannel | discord.Thread, stored: list[tuple[int, str]]
) -> tuple[set[int], int | None] | None:
    """Which tracked messages the channel still holds, in one history pass.

    Snowflakes sort by time, so reading ``after`` the oldest tracked message
    bounds the window to exactly the range the bot cares about — one request
    per 100 messages rather than a fetch per tracked position. Returns
    ``(seen_ids, horizon)`` where ``horizon`` is ``None`` for a complete
    sweep and the last id reached for a capped one, or ``None`` overall when
    Discord wouldn't tell us (the caller must then assume nothing is
    missing).
    """
    tracked = {mid for mid, _ in stored}
    seen: set[int] = set()
    read = 0
    last = 0
    try:
        async for msg in channel.history(
            limit=_SWEEP_LIMIT,
            after=discord.Object(id=min(tracked) - 1),
            oldest_first=True,
        ):
            read += 1
            last = msg.id
            if msg.id in tracked:
                seen.add(msg.id)
                if len(seen) == len(tracked):
                    # Nothing is missing — stop rather than reading whatever
                    # humans have posted since. This is the common case, and
                    # it usually lands inside the first request.
                    return seen, None
    except discord.HTTPException:
        log.warning("intake reference: history sweep failed")
        return None
    return seen, (last if read >= _SWEEP_LIMIT else None)


async def sync_channel(ctx: AppContext, guild: discord.Guild) -> dict:
    """Reconcile the reference channel with the configured blocks.

    Returns counts for the dashboard's save feedback. Only tracked messages
    are ever edited or deleted.

    A tracked message someone deleted by hand is reposted: one history
    sweep per sync says which tracked ids still exist, and the diff rebuilds
    from the first gap so the procedure keeps its reading order. Without the
    sweep an unchanged config hashes "keep" at every position and the sync
    makes no Discord calls at all, which is exactly how a hand-deleted
    message used to stay gone forever.
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

    incomplete = False
    missing: set[int] = set()
    if stored:
        swept = await _sweep_existing(channel, stored)
        if swept is None:
            # Couldn't read the channel — report it rather than treating
            # "no information" as "everything was deleted".
            incomplete = True
        else:
            seen, horizon = swept
            # A truncated sweep left part of the channel unread, so a
            # deletion in there goes unnoticed this pass. Say so instead of
            # reporting a clean sync.
            incomplete = horizon is not None
            missing = missing_message_ids(stored, seen, horizon)

    rendered = render_blocks(blocks)
    gap = rebuild_gap(stored, missing, len(rendered))
    ops, deletes = diff_messages(rendered, stored, missing)
    mapping: list[tuple[int, str]] = []
    edited = posted = deleted = repaired = 0
    for index, (op, mid, content) in enumerate(ops):
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
                # Keep the message tracked under its *old* hash: storing the
                # intended new hash would make the next diff emit "keep" and
                # the failed edit would never be retried.
                mapping.append((mid, stored[index][1]))
                incomplete = True
                continue
        try:
            sent = await channel.send(
                content, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            log.warning("intake reference: post failed in guild %s", guild.id)
            # Stop syncing, but keep every still-unprocessed position that
            # already had a message tracked under its old hash — dropping
            # them would orphan real messages the bot then refuses to touch
            # (and would repost them as duplicates on a later save). Bound
            # the slice to the rendered range: anything past it is surplus
            # the delete pass below is about to remove.
            mapping.extend(stored[index:len(ops)])
            incomplete = True
            break
        mapping.append((sent.id, h))
        posted += 1
        # Count a restore only once the replacement is really in the
        # channel, so the "restored" the panel prints is what happened
        # rather than what was planned.
        if gap is not None and index < len(stored) and stored[index][0] in missing:
            repaired += 1
    # A rebuild that dies mid-send keeps the old ids tracked for the
    # positions it never reached (see the send-failure branch above) *and*
    # deletes those messages here. That pairing is deliberate: the next
    # sync's sweep finds them missing and rebuilds from there, so the
    # channel heals into the right order. Skipping the delete instead would
    # leave the stale copies in place, matching their stored hashes — the
    # next sync would see nothing to do and the procedure would read out of
    # order permanently.
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
    return {
        "synced": True,
        "edited": edited,
        "posted": posted,
        "deleted": deleted,
        # Tracked messages found deleted from the channel and put back.
        "repaired": repaired,
        # Discord rejected an edit/send, or wouldn't hand over the history
        # the existence check needs: the channel is only partially
        # reconciled and the next save retries the rest. Surfaced so the
        # dashboard doesn't report a clean sync.
        "incomplete": incomplete,
    }


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
