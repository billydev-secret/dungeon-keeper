"""Discord-side Chat Revive actions shared by the cog and the monitor loop."""

from __future__ import annotations

import asyncio
import io
import logging
import re
from typing import TYPE_CHECKING

import discord

from bot_modules.chat_revive.logic import render_revive, render_revive_caption
from bot_modules.games.utils.game_manager import get_active_game
from bot_modules.services.quote_renderer import (
    QUOTE_MAX_CHARS,
    THEMES,
    render_quote_card,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.chat_revive")

CARD_FILENAME = "revive.png"
# The persona, not the feature name: a revive should read as an organic nudge,
# not as a bot announcing its own machinery.
CARD_HEADING = "Ember"


async def channel_is_busy(bot: Bot, channel_id: int) -> bool:
    """Is a game or event running here? Checks the shared games_active_games
    table plus every registered in-memory busy check (e.g. Risky Roll).

    Fails closed: a check that raises counts as busy. The tradeoff is that a
    broken check suppresses revives instead of posting over a live game — the
    safe direction here, since a missed nudge costs nothing and talking over an
    active room is the failure this gate exists to prevent.
    """
    games_db = getattr(bot, "games_db", None)
    if games_db is not None:
        try:
            if await get_active_game(games_db, channel_id) is not None:
                return True
        except Exception:
            log.exception("active-game check failed for channel %s", channel_id)
            return True
    for name, check in getattr(bot, "game_busy_checks", {}).items():
        try:
            if await check(channel_id):
                return True
        except Exception:
            log.exception("busy check %r failed for channel %s", name, channel_id)
            return True
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


async def _render_card(guild: discord.Guild | None, question_text: str) -> bytes | None:
    """The question as a banner card, or None if we can't make one.

    Background is the server icon — the revive has no host to borrow an avatar
    from, so a guild without an icon simply falls back to plain text. A question
    too long for the card is left to plain text as well, rather than posting a
    card that silently trims the tail off the question.
    """
    if guild is None or guild.icon is None:
        return None
    if len(question_text) > QUOTE_MAX_CHARS:
        return None
    try:
        icon_bytes = await guild.icon.replace(size=512).read()
    except discord.HTTPException:
        log.warning("revive: failed to read guild icon for %s", guild.id)
        return None
    try:
        return await asyncio.to_thread(
            render_quote_card,
            question_text,
            author_name=CARD_HEADING,
            avatar_bytes=icon_bytes,
            theme=THEMES["midnight"],
            pfp_shape="none",
        )
    except Exception:
        log.exception("revive: card render failed for guild %s", guild.id)
        return None


async def send_revive(
    channel: discord.abc.Messageable,
    *,
    question_text: str,
    role_id: int | None,
    flourish: str | None,
) -> discord.Message:
    """Post the revive as a banner card, falling back to plain text.

    The question rides on the card, but a role mention can't live inside an
    image — so the ping and flourish go in the message content beside it. The
    allowed-mentions whitelist is exactly the revive role (or nothing), so a
    question containing @everyone or a user mention can never actually ping
    anyone else. If there's no icon to build a card from, or the renderer
    raises, the old plain-text footprint carries the same question and ping.
    """
    if role_id is not None:
        allowed = discord.AllowedMentions(
            everyone=False, users=False, replied_user=False,
            roles=[discord.Object(id=role_id)],
        )
    else:
        allowed = discord.AllowedMentions.none()
    card_bytes = await _render_card(getattr(channel, "guild", None), question_text)
    if card_bytes is None:
        text = render_revive(question_text, role_id=role_id, flourish=flourish)
        return await channel.send(text, allowed_mentions=allowed)
    return await channel.send(
        render_revive_caption(role_id=role_id, flourish=flourish) or None,
        file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME),
        allowed_mentions=allowed,
    )
