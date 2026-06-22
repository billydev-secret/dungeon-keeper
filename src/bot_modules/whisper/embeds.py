"""Embed builders for the whisper subsystem.

Each builder takes plain values (ints, strings, ``Whisper`` / ``WhisperReply``
dataclasses) and returns a ``discord.Embed``. They make no network calls and
no DB queries — the cog gathers data and these turn it into Discord output.

The mod-log embeds (reply / report / reply-report) used to live inline in
``cogs/whisper_cog.py`` modal ``on_submit`` handlers, which made them
impossible to test without a fake interaction. The inbox embed pulled in
``is_locked``, ``_status_pill``, ``_format_time_ago``, and ``_preview`` —
all of which now live in ``whisper.logic`` for the same reason.

``discord.Embed.timestamp`` requires a real ``datetime``; pass one in and
the builder falls back to ``datetime.now(timezone.utc)`` so tests can pin
the value.
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot_modules.services.whisper_models import Whisper, WhisperReply
from bot_modules.services.whisper_service import safe_codefence_content
from bot_modules.whisper.logic import (
    format_time_ago,
    inbox_footer,
    preview,
    status_pill,
)


# ── Mod-log embeds ───────────────────────────────────────────────────────────


def build_reply_audit_embed(
    *,
    whisper_id: int,
    from_user_id: int,
    to_user_id: int,
    content: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the "Whisper Reply" mod-log embed.

    The reply body is rendered into the embed description (with codefence
    homoglyph protection so user content can't break formatting). Three
    fields surface the participants and the whisper id for cross-reference.
    """
    embed = discord.Embed(
        title="Whisper Reply",
        description=safe_codefence_content(content),
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(
        name="From",
        value=f"<@{from_user_id}> (`{from_user_id}`)",
        inline=False,
    )
    embed.add_field(
        name="To",
        value=f"<@{to_user_id}> (`{to_user_id}`)",
        inline=False,
    )
    embed.add_field(name="Whisper ID", value=str(whisper_id), inline=False)
    return embed


def build_report_audit_embed(
    *,
    whisper: Whisper,
    reason: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the "Whisper Reported" mod-log embed.

    Includes the original whisper body, the sender (which the recipient may
    have already exposed), the reporter (always the target), the moderator-
    supplied reason, and the whisper id.
    """
    embed = discord.Embed(
        title="Whisper Reported",
        description=safe_codefence_content(whisper.message),
        color=discord.Color.red(),
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Sender",
        value=f"<@{whisper.sender_id}> (`{whisper.sender_id}`)",
        inline=False,
    )
    embed.add_field(
        name="Reporter (Target)",
        value=f"<@{whisper.target_id}> (`{whisper.target_id}`)",
        inline=False,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Whisper ID", value=str(whisper.id), inline=False)
    return embed


def build_reply_report_audit_embed(
    *,
    reply: WhisperReply,
    reporter_id: int,
    reason: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the "Whisper Reply Reported" mod-log embed.

    Surfaces the reply body, anonymized sender, the reporter (the reply
    recipient), the reason, and both the reply and whisper ids.
    """
    embed = discord.Embed(
        title="Whisper Reply Reported",
        description=safe_codefence_content(reply.content),
        color=discord.Color.red(),
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Sender (anonymous)",
        value=f"<@{reply.from_user_id}> (`{reply.from_user_id}`)",
        inline=False,
    )
    embed.add_field(
        name="Reporter (recipient)",
        value=f"<@{reporter_id}> (`{reporter_id}`)",
        inline=False,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Reply ID", value=str(reply.id), inline=False)
    embed.add_field(name="Whisper ID", value=str(reply.whisper_id), inline=False)
    return embed


# ── Shared-whisper feed embed ────────────────────────────────────────────────


def build_send_feed_embed(target_id: int) -> discord.Embed:
    """The public feed announcement posted when a Whisper is sent.

    Replaces the old plain-text ``📬 Someone sent @x an anonymous message``
    line with a styled embed (accent bar + bold heading). The embed carries
    the target's mention as the one *visible* name (embed mentions render but
    don't ping); the ping itself rides in the message content as a spoiler
    (``||<@id>||``), where the name stays hidden behind the spoiler bars.
    """
    return discord.Embed(
        title="\U0001f4ec Someone sent a Whisper",
        description=f"Someone sent <@{target_id}> an anonymous message.",
        color=discord.Color.blurple(),
    )


def build_share_feed_embed(whisper: Whisper) -> discord.Embed:
    """The public "a fresh whisper was shared" feed post, as a styled embed.

    Replaces the old plain-text + code-fence body with an embed (accent bar,
    bold heading, message rendered as a clean quote). The body is
    markdown-escaped rather than code-fenced so it reads like an ordinary
    quote while still preventing anonymous content from injecting headers,
    blockquotes, links, or other formatting into the public feed.
    """
    safe = discord.utils.escape_markdown(whisper.message)
    embed = discord.Embed(
        title="\U0001f4ec A fresh Whisper was shared",
        description=(
            f"Someone sent <@{whisper.target_id}> an anonymous message!\n\n"
            f"“{safe}”"
        ),
        color=discord.Color.blurple(),
    )
    return embed


# ── Inbox embed ──────────────────────────────────────────────────────────────


def _inbox_title(mode: str) -> str:
    return "Your Inbox" if mode == "received" else "Whispers You've Sent"


def build_inbox_embed(
    *,
    whispers: list[Whisper],
    selected: Whisper | None,
    mode: str,
    now: float | None = None,
) -> discord.Embed:
    """Build the embed shown above the whisper-inbox dropdown view.

    Three states:
      - ``whispers`` empty: mode-specific empty message.
      - ``selected is None``: prompt to pick from the dropdown.
      - selected populated: header (with sender/target depending on mode),
        codefenced body, and a status-aware footer.
    """
    embed = discord.Embed(
        title=f"{_inbox_title(mode)} ({len(whispers)})",
        color=discord.Color.blurple(),
    )
    if not whispers:
        embed.description = (
            "*No whispers in your inbox.*"
            if mode == "received"
            else "*You haven't sent any active whispers in this server.*"
        )
        return embed
    if selected is None:
        embed.description = "*Pick a whisper from the dropdown.*"
        return embed

    status = status_pill(selected, now=now)
    time_ago = format_time_ago(selected.created_at, now=now)
    if mode == "received":
        header = f"**Whisper #{selected.id}** · {status} · *{time_ago}*"
    else:
        header = (
            f"**Whisper #{selected.id} → <@{selected.target_id}>** · "
            f"{status} · *{time_ago}*"
        )
    embed.description = (
        f"{header}\n```{safe_codefence_content(selected.message)}```"
    )
    embed.set_footer(text=inbox_footer(selected, mode=mode, now=now))
    return embed


# ── Inbox-row formatting (used by the dropdown options) ──────────────────────


def inbox_option_label(w: Whisper, *, now: float | None = None) -> str:
    """Label shown for one whisper in the inbox dropdown's select options."""
    return f"#{w.id} · {status_pill(w, now=now)} · {format_time_ago(w.created_at, now=now)}"


def inbox_option_description(w: Whisper) -> str | None:
    """Discord caps select-option descriptions at 100 chars. ``None`` if empty
    so the option renders without a description row."""
    return preview(w.message)[:100] or None
