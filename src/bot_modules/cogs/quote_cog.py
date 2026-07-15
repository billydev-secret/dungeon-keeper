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

import asyncio
import io
import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.quote_renderer import (
    BORDERS,
    CUSTOM_BORDER_KEY,
    CUSTOM_BORDER_NAME,
    FONT_STYLES,
    QUOTE_MAX_CHARS,
    THEMES,
    BorderStyle,
    QuoteTheme,
    custom_border_style,
    render_quote_card,
)
from bot_modules.services.starboard_service import get_starboard_config

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.quote")

_STYLE_TIMEOUT = 120
_EMOJI_RE = re.compile(r'<(a?):([^:]+):(\d+)>')
_PREVIEW_TIMEOUT = 120

# Reply to a message and ping a role whose name normalizes to this, and the bot
# renders a quote card of the replied-to message (MakeItAQuote-style trigger).
_TRIGGER_ROLE_NAME = "makeitaquote"


def _normalize_role_name(name: str) -> str:
    # Fold to lowercase alphanumerics so "MakeItaQuote", "make_it_a_quote",
    # and "Make It A Quote" all match the trigger.
    return re.sub(r"[^a-z0-9]", "", name.lower())


# ── Shared render path ────────────────────────────────────────────────────────

async def _fetch_custom_emojis(text: str) -> dict[str, bytes]:
    """Download the PNG/GIF bytes for each Discord custom-emoji token in ``text``."""
    custom_emojis: dict[str, bytes] = {}
    emoji_matches = _EMOJI_RE.findall(text)
    if not emoji_matches:
        return custom_emojis
    async with aiohttp.ClientSession() as _session:
        for animated, _name, eid in emoji_matches:
            if eid in custom_emojis:
                continue
            ext = "gif" if animated else "png"
            try:
                async with _session.get(
                    f"https://cdn.discordapp.com/emojis/{eid}.{ext}",
                    headers={"User-Agent": "DungeonKeeper/1.0"},
                ) as resp:
                    if resp.status == 200:
                        custom_emojis[eid] = await resp.read()
            except Exception:
                log.warning("quote: failed to fetch emoji %s", eid)
    return custom_emojis


def _resolve_border(bot: "Bot", guild_id: int | None, key: str) -> BorderStyle:
    """Map a border key to a ``BorderStyle``, resolving a guild's uploaded frame.

    ``CUSTOM_BORDER_KEY`` looks up the per-guild upload; falls back to the default
    bundled border if the key is custom but no upload exists (e.g. it was removed
    between selection and render).
    """
    if key == CUSTOM_BORDER_KEY and guild_id is not None:
        custom = custom_border_style(bot.ctx.db_path, guild_id)
        if custom is not None:
            return custom
    return BORDERS.get(key, BORDERS["golden_poppy"])


async def _build_card_for_message(
    message: discord.Message,
    *,
    theme: QuoteTheme,
    font_style: str,
    border_style: BorderStyle,
) -> bytes:
    """Fetch avatar + custom emojis for ``message`` and render its quote card.

    Raises ``discord.HTTPException`` if the avatar can't be fetched and whatever
    the renderer raises on failure — callers handle both.
    """
    avatar_bytes = await message.author.display_avatar.with_size(512).read()
    custom_emojis = await _fetch_custom_emojis(message.content.strip())
    return await asyncio.to_thread(
        render_quote_card,
        message.content.strip(),
        author_name=message.author.display_name,
        avatar_bytes=avatar_bytes,
        theme=theme,
        font_style=font_style,
        border_style=border_style,
        custom_emojis=custom_emojis or None,
    )


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
        self._font_key = "times"

        # A guild that uploaded its own border gets it as the default choice —
        # "a border for their installation" — while the bundled frames stay
        # selectable. Resolution is a cheap filesystem stat.
        guild_id = message.guild.id if message.guild else None
        has_custom = (
            guild_id is not None
            and custom_border_style(bot.ctx.db_path, guild_id) is not None
        )
        self._border_key = CUSTOM_BORDER_KEY if has_custom else "golden_poppy"
        border_options: list[discord.SelectOption] = []
        if has_custom:
            border_options.append(
                discord.SelectOption(
                    label=CUSTOM_BORDER_NAME, value=CUSTOM_BORDER_KEY, default=True
                )
            )
        border_options += [
            discord.SelectOption(
                label=b.name, value=k, default=(k == self._border_key)
            )
            for k, b in BORDERS.items()
        ]

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

        border_select: discord.ui.Select = discord.ui.Select(  # type: ignore[type-arg]
            placeholder="Border",
            options=border_options,
            min_values=1,
            max_values=1,
            row=2,
        )
        border_select.callback = self._on_border
        self._border_select = border_select
        self.add_item(border_select)

        generate_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Generate",
            style=discord.ButtonStyle.primary,
            row=3,
        )
        generate_btn.callback = self._on_generate
        self.add_item(generate_btn)

        cancel_btn: discord.ui.Button = discord.ui.Button(  # type: ignore[type-arg]
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _on_theme(self, interaction: discord.Interaction) -> None:
        self._theme_key = self._theme_select.values[0]
        await interaction.response.defer()

    async def _on_font(self, interaction: discord.Interaction) -> None:
        self._font_key = self._font_select.values[0]
        await interaction.response.defer()

    async def _on_border(self, interaction: discord.Interaction) -> None:
        self._border_key = self._border_select.values[0]
        await interaction.response.defer()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def _on_generate(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        msg = self.message

        try:
            card_bytes = await _build_card_for_message(
                msg,
                theme=THEMES[self._theme_key],
                font_style=self._font_key,
                border_style=_resolve_border(
                    self.bot, msg.guild.id if msg.guild else None, self._border_key
                ),
            )
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Couldn't fetch the author's avatar.", view=None
            )
            return
        except Exception:
            log.exception("quote: render_quote_card failed")
            await interaction.edit_original_response(
                content="Failed to render the quote card.", view=None
            )
            return

        file = discord.File(io.BytesIO(card_bytes), filename="quote.png")
        preview_view = QuotePreviewView(
            bot=self.bot,
            channel=msg.channel,  # type: ignore[arg-type]
            card_bytes=card_bytes,
            quoter_id=interaction.user.id,
            quoted_user_id=msg.author.id,
            quoted_message_id=msg.id,
            theme_key=self._theme_key,
            font_key=self._font_key,
            border_key=self._border_key,
        )
        self.stop()
        await interaction.edit_original_response(
            content="",
            embed=discord.Embed().set_image(url="attachment://quote.png"),
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
        quoter_id: int = 0,
        quoted_user_id: int = 0,
        quoted_message_id: int = 0,
        theme_key: str = "",
        font_key: str = "",
        border_key: str = "",
    ) -> None:
        super().__init__(timeout=_PREVIEW_TIMEOUT)
        self.bot = bot
        self.channel = channel
        self.card_bytes = card_bytes
        self.quoter_id = quoter_id
        self.quoted_user_id = quoted_user_id
        self.quoted_message_id = quoted_message_id
        self.theme_key = theme_key
        self.font_key = font_key
        self.border_key = border_key

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
            file = discord.File(io.BytesIO(self.card_bytes), filename="quote.png")
            posted_msg = await self.channel.send(file=file)
        except discord.HTTPException:
            log.exception("quote: failed to post card to channel")
            return

        if self.channel.guild:
            guild_id = self.channel.guild.id

            try:
                def _do_get_starboard_cfg():
                    with self.bot.ctx.open_db() as conn:
                        return get_starboard_config(conn, guild_id)

                cfg = await asyncio.to_thread(_do_get_starboard_cfg)
                emoji = cfg["emoji"] if cfg else "⭐"
                await posted_msg.add_reaction(emoji)
            except Exception:
                log.warning("quote: could not add starboard reaction", exc_info=True)

            channel_id = self.channel.id
            quoter_id = self.quoter_id
            quoted_user_id = self.quoted_user_id
            quoted_message_id = self.quoted_message_id
            posted_msg_id = posted_msg.id
            theme_key = self.theme_key
            font_key = self.font_key
            border_key = self.border_key
            try:
                def _do_write_audit_log():
                    with self.bot.ctx.open_db() as conn:
                        conn.execute(
                            """
                            INSERT INTO quote_audit_log
                                (ts, guild_id, channel_id, quoter_id, quoted_user_id,
                                 quoted_message_id, posted_message_id, theme, font, border)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                time.time(),
                                guild_id,
                                channel_id,
                                quoter_id,
                                quoted_user_id,
                                quoted_message_id,
                                posted_msg_id,
                                theme_key,
                                font_key,
                                border_key,
                            ),
                        )

                await asyncio.to_thread(_do_write_audit_log)
            except Exception:
                log.warning("quote: could not write audit log", exc_info=True)


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

    @app_commands.command(
        name="banner",
        description="Render a string as a banner-style quote card and post it here.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.describe(
        text="The quote to put on the banner.",
        theme="Colour grading for the card (default: Golden Meadow).",
        font="Typeface for the quote (default: Times).",
        title="Optional heading shown above the quote.",
    )
    @app_commands.choices(
        theme=[app_commands.Choice(name=t.name, value=k) for k, t in THEMES.items()],
        font=[
            app_commands.Choice(name=k.title(), value=k) for k in FONT_STYLES
        ],
    )
    async def banner(
        self,
        interaction: discord.Interaction,
        text: str,
        theme: str | None = None,
        font: str | None = None,
        title: str | None = None,
    ) -> None:
        ctx = self.bot.ctx
        if not ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(
            channel, discord.TextChannel | discord.Thread | discord.VoiceChannel
        ):
            await interaction.response.send_message(
                "This command can only be used in a server text channel.", ephemeral=True
            )
            return

        text = text.strip()
        if not text:
            await interaction.response.send_message(
                "Give me some text to put on the banner.", ephemeral=True
            )
            return

        theme_obj = THEMES.get(theme or "", THEMES["golden_meadow"])
        font_style = font if font in FONT_STYLES else "times"

        await interaction.response.defer(ephemeral=True)

        # Background image: guild icon first, then the invoker's avatar. The card
        # colour-grades whatever it gets, so both look fine — the icon just keeps
        # the banner on-brand.
        avatar_bytes: bytes | None = None
        if guild.icon is not None:
            try:
                avatar_bytes = await guild.icon.replace(size=512).read()
            except discord.HTTPException:
                log.warning("banner: failed to read guild icon for %s", guild.id)
        if avatar_bytes is None:
            try:
                avatar_bytes = await interaction.user.display_avatar.with_size(512).read()
            except discord.HTTPException:
                avatar_bytes = None
        if avatar_bytes is None:
            await interaction.followup.send(
                content="Couldn't fetch an image to use as the banner background.",
                ephemeral=True,
            )
            return

        try:
            card_bytes = await asyncio.to_thread(
                render_quote_card,
                text,
                author_name=(title or "").strip(),
                avatar_bytes=avatar_bytes,
                theme=theme_obj,
                font_style=font_style,
                pfp_shape="none",
                # Use the server's uploaded border by default (falls back to the
                # bundled Golden Poppy frame when none is set).
                border_style=_resolve_border(self.bot, guild.id, CUSTOM_BORDER_KEY),
            )
        except Exception:
            log.exception("banner: render_quote_card failed")
            await interaction.followup.send(
                content="Failed to render the banner.", ephemeral=True
            )
            return

        file = discord.File(io.BytesIO(card_bytes), filename="banner.png")
        try:
            await channel.send(file=file)
        except discord.HTTPException:
            log.exception("banner: failed to post card to channel")
            await interaction.followup.send(
                content="Couldn't post the banner in this channel.", ephemeral=True
            )
            return

        note = "Posted."
        if len(text) > QUOTE_MAX_CHARS:
            note = f"Posted (trimmed to {QUOTE_MAX_CHARS} characters)."
        await interaction.followup.send(content=note, ephemeral=True)

    @app_commands.command(
        name="quote-role",
        description='Create the mentionable "MakeItAQuote" reply-to-quote role.',
    )
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def quote_role(self, interaction: discord.Interaction) -> None:
        if not self.bot.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        existing = discord.utils.find(
            lambda r: _normalize_role_name(r.name) == _TRIGGER_ROLE_NAME, guild.roles
        )
        if existing is not None:
            if not existing.mentionable:
                try:
                    await existing.edit(
                        mentionable=True, reason="Enable reply-to-quote trigger"
                    )
                except discord.HTTPException:
                    await interaction.response.send_message(
                        f"The {existing.mention} role exists but I couldn't make it "
                        "mentionable — check that my role sits above it and I have "
                        "**Manage Roles**.",
                        ephemeral=True,
                    )
                    return
            await interaction.response.send_message(
                f"Ready — reply to any message and ping {existing.mention} to "
                "turn it into a quote card.",
                ephemeral=True,
            )
            return

        try:
            role = await guild.create_role(
                name="MakeItAQuote",
                mentionable=True,
                reason=f"Reply-to-quote trigger (by {interaction.user})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I need the **Manage Roles** permission to create the role.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Couldn't create the role — please try again.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Created {role.mention}. Reply to any message and ping it to turn "
            "that message into a quote card.",
            ephemeral=True,
        )

    @commands.Cog.listener("on_message")
    async def _on_quote_trigger(self, message: discord.Message) -> None:
        # Reply + ping the make_it_a_quote role → quote the replied-to message.
        if message.author.bot or message.guild is None:
            return
        if message.reference is None or message.reference.message_id is None:
            return
        if not any(
            _normalize_role_name(r.name) == _TRIGGER_ROLE_NAME
            for r in message.role_mentions
        ):
            return

        channel = message.channel
        if not isinstance(
            channel, discord.TextChannel | discord.Thread | discord.VoiceChannel
        ):
            return

        # Resolve the replied-to message: use the cached one if it's a full
        # Message, otherwise fetch it; a deleted/unfetchable target → bail.
        ref = message.reference.resolved
        target: discord.Message | None = None
        if isinstance(ref, discord.Message):
            target = ref
        else:
            try:
                target = await channel.fetch_message(message.reference.message_id)
            except discord.HTTPException:
                target = None
        if target is None:
            return

        if not target.content or not target.content.strip():
            return
        if target.type not in (
            discord.MessageType.default,
            discord.MessageType.reply,
        ):
            return

        try:
            card_bytes = await _build_card_for_message(
                target,
                theme=THEMES["golden_meadow"],
                font_style="inter",
                border_style=_resolve_border(
                    self.bot, message.guild.id, CUSTOM_BORDER_KEY
                ),
            )
        except Exception:
            log.exception("quote: reply-trigger render failed")
            return

        try:
            posted = await channel.send(
                file=discord.File(io.BytesIO(card_bytes), filename="quote.png"),
                reference=target,
                mention_author=False,
            )
        except discord.HTTPException:
            log.warning("quote: reply-trigger failed to post card in %s", channel.id)
            return

        # Credit the quote creator (the member who invoked the role) — one
        # payout per quoted message via the occurrence key. Skip self-quotes:
        # the invoker has no rate limit here, so crediting them for quoting
        # their own message would be a trivial farm. Guarded/non-raising.
        if target.author.id != message.author.id:
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                self.bot, message.guild.id, message.author.id, "quote",
                occurrence=str(target.id),
            )

        # Seed the starboard reaction, matching the context-menu Post button.
        guild = message.guild
        if guild is not None:
            gid = guild.id
            try:
                def _get_cfg():
                    with self.bot.ctx.open_db() as conn:
                        return get_starboard_config(conn, gid)

                cfg = await asyncio.to_thread(_get_cfg)
                emoji = cfg["emoji"] if cfg else "⭐"
                await posted.add_reaction(emoji)
            except Exception:
                log.warning(
                    "quote: reply-trigger could not add starboard reaction",
                    exc_info=True,
                )


async def setup(bot: "Bot") -> None:
    await bot.add_cog(QuoteCog(bot))
