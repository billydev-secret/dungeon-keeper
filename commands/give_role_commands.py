"""Configurable /give_role command with per-role permission control.

Mods configure which users/roles are allowed to give specific roles:
  /give_role_allow  role_to_give: @fashion  allowed: @moderators
  /give_role_allow  role_to_give: @fashion  allowed: @amber
  /give_role_deny   role_to_give: @fashion  allowed: @moderators
  /give_role_list

Then authorised users can:
  /give_role  member: @someone  role: @fashion
"""
from __future__ import annotations

import logging
import re
import sqlite3
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from utils import format_user_for_log, get_bot_member

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.give_role")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def init_give_role_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS give_role_permissions (
            guild_id     INTEGER NOT NULL,
            target_role_id INTEGER NOT NULL,
            entity_type  TEXT NOT NULL CHECK(entity_type IN ('user', 'role')),
            entity_id    INTEGER NOT NULL,
            PRIMARY KEY (guild_id, target_role_id, entity_type, entity_id)
        )
        """
    )


def add_give_role_permission(
    conn: sqlite3.Connection, guild_id: int, target_role_id: int, entity_type: str, entity_id: int
) -> bool:
    """Returns True if a new row was inserted, False if it already existed."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO give_role_permissions (guild_id, target_role_id, entity_type, entity_id)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, target_role_id, entity_type, entity_id),
    )
    return cur.rowcount > 0


def remove_give_role_permission(
    conn: sqlite3.Connection, guild_id: int, target_role_id: int, entity_type: str, entity_id: int
) -> bool:
    """Returns True if a row was deleted."""
    cur = conn.execute(
        """
        DELETE FROM give_role_permissions
        WHERE guild_id = ? AND target_role_id = ? AND entity_type = ? AND entity_id = ?
        """,
        (guild_id, target_role_id, entity_type, entity_id),
    )
    return cur.rowcount > 0


def get_give_role_permissions(
    conn: sqlite3.Connection, guild_id: int
) -> list[tuple[int, str, int]]:
    """Returns all (target_role_id, entity_type, entity_id) rows for a guild."""
    rows = conn.execute(
        """
        SELECT target_role_id, entity_type, entity_id
        FROM give_role_permissions
        WHERE guild_id = ?
        ORDER BY target_role_id, entity_type, entity_id
        """,
        (guild_id,),
    ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def can_give_role(
    conn: sqlite3.Connection, guild_id: int, target_role_id: int, member: discord.Member
) -> bool:
    """Check whether *member* is authorised to give *target_role_id*."""
    member_role_ids = [r.id for r in member.roles]
    # Check user-level permission
    row = conn.execute(
        """
        SELECT 1 FROM give_role_permissions
        WHERE guild_id = ? AND target_role_id = ? AND entity_type = 'user' AND entity_id = ?
        LIMIT 1
        """,
        (guild_id, target_role_id, member.id),
    ).fetchone()
    if row:
        return True
    # Check role-level permissions
    if member_role_ids:
        placeholders = ",".join("?" * len(member_role_ids))
        row = conn.execute(
            f"""
            SELECT 1 FROM give_role_permissions
            WHERE guild_id = ? AND target_role_id = ? AND entity_type = 'role'
              AND entity_id IN ({placeholders})
            LIMIT 1
            """,
            (guild_id, target_role_id, *member_role_ids),
        ).fetchone()
        if row:
            return True
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def register_give_role_commands(bot: Bot, ctx: AppContext) -> None:

    @bot.tree.command(name="give_role", description="Give a role to a member.")
    @app_commands.describe(member="Member to receive the role.", role="Role to give.")
    async def give_role_cmd(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        actor = ctx.get_interaction_member(interaction)
        if actor is None:
            await interaction.response.send_message("Could not resolve your membership.", ephemeral=True)
            return

        # Mods always pass; otherwise check DB permissions
        if not ctx.is_mod(interaction):
            with ctx.open_db() as conn:
                if not can_give_role(conn, guild.id, role.id, actor):
                    await interaction.response.send_message(
                        "You don't have permission to give that role.", ephemeral=True
                    )
                    return

        if member.bot:
            await interaction.response.send_message("Bots can't receive roles via this command.", ephemeral=True)
            return

        if role in member.roles:
            await interaction.response.send_message(
                f"{member.mention} already has {role.mention}.", ephemeral=True
            )
            return

        bot_member = get_bot_member(guild)
        if bot_member is None:
            await interaction.response.send_message("Bot member context is unavailable right now.", ephemeral=True)
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need the Manage Roles permission to do that.", ephemeral=True
            )
            return

        if role >= bot_member.top_role:
            await interaction.response.send_message(
                f"I can't grant {role.mention} because it is above my highest role.", ephemeral=True
            )
            return

        try:
            await member.add_roles(role, reason=f"Granted by {interaction.user} via /give_role")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I couldn't grant {role.mention}. Check my role hierarchy and permissions.", ephemeral=True
            )
            return

        log.info(
            "%s gave %s to %s via /give_role.",
            format_user_for_log(actor, interaction.user.id),
            role.name,
            format_user_for_log(member),
        )
        await interaction.response.send_message(
            f"{member.mention} has been given {role.mention}.", ephemeral=False
        )

    # -- /give_role_allow ------------------------------------------------

    @bot.tree.command(
        name="give_role_allow",
        description="Allow a user or role to give a specific role via /give_role.",
    )
    @app_commands.describe(
        role_to_give="The role that can be given.",
        allowed="The user or role to authorise (mention a role or user).",
    )
    async def give_role_allow_cmd(
        interaction: discord.Interaction,
        role_to_give: discord.Role,
        allowed: str,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "Only mods can configure give_role permissions.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        entity_type, entity_id, display = _parse_mention(allowed, guild)
        if entity_type is None:
            await interaction.response.send_message(
                "Could not parse that mention. Please provide a role or user ID/mention.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            added = add_give_role_permission(conn, guild.id, role_to_give.id, entity_type, entity_id)

        if added:
            await interaction.response.send_message(
                f"{display} can now give {role_to_give.mention} via `/give_role`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{display} was already allowed to give {role_to_give.mention}.",
                ephemeral=True,
            )

    # -- /give_role_deny -------------------------------------------------

    @bot.tree.command(
        name="give_role_deny",
        description="Remove a user or role's permission to give a specific role.",
    )
    @app_commands.describe(
        role_to_give="The role that was being given.",
        denied="The user or role to remove authorisation from.",
    )
    async def give_role_deny_cmd(
        interaction: discord.Interaction,
        role_to_give: discord.Role,
        denied: str,
    ):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "Only mods can configure give_role permissions.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        entity_type, entity_id, display = _parse_mention(denied, guild)
        if entity_type is None:
            await interaction.response.send_message(
                "Could not parse that mention. Please provide a role or user ID/mention.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            removed = remove_give_role_permission(conn, guild.id, role_to_give.id, entity_type, entity_id)

        if removed:
            await interaction.response.send_message(
                f"{display} can no longer give {role_to_give.mention} via `/give_role`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"{display} didn't have permission to give {role_to_give.mention}.",
                ephemeral=True,
            )

    # -- /give_role_list -------------------------------------------------

    @bot.tree.command(
        name="give_role_list",
        description="List all /give_role permission rules.",
    )
    async def give_role_list_cmd(interaction: discord.Interaction):
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "Only mods can view give_role permissions.", ephemeral=True
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return

        with ctx.open_db() as conn:
            perms = get_give_role_permissions(conn, guild.id)

        if not perms:
            await interaction.response.send_message("No /give_role rules configured.", ephemeral=True)
            return

        lines: list[str] = []
        current_role_id = None
        for target_role_id, entity_type, entity_id in perms:
            if target_role_id != current_role_id:
                role = guild.get_role(target_role_id)
                role_name = role.mention if role else f"Unknown role ({target_role_id})"
                lines.append(f"\n**{role_name}** can be given by:")
                current_role_id = target_role_id
            if entity_type == "role":
                entity_role = guild.get_role(entity_id)
                label = entity_role.mention if entity_role else f"Unknown role ({entity_id})"
            else:
                member = guild.get_member(entity_id)
                label = member.mention if member else f"Unknown user ({entity_id})"
            lines.append(f"  - {label} ({entity_type})")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


# ---------------------------------------------------------------------------
# Mention parsing helper
# ---------------------------------------------------------------------------

_ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
_USER_MENTION_RE = re.compile(r"<@!?(\d+)>")


def _parse_mention(text: str, guild: discord.Guild) -> tuple[str | None, int, str]:
    """Parse a role mention, user mention, or raw ID.

    Returns (entity_type, entity_id, display_string) or (None, 0, "") on failure.
    """
    text = text.strip()

    # Role mention: <@&123>
    m = _ROLE_MENTION_RE.fullmatch(text)
    if m:
        rid = int(m.group(1))
        role = guild.get_role(rid)
        return "role", rid, role.mention if role else f"Role {rid}"

    # User mention: <@123> or <@!123>
    m = _USER_MENTION_RE.fullmatch(text)
    if m:
        uid = int(m.group(1))
        member = guild.get_member(uid)
        return "user", uid, member.mention if member else f"User {uid}"

    # Raw numeric ID — try role first, then user
    if text.isdigit():
        eid = int(text)
        role = guild.get_role(eid)
        if role:
            return "role", eid, role.mention
        member = guild.get_member(eid)
        if member:
            return "user", eid, member.mention
        # Unknown — default to user
        return "user", eid, f"User {eid}"

    return None, 0, ""
