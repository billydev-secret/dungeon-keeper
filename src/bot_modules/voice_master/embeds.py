"""Embed builders for the Voice Master cog.

These functions take primitive arguments and return new ``discord.Embed``
objects. None of them perform IO. The cog used to build identical embeds
inline at several sites; consolidating here means a copy change happens
in one place and is testable without a Discord client.

The cog still owns the message sends and view attachments; this module
only builds the embed payload.
"""

from __future__ import annotations

import discord


# Human labels for the four access states, shown in the profile embed.
_ACCESS_STATE_LABELS: dict[str, str] = {
    "open": "🔓 Open",
    "nsfw": "🔞 NSFW — open",
    "locked": "🔒 NSFW — locked",
    "spectate": "🎭 Spectator",
}


def build_profile_show_embed(
    *,
    saved_name: str | None,
    saved_limit: int,
    access_state: str,
    trusted_count: int,
    blocked_count: int,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed for ``/voice profile show``.

    Empty ``saved_name`` renders ``*(template default)*`` so the user can
    tell the difference between "I cleared this" and "I never set it"
    (functionally identical, but the UI explains the fall-through).
    """
    if colour is None:
        colour = discord.Color.blurple()
    embed = discord.Embed(
        title="Your Voice Master profile",
        color=colour,
    )
    embed.add_field(
        name="Saved name",
        value=saved_name or "*(template default)*",
        inline=False,
    )
    embed.add_field(
        name="User limit",
        value=str(saved_limit) if saved_limit else "no cap",
        inline=True,
    )
    embed.add_field(
        name="Access",
        value=_ACCESS_STATE_LABELS.get(access_state, access_state),
        inline=True,
    )
    embed.add_field(name="Trusted (count)", value=str(trusted_count), inline=True)
    embed.add_field(name="Blocked (count)", value=str(blocked_count), inline=True)
    return embed


def build_admin_audit_mirror_embed(
    *,
    action: str,
    summary: str,
    actor_name: str,
    actor_id: int,
) -> discord.Embed:
    """Embed posted to mod-log for any web admin force-* action.

    ``action`` is the short label (e.g. ``"force-delete"``); the title
    prefixes with ``Voice Master ·`` so the audit feed groups our
    actions visually with other domain entries.
    """
    embed = discord.Embed(
        title=f"Voice Master · {action}",
        description=summary,
        color=discord.Color.orange(),
    )
    embed.set_footer(text=f"by {actor_name} ({actor_id})")
    return embed


def build_claim_prompt_embed(
    *, channel_name: str, colour: "discord.Colour | None" = None
) -> discord.Embed:
    """Prompt dropped into a channel's side chat once its owner is gone for good.

    Posted only after the owner-grace window elapses (a brief disconnect won't
    trigger it), so its presence means the channel is genuinely claimable now.
    """
    if colour is None:
        colour = discord.Color.gold()
    embed = discord.Embed(
        title="👑 Channel up for grabs",
        description=(
            "The owner left and didn't come back. Anyone in this channel can "
            "take it over — claim it to rename, invite, and manage the room."
        ),
        color=colour,
    )
    embed.set_footer(text=channel_name)
    return embed


def build_claim_done_embed(
    *, claimer_mention: str, channel_name: str
) -> discord.Embed:
    """Replaces the claim prompt once someone takes ownership."""
    embed = discord.Embed(
        title="👑 Channel claimed",
        description=f"{claimer_mention} is now the owner of this channel.",
        color=discord.Color.green(),
    )
    embed.set_footer(text=channel_name)
    return embed


def build_panel_embed(colour: "discord.Colour | None" = None) -> discord.Embed:
    """Embed for the persistent control-channel Voice Master panel."""
    if colour is None:
        colour = discord.Color.blurple()
    embed = discord.Embed(
        title="Voice Master controls",
        description=(
            "Join the Hub voice channel to spin up your own room.\n"
            "Use the menus below to manage **the channel you currently own**.\n\n"
            "Set **who can see and join** in one pick:\n"
            "🔓 **Open** · 🔞 **NSFW — open** (age-gated) · "
            "🔒 **NSFW — locked** (age-gated, hidden, invite-only) · "
            "🎭 **Spectator** (age-gated audience).\n"
            "People you invite can always get in, even when locked.\n"
        ),
        color=colour,
    )
    embed.set_footer(
        text="Menus act on the channel you own. Don't own one? Join the Hub."
    )
    return embed


def build_inline_panel_embed(
    *, owner_mention: str, colour: "discord.Colour | None" = None
) -> discord.Embed:
    """Owner-greeting embed for the panel posted into a new channel's chat."""
    if colour is None:
        colour = discord.Color.blurple()
    return discord.Embed(
        title="Your voice channel is ready",
        description=(
            f"Welcome, {owner_mention}. Use the menus below to manage "
            "**this channel** — set its access (open, NSFW, locked, or "
            "spectator), rename it, set a user limit, invite or kick members, "
            "transfer ownership, or reset it. "
            "Changes you make are saved as your default for next time."
        ),
        color=colour,
    )


def build_howto_embed(
    *, hub_mention: str | None = None, colour: "discord.Colour | None" = None
) -> discord.Embed:
    """A member-facing 'how it works' guide, meant for a lobby channel.

    ``hub_mention`` is an optional ``<#id>`` mention for the configured Hub
    channel; when absent (Hub unset) we fall back to plain text so the guide
    still reads correctly on an unconfigured guild.

    Kept comfortably inside Discord's embed limits (field values ≤1024,
    ≤25 fields) — the command lists are short by design.
    """
    if colour is None:
        colour = discord.Color.blurple()
    hub = hub_mention or "the **Hub** voice channel"
    embed = discord.Embed(
        title="🔊 Make Your Own Voice Channel",
        description=(
            f"Click {hub} to join, and the bot instantly makes you a "
            "**private room of your own** that cleans itself up once everyone "
            "leaves.\n\n"
            "Set it up right in the room's **side chat** — use the control "
            "panel there, or `/voice` commands."
        ),
        color=colour,
    )
    embed.add_field(
        name="🔑 Who can get in",
        value=(
            "Pick one **access** state:\n"
            "🔓 **Open** — all welcome · 🔞 **NSFW — open** — age-gated but open\n"
            "🔒 **NSFW — locked** — age-gated, hidden, others must knock\n"
            "🎭 **Spectator** — age-gated audience: join muted, read-only\n"
            "👋 **Invite** someone in · 🚫 **Kick** someone out · "
            "🔔 **Knock** to ask into a locked room"
        ),
        inline=False,
    )
    embed.set_footer(text="Join the Hub to get started — your room is yours.")
    return embed


def build_knock_request_embed(
    *,
    requester_mention: str,
    owner_mention: str,
    channel_name: str,
    colour: "discord.Colour | None" = None,
) -> discord.Embed:
    """Embed posted to the control channel when someone knocks on a channel."""
    if colour is None:
        colour = discord.Color.gold()
    return discord.Embed(
        title="🔔 Voice channel knock",
        description=(
            f"{requester_mention} is asking to join **{channel_name}**.\n"
            f"Owner: {owner_mention} — choose below."
        ),
        color=colour,
    )
