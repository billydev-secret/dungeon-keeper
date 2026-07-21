"""Shared guards for features that hand out roles on a member's own click.

Any self-service role grant (role menus, announcement buttons) faces the same
question: is it safe to let an unprivileged member take this role by pressing a
button? The answer lives here so there is exactly one list of dangerous
permissions to keep current — role menus grew the original copy (spec §3.2).

Two call sites with different stakes:

* **Write path** (a dashboard route saving a config) — reject early with a
  message naming the role, so misconfiguration is nearly impossible.
* **Click path** (a button callback, possibly on a message posted months ago) —
  re-check and fail closed. A role that was harmless when it was configured can
  be granted ``administrator`` later, and a posted announcement is never
  revisited; without this the stale button would still hand it out.
"""

from __future__ import annotations

import discord

# Permission bits that make a role dangerous to hand out via self-service.
_DANGEROUS_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mention_everyone",
)


def is_dangerous(role: discord.Role) -> bool:
    """True if the role carries any permission we won't self-serve by default."""
    perms = role.permissions
    return any(getattr(perms, name, False) for name in _DANGEROUS_PERMS)


def role_block_reason(
    role: discord.Role | None,
    bot_member: discord.Member | None,
    *,
    allow_elevated: bool = False,
) -> str | None:
    """Why this role can't be self-assigned, or None if it can.

    The string is written to be shown to an admin on the dashboard; callers on
    the click path log it rather than surfacing it to the member (it names
    server configuration a member has no business seeing).

    ``allow_elevated`` is the role-menus per-option override — announcements
    deliberately never pass it, so an elevated role can't ride a public post.
    ``bot_member`` of None skips only the hierarchy check (the bot isn't in the
    guild yet, or we're off-gateway); every other guard still applies.
    """
    if role is None:
        return "that role doesn't exist (anymore)"
    if role.is_default():
        return f"“{role.name}” is the default role and can't be assigned"
    if role.managed:
        return f"“{role.name}” is managed by an integration and can't be self-assigned"
    if bot_member is not None and role >= bot_member.top_role:
        return f"“{role.name}” is above my highest role — I can't grant it"
    if is_dangerous(role) and not allow_elevated:
        return f"“{role.name}” carries elevated permissions"
    return None
