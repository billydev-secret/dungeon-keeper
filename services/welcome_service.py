"""Welcome and leave message formatting."""
from __future__ import annotations

import discord

DEFAULT_WELCOME_MESSAGE = "Glad to have you here! Feel free to introduce yourself."
DEFAULT_LEAVE_MESSAGE = "{member_name} has left the server."

# Placeholders available in both welcome and leave templates
PLACEHOLDER_HELP = (
    "`{member}` — mention  •  `{member_name}` — display name  •  "
    "`{member_id}` — user ID  •  `{server}` — server name  •  `{member_count}` — member count"
)


def _resolve(template: str, member: discord.Member) -> str:
    guild = member.guild
    return (
        template
        .replace("{member}", member.mention)
        .replace("{member_name}", member.display_name)
        .replace("{member_id}", str(member.id))
        .replace("{server}", guild.name)
        .replace("{member_count}", str(guild.member_count or 0))
    )


def build_welcome_embed(member: discord.Member, message_template: str) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(message_template, member),
        color=discord.Color.blurple(),
    )
    embed.set_author(name=f"Welcome, {member.display_name}!", icon_url=member.display_avatar.url)
    if member.guild.icon:
        embed.set_thumbnail(url=member.guild.icon.url)
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"Member #{member_count} · {member.guild.name}")
    return embed


def build_leave_embed(member: discord.Member, message_template: str) -> discord.Embed:
    embed = discord.Embed(
        description=_resolve(message_template, member),
        color=discord.Color.dark_grey(),
    )
    embed.set_author(name=f"{member.display_name} left", icon_url=member.display_avatar.url)
    member_count = member.guild.member_count or 0
    embed.set_footer(text=f"{member.guild.name} · {member_count} members remaining")
    return embed
