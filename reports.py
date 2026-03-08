from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot

SAFE_TEXT_CHUNK = 1900


def chunk_text(text: str, limit: int = SAFE_TEXT_CHUNK) -> list[str]:
    if not text:
        return [""]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


async def send_ephemeral_text(interaction: discord.Interaction, text: str) -> None:
    for chunk in chunk_text(text):
        await interaction.followup.send(chunk, ephemeral=True)


def format_member_activity_line(member: discord.Member, activity) -> str:
    if activity is None:
        return f"{member.display_name} - no recorded message yet"

    created_at = int(activity.created_at)
    if getattr(activity, "channel_id", 0) <= 0:
        return (
            f"{member.display_name} - last seen <t:{created_at}:R> "
            f"(<t:{created_at}:f>)"
        )
    return (
        f"{member.display_name} - last seen <t:{created_at}:R> "
        f"(<t:{created_at}:f>) in <#{activity.channel_id}>"
    )


def register_reports(bot: Bot, ctx: AppContext) -> None:
    @bot.tree.command(name="listrole", description="List members who currently have a role.")
    @app_commands.describe(role="The role to inspect")
    async def listrole(interaction: discord.Interaction, role: discord.Role):
        if not role.members:
            await interaction.response.send_message(f"No members found in **{role.name}**.", ephemeral=True)
            return
        output = "\n".join(member.display_name for member in role.members)
        if len(output) > 1900:
            output = output[:1900] + "\n... (truncated)"
        await interaction.response.send_message(f"**Members in {role.name}:**\n{output}", ephemeral=True)

    @bot.tree.command(name="inactive_role", description="Report role members inactive for N days.")
    @app_commands.describe(role="Role to analyze", days="Number of days to check (default 7)")
    async def inactive_role(
        interaction: discord.Interaction, role: discord.Role, days: app_commands.Range[int, 1, 60] = 7
    ):
        member = ctx.get_interaction_member(interaction)
        if member is None or not member.guild_permissions.manage_roles:
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command only works in a server.", ephemeral=True)
            return
        cutoff = discord.utils.utcnow() - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        role_members = sorted(role.members, key=lambda current: current.display_name.lower())
        role_member_ids = [current.id for current in role_members]
        with ctx.open_db() as conn:
            activities = ctx.get_member_last_activity_map(conn, guild.id, role_member_ids)

        inactive_members = [
            current
            for current in role_members
            if activities.get(current.id) is None or activities[current.id].created_at < cutoff_ts
        ]
        total = len(role_members)
        inactive_count = len(inactive_members)
        percent = (inactive_count / total * 100) if total else 0
        summary = (
            f"**Role Activity Report -- {role.name} ({days} days)**\n"
            f"Total Members: {total}\n"
            f"Inactive: {inactive_count} ({percent:.1f}%)\n"
            f"Tracking Coverage: {len(activities)}/{total}\n"
            f"----------------------------------\n"
        )
        if inactive_members:
            block = "\n".join(
                format_member_activity_line(current, activities.get(current.id))
                for current in inactive_members
            )
            summary += "\n**Inactive Members:**\n" + block
        else:
            summary += "\nAll members active in this period."
        if any(current.id not in activities for current in inactive_members):
            summary += (
                "\n\nSome members have no recorded message yet because activity tracking "
                "starts after this version is deployed."
            )
        await send_ephemeral_text(interaction, summary)

