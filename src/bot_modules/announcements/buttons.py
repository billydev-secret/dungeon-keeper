"""Self-assign role buttons carried by a posted announcement.

The role id *is* the state: it's embedded in the ``custom_id`` and matched back
out by the dynamic-items registry, so a click routes correctly after a restart
and even after the announcement row is deleted. That's deliberate — an
announcement is a fire-and-forget post, and a button that silently died with its
database row would be worse than one that keeps working.

Because those posts stay clickable indefinitely, the grant path re-runs the
safety check on every click (``role_block_reason``) instead of trusting the
validation the dashboard did when the announcement was written. A role that
picks up ``administrator`` six months later stops being grantable the moment it
does. Shedding a role is never blocked — only handing one out.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.role_safety import role_block_reason
from bot_modules.core.utils import get_bot_member

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.announcements")

CUSTOM_ID_PREFIX = "ann_role"

# One Discord action row.
MAX_BUTTONS = 5

BUTTON_STYLES = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "success": discord.ButtonStyle.success,
}
DEFAULT_STYLE = "primary"

MSG_NOT_IN_GUILD = "This only works in a server."
MSG_GONE = "That role isn't available anymore — ask a mod."
MSG_BLOCKED = "That role isn't available anymore — ask a mod."
MSG_FAILED = "I couldn't change your roles just now — ask a mod to check my permissions."


def resolve_style(name: str | None) -> discord.ButtonStyle:
    """Config string → ButtonStyle, tolerating anything unexpected."""
    return BUTTON_STYLES.get((name or "").strip().lower(), BUTTON_STYLES[DEFAULT_STYLE])


def button_label(row, guild: discord.Guild | None) -> str:
    """The button's text: the configured label, else the role's current name.

    Falling back to the live role name means a renamed role relabels its own
    button on the next post without anyone editing the announcement.
    """
    label = (row["label"] or "").strip()
    if label:
        return label[:80]
    role = guild.get_role(int(row["role_id"])) if guild is not None else None
    return (role.name if role is not None else "Get role")[:80]


def build_announcement_view(rows, guild: discord.Guild | None) -> discord.ui.View | None:
    """A view of role buttons for a posted announcement, or None if it has none.

    ``timeout=None`` keeps the view alive in-process; persistence across restarts
    comes from the dynamic-items registry matching the custom_id, not from here.
    """
    rows = list(rows)[:MAX_BUTTONS]
    if not rows:
        return None
    view = discord.ui.View(timeout=None)
    for row in rows:
        view.add_item(
            AnnouncementRoleButton(
                int(row["role_id"]),
                label=button_label(row, guild),
                emoji=(row["emoji"] or "").strip() or None,
                style=resolve_style(row["style"]),
            )
        )
    return view


class AnnouncementRoleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=rf"{CUSTOM_ID_PREFIX}:(?P<role_id>\d+)",
):
    """Persistent toggle that grants or sheds one role.

    Label/emoji/style are cosmetic and live only on the posted message, so a
    reconstructed instance (see ``from_custom_id``) doesn't need them — the click
    path only ever reads ``role_id``.
    """

    def __init__(
        self,
        role_id: int,
        *,
        label: str = "Get role",
        emoji: str | None = None,
        style: discord.ButtonStyle = discord.ButtonStyle.primary,
    ) -> None:
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"{CUSTOM_ID_PREFIX}:{role_id}",
            )
        )
        self.role_id = role_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> AnnouncementRoleButton:
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(MSG_NOT_IN_GUILD, ephemeral=True)
            return

        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(MSG_GONE, ephemeral=True)
            return

        # Giving a role back is always allowed — only the grant is gated, so a
        # role that turned dangerous can still be shed by whoever holds it.
        if role in member.roles:
            await self._apply(interaction, member, role, grant=False)
            return

        reason = role_block_reason(role, get_bot_member(guild))
        if reason is not None:
            # The member can't act on this and shouldn't see the server's
            # permission layout; the detail goes to the log for an admin.
            log.warning(
                "announcement role button refused role %s in guild %s: %s",
                self.role_id, guild.id, reason,
            )
            await interaction.response.send_message(MSG_BLOCKED, ephemeral=True)
            return

        await self._apply(interaction, member, role, grant=True)

    async def _apply(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        role: discord.Role,
        *,
        grant: bool,
    ) -> None:
        try:
            if grant:
                await member.add_roles(role, reason="Announcement role button")
            else:
                await member.remove_roles(role, reason="Announcement role button")
        except discord.HTTPException:
            log.exception("announcement role toggle failed for role %s", self.role_id)
            await interaction.response.send_message(MSG_FAILED, ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ You now have **@{role.name}**." if grant
            else f"✅ Removed **@{role.name}**.",
            ephemeral=True,
        )
        if grant:
            # role_pick quest trigger — same setup kind the role-menu path
            # fires; constant occurrence, so a button and a menu can't
            # both pay.
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                cast("Bot", interaction.client), member.guild.id, member.id,
                "role_pick", occurrence="set",
            )
