"""AI-powered moderation helpers using the OpenAI API."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import discord
from openai import AsyncOpenAI

log = logging.getLogger("dungeonkeeper.ai_mod")

_MAX_MSG_CHARS = 400  # truncate individual messages to avoid token bloat

_REVIEW_SYSTEM = """\
You are a Discord server moderation assistant. A moderator has requested a review of a user's recent messages.

Analyze the provided messages and report concisely on:
1. Any potential rule violations (harassment, hate speech, spam, threats, doxxing, etc.)
2. Notable behavioral patterns
3. Any concerns worth moderator attention

Cite specific messages as evidence when flagging concerns. \
If the messages appear normal and rule-abiding, say so clearly."""

_SCAN_SYSTEM = """\
You are a Discord server moderation assistant. A moderator has requested a scan of recent channel activity.

Analyze the messages and report concisely on:
1. Any messages that may violate typical server rules
2. Conflicts, hostility, or tension between users
3. Spam or coordinated behavior
4. A one-line overall health summary

Cite specific users and messages when noting concerns. \
If the channel looks healthy, say so clearly."""

_QUERY_SYSTEM = """\
You are a Discord server moderation assistant helping a moderator investigate a user. \
Answer the moderator's question based solely on the provided message history. \
Be concise and cite specific messages as evidence."""


@dataclass
class AiModerationResult:
    analysis: str
    message_count: int
    channels_checked: int


async def _fetch_user_messages(
    guild: discord.Guild,
    user: discord.Member,
    *,
    max_messages: int = 60,
    lookback_days: int = 7,
    per_channel_limit: int = 20,
) -> tuple[list[tuple[str, str, str]], int]:
    """Return ((channel_name, timestamp, content), ...) and channels_checked count."""
    after_dt = discord.utils.utcnow() - timedelta(days=lookback_days)
    messages: list[tuple[str, str, str]] = []
    channels_checked = 0

    channels: list[discord.TextChannel | discord.Thread] = list(guild.text_channels)
    for tc in guild.text_channels:
        channels.extend(tc.threads)

    bot_member = guild.me
    for channel in channels:
        if len(messages) >= max_messages:
            break
        if bot_member and not channel.permissions_for(bot_member).read_message_history:
            continue
        channels_checked += 1
        count = 0
        try:
            async for msg in channel.history(limit=200, after=after_dt, oldest_first=False):
                if msg.author.id != user.id or not msg.content:
                    continue
                ts = msg.created_at.strftime("%Y-%m-%d %H:%M") if msg.created_at else "unknown"
                content = msg.content[:_MAX_MSG_CHARS]
                messages.append((getattr(channel, "name", str(channel.id)), ts, content))
                count += 1
                if count >= per_channel_limit or len(messages) >= max_messages:
                    break
        except (discord.Forbidden, discord.HTTPException):
            continue

    return messages, channels_checked


def _format_user_messages(
    user: discord.Member,
    messages: list[tuple[str, str, str]],
) -> str:
    if not messages:
        return f"No messages found for {user.display_name} in the requested time window."
    lines = [f"Messages from {user.display_name} (@{user.name}), newest first:\n"]
    for channel_name, ts, content in messages:
        lines.append(f"[#{channel_name} | {ts}] {content}")
    return "\n".join(lines)


async def ai_review_user(
    client: AsyncOpenAI,
    guild: discord.Guild,
    user: discord.Member,
    *,
    days: int = 7,
    model: str = "gpt-4o-mini",
) -> AiModerationResult:
    messages, channels_checked = await _fetch_user_messages(guild, user, lookback_days=days)
    user_text = _format_user_messages(user, messages)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _REVIEW_SYSTEM},
            {"role": "user", "content": user_text},
        ],
        max_tokens=800,
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=len(messages),
        channels_checked=channels_checked,
    )


async def ai_scan_channel(
    client: AsyncOpenAI,
    channel: discord.TextChannel | discord.Thread,
    *,
    count: int = 50,
    model: str = "gpt-4o-mini",
) -> AiModerationResult:
    raw: list[discord.Message] = []
    async for msg in channel.history(limit=count, oldest_first=False):
        if msg.content:
            raw.append(msg)
    raw.reverse()  # oldest first for readability

    if not raw:
        return AiModerationResult(
            analysis="No messages with text content found in this channel.",
            message_count=0,
            channels_checked=1,
        )

    lines = ["Recent channel messages (oldest first):\n"]
    for msg in raw:
        ts = msg.created_at.strftime("%H:%M") if msg.created_at else "?"
        content = msg.content[:_MAX_MSG_CHARS]
        lines.append(f"[{ts}] {msg.author.display_name}: {content}")
    channel_text = "\n".join(lines)

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SCAN_SYSTEM},
            {"role": "user", "content": channel_text},
        ],
        max_tokens=800,
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(analysis=analysis, message_count=len(raw), channels_checked=1)


async def ai_query_user(
    client: AsyncOpenAI,
    guild: discord.Guild,
    user: discord.Member,
    question: str,
    *,
    days: int = 14,
    model: str = "gpt-4o-mini",
) -> AiModerationResult:
    messages, channels_checked = await _fetch_user_messages(
        guild, user, lookback_days=days, max_messages=80
    )
    user_text = _format_user_messages(user, messages)
    prompt = f"Moderator question: {question}\n\nMessage history:\n{user_text}"

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _QUERY_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        max_tokens=800,
    )
    analysis = response.choices[0].message.content or "No analysis returned."
    return AiModerationResult(
        analysis=analysis,
        message_count=len(messages),
        channels_checked=channels_checked,
    )
