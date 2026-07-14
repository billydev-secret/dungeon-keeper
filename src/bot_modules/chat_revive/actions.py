"""Discord-side Chat Revive actions shared by the cog and the monitor loop."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord

from bot_modules.chat_revive.logic import render_revive
from bot_modules.games.utils.game_manager import get_active_game

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.chat_revive")


async def channel_is_busy(bot: Bot, channel_id: int) -> bool:
    """Is a game or event running here? Checks the shared games_active_games
    table plus every registered in-memory busy check (e.g. Risky Roll)."""
    games_db = getattr(bot, "games_db", None)
    if games_db is not None:
        try:
            if await get_active_game(games_db, channel_id) is not None:
                return True
        except Exception:
            log.exception("active-game check failed for channel %s", channel_id)
    for name, check in getattr(bot, "game_busy_checks", {}).items():
        try:
            if await check(channel_id):
                return True
        except Exception:
            log.exception("busy check %r failed for channel %s", name, channel_id)
    return False


class ReviveOptInButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"chat_revive_optin:(?P<role_id>\d+)",
):
    """Persistent join/leave toggle for the opt-in ping role.

    Taking the role means "I like being summoned to restart conversation";
    tapping again sheds it. Survives restarts via the dynamic-items registry.
    """

    def __init__(self, role_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="🔥 Wake me for revives",
                style=discord.ButtonStyle.primary,
                custom_id=f"chat_revive_optin:{role_id}",
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
    ) -> ReviveOptInButton:
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return
        role = guild.get_role(self.role_id)
        if role is None:
            await interaction.response.send_message(
                "That role no longer exists — ask a mod to re-run `/revive setup`.",
                ephemeral=True,
            )
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Chat Revive opt-out")
                await interaction.response.send_message(
                    "Rest easy — no more revive pings for you.", ephemeral=True
                )
            else:
                await member.add_roles(role, reason="Chat Revive opt-in")
                await interaction.response.send_message(
                    "🔥 You're on the summon list. It's rare — a few times a week at most.",
                    ephemeral=True,
                )
        except discord.HTTPException:
            log.exception("opt-in toggle failed for role %s", self.role_id)
            await interaction.response.send_message(
                "Couldn't change that role — the bot may lack permission.",
                ephemeral=True,
            )


async def send_revive(
    channel: discord.abc.Messageable,
    *,
    question_text: str,
    role_id: int | None,
    flourish: str | None,
) -> discord.Message:
    """Post the revive. Plain text; the allowed-mentions whitelist is exactly
    the revive role (or nothing), so a question containing @everyone or a
    user mention can never actually ping anyone else."""
    if role_id is not None:
        allowed = discord.AllowedMentions(
            everyone=False, users=False, replied_user=False,
            roles=[discord.Object(id=role_id)],
        )
    else:
        allowed = discord.AllowedMentions.none()
    text = render_revive(question_text, role_id=role_id, flourish=flourish)
    return await channel.send(text, allowed_mentions=allowed)
