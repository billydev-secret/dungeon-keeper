"""Role grant commands — single /grant command driven from the grant_roles DB table."""

from __future__ import annotations

import asyncio
import logging
import time
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
    from bot_modules.services.role_grant_audit_service import mark_restored

    guild_id = guild.id
    member_id = member.id
    role_name = role.name
    granted_role_id = role.id

    def _do_log() -> None:
        with ctx.open_db() as db_conn:
            log_role_event(db_conn, guild_id, member_id, role_name, "grant")
            # A re-grant closes any open prune event so the grant-audit panel
            # stops listing this member as stripped-but-not-restored.
            mark_restored(db_conn, guild_id, member_id, granted_role_id, time.time())

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


# The old /grant_missing audit view moved to the web dashboard's Grant Audit
# panel (GET /api/reports/grant-audit), backed by
# bot_modules.services.role_grant_audit_service. /grant_audit below posts the
# same buckets as an auto-updating channel card.


async def _execute_grant_audit_post(
    interaction: discord.Interaction,
    role_key: str,
    min_level: int,
    channel: discord.TextChannel | None,
    ctx: AppContext,
) -> None:
    """Post (or refresh/move) the auto-updating grant-audit card.

    Same channel-id/message-id pattern as the economy leaderboard panel:
    posting again in the same channel edits in place, posting elsewhere moves
    the card (deleting the stale one when reachable), and the hourly
    ``grant_audit_card_loop`` keeps whichever message is stored fresh.
    """
    from bot_modules.core.branding import resolve_accent_color
    from bot_modules.services.role_grant_audit_service import (
        build_grant_audit_embed,
        gather_grant_audit,
        load_card_ref,
        resolve_grant_audit_buckets,
        save_card_ref,
    )

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

    role = guild.get_role(cfg["role_id"])
    if role is None:
        await interaction.response.send_message(
            "The configured role no longer exists.", ephemeral=True
        )
        return

    if min_level < 1:
        await interaction.response.send_message(
            "min_level must be at least 1.", ephemeral=True
        )
        return

    target = channel or interaction.channel
    if not isinstance(target, discord.TextChannel):
        await interaction.response.send_message(
            "Pick a regular text channel for the card.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    import time as _time

    guild_id = guild.id
    role_id = role.id
    now_ts = _time.time()

    def _load():
        with ctx.open_db() as conn:
            return (
                load_card_ref(conn, guild_id),
                gather_grant_audit(conn, guild_id, role_id, min_level),
            )

    ref, gathered = await asyncio.to_thread(_load)
    snap = resolve_grant_audit_buckets(guild, role, gathered, min_level, now_ts)
    accent = await resolve_accent_color(ctx.db_path, guild)
    embed = build_grant_audit_embed(cfg["label"], snap, now_ts=now_ts, color=accent)

    message: discord.Message | None = None
    if ref.message_id and ref.channel_id == target.id:
        try:
            old = await target.fetch_message(ref.message_id)
            await old.edit(embed=embed)
            message = old
        except discord.HTTPException:
            pass  # gone or unreachable — fall through to a fresh post

    if message is None:
        if ref.message_id and ref.channel_id and ref.channel_id != target.id:
            old_channel = guild.get_channel(ref.channel_id)
            if isinstance(old_channel, discord.TextChannel):
                try:
                    stale = await old_channel.fetch_message(ref.message_id)
                    await stale.delete()
                except discord.HTTPException:
                    pass
        try:
            message = await target.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I can't post in {target.mention}.", ephemeral=True
            )
            return

    message_id = message.id

    def _save() -> None:
        with ctx.open_db() as conn:
            save_card_ref(conn, guild_id, target.id, message_id, role_key, min_level)

    await asyncio.to_thread(_save)
    await interaction.followup.send(
        f"Grant-audit card for **{cfg['label']}** is live in {target.mention} — "
        "it refreshes hourly; delete the message to retire it.",
        ephemeral=True,
    )
