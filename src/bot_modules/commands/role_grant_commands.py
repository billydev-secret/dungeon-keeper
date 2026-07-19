"""Role grant commands — single /grant command driven from the grant_roles DB table."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import format_user_for_log, get_bot_member

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.role_grant")


def _resolve_grant_message(
    template: str,
    member: discord.Member,
    role: discord.Role,
    actor: discord.Member | None,
    interaction: discord.Interaction,
) -> str:
    return (
        template.replace("{member}", member.mention)
        .replace("{member_name}", member.display_name)
        .replace("{role}", role.mention)
        .replace("{role_name}", role.name)
        .replace("{actor}", actor.mention if actor else interaction.user.mention)
    )


async def _execute_grant(
    interaction: discord.Interaction,
    member: discord.Member,
    role_id: int,
    log_channel_id: int,
    announce_channel_id: int,
    grant_message: str,
    ctx: AppContext,
    required_role_id: int = 0,
) -> None:
    """Shared grant logic for all role-grant commands."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True
        )
        return

    actor = ctx.get_interaction_member(interaction)

    if member.bot:
        await interaction.response.send_message(
            "Bots can't receive this role.", ephemeral=True
        )
        return

    if actor is not None and member.id == actor.id and not ctx.is_mod(interaction):
        await interaction.response.send_message(
            "You can't grant this role to yourself.", ephemeral=True
        )
        return

    if role_id <= 0:
        await interaction.response.send_message(
            "This role is not configured yet.", ephemeral=True
        )
        return

    role = guild.get_role(role_id)
    if role is None:
        await interaction.response.send_message(
            "The configured role no longer exists.", ephemeral=True
        )
        return

    if required_role_id > 0 and not ctx.is_mod(interaction):
        req_role = guild.get_role(required_role_id)
        if req_role is None:
            await interaction.response.send_message(
                "This grant is misconfigured — the required role no longer exists. Contact an admin.",
                ephemeral=True,
            )
            return
        if req_role not in member.roles:
            await interaction.response.send_message(
                f"{member.mention} needs {req_role.mention} before they can receive {role.mention}.",
                ephemeral=True,
            )
            return

    if role in member.roles:
        await interaction.response.send_message(
            f"{member.mention} already has {role.mention}.", ephemeral=True
        )
        return

    bot_member = get_bot_member(guild)
    if bot_member is None:
        await interaction.response.send_message(
            "Bot member context is unavailable right now.", ephemeral=True
        )
        return

    if not bot_member.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "I need the Manage Roles permission to do that.", ephemeral=True
        )
        return

    if role >= bot_member.top_role:
        await interaction.response.send_message(
            f"I can't grant {role.mention} because it is above my highest role.",
            ephemeral=True,
        )
        return

    # Defer before the slow add_roles API call to avoid the 3-second timeout.
    await interaction.response.defer(ephemeral=True)

    try:
        await member.add_roles(
            role, reason=f"Granted by {interaction.user} via slash command"
        )
    except discord.Forbidden:
        await interaction.followup.send(
            f"I couldn't grant {role.mention}. Check my role hierarchy and permissions.",
            ephemeral=True,
        )
        return

    from bot_modules.core.xp_system import log_role_event

    guild_id = guild.id
    member_id = member.id
    role_name = role.name

    def _do_log() -> None:
        with ctx.open_db() as db_conn:
            log_role_event(db_conn, guild_id, member_id, role_name, "grant")

    await asyncio.to_thread(_do_log)

    log.info(
        "%s granted %s to %s.",
        format_user_for_log(actor, interaction.user.id),
        role.name,
        format_user_for_log(member),
    )
    await interaction.followup.send(
        f"{member.mention} has been granted {role.mention}."
    )

    if announce_channel_id > 0 and grant_message:
        announce_channel = guild.get_channel(announce_channel_id)
        if isinstance(announce_channel, discord.TextChannel):
            await announce_channel.send(
                _resolve_grant_message(grant_message, member, role, actor, interaction)
            )

    if log_channel_id > 0:
        log_channel = guild.get_channel(log_channel_id)
        if isinstance(log_channel, discord.TextChannel):
            audit_embed = discord.Embed(
                description=(
                    f"{member.display_name} was granted {role.name}"
                    f" by {interaction.user.display_name}."
                ),
                color=discord.Color.green(),
            )
            await log_channel.send(
                embed=audit_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def _execute_grant_missing(
    interaction: discord.Interaction,
    role_key: str,
    min_level: int,
    ctx: AppContext,
) -> None:
    """List members past a level who are missing a configured grant role."""
    from bot_modules.inactive.store import active_inactive_user_ids
    from bot_modules.services.xp_service import candidates_missing_grant_check

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "This command only works in a server.", ephemeral=True
        )
        return

    cfg = ctx.guild_config(guild.id).grant_roles.get(role_key)
    if cfg is None or cfg["role_id"] <= 0:
        await interaction.response.send_message(
            "This grant role is not configured.", ephemeral=True
        )
        return

    grant_role = guild.get_role(cfg["role_id"])
    if grant_role is None:
        await interaction.response.send_message(
            "The configured role no longer exists.", ephemeral=True
        )
        return

    if min_level < 1:
        await interaction.response.send_message(
            "min_level must be at least 1.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    guild_id = guild.id

    def _query() -> tuple[dict[int, int], set[int]]:
        with ctx.open_db() as conn:
            levels = {
                int(r["user_id"]): int(r["level"])
                for r in conn.execute(
                    "SELECT user_id, level FROM member_xp WHERE guild_id=? AND level>=?",
                    (guild_id, min_level),
                ).fetchall()
            }
            inactive_ids = active_inactive_user_ids(conn, guild_id)
        return levels, inactive_ids

    levels, inactive_ids = await asyncio.to_thread(_query)

    missing: list[tuple[discord.Member, int]] = []
    for user_id, level in candidates_missing_grant_check(levels, inactive_ids):
        member = guild.get_member(user_id)
        if member is None or member.bot or grant_role in member.roles:
            continue
        missing.append((member, level))

    if not missing:
        await interaction.followup.send(
            f"Nobody at level {min_level}+ is missing **{cfg['label']}**.",
            ephemeral=True,
        )
        return

    shown_cap = 40
    lines = [f"• {m.mention} — level {lvl}" for m, lvl in missing[:shown_cap]]
    extra = len(missing) - shown_cap
    if extra > 0:
        lines.append(f"…and {extra} more.")
    embed = discord.Embed(
        title=f"Level {min_level}+ missing {cfg['label']}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(
        text="Excludes members on an active inactive-channel hold — their roles were stripped, not skipped."
    )
    await interaction.followup.send(
        embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )
