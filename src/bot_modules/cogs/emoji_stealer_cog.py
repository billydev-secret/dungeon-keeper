"""Emoji stealer cog.

Two entry points:
  - Right-click message → Apps → "Steal Emoji"  (picks from custom emojis in the message)
  - /steal_emoji url:<url> name:<name>           (any direct image URL)

Both show a guild picker when DungeonKeeper is in multiple emoji-capable servers.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from bot_modules.emoji_stealer.compress import compress_gif_for_emoji
from bot_modules.emoji_stealer.logic import (
    build_steal_prompt,
    emoji_cdn_url,
    extract_emojis_from_text,
    format_steal_all_summary,
    is_https_url,
    sanitize_emoji_name,
    validate_emoji_name,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.emoji_stealer")


async def _fetch_bytes(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.content


def _eligible_guilds(bot: Bot) -> list[discord.Guild]:
    return [g for g in bot.guilds if g.me and g.me.guild_permissions.manage_expressions]


async def _upload(
    guild: discord.Guild, name: str, data: bytes
) -> discord.Emoji:
    return await guild.create_custom_emoji(
        name=sanitize_emoji_name(name), image=compress_gif_for_emoji(data)
    )


# ---------------------------------------------------------------------------
# Guild picker view (used by both entry points after emoji/URL is resolved)
# ---------------------------------------------------------------------------

class _GuildPickView(discord.ui.View):
    """Pick which DungeonKeeper server to add a pre-fetched emoji to."""

    def __init__(
        self,
        url: str,
        name: str,
        guilds: list[discord.Guild],
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._url = url
        self._name = name
        self._invoker_id = invoker_id
        self._guild_map: dict[str, discord.Guild] = {str(g.id): g for g in guilds}
        self._sel_guild = guilds[0]

        opts = [discord.SelectOption(label=g.name[:100], value=str(g.id)) for g in guilds[:25]]
        sel: discord.ui.Select = discord.ui.Select(placeholder="Which server?", options=opts)  # type: ignore[type-arg]
        sel.callback = self._on_guild
        self.add_item(sel)

        steal: discord.ui.Button = discord.ui.Button(label="Steal!", style=discord.ButtonStyle.primary)  # type: ignore[type-arg]
        steal.callback = self._on_steal
        self.add_item(steal)

        cancel: discord.ui.Button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)  # type: ignore[type-arg]
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_guild(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        self._sel_guild = self._guild_map[key]
        await interaction.response.defer()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def _on_steal(self, interaction: discord.Interaction) -> None:
        self.stop()
        guild = self._sel_guild
        await interaction.response.edit_message(content="Downloading emoji...", view=None)
        try:
            data = await _fetch_bytes(self._url)
            new_emoji = await _upload(guild, self._name, data)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=f"I don't have **Manage Expressions** in **{guild.name}**."
            )
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"Discord rejected it: {exc.text}")
            return
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(content=f"Couldn't download the image: {exc}")
            return
        await interaction.edit_original_response(
            content=f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!"
        )


# ---------------------------------------------------------------------------
# Context-menu picker view (emoji from message → guild)
# ---------------------------------------------------------------------------

class _StealView(discord.ui.View):
    """Emoji picker + guild picker for the message context menu."""

    def __init__(
        self,
        emojis: list[tuple[bool, str, int]],
        guilds: list[discord.Guild],
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._invoker_id = invoker_id
        self._emoji_map: dict[str, tuple[bool, str, int]] = {str(e[2]): e for e in emojis}
        self._guild_map: dict[str, discord.Guild] = {str(g.id): g for g in guilds}
        self._sel_emoji = emojis[0]
        self._sel_guild = guilds[0]

        row = 0
        if len(emojis) > 1:
            opts = [
                discord.SelectOption(
                    label=f":{name}:"[:100],
                    value=str(emoji_id),
                    description="Animated" if animated else "Static",
                    emoji=discord.PartialEmoji(name=name, id=emoji_id, animated=animated),
                )
                for animated, name, emoji_id in emojis[:25]
            ]
            sel: discord.ui.Select = discord.ui.Select(placeholder="Which emoji?", options=opts, row=row)  # type: ignore[type-arg]
            sel.callback = self._on_emoji
            self.add_item(sel)
            row += 1

        if len(guilds) > 1:
            opts2 = [
                discord.SelectOption(label=g.name[:100], value=str(g.id))
                for g in guilds[:25]
            ]
            sel2: discord.ui.Select = discord.ui.Select(placeholder="Add to which server?", options=opts2, row=row)  # type: ignore[type-arg]
            sel2.callback = self._on_guild
            self.add_item(sel2)
            row += 1

        steal: discord.ui.Button = discord.ui.Button(label="Steal!", style=discord.ButtonStyle.primary, row=row)  # type: ignore[type-arg]
        steal.callback = self._on_steal
        self.add_item(steal)

        # Only show "Steal All" when there are multiple emojis to grab
        if len(emojis) > 1:
            steal_all: discord.ui.Button = discord.ui.Button(label="Steal All", style=discord.ButtonStyle.success, row=row)  # type: ignore[type-arg]
            steal_all.callback = self._on_steal_all
            self.add_item(steal_all)

        cancel: discord.ui.Button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=row)  # type: ignore[type-arg]
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_emoji(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        self._sel_emoji = self._emoji_map[key]
        await interaction.response.defer()

    async def _on_guild(self, interaction: discord.Interaction) -> None:
        key = interaction.data["values"][0]  # type: ignore[index]
        self._sel_guild = self._guild_map[key]
        await interaction.response.defer()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)

    async def _on_steal(self, interaction: discord.Interaction) -> None:
        self.stop()
        animated, name, emoji_id = self._sel_emoji
        guild = self._sel_guild
        await interaction.response.edit_message(content="Downloading emoji...", view=None)
        try:
            data = await _fetch_bytes(emoji_cdn_url(emoji_id, animated))
            new_emoji = await _upload(guild, name, data)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=f"I don't have **Manage Expressions** in **{guild.name}**."
            )
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"Discord rejected it: {exc.text}")
            return
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(content=f"Couldn't download the emoji: {exc}")
            return
        await interaction.edit_original_response(
            content=f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!"
        )

    async def _on_steal_all(self, interaction: discord.Interaction) -> None:
        self.stop()
        guild = self._sel_guild
        all_emojis = list(self._emoji_map.values())
        await interaction.response.edit_message(
            content=f"Stealing {len(all_emojis)} emojis...", view=None
        )
        added: list[discord.Emoji] = []
        failed: list[tuple[str, str]] = []
        for animated, name, emoji_id in all_emojis:
            try:
                data = await _fetch_bytes(emoji_cdn_url(emoji_id, animated))
                new_emoji = await _upload(guild, name, data)
                added.append(new_emoji)
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content=f"I don't have **Manage Expressions** in **{guild.name}**."
                )
                return
            except discord.HTTPException as exc:
                failed.append((name, exc.text or str(exc)))
            except httpx.HTTPError as exc:
                failed.append((name, str(exc)))

        await interaction.edit_original_response(
            content=format_steal_all_summary(
                added_mentions=[str(e) for e in added],
                guild_name=guild.name,
                failed=failed,
            ),
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EmojiStealerCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self.ctx_menu = app_commands.ContextMenu(
            name="Steal Emoji",
            callback=self._steal_from_message,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # ------------------------------------------------------------------
    # Context menu: right-click a message
    # ------------------------------------------------------------------

    async def _steal_from_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        guilds = _eligible_guilds(self.bot)
        if not guilds:
            await interaction.response.send_message(
                "I don't have **Manage Expressions** in any server.", ephemeral=True
            )
            return

        emojis = extract_emojis_from_text(message.content or "")
        if not emojis:
            await interaction.response.send_message(
                "No custom emojis found in that message.", ephemeral=True
            )
            return

        # Fast path: one emoji, one guild
        if len(emojis) == 1 and len(guilds) == 1:
            animated, name, emoji_id = emojis[0]
            guild = guilds[0]
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                data = await _fetch_bytes(emoji_cdn_url(emoji_id, animated))
                new_emoji = await _upload(guild, name, data)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"I don't have **Manage Expressions** in **{guild.name}**.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f"Discord rejected it: {exc.text}", ephemeral=True)
                return
            except httpx.HTTPError as exc:
                await interaction.followup.send(f"Couldn't download the emoji: {exc}", ephemeral=True)
                return
            await interaction.followup.send(
                f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!", ephemeral=True
            )
            return

        prompt = build_steal_prompt(
            n_emoji=len(emojis),
            guild_count=len(guilds),
            first_emoji_name=emojis[0][1],
            first_guild_name=guilds[0].name,
        )

        await interaction.response.send_message(
            prompt, view=_StealView(emojis, guilds, interaction.user.id), ephemeral=True
        )

    # ------------------------------------------------------------------
    # Slash command: /steal_emoji url:<url> name:<name>
    # ------------------------------------------------------------------

    @app_commands.command(name="steal_emoji", description="Add an emoji from any image URL.")
    @app_commands.describe(
        url="Direct image URL (PNG, GIF, WEBP, JPG)",
        name="Name for the new emoji (letters, numbers, underscores)",
    )
    async def steal_emoji_url(
        self,
        interaction: discord.Interaction,
        url: str,
        name: str,
    ) -> None:
        if not is_https_url(url):
            await interaction.response.send_message(
                "URL must start with `https://`.", ephemeral=True
            )
            return

        ok, clean_name, error_msg = validate_emoji_name(name)
        if not ok:
            await interaction.response.send_message(error_msg, ephemeral=True)
            return

        guilds = _eligible_guilds(self.bot)
        if not guilds:
            await interaction.response.send_message(
                "I don't have **Manage Expressions** in any server.", ephemeral=True
            )
            return

        # Fast path: one guild
        if len(guilds) == 1:
            guild = guilds[0]
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                data = await _fetch_bytes(url)
                new_emoji = await _upload(guild, clean_name, data)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"I don't have **Manage Expressions** in **{guild.name}**.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f"Discord rejected it: {exc.text}", ephemeral=True)
                return
            except httpx.HTTPError as exc:
                await interaction.followup.send(f"Couldn't download the image: {exc}", ephemeral=True)
                return
            await interaction.followup.send(
                f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Add `:{clean_name}:` — which server?",
            view=_GuildPickView(url, clean_name, guilds, interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(EmojiStealerCog(bot, bot.ctx))
