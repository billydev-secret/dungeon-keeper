"""Embed builders for the DM-permission cog.

These functions take primitive arguments (and occasionally a
``discord.Guild`` for icon access) and return new ``discord.Embed``
objects. None of them perform IO. The DM cog used to build identical
embeds inline at half-a-dozen sites; consolidating here means a copy
change happens in one place and is testable without a Discord client.

The cog still owns the message sends and view attachments; this module
only builds the embed payload.
"""

from __future__ import annotations

from typing import Optional

import discord

from bot_modules.dm_perms.logic import safe_field_text
from bot_modules.services.embeds import (
    DM_ACCEPT,
    DM_DENY,
    DM_PENDING,
    DM_PRIMARY,
)


def build_stale_request_embed() -> discord.Embed:
    """Embed shown when a consent button is clicked on an already-resolved request.

    Replaces the original "someone wants to connect" embed in place. The
    request may have been accepted, denied, expired, or cancelled — we
    don't try to disambiguate because the underlying row is gone.
    """
    return discord.Embed(
        title="⌛ Request no longer active",
        description=(
            "This DM request has already been answered, expired, "
            "or was cancelled."
        ),
        color=DM_PENDING,
    )


def build_guild_unavailable_embed() -> discord.Embed:
    """Embed shown when the bot has been removed from the request's guild.

    The DM still contains the buttons, but the consent records and
    member lookups all require the guild. Surface the situation rather
    than leaving the buttons dead.
    """
    return discord.Embed(
        title="❌ Server unavailable",
        description=(
            "The server this request belongs to is no longer reachable."
        ),
        color=DM_DENY,
    )


def build_acceptance_embed(
    *,
    requester_display_name: str,
    target_display_name: str,
    requester_mention: str,
    target_mention: str,
    type_label: str,
    reason: str,
) -> discord.Embed:
    """Embed shown on accept — used as the in-DM update AND the two follow-up DMs.

    Mirrors the same content across all three surfaces so the requester,
    the target, and the original button message stay consistent.
    """
    embed = discord.Embed(
        title="✅ Connection accepted!",
        color=DM_ACCEPT,
    )
    embed.description = (
        f"**{requester_display_name}** ↔ **{target_display_name}**\n\n"
        f"{requester_mention} and {target_mention} can now DM each other.\n\n"
        "Either of you can undo this at any time with `/dm_revoke`."
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    return embed


def build_denial_embed_for_view(
    *, type_label: str, reason: str, reply: str = ""
) -> discord.Embed:
    """Embed that replaces the buttons on the target's DM when they deny.

    When the denier chose "Deny with reply", ``reply`` is echoed back so they
    can see the note they sent to the requester.
    """
    embed = discord.Embed(
        title="❌ Request declined",
        description="No worries — the request was turned down.",
        color=DM_DENY,
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    if reply:
        embed.add_field(name="Your reply", value=safe_field_text(reply), inline=False)
    return embed


def build_denial_embed_for_requester(
    *,
    target_display_name: str,
    guild_name: str,
    type_label: str,
    reason: str,
    reply: str = "",
) -> discord.Embed:
    """Embed DM'd back to the requester when their request is denied.

    ``type_label`` is the human-readable label (e.g. "Direct Message");
    the description lowercases it for the natural-language sentence. When the
    denier included a ``reply``, it is shown as a message from them.
    """
    embed = discord.Embed(
        title="❌ Request declined",
        description=(
            f"Your {type_label.lower()} request "
            f"to **{target_display_name}** "
            f"in **{guild_name}** was declined."
        ),
        color=DM_DENY,
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    if reply:
        embed.add_field(
            name=f"Reply from {target_display_name}",
            value=safe_field_text(reply),
            inline=False,
        )
    return embed


def build_request_dm_embed(
    *,
    guild_name: str,
    requester_display_name: str,
    requester_avatar_url: str,
    request_timeout_label: str,
    type_label: str,
    reason: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed delivered to the target's DMs — this is the actual request prompt."""
    if colour is None:
        colour = discord.Colour(DM_PRIMARY)
    embed = discord.Embed(
        title="📨 Someone wants to connect with you",
        description=(
            f"A member of **{guild_name}** would like to connect.\n\n"
            f"This request expires in {request_timeout_label}."
        ),
        color=colour,
    )
    embed.set_author(name=requester_display_name, icon_url=requester_avatar_url)
    embed.set_footer(
        text="You can revoke this permission at any time with /dm_revoke"
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    return embed


def build_request_sent_embed(
    *,
    target_display_name: str,
    guild_name: str,
    request_timeout_label: str,
    type_label: str,
    reason: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed DM'd back to the requester confirming delivery of their request."""
    if colour is None:
        colour = discord.Colour(DM_PRIMARY)
    embed = discord.Embed(
        title="📨 Request sent!",
        description=(
            f"Your {type_label.lower()} request to **{target_display_name}** "
            f"in **{guild_name}** has been delivered.\n\n"
            f"You'll get a DM when they respond. "
            f"The request expires in {request_timeout_label}."
        ),
        color=colour,
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    return embed


def build_expired_embed(
    *,
    target_display_name: str,
    guild_name: str,
    type_label: str,
    request_timeout_label: str,
) -> discord.Embed:
    """Embed DM'd to a requester whose pending request aged out unanswered."""
    return discord.Embed(
        title="⌛ Request expired",
        description=(
            f"Your {type_label.lower()} request to "
            f"**{target_display_name}** in **{guild_name}** "
            f"expired after {request_timeout_label} without a response."
        ),
        color=DM_PENDING,
    )


def build_revoked_embed(
    *,
    requester_display_name: str,
    target_display_name: str,
    type_label: str,
    reason: Optional[str],
) -> discord.Embed:
    """Embed shown on the in-server message AND DM'd to both sides on revoke.

    ``reason`` may be ``None`` if the original consent-pair metadata was
    lost (older rows without it); ``safe_field_text`` renders the em-dash.
    """
    embed = discord.Embed(
        title="🚫 Connection removed",
        description=(
            f"**{requester_display_name}** ↔ **{target_display_name}**\n\n"
            "The DM connection between you two has been removed."
        ),
        color=DM_DENY,
    )
    embed.add_field(name="Request Type", value=type_label, inline=True)
    embed.add_field(name="Reason", value=safe_field_text(reason), inline=False)
    return embed


def build_dm_help_embed(
    guild_icon_url: Optional[str],
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed shown by ``/dm_help`` — a static overview of the DM-perm system."""
    if colour is None:
        colour = discord.Colour(DM_PRIMARY)
    embed = discord.Embed(
        title="📬 DM Request System",
        description="Control how users may request DM access with you.",
        color=colour,
    )
    if guild_icon_url:
        embed.set_thumbnail(url=guild_icon_url)
    embed.add_field(
        name="Your DM Modes",
        value=(
            "**OPEN** — Anyone may DM.\n"
            "**ASK** — You must approve requests.\n"
            "**CLOSED** — DM requests are blocked."
        ),
        inline=False,
    )
    embed.add_field(
        name="Your Commands",
        value=(
            "`/dm_set_mode` — Set your DM preference\n"
            "`/dm_revoke @user` — Revoke relationship\n"
            "`/dm_status @user` — Check relationship status\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="Moderator Tools",
        value=(
            "Request panels are set up and reposted via `/setup` and the web "
            "dashboard."
        ),
        inline=False,
    )
    embed.set_footer(text="DM relationships are logged for audit transparency.")
    return embed


def build_mode_updated_embed(
    mode: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed confirming a member's DM mode change.

    ``mode`` is expected to be one of "open", "ask", "closed". Upper-
    casing it keeps the visual consistent with the role names without
    requiring a separate label table.
    """
    if colour is None:
        colour = discord.Colour(DM_PRIMARY)
    return discord.Embed(
        title="DM preference updated",
        description=f"You're now set to **{mode.upper()}**.",
        color=colour,
    )
