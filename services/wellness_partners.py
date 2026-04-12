"""Wellness Guardian persistent partner request view.

The Accept/Decline buttons sent in DMs need to survive bot restarts. Following
the BoosterRoleDynamicButton pattern (services/booster_roles.py:144), we
register two DynamicItem subclasses on the bot. They look up the partnership
record by ID embedded in the button's custom_id.
"""
from __future__ import annotations

import logging
from pathlib import Path

import discord

from db_utils import open_db
from services.wellness_service import (
    accept_partner_request,
    dissolve_partnership,
    get_partnership,
)

log = logging.getLogger("dungeonkeeper.wellness.partners")


def _db_path(interaction: discord.Interaction) -> Path:
    return getattr(interaction.client, "db_path", Path("dungeonkeeper.db"))


class WellnessPartnerAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"wellness_partner_accept:(?P<pid>\d+)",
):
    """Persistent Accept button on partner request DM."""

    def __init__(self, partner_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Accept",
                style=discord.ButtonStyle.success,
                emoji="💚",
                custom_id=f"wellness_partner_accept:{partner_id}",
            )
        )
        self.partner_id = partner_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ) -> "WellnessPartnerAcceptButton":
        return cls(int(match["pid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        with open_db(_db_path(interaction)) as conn:
            partnership = get_partnership(conn, self.partner_id)
            if partnership is None:
                await interaction.response.edit_message(
                    content="This partner request no longer exists.",
                    view=None,
                )
                return
            if partnership.status != "pending":
                await interaction.response.edit_message(
                    content="This partner request was already handled.",
                    view=None,
                )
                return
            # Only the non-requester can accept
            if interaction.user.id == partnership.requester_id:
                await interaction.response.send_message(
                    "You can't accept your own request.", ephemeral=True,
                )
                return
            target_id = partnership.other(partnership.requester_id)
            if interaction.user.id != target_id:
                await interaction.response.send_message(
                    "This request isn't for you.", ephemeral=True,
                )
                return
            accept_partner_request(conn, self.partner_id)

        # Notify the requester
        client = interaction.client
        try:
            requester = await client.fetch_user(partnership.requester_id)
            embed = discord.Embed(
                title="💚 Partner request accepted!",
                description=f"**{interaction.user.display_name}** is now your accountability partner.",
                color=discord.Color.from_str("#7BC97B"),
            )
            await requester.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass

        await interaction.response.edit_message(
            content="💚 You're now accountability partners. Cheer each other on!",
            embed=None,
            view=None,
        )


class WellnessPartnerDeclineButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"wellness_partner_decline:(?P<pid>\d+)",
):
    """Persistent Decline button on partner request DM."""

    def __init__(self, partner_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Decline",
                style=discord.ButtonStyle.secondary,
                custom_id=f"wellness_partner_decline:{partner_id}",
            )
        )
        self.partner_id = partner_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls, interaction, item, match, /,
    ) -> "WellnessPartnerDeclineButton":
        return cls(int(match["pid"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        with open_db(_db_path(interaction)) as conn:
            partnership = get_partnership(conn, self.partner_id)
            if partnership is None:
                await interaction.response.edit_message(
                    content="This partner request no longer exists.",
                    view=None,
                )
                return
            if partnership.status != "pending":
                await interaction.response.edit_message(
                    content="This partner request was already handled.",
                    view=None,
                )
                return
            target_id = partnership.other(partnership.requester_id)
            if interaction.user.id != target_id:
                await interaction.response.send_message(
                    "This request isn't for you.", ephemeral=True,
                )
                return
            dissolve_partnership(conn, self.partner_id)

        await interaction.response.edit_message(
            content="No worries — request declined politely.",
            embed=None,
            view=None,
        )


def make_partner_request_view(partner_id: int) -> discord.ui.View:
    """Build the request DM view (used both for new requests and rehydration)."""
    view = discord.ui.View(timeout=None)
    view.add_item(WellnessPartnerAcceptButton(partner_id))
    view.add_item(WellnessPartnerDeclineButton(partner_id))
    return view
