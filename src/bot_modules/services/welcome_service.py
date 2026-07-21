"""Welcome and leave message formatting."""

from __future__ import annotations

import discord

DEFAULT_WELCOME_MESSAGE = "Glad to have you here! Feel free to introduce yourself."
DEFAULT_LEAVE_MESSAGE = "{member_name} has left the server."

# Placeholders available in both welcome and leave templates
PLACEHOLDER_HELP = (
    "`{member}` — mention  •  `{member_name}` — display name  •  "
    "`{member_id}` — user ID  •  `{server}` — server name  •  "
    "`{member_count}` — member count  •  `{bios_channel}` — bios channel mention  •  "
    "`{bio_link}` — direct link to the bios trigger button  •  "
    "`{member_bio_link}` — jump URL to this member's own bio post (empty if they have no bio; "
    "auto-resurrects an archived bio for returning members)  •  "
    "`{server_guide}` — mention of the configured server-guide channel (empty if unset)"
)


def _resolve(
    template: str,
    member: discord.Member,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",
    member_bio_link: str = "",
    server_guide_mention: str = "",
) -> str:
    guild = member.guild
    return (
        template.replace("{member}", member.mention)
        .replace("{member_name}", member.display_name)
        .replace("{member_id}", str(member.id))
        .replace("{server}", guild.name)
        .replace("{member_count}", str(guild.member_count or 0))
        .replace("{bio_link}", bio_link)
        .replace("{bios_channel}", bios_channel_mention)
        .replace("{member_bio_link}", member_bio_link)
        .replace("{server_guide}", server_guide_mention)
    )


def build_welcome_embed(
    member: discord.Member,
    message_template: str,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",
    member_bio_link: str = "",
    server_guide_mention: str = "",
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(
            message_template,
            member,
            bio_link=bio_link,
            bios_channel_mention=bios_channel_mention,
            member_bio_link=member_bio_link,
            server_guide_mention=server_guide_mention,
        ),
        color=color or discord.Color.blurple(),
    )
    embed.set_author(
        name=f"Welcome, {member.display_name}!", icon_url=member.display_avatar.url
    )
    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"Member #{member_count} · {member.guild.name}")
    return embed


def build_leave_embed(
    member: discord.Member,
    message_template: str,
    *,
    bio_link: str = "",
    bios_channel_mention: str = "",
    member_bio_link: str = "",
    server_guide_mention: str = "",
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(
            message_template,
            member,
            bio_link=bio_link,
            bios_channel_mention=bios_channel_mention,
            member_bio_link=member_bio_link,
            server_guide_mention=server_guide_mention,
        ),
        color=color or discord.Color.blurple(),
    )
    embed.set_author(
        name=f"{member.display_name} left", icon_url=member.display_avatar.url
    )
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"{member.guild.name} · {member_count} members remaining")
    return embed


def server_guide_mention_for(channel_id: int) -> str:
    """Return ``<#channel_id>`` mention or empty string when unset."""
    return f"<#{channel_id}>" if channel_id else ""
