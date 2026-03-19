"""AI-powered moderation helpers using the OpenAI API."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import discord
from openai import AsyncOpenAI

log = logging.getLogger("dungeonkeeper.ai_mod")


async def _chat(
    client: AsyncOpenAI,
    *,
    model: str,
    messages: list[dict],
    max_tokens: int,
    reasoning_effort: str | None = None,
):
    """Log the outgoing payload at DEBUG level, then call the OpenAI chat API."""
    if log.isEnabledFor(logging.DEBUG):
        for i, m in enumerate(messages):
            log.debug(
                "OpenAI request [%d/%d] role=%s reasoning_effort=%s content=%.500s",
                i + 1, len(messages), m.get("role"), reasoning_effort, m.get("content", ""),
            )
    kwargs: dict = {"model": model, "messages": messages, "max_completion_tokens": max_tokens}
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    return await client.chat.completions.create(**kwargs)


_MAX_MSG_CHARS = 400   # truncate individual messages to avoid token bloat
_CONTEXT_WINDOW = 2    # messages before/after each target message to include
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

Analyze the log and report concisely on:
1. Any violations of the server rules listed above, citing which rule is implicated
2. Notable behavioral patterns
3. Any concerns worth moderator attention

Cite specific messages as evidence when flagging concerns. \
If the messages appear normal and rule-abiding, say so clearly."""

_SCAN_SYSTEM = f"""\
You are a Discord server moderation assistant. A moderator has requested a scan of recent channel activity.

{_SERVER_RULES}

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

Answer the moderator's question based solely on the provided log, referencing the server rules above \
where relevant. Be concise and cite specific messages as evidence."""


@dataclass
class AiModerationResult:
    analysis: str
    message_count: int
    channels_checked: int


def _tag(msg: discord.Message, user: discord.Member, replied_to_ids: set[int]) -> str:
    """Return the display tag for a message given its relationship to the target user."""
    if msg.author.id == user.id:
        return "TARGET"
    if msg.id in replied_to_ids:
        return "REPLY→TARGET"
    return "CONTEXT"


def _fmt_msg(tag: str, channel_name: str, msg: discord.Message) -> str:
    ts = msg.created_at.strftime("%Y-%m-%d %H:%M") if msg.created_at else "?"
    content = (msg.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
    return f"[{tag}] #{channel_name} | {ts} | {msg.author.display_name}: {content}"


async def _fetch_user_context(
    guild: discord.Guild,
    user: discord.Member,
    *,
    max_user_messages: int = _MAX_USER_MSGS,
    lookback_days: int = 7,
) -> tuple[list[str], int, int]:
    """
    Fetch target user's messages with surrounding conversational context.

    For each message the target user sent, includes:
    - Up to _CONTEXT_WINDOW messages before and after in the same channel
    - The message the target was replying to (if resolvable within the batch)
    - Messages from others that directly reply to the target

    Returns (formatted_lines, user_message_count, channels_checked).
    """
    after_dt = discord.utils.utcnow() - timedelta(days=lookback_days)
    bot_member = guild.me
    channels: list[discord.TextChannel | discord.Thread] = list(guild.text_channels)
    for tc in guild.text_channels:
        channels.extend(tc.threads)

    all_lines: list[str] = []
    total_user_msgs = 0
    channels_checked = 0

    for channel in channels:
        if total_user_msgs >= max_user_messages:
            break
        if bot_member and not channel.permissions_for(bot_member).read_message_history:
            continue

        channel_name = getattr(channel, "name", str(channel.id))

        try:
            batch: list[discord.Message] = []
            async for msg in channel.history(limit=None, after=after_dt, oldest_first=True):
                batch.append(msg)
        except (discord.Forbidden, discord.HTTPException):
            continue

        # Only process channels where the user actually posted
        target_indices = [i for i, m in enumerate(batch) if m.author.id == user.id]
        if not target_indices:
            continue

        channels_checked += 1

        # Build lookup: message_id → index in batch
        id_to_idx: dict[int, int] = {m.id: i for i, m in enumerate(batch)}

        # Find messages in the batch that reply to a target message
        target_ids = {batch[i].id for i in target_indices}
        reply_to_target: set[int] = set()  # ids of messages that reply TO the user
        for msg in batch:
            if msg.reference and msg.reference.message_id in target_ids:
                reply_to_target.add(msg.id)

        # Determine which indices to include
        include: set[int] = set()
        for i in target_indices:
            # Context window around each target message
            for j in range(
                max(0, i - _CONTEXT_WINDOW),
                min(len(batch), i + _CONTEXT_WINDOW + 1),
            ):
                include.add(j)

            # The message the target is replying to
            target_ref = batch[i].reference
            ref_id = target_ref.message_id if target_ref is not None else None
            if ref_id is not None and ref_id in id_to_idx:
                include.add(id_to_idx[ref_id])

        # Include messages that reply to the target
        for j, msg in enumerate(batch):
            if msg.id in reply_to_target:
                include.add(j)

        # Emit lines in chronological order
        for i in sorted(include):
            msg = batch[i]

            if msg.author.id == user.id:
                tag = "TARGET"
            elif msg.id in reply_to_target:
                tag = "REPLY→TARGET"
            elif (
                msg.reference is not None
                and msg.reference.message_id in target_ids
            ):
                tag = "REPLY→TARGET"
            else:
                is_replied_to = False
                for ti in target_indices:
                    ref = batch[ti].reference
                    if ref is not None and ref.message_id == msg.id:
                        is_replied_to = True
                        break
                tag = "TARGET REPLIED TO" if is_replied_to else "CONTEXT"

            all_lines.append(_fmt_msg(tag, channel_name, msg))

        total_user_msgs += len(target_indices)

        # Blank line between channels for readability
        if all_lines and all_lines[-1] != "":
            all_lines.append("")

    return all_lines, total_user_msgs, channels_checked


async def ai_review_user(
    client: AsyncOpenAI,
    guild: discord.Guild,
    user: discord.Member,
    *,
    days: int = 7,
    model: str = "gpt-5.4",
) -> AiModerationResult:
    lines, user_msg_count, channels_checked = await _fetch_user_context(
        guild, user, lookback_days=days
    )

    if not lines:
        body = f"No messages found for {user.display_name} in the last {days} days."
    else:
        body = (
            f"Message log for {user.display_name} (@{user.name}), "
            f"last {days} days:\n\n" + "\n".join(lines)
        )

    response = await _chat(
        client, model=model,
        messages=[
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": body},
        ],
        max_tokens=800,
        reasoning_effort="high",
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=channels_checked,
    )


async def ai_scan_channel(
    client: AsyncOpenAI,
    channel: discord.TextChannel | discord.Thread,
    *,
    count: int = 50,
    model: str = "gpt-5.4",
) -> AiModerationResult:
    raw: list[discord.Message] = []
    async for msg in channel.history(limit=count, oldest_first=False):
        if msg.content:
            raw.append(msg)
    raw.reverse()

    if not raw:
        return AiModerationResult(
            analysis="No messages with text content found in this channel.",
            message_count=0,
            channels_checked=1,
        )

    lines = ["Recent channel messages (oldest first):\n"]
    for msg in raw:
        ts = msg.created_at.strftime("%H:%M") if msg.created_at else "?"
        content = (msg.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
        reply_note = ""
        if msg.reference and isinstance(msg.reference.resolved, discord.Message):
            replied_to = msg.reference.resolved
            reply_note = f" [↩ replying to {replied_to.author.display_name}]"
        lines.append(f"[{ts}] {msg.author.display_name}{reply_note}: {content}")
    channel_text = "\n".join(lines)

    response = await _chat(
        client, model=model,
        messages=[
            {"role": "system", "content": _SCAN_SYSTEM},
            {"role": "user", "content": channel_text},
        ],
        max_tokens=800,
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(analysis=analysis, message_count=len(raw), channels_checked=1)


async def ai_check_watched_message(
    client: AsyncOpenAI,
    message: discord.Message,
    *,
    model: str = "gpt-5.4",
) -> tuple[bool, str]:
    """
    Check a single message against server rules.

    Returns (is_violation, reason) where reason is a one-sentence explanation
    if a violation was detected, or an empty string if the message is clean.
    Errors in the API call are raised to the caller.
    """
    ts = message.created_at.strftime("%Y-%m-%d %H:%M") if message.created_at else "?"
    channel_name = getattr(message.channel, "name", str(message.channel.id))
    content = (message.content or "").replace("\n", " ")[:_MAX_MSG_CHARS]
    prompt = f"[{ts}] #{channel_name} | {message.author.display_name}: {content}"

    response = await _chat(
        client, model=model,
        messages=[
            {"role": "system", "content": _WATCH_CHECK_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=120,
    )
    reply = (response.choices[0].message.content or "").strip()
    is_violation = reply.upper().startswith("VIOLATION")
    reason = reply[len("VIOLATION:"):].strip() if is_violation else ""
    return is_violation, reason


async def ai_query_user(
    client: AsyncOpenAI,
    guild: discord.Guild,
    user: discord.Member,
    question: str,
    *,
    days: int = 14,
    model: str = "gpt-5.4",
) -> AiModerationResult:
    lines, user_msg_count, channels_checked = await _fetch_user_context(
        guild, user, lookback_days=days, max_user_messages=80
    )

    if not lines:
        body = f"No messages found for {user.display_name} in the last {days} days."
    else:
        body = (
            f"Message log for {user.display_name} (@{user.name}), "
            f"last {days} days:\n\n" + "\n".join(lines)
        )

    prompt = f"Moderator question: {question}\n\n{body}"

    response = await _chat(
        client, model=model,
        messages=[
            {"role": "system", "content": _QUERY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=800,
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=user_msg_count,
        channels_checked=channels_checked,
    )
