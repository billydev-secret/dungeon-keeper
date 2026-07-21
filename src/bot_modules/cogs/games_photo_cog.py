import asyncio
import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from bot_modules.games.utils.game_manager import (
    create_game,
    get_game_options,
    update_session,
    end_game,
)
from bot_modules.games.utils.question_source import get_photo_prompt, channel_allows_nsfw
from bot_modules.services.quote_renderer import render_quote_card, THEMES

log = logging.getLogger(__name__)

CARD_FILENAME = "photo.png"

# Header drawn on the card. Plain text — the card header is rendered by PIL
# with Inter, which has no color-emoji glyphs (the 📸 lives in GAME_ICONS and
# Discord message text only).
LABEL = "PHOTO CHALLENGE"


async def _resolve_card_image(guild: discord.Guild | None, bot, host_id: int) -> bytes | None:
    """Bytes for the card background — the server avatar, host avatar fallback.

    The card *is* the deliverable, so this tries hard to return something:
    guild icon first, then the host's avatar if the server has no icon.
    Returns None only if everything fails.
    """
    if guild is not None and guild.icon is not None:
        try:
            return await guild.icon.replace(size=512).read()
        except discord.HTTPException:
            log.warning("photo: failed to read guild icon for %s", getattr(guild, "id", "?"))
    member = guild.get_member(host_id) if guild else None
    user = member
    if user is None:
        try:
            user = await bot.fetch_user(host_id)
        except discord.HTTPException:
            user = None
    if user is not None:
        try:
            return await user.display_avatar.with_size(512).read()
        except discord.HTTPException:
            log.warning("photo: failed to read host avatar for %s", host_id)
    return None


class PhotoCog(commands.Cog):
    def __init__(self, bot: "Bot"):
        self.bot = bot

    @property
    def db(self):
        return self.bot.games_db

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (driven by the Photo Challenge scheduler). Returns game_id, or None.

        Returns None when the bank is empty (and no custom prompt was given) so the
        scheduler simply skips the run instead of posting an empty card.
        """
        custom = (options.get("prompt") or "").strip()
        tags = list(options.get("tags") or [])

        text = custom or await get_photo_prompt(
            self.db, tags=tags or None, allow_nsfw=channel_allows_nsfw(channel)
        )
        if not text:
            log.warning(
                "photo launch: bank empty for tags=%s in channel %s — scheduled run skipped; "
                "add prompts in the Games Studio", tags, channel.id,
            )
            return None

        guild = getattr(channel, "guild", None)
        image_bytes = await _resolve_card_image(guild, self.bot, host_id)
        if image_bytes is None:
            log.warning("photo launch could not resolve a card image in channel %s", channel.id)
            return None

        try:
            card_bytes = await asyncio.to_thread(
                render_quote_card,
                text,
                author_name=LABEL,
                avatar_bytes=image_bytes,
                theme=THEMES["golden_meadow"],
                pfp_shape="none",
            )
        except Exception:
            log.exception("photo launch failed to render card in channel %s", channel.id)
            return None

        # Post the card (bare image — members reply with their photos). A
        # configured ping role (Photo Challenge dashboard) rides along.
        content = None
        allowed = discord.utils.MISSING
        options = await get_game_options(self.db, "photo", guild_id)
        try:
            ping_role_id = int(str(options.get("ping_role_id", "")).strip() or 0)
        except ValueError:
            ping_role_id = 0
        if ping_role_id > 0:
            content = f"<@&{ping_role_id}>"
            allowed = discord.AllowedMentions(roles=True)
        try:
            msg = await channel.send(
                content=content,
                file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME),
                allowed_mentions=allowed,
            )
        except discord.Forbidden:
            log.warning("photo launch lacked send perms in channel %s", channel.id)
            return None

        # Record the play to history for stats (fire-and-forget: there's no
        # interactive game state to keep alive — people just post in the channel).
        game_id = await create_game(
            self.db,
            channel.id,
            host_id,
            "photo",
            message_id=msg.id,
            state="open",
            payload={
                "prompt": text,
                "tags": tags,
            },
        )
        log.info("Game %s (photo) posted by host %s in #%s", game_id, host_id, getattr(channel, "name", channel.id))

        await update_session(self.db, channel.id, game_id, [host_id])
        await end_game(self.db, game_id)
        return game_id


async def setup(bot: "Bot"):
    # No slash command — Photo Challenge is scheduled-only, driven from its own
    # dashboard feature. The launcher stays registered so the shared scheduler
    # loop can fire the standalone photo schedules.
    cog = PhotoCog(bot)
    await bot.add_cog(cog)
    bot.game_launchers["photo"] = cog.launch
