import asyncio
import io
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot  # noqa: F401

import discord
from discord.ext import commands
from discord import app_commands
from bot_modules.games.utils.game_manager import (
    check_allowed_channel,
    create_game,
    update_session,
    end_game,
    channel_name,
)
from bot_modules.games.command_groups import play
from bot_modules.games.utils.question_source import get_photo_prompt, channel_allows_nsfw
from bot_modules.services.quote_renderer import render_quote_card, THEMES

log = logging.getLogger(__name__)

CARD_FILENAME = "photo.png"

# Header drawn on the card. Plain text — the card header is rendered by PIL
# with Inter, which has no colour-emoji glyphs (the 📸 lives in GAME_ICONS and
# Discord message text only).
LABEL = "PHOTO CHALLENGE"


async def _resolve_card_image(guild: discord.Guild, bot, host_id: int) -> bytes | None:
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

    @app_commands.command(
        name="photo",
        description="Drop a Photo Challenge card in the channel!",
    )
    @app_commands.describe(
        tags="Comma-separated tags to filter the prompt bank",
        prompt="Write your own challenge instead of pulling one from the bank (optional)",
    )
    async def photo(
        self,
        interaction: discord.Interaction,
        tags: str = "",
        prompt: str | None = None,
    ):
        await self.start_photo(interaction, tags, prompt)

    async def start_photo(
        self,
        interaction: discord.Interaction,
        tags: str = "",
        prompt: str | None = None,
    ):
        log.info(
            "%s used /games play photo in #%s",
            interaction.user.display_name,
            channel_name(interaction.channel),
        )
        if not await check_allowed_channel(self.db, interaction.channel_id):
            await interaction.response.send_message(
                "This channel isn't set up for games. An admin can enable it from the web dashboard.",
                ephemeral=True,
            )
            return

        custom = (prompt or "").strip()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        # When no custom prompt is given, pull one from the curated bank now so we
        # can give a friendly notice (instead of a generic error) if it's empty.
        text = custom
        if not text:
            text = await get_photo_prompt(
                self.db, tags=tag_list or None,
                allow_nsfw=channel_allows_nsfw(interaction.channel),
            )
            if not text:
                msg = (
                    f"📸 No photo challenges match tags: {', '.join(tag_list)}."
                    if tag_list
                    else "📸 No photo challenges are in the bank yet — an editor can add some "
                    "from the **Games Studio** in the web dashboard."
                )
                await interaction.response.send_message(msg, ephemeral=True)
                return

        await interaction.response.defer()
        game_id = await self.launch(
            channel=interaction.channel,
            host_id=interaction.user.id,
            host_name=interaction.user.display_name,
            guild_id=interaction.guild_id or 0,
            options={"tags": tag_list, "prompt": text},
        )
        if game_id is None:
            try:
                await interaction.followup.send(
                    "I couldn't start the game here. Please grant me **View Channel**, "
                    "**Send Messages**, and **Attach Files**.",
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass

    async def launch(
        self,
        *,
        channel,
        host_id: int,
        host_name: str,
        guild_id: int,
        options: dict,
    ) -> str | None:
        """Interaction-free launch (slash command + scheduler). Returns game_id, or None.

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

        # Post the card (bare image — members post their photos in the channel).
        try:
            msg = await channel.send(file=discord.File(io.BytesIO(card_bytes), filename=CARD_FILENAME))
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
    cog = PhotoCog(bot)
    await bot.add_cog(cog)
    bot.tree.remove_command("photo")
    play.add_command(cog.photo, override=True)
    bot.game_launchers["photo"] = cog.launch
