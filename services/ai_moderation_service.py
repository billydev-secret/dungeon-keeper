"""AI-powered moderation helpers using the Anthropic API."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock

log = logging.getLogger("dungeonkeeper.ai_mod")

DEFAULT_MODEL = "claude-opus-4-6"  # fallback; runtime default loaded from ai_config


async def _chat(
    client: AsyncAnthropic,
    *,
    model: str,
    system: str,
    user_content: str,
    max_tokens: int,
    use_thinking: bool = False,
) -> str:
    """Log the outgoing payload at DEBUG level, then call the Anthropic messages API.

    Streams the response (via `messages.stream`) so long completions don't hit the
    request timeout, and returns the concatenated text content. Thinking blocks are
    skipped — only `text` blocks are included in the returned string.
    """
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "Anthropic request model=%s thinking=%s system=%.500s user=%.500s",
            model, use_thinking, system, user_content,
        )
    kwargs: dict = {
        "model": model,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "max_tokens": max_tokens,
    }
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}

    async with client.messages.stream(**kwargs) as stream:
        message = await stream.get_final_message()

    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
    return "".join(parts).strip()


_MAX_MSG_CHARS = 400   # truncate individual messages to avoid token bloat
_CONTEXT_WINDOW = 4    # messages before/after each target message to include
_MAX_USER_MSGS = 200   # stop collecting after this many target-user messages

_WATCH_CHECK_SYSTEM = (
    "You are a Discord moderation assistant. Determine whether the message below violates any "
    "server rule.\n\n"
    "Rules:\n"
    "  Rule 1 — Adults only (21+). NSFW is permitted but members must follow laws in their area.\n"
    "  Rule 2 — No harassment, coercion, threats, demeaning behavior, slurs, or boundary violations.\n"
    "  Rule 3 — Explicit content only in designated channels. Spoiler NSFW images. Use content "
    "warnings for sensitive material.\n"
    "  Rule 4 — No callouts or conflicts imported from other servers.\n"
    "  Rule 5 — DMs are opt-in; use the permissions bot and wait for consent before messaging anyone "
    "privately or on other platforms.\n"
    "  Rule 6 — Settle disputes in tickets, not public chat.\n\n"
    "Reply with exactly one of:\n"
    "  VIOLATION: <one-sentence reason citing the specific rule number>\n"
    "  OK\n\n"
    "No other output."
)

_SERVER_RULES = """\
Server rules (check all messages against these):
  Rule 1 — Adults only (21+): This is an adult community. NSFW material is permitted but members must
    know and follow the laws in their area.
  Rule 2 — Be good to others: Harassment, coercion, threats, demeaning behavior, and discriminatory
    language including slurs are not allowed. Boundaries must be respected immediately. The space is
    built on consent, respect, and accountability.
  Rule 3 — Keep things in the right channels: SFW content in SFW spaces, explicit content only in
    designated areas. Explicit images must be spoilered to avoid push-notification previews. Content
    warnings required for sensitive material (knives, food, body image, etc.).
  Rule 4 — Keep the focus on this server: Do not bring callouts, beef, or conflicts from other Discord
    servers into this space.
  Rule 5 — Use the DM permissions bot: DMs are opt-in. Members must use the DM permissions bot and
    wait for consent before messaging anyone privately. This extends to contacting members on other
    platforms (Reddit, etc.) without their explicit permission.
  Rule 6 — Settle disputes in tickets: Conflicts and moderation concerns go through the ticket system,
    not public chat. Do not argue publicly, escalate in chat, or involve bystanders.
  Rule 7 — Breaking rules has consequences: Violations may result in a warning, loss of access, or a
    permanent ban depending on severity."""

_REVIEW_SYSTEM = f"""\
You are a Discord server moderation assistant. A moderator has requested a review of a user's recent messages.

{_SERVER_RULES}

The log below shows conversation context. Each line is prefixed with a tag:
  [TARGET]   — a message written by the user being reviewed
  [CONTEXT]  — a nearby message from another user, shown for conversational context
  [REPLY→TARGET] — another user replying directly to the target user
  [TARGET REPLIED TO] — the message the target user was replying to

Additional inline markers you may see:
  [📎 ext, ...]   — the message included file attachments (image extensions like jpg/png/gif suggest photos)
  [@Name, ...]    — the message mentioned these users
  [NSFW]          — after a channel name means the channel is designated for explicit content

Analyze the log and report concisely on:
1. Any violations of the server rules listed above, citing which rule is implicated
2. Notable behavioral patterns
3. Any concerns worth moderator attention

Cite specific messages as evidence when flagging concerns. \
If the messages appear normal and rule-abiding, say so clearly."""

_SCAN_SYSTEM = f"""\
You are a Discord server moderation assistant. A moderator has requested a scan of recent channel activity.

{_SERVER_RULES}

Additional inline markers you may see:
  [📎 ext, ...]   — the message included file attachments (image extensions like jpg/png/gif suggest photos)
  [@Name, ...]    — the message mentioned these users
  [NSFW]          — after a channel name means the channel is designated for explicit content

Analyze the messages and report concisely on:
1. Any messages that violate the server rules listed above — note which rule is implicated
2. Conflicts, hostility, or tension between users
3. Spam or coordinated behavior
4. A one-line overall health summary

Cite specific users and messages when noting concerns. \
If the channel looks healthy and rule-compliant, say so clearly."""

_QUERY_SYSTEM = f"""\
You are a Discord server moderation assistant helping a moderator investigate a user.

{_SERVER_RULES}

The log below shows conversation context. Each line is prefixed with a tag:
  [TARGET]   — a message written by the user being investigated
  [CONTEXT]  — a nearby message from another user, shown for conversational context
  [REPLY→TARGET] — another user replying directly to the target user
  [TARGET REPLIED TO] — the message the target user was replying to

Additional inline markers you may see:
  [📎 ext, ...]   — the message included file attachments (image extensions like jpg/png/gif suggest photos)
  [@Name, ...]    — the message mentioned these users
  [NSFW]          — after a channel name means the channel is designated for explicit content

Answer the moderator's question based solely on the provided log, referencing the server rules above \
where relevant. Be concise and cite specific messages as evidence."""

_CHANNEL_QUERY_SYSTEM = f"""\
You are a Discord server moderation assistant helping a moderator investigate recent activity in a channel.

{_SERVER_RULES}

The log below shows messages from a specific time window, oldest first. Each line is formatted as:
  [HH:MM] author [↩ replying to other_author]: message content

Additional inline markers you may see:
  [📎 ext, ...]   — the message included file attachments (image extensions like jpg/png/gif suggest photos)
  [@Name, ...]    — the message mentioned these users
  [NSFW]          — after a channel name means the channel is designated for explicit content

Answer the moderator's question based solely on the provided log, referencing the server rules where \
relevant. Be concise and cite specific users and messages as evidence."""


@dataclass
class AiModerationResult:
    analysis: str
    message_count: int
    channels_checked: int


def _ts_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _resolve_name(guild: discord.Guild, name_cache: dict[int, str], author_id: int) -> str:
    if author_id not in name_cache:
        m = guild.get_member(author_id)
        name_cache[author_id] = m.display_name if m else f"User {author_id}"
    return name_cache[author_id]


def _channel_label(guild: discord.Guild, channel_id: int) -> str:
    """Return channel name with an [NSFW] tag when applicable."""
    ch = guild.get_channel(channel_id)
    if not ch or not hasattr(ch, "name"):
        return str(channel_id)
    name = ch.name
    if getattr(ch, "nsfw", False):
        return f"{name} [NSFW]"
    return name


def _fetch_attachment_map(conn: sqlite3.Connection, message_ids: set[int]) -> dict[int, list[str]]:
    """Return {message_id: [url, ...]} for the given message IDs."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, url FROM message_attachments WHERE message_id IN ({placeholders})",
        list(message_ids),
    ).fetchall()
    result: dict[int, list[str]] = {}
    for mid, url in rows:
        result.setdefault(mid, []).append(url)
    return result


def _fetch_mention_map(conn: sqlite3.Connection, message_ids: set[int]) -> dict[int, list[int]]:
    """Return {message_id: [user_id, ...]} for the given message IDs."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, user_id FROM message_mentions WHERE message_id IN ({placeholders})",
        list(message_ids),
    ).fetchall()
    result: dict[int, list[int]] = {}
    for mid, uid in rows:
        result.setdefault(mid, []).append(uid)
    return result


def _attachment_note(urls: list[str]) -> str:
    """Summarise attachments as a compact inline note."""
    if not urls:
        return ""
    exts = []
    for u in urls:
        dot = u.rsplit(".", 1)
        exts.append(dot[-1].split("?")[0].lower() if len(dot) > 1 else "file")
    return " [📎 " + ", ".join(exts) + "]"


def _mention_note(
    user_ids: list[int], guild: discord.Guild, name_cache: dict[int, str],
) -> str:
    """Summarise mentions as a compact inline note."""
    if not user_ids:
        return ""
    names = [_resolve_name(guild, name_cache, uid) for uid in user_ids]
    return " [@" + ", @".join(names) + "]"


def _fetch_user_context_from_db(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    user: discord.Member,
    *,
    lookback_days: int = 7,
    max_user_messages: int = _MAX_USER_MSGS,
) -> tuple[list[str], int, int]:
    """
    Query the local message archive for a user's messages with surrounding context.

    For each channel where the user posted, fetches all messages in the lookback
    window and applies the same context-window logic as the live fetch:
      - ±_CONTEXT_WINDOW messages around each target message
      - The message the target was replying to
      - Messages from others that reply directly to the target

    Returns (formatted_lines, user_message_count, channels_checked).
    """
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())

    channel_rows = conn.execute(
        "SELECT channel_id, MAX(ts) AS latest FROM messages "
        "WHERE guild_id = ? AND author_id = ? AND ts >= ? "
        "GROUP BY channel_id ORDER BY latest DESC",
        (guild.id, user.id, cutoff_ts),
    ).fetchall()

    if not channel_rows:
        return [], 0, 0

    name_cache: dict[int, str] = {}
    all_lines: list[str] = []
    total_user_msgs = 0
    channels_checked = 0

    for channel_id, _ in channel_rows:
        if total_user_msgs >= max_user_messages:
            break

        channel_name = _channel_label(guild, channel_id)

        # All messages in this channel during the lookback window, oldest first
        # Columns: 0=message_id, 1=author_id, 2=content, 3=reply_to_id, 4=ts
        batch = conn.execute(
            "SELECT message_id, author_id, content, reply_to_id, ts "
            "FROM messages WHERE guild_id = ? AND channel_id = ? AND ts >= ? "
            "ORDER BY ts ASC",
            (guild.id, channel_id, cutoff_ts),
        ).fetchall()

        target_indices = [i for i, r in enumerate(batch) if r[1] == user.id]
        if not target_indices:
            continue

        # Enforce the per-run cap within this channel too, not just across channels.
        remaining_cap = max_user_messages - total_user_msgs
        if len(target_indices) > remaining_cap:
            target_indices = target_indices[-remaining_cap:]  # keep most recent

        channels_checked += 1
        id_to_idx: dict[int, int] = {r[0]: i for i, r in enumerate(batch)}
        target_ids: set[int] = {batch[i][0] for i in target_indices}

        # Messages that reply TO the target user
        reply_to_target: set[int] = {
            r[0] for r in batch if r[3] in target_ids
        }

        include: set[int] = set()
        for i in target_indices:
            for j in range(
                max(0, i - _CONTEXT_WINDOW),
                min(len(batch), i + _CONTEXT_WINDOW + 1),
            ):
                include.add(j)
            ref_id = batch[i][3]  # reply_to_id
            if ref_id and ref_id in id_to_idx:
                include.add(id_to_idx[ref_id])
        for j, r in enumerate(batch):
            if r[0] in reply_to_target:
                include.add(j)

        included_ids = {batch[i][0] for i in include}
        attach_map = _fetch_attachment_map(conn, included_ids)
        mention_map = _fetch_mention_map(conn, included_ids)

        for i in sorted(include):
            r = batch[i]
            msg_id, author_id, content, _, ts = r[0], r[1], r[2], r[3], r[4]

            if author_id == user.id:
                tag = "TARGET"
            elif msg_id in reply_to_target:
                tag = "REPLY→TARGET"
            else:
                is_replied_to = any(batch[ti][3] == msg_id for ti in target_indices)
                tag = "TARGET REPLIED TO" if is_replied_to else "CONTEXT"

            name = _resolve_name(guild, name_cache, author_id)
            content_str = (content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
            extras = _attachment_note(attach_map.get(msg_id, []))
            extras += _mention_note(mention_map.get(msg_id, []), guild, name_cache)
            all_lines.append(
                f"[{tag}] #{channel_name} | {_ts_fmt(ts)} | {name}: {content_str}{extras}"
            )

        total_user_msgs += len(target_indices)
        if all_lines and all_lines[-1] != "":
            all_lines.append("")

    return all_lines, total_user_msgs, channels_checked


async def ai_review_user(
    client: AsyncAnthropic,
    conn: sqlite3.Connection,
    guild: discord.Guild,
    user: discord.Member,
    *,
    days: int = 7,
    model: str | None = None,
) -> AiModerationResult:
    from services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_review")
    system = get_prompt(conn, "ai_prompt_review")

    lines, user_msg_count, channels_checked = _fetch_user_context_from_db(
        conn, guild, user, lookback_days=days
    )

    if not lines:
        body = f"No messages found for {user.display_name} in the last {days} days."
    else:
        body = (
            f"Message log for {user.display_name} (@{user.name}), "
            f"last {days} days:\n\n" + "\n".join(lines)
        )

    analysis = await _chat(
        client, model=model,
        system=system,
        user_content=body,
        max_tokens=16000,
        use_thinking=True,
    ) or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=channels_checked,
    )


async def ai_scan_channel(
    client: AsyncAnthropic,
    conn: sqlite3.Connection,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.Thread,
    *,
    count: int = 50,
    model: str | None = None,
) -> AiModerationResult:
    from services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_scan")
    system = get_prompt(conn, "ai_prompt_scan")

    # Columns: 0=message_id, 1=author_id, 2=content, 3=reply_to_id, 4=ts
    rows = conn.execute(
        "SELECT message_id, author_id, content, reply_to_id, ts "
        "FROM messages WHERE guild_id = ? AND channel_id = ? AND content IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        (guild.id, channel.id, count),
    ).fetchall()
    rows = list(reversed(rows))  # oldest first

    channel_name = _channel_label(guild, channel.id)

    if not rows:
        return AiModerationResult(
            analysis=f"No messages found in #{channel_name} in the local archive. "
                     "Run `/interaction_scan` to populate it.",
            message_count=0,
            channels_checked=1,
        )

    name_cache: dict[int, str] = {}
    id_to_author: dict[int, int] = {r[0]: r[1] for r in rows}
    all_ids = {r[0] for r in rows}
    attach_map = _fetch_attachment_map(conn, all_ids)
    mention_map = _fetch_mention_map(conn, all_ids)

    lines = [f"#{channel_name} — last {len(rows)} messages (oldest first):\n"]
    for r in rows:
        msg_id, author_id, content, reply_to_id, ts = r[0], r[1], r[2], r[3], r[4]
        name = _resolve_name(guild, name_cache, author_id)
        content_str = (content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
        reply_note = ""
        if reply_to_id and reply_to_id in id_to_author:
            reply_note = f" [↩ replying to {_resolve_name(guild, name_cache, id_to_author[reply_to_id])}]"
        extras = _attachment_note(attach_map.get(msg_id, []))
        extras += _mention_note(mention_map.get(msg_id, []), guild, name_cache)
        lines.append(f"[{_ts_fmt(ts)[11:16]}] {name}{reply_note}: {content_str}{extras}")

    analysis = await _chat(
        client, model=model,
        system=system,
        user_content="\n".join(lines),
        max_tokens=2048,
    ) or "No analysis returned."
    return AiModerationResult(analysis=analysis, message_count=len(rows), channels_checked=1)


async def ai_check_watched_message(
    client: AsyncAnthropic,
    message: discord.Message,
    *,
    model: str | None = None,
    db_path: "Path | None" = None,
) -> tuple[bool, str]:
    """
    Check a single live message against server rules.

    Returns (is_violation, reason). Errors are raised to the caller.
    """
    from services.ai_config import (
        DEFAULT_MOD_MODEL,
        get_command_model_from_path,
        get_prompt_from_path,
    )

    if model is None:
        model = get_command_model_from_path(db_path, "ai_prompt_watch_check") if db_path else DEFAULT_MOD_MODEL
    system = get_prompt_from_path(db_path, "ai_prompt_watch_check") if db_path else _WATCH_CHECK_SYSTEM

    ts = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else "?"
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    content = (message.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
    prompt = f"[{ts}] #{channel_name} | {message.author.display_name}: {content}"

    reply = await _chat(
        client, model=model,
        system=system,
        user_content=prompt,
        max_tokens=256,
    )
    is_violation = reply.upper().startswith("VIOLATION")
    reason = reply[len("VIOLATION:"):].strip() if is_violation else ""
    return is_violation, reason


async def ai_query_channel(
    client: AsyncAnthropic,
    conn: sqlite3.Connection,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.Thread,
    question: str,
    *,
    minutes: int = 60,
    model: str | None = None,
) -> AiModerationResult:
    from services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_query_channel")
    system = get_prompt(conn, "ai_prompt_query_channel")

    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())

    rows = conn.execute(
        "SELECT message_id, author_id, content, reply_to_id, ts "
        "FROM messages WHERE guild_id = ? AND channel_id = ? AND ts >= ? AND content IS NOT NULL "
        "ORDER BY ts ASC",
        (guild.id, channel.id, cutoff_ts),
    ).fetchall()

    channel_name = _channel_label(guild, channel.id)
    label = f"{minutes} minute{'s' if minutes != 1 else ''}"

    if not rows:
        return AiModerationResult(
            analysis=f"No messages found in #{channel_name} in the last {label} in the local archive. "
                     "Run `/interaction_scan` to populate it.",
            message_count=0,
            channels_checked=1,
        )

    name_cache: dict[int, str] = {}
    id_to_author: dict[int, int] = {r[0]: r[1] for r in rows}
    all_ids = {r[0] for r in rows}
    attach_map = _fetch_attachment_map(conn, all_ids)
    mention_map = _fetch_mention_map(conn, all_ids)

    lines = [f"#{channel_name} — last {label} (oldest first):\n"]
    for r in rows:
        msg_id, author_id, content, reply_to_id, ts = r[0], r[1], r[2], r[3], r[4]
        name = _resolve_name(guild, name_cache, author_id)
        content_str = (content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
        reply_note = ""
        if reply_to_id and reply_to_id in id_to_author:
            reply_note = f" [↩ replying to {_resolve_name(guild, name_cache, id_to_author[reply_to_id])}]"
        extras = _attachment_note(attach_map.get(msg_id, []))
        extras += _mention_note(mention_map.get(msg_id, []), guild, name_cache)
        lines.append(f"[{_ts_fmt(ts)[11:16]}] {name}{reply_note}: {content_str}{extras}")

    prompt = f"Moderator question: {question}\n\n" + "\n".join(lines)

    analysis = await _chat(
        client, model=model,
        system=system,
        user_content=prompt,
        max_tokens=16000,
        use_thinking=True,
    ) or "No analysis returned."
    return AiModerationResult(analysis=analysis, message_count=len(rows), channels_checked=1)


async def ai_query_user(
    client: AsyncAnthropic,
    conn: sqlite3.Connection,
    guild: discord.Guild,
    user: discord.Member,
    question: str,
    *,
    days: int = 14,
    model: str | None = None,
) -> AiModerationResult:
    from services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_query_user")
    system = get_prompt(conn, "ai_prompt_query_user")

    lines, user_msg_count, _ = _fetch_user_context_from_db(
        conn, guild, user, lookback_days=days
    )

    if not lines:
        body = f"No messages found for {user.display_name} in the last {days} days."
    else:
        body = (
            f"Message log for {user.display_name} (@{user.name}), "
            f"last {days} days:\n\n" + "\n".join(lines)
        )

    prompt = f"Moderator question: {question}\n\n{body}"

    analysis = await _chat(
        client, model=model,
        system=system,
        user_content=prompt,
        max_tokens=16000,
        use_thinking=True,
    ) or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=0,
    )
