"""Quote cog — right-click a message to generate a quote card with the
author's pfp as a color-graded background.

Context menu: Apps > Quote (on any non-system message with text content)

Flow:
  1. User right-clicks message → "Quote"
  2. Bot sends ephemeral QuoteStyleView (theme + font selects + Generate button)
  3. User picks options, clicks Generate
  4. Bot fetches avatar, renders card, shows QuotePreviewView (Post / Cancel)
  5. User clicks Post → card sent publicly in the same channel
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.quote_renderer import FONT_STYLES, THEMES, render_quote_card
from services.starboard_service import get_starboard_config

if TYPE_CHECKING:
    from core.app_context import Bot

log = logging.getLogger("dungeonkeeper.quote")

_STYLE_TIMEOUT = 120
_PREVIEW_TIMEOUT = 120


# ── Style selector view ───────────────────────────────────────────────────────

class QuoteStyleView(discord.ui.View):
    """Ephemeral view: pick theme + font, then generate."""

    def __init__(
        self,
        bot: "Bot",
        message: discord.Message,
    ) -> None:
        super().__init__(timeout=_STYLE_TIMEOUT)
        self.bot = bot
        self.message = message
        self._theme_key = "golden_meadow"
        self._font_key = "inter"

        theme_select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder="Theme",
            options=[
                discord.SelectOption(
                    label=t.name,
                    value=k,
                    default=(k == self._theme_key),
                )
                for k, t in THEMES.items()
            ],
            min_values=1,
            max_values=1,
            row=0,
        )
        theme_select.callback = self._on_theme
        self._theme_select = theme_select
        self.add_item(theme_select)

        font_select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder="Font",
            options=[
                discord.SelectOption(label=k.title(), value=k, default=(k == self._font_key))
                for k in FONT_STYLES
            ],
            min_values=1,
            max_values=1,
            row=1,
        )
        font_select.callback = self._on_font
        self._font_select = font_select
        self.add_item(font_select)

        generate_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Generate",
            style=discord.ButtonStyle.primary,
            row=2,
        )
        generate_btn.callback = self._on_generate
        self.add_item(generate_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_theme(self, interaction: discord.Interaction) -> None:
        self._theme_key = self._theme_select.values[0]
        await interaction.response.defer()

    async def _on_font(self, interaction: discord.Interaction) -> None:
        self._font_key = self._font_select.values[0]
        await interaction.response.defer()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def _on_generate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        theme = THEMES[self._theme_key]
        font_style = self._font_key
        msg = self.message

        try:
            avatar_bytes = await msg.author.display_avatar.with_size(512).read()
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Couldn't fetch the author's avatar.", view=None
            )
            return

        text = msg.content.strip()
        author_name = msg.author.display_name

        try:
            import asyncio  # noqa: PLC0415
            card_bytes = await asyncio.to_thread(
                render_quote_card,
                text,
                author_name=author_name,
                avatar_bytes=avatar_bytes,
                theme=theme,
                font_style=font_style,
            )
        except Exception:
            log.exception("quote: render_quote_card failed")
            await interaction.edit_original_response(
                content="Failed to render the quote card.", view=None
            )
            return

        file = discord.File(io.BytesIO(card_bytes), filename="quote.jpg")
        preview_view = QuotePreviewView(
            bot=self.bot,
            channel=msg.channel,  # type: ignore[arg-type]
            card_bytes=card_bytes,
        )
        self.stop()
        await interaction.edit_original_response(
            content="",
            embed=discord.Embed().set_image(url="attachment://quote.jpg"),
            attachments=[file],
            view=preview_view,
        )


# ── Preview/post view ─────────────────────────────────────────────────────────

class QuotePreviewView(discord.ui.View):
    """Post the rendered card publicly or discard."""

    def __init__(
        self,
        bot: "Bot",
        channel: discord.TextChannel | discord.Thread | discord.VoiceChannel,
        card_bytes: bytes,
    ) -> None:
        super().__init__(timeout=_PREVIEW_TIMEOUT)
        self.bot = bot
        self.channel = channel
        self.card_bytes = card_bytes

        post_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Post",
            style=discord.ButtonStyle.success,
        )
        post_btn.callback = self._on_post
        self.add_item(post_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Cancel",
            style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, attachments=[], view=None)

    async def _on_post(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Posted!", embed=None, attachments=[], view=None)
        try:
            file = discord.File(io.BytesIO(self.card_bytes), filename="quote.jpg")
            posted_msg = await self.channel.send(file=file)
        except discord.HTTPException:
            log.exception("quote: failed to post card to channel")
            return

        if self.channel.guild:
            try:
                with self.bot.ctx.open_db() as conn:
                    cfg = get_starboard_config(conn, self.channel.guild.id)
                emoji = cfg["emoji"] if cfg else "⭐"
                await posted_msg.add_reaction(emoji)
            except Exception:
                log.warning("quote: could not add starboard reaction", exc_info=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class QuoteCog(commands.Cog):
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot
        self._quote_ctx_menu = app_commands.ContextMenu(
            name="Quote",
            callback=self._quote_context_menu,
        )
        bot.tree.add_command(self._quote_ctx_menu)
        super().__init__()

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._quote_ctx_menu.name, type=self._quote_ctx_menu.type)

    async def _quote_context_menu(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> None:
        if not message.content or not message.content.strip():
            await interaction.response.send_message(
                "That message has no text to quote.", ephemeral=True
            )
            return

        if message.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            await interaction.response.send_message(
                "Can't quote system messages.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Pick a style for your quote card:",
            view=QuoteStyleView(self.bot, message),
            ephemeral=True,
        )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(QuoteCog(bot))
