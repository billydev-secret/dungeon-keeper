"""AI-powered moderation helpers using a locally-hosted Ollama instance."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import discord

from bot_modules.services import ollama_client

log = logging.getLogger("dungeonkeeper.ai_mod")

_MAX_MSG_CHARS = 400
_CONTEXT_WINDOW = 4
_MAX_USER_MSGS = 200

_WATCH_CHECK_SYSTEM = (
    "You are a Discord moderation assistant. Determine whether the message below violates any "
    "server rule.\n\n"
    "Rules:\n"
    "  Rule 1 — Adults only (21+): This is a membership age requirement — the server is for adults. "
    "Rule 1 is NEVER violated by the content of a message. Only flag Rule 1 if a member explicitly "
    "states they are under 21.\n"
    "  Rule 2 — No harassment, coercion, threats, demeaning behavior, slurs, or boundary violations. "
    "Compliments, flattery, expressions of attraction, flirting, playful teasing, emoji, and ordinary "
    "social interaction are NOT violations — even if they use words like 'sexy', 'hot', or 'gorgeous'. "
    "Only flag clear, explicit hostile intent: direct insults, threats, slurs, or sustained pressure "
    "on someone who has expressed they are not interested.\n"
    "  Rule 3 — Explicit content only in designated channels. This applies to explicit sexual text or "
    "images posted in non-designated channels. Ordinary conversation, emoji, compliments, and "
    "ambiguous phrasing are NOT violations. If the message header says the channel is NSFW-designated, "
    "Rule 3 cannot be violated. Evaluate the message text only — the sender's display name is not "
    "message content.\n"
    "  Rule 4 — No callouts or conflicts imported from other servers. Casually mentioning another "
    "server, referencing shared communities, or general chat about other places is NOT a violation. "
    "Only flag active drama, accusations, or beef being brought in from elsewhere.\n"
    "  Rule 5 — DMs are opt-in; do not DM members without their consent. Mentioning DMs, "
    "offering to DM, or discussing DM policies in public chat is NOT a violation.\n"
    "  Rule 6 — Settle disputes in tickets, not public chat. Ordinary venting, expressing a preference, "
    "or asking a question is NOT a violation. Only flag active public arguments or attempts to "
    "escalate a conflict in chat.\n\n"
    "Important: The vast majority of messages are completely normal and friendly. Your threshold "
    "must be HIGH — only flag messages where a reasonable moderator would take immediate action. "
    "If the message could plausibly be friendly, joking, or harmless, reply OK.\n\n"
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

_RULES_WATCH_SYSTEM = f"""\
You are a recall-leaning moderation guard for an adult Discord community. Your job is to surface
messages that may warrant human review — you flag generously and let moderators dismiss false
positives. False negatives (missing a real problem) are much worse than false positives here.

{_SERVER_RULES}

You will receive a conversation window (multiple recent messages in a channel), oldest first.
Each line is formatted as:
  [HH:MM] author [↩ replying to other]: content

Additional markers:
  [📎 ext, ...]   — file attachments
  [@Name, ...]    — mentions
  [NSFW]          — the channel is designated for explicit content

Evaluate the MOST RECENT message (the last one in the window) in the context of the whole window.
Flag if:
- The message contains a slur or identity attack (always a violation regardless of consent)
- The message applies pressure, coercion, or threats
- The message continues unwanted contact with someone who has expressed disinterest
- There is escalating one-sided directed behavior in the window
- The message violates channel designation (Rule 3)

Respond with ONLY valid JSON, no markdown fences, in this exact format:
{{"verdict": "flag", "rule": "2", "reason": "brief reason", "confidence": 0.85}}
or
{{"verdict": "ok", "rule": null, "reason": null, "confidence": 0.1}}

"confidence" is your certainty that this is a genuine concern (0.0–1.0).
"rule" is the primary rule implicated (as a string: "1", "2", "3", "4", "5", "6", or null).
No other output."""


class RulesWatchGuardResult(NamedTuple):
    verdict: str          # 'flag' | 'ok'
    rule: str | None      # '2', '3', etc.
    reason: str | None
    confidence: float


async def ai_rules_watch_check(
    window_text: str,
    *,
    channel_is_nsfw: bool = False,
    model: str | None = None,
    db_path: Path | None = None,
    guild_id: int = 0,
) -> RulesWatchGuardResult:
    """Run the recall-leaning guard model over a conversation window.

    Returns a structured result; falls back to ok on any parse failure.
    """
    from bot_modules.services.ai_config import DEFAULT_MOD_MODEL, get_command_model_from_path

    if model is None:
        model = (
            get_command_model_from_path(db_path, "ai_prompt_rules_watch", guild_id)
            if db_path
            else DEFAULT_MOD_MODEL
        )

    nsfw_note = " [Channel is NSFW-designated — explicit content is permitted here]" if channel_is_nsfw else ""
    user_content = f"{nsfw_note}\n{window_text}".strip()

    raw = await ollama_client.chat(
        model=model,
        system=_RULES_WATCH_SYSTEM,
        user_content=user_content,
        max_tokens=256,
        temperature=0.0,
    )

    try:
        data = json.loads(raw or "{}")
        verdict = str(data.get("verdict", "ok")).lower()
        if verdict not in ("flag", "ok"):
            verdict = "ok"
        rule = str(data["rule"]) if data.get("rule") else None
        reason = str(data["reason"]) if data.get("reason") else None
        confidence = float(data.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        return RulesWatchGuardResult(verdict=verdict, rule=rule, reason=reason, confidence=confidence)
    except Exception:
        log.debug("rules_watch guard: failed to parse LLM response: %r", raw)
        return RulesWatchGuardResult(verdict="ok", rule=None, reason=None, confidence=0.0)


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
    ch = guild.get_channel(channel_id)
    if not ch or not hasattr(ch, "name"):
        return str(channel_id)
    name = ch.name
    if getattr(ch, "nsfw", False):
        return f"{name} [NSFW]"
    return name


def _fetch_attachment_map(
    conn: sqlite3.Connection, message_ids: set[int]
) -> dict[int, list[str]]:
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


def _fetch_mention_map(
    conn: sqlite3.Connection, message_ids: set[int]
) -> dict[int, list[int]]:
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
    if not urls:
        return ""
    exts = []
    for u in urls:
        dot = u.rsplit(".", 1)
        exts.append(dot[-1].split("?")[0].lower() if len(dot) > 1 else "file")
    return " [📎 " + ", ".join(exts) + "]"


def _mention_note(
    user_ids: list[int], guild: discord.Guild, name_cache: dict[int, str]
) -> str:
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
    cutoff_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    )

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

        batch = conn.execute(
            "SELECT message_id, author_id, content, reply_to_id, ts "
            "FROM messages WHERE guild_id = ? AND channel_id = ? AND ts >= ? "
            "ORDER BY ts ASC",
            (guild.id, channel_id, cutoff_ts),
        ).fetchall()

        target_indices = [i for i, r in enumerate(batch) if r[1] == user.id]
        if not target_indices:
            continue

        remaining_cap = max_user_messages - total_user_msgs
        if len(target_indices) > remaining_cap:
            target_indices = target_indices[-remaining_cap:]

        channels_checked += 1
        id_to_idx: dict[int, int] = {r[0]: i for i, r in enumerate(batch)}
        target_ids: set[int] = {batch[i][0] for i in target_indices}
        reply_to_target: set[int] = {r[0] for r in batch if r[3] in target_ids}

        include: set[int] = set()
        for i in target_indices:
            for j in range(
                max(0, i - _CONTEXT_WINDOW),
                min(len(batch), i + _CONTEXT_WINDOW + 1),
            ):
                include.add(j)
            ref_id = batch[i][3]
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
    conn: sqlite3.Connection,
    guild: discord.Guild,
    user: discord.Member,
    *,
    days: int = 7,
    model: str | None = None,
) -> AiModerationResult:
    from bot_modules.services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_review", guild.id)
    system = get_prompt(conn, "ai_prompt_review", guild.id)

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

    analysis = (
        await ollama_client.chat(
            model=model,
            system=system,
            user_content=body,
            max_tokens=4096,
        )
        or "No analysis returned."
    )
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=channels_checked,
    )


async def ai_scan_channel(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.Thread,
    *,
    count: int = 50,
    model: str | None = None,
) -> AiModerationResult:
    from bot_modules.services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_scan", guild.id)
    system = get_prompt(conn, "ai_prompt_scan", guild.id)

    rows = conn.execute(
        "SELECT message_id, author_id, content, reply_to_id, ts "
        "FROM messages WHERE guild_id = ? AND channel_id = ? AND content IS NOT NULL "
        "ORDER BY ts DESC LIMIT ?",
        (guild.id, channel.id, count),
    ).fetchall()
    rows = list(reversed(rows))

    channel_name = _channel_label(guild, channel.id)

    if not rows:
        return AiModerationResult(
            analysis=f"No messages found in #{channel_name} in the local archive.",
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

    analysis = (
        await ollama_client.chat(
            model=model,
            system=system,
            user_content="\n".join(lines),
            max_tokens=2048,
        )
        or "No analysis returned."
    )
    return AiModerationResult(analysis=analysis, message_count=len(rows), channels_checked=1)


async def ai_check_watched_message(
    message: discord.Message,
    *,
    model: str | None = None,
    db_path: Path | None = None,
) -> tuple[bool, str]:
    """Check a single live message against server rules.

    Returns (is_violation, reason). Errors are raised to the caller.
    """
    from bot_modules.services.ai_config import DEFAULT_MOD_MODEL, get_command_model_from_path, get_prompt_from_path

    guild_id = message.guild.id if message.guild else 0
    if model is None:
        model = (
            get_command_model_from_path(db_path, "ai_prompt_watch_check", guild_id)
            if db_path
            else DEFAULT_MOD_MODEL
        )
    system = (
        get_prompt_from_path(db_path, "ai_prompt_watch_check", guild_id)
        if db_path
        else _WATCH_CHECK_SYSTEM
    )

    ts = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else "?"
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    is_nsfw = getattr(message.channel, "nsfw", False)
    nsfw_tag = " [NSFW-designated]" if is_nsfw else ""
    content = (message.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
    prompt = f"[{ts}] #{channel_name}{nsfw_tag} | {message.author.display_name}: {content}"

    reply = await ollama_client.chat(
        model=model,
        system=system,
        user_content=prompt,
        max_tokens=256,
        temperature=0.0,
    )
    is_violation = reply.upper().startswith("VIOLATION")
    reason = reply[len("VIOLATION:"):].strip() if is_violation else ""
    return is_violation, reason


async def ai_query_channel(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.Thread,
    question: str,
    *,
    minutes: int = 60,
    model: str | None = None,
) -> AiModerationResult:
    from bot_modules.services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_query_channel", guild.id)
    system = get_prompt(conn, "ai_prompt_query_channel", guild.id)

    cutoff_ts = int(
        (datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp()
    )

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
            analysis=f"No messages found in #{channel_name} in the last {label} in the local archive.",
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

    analysis = (
        await ollama_client.chat(
            model=model,
            system=system,
            user_content=prompt,
            max_tokens=4096,
        )
        or "No analysis returned."
    )
    return AiModerationResult(analysis=analysis, message_count=len(rows), channels_checked=1)


async def ai_query_user(
    conn: sqlite3.Connection,
    guild: discord.Guild,
    user: discord.Member,
    question: str,
    *,
    days: int = 14,
    model: str | None = None,
) -> AiModerationResult:
    from bot_modules.services.ai_config import get_command_model, get_prompt

    if model is None:
        model = get_command_model(conn, "ai_prompt_query_user", guild.id)
    system = get_prompt(conn, "ai_prompt_query_user", guild.id)

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

    analysis = (
        await ollama_client.chat(
            model=model,
            system=system,
            user_content=prompt,
            max_tokens=4096,
        )
        or "No analysis returned."
    )
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=0,
    )
