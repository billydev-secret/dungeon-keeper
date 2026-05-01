"""Shared utilities for /beta slash command modules."""

from __future__ import annotations

import discord


def has_mod_or_admin(member: discord.Member) -> bool:
    """True if member has any role named 'Mod' or 'Admin'."""
    if not getattr(member, "roles", None):
        return False
    names = {r.name for r in member.roles}
    return bool(names & {"Mod", "Admin"})


async def reject_if_not_mod(interaction: discord.Interaction) -> bool:
    """Send an ephemeral 'mods only' message and return False if not authorized.
    Otherwise return True."""
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if member is None or not has_mod_or_admin(member):
        await interaction.response.send_message(
            "This command is restricted to moderators.", ephemeral=True,
        )
        return False
    return True
