"""Emoji stealer cog — right-click any message (user-installed app) to copy custom
emojis into any server DungeonKeeper is in.

Flow:
  1. Right-click a message → Apps → "Steal Emoji"
  2. If multiple emojis in message: emoji picker select
  3. If DungeonKeeper is in multiple guilds with emoji perms: guild picker select
  4. Click Steal → emoji downloaded and added to the chosen guild
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import discord
import httpx
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.emoji_stealer")

_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")


def _emoji_url(emoji_id: int, animated: bool) -> str:
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}"


def _sanitize_name(name: str) -> str:
    name = re.sub(r"[^\w]", "_", name)[:32]
    return name if len(name) >= 2 else name + "_e"


async def _fetch_bytes(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.content


def _eligible_guilds(bot: Bot) -> list[discord.Guild]:
    return [g for g in bot.guilds if g.me and g.me.guild_permissions.manage_expressions]


# ---------------------------------------------------------------------------
# Combined picker + steal view
# ---------------------------------------------------------------------------

class _StealView(discord.ui.View):
    """Emoji picker + guild picker + Steal button."""

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
        await interaction.response.edit_message(content="Downloading emoji...", view=None)
        animated, name, emoji_id = self._sel_emoji
        guild = self._sel_guild
        try:
            data = await _fetch_bytes(_emoji_url(emoji_id, animated))
            new_emoji = await guild.create_custom_emoji(name=_sanitize_name(name), image=data)
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


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EmojiStealerCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self.ctx_menu = app_commands.ContextMenu(
            name="Steal Emoji",
            callback=self.steal_emoji,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    async def steal_emoji(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        guilds = _eligible_guilds(self.bot)
        if not guilds:
            await interaction.response.send_message(
                "I don't have **Manage Expressions** permission in any server.", ephemeral=True
            )
            return

        seen: set[int] = set()
        emojis: list[tuple[bool, str, int]] = []
        for animated_str, name, id_str in _EMOJI_RE.findall(message.content or ""):
            emoji_id = int(id_str)
            if emoji_id not in seen:
                seen.add(emoji_id)
                emojis.append((bool(animated_str), name, emoji_id))

        if not emojis:
            await interaction.response.send_message(
                "No custom emojis found in that message.", ephemeral=True
            )
            return

        # Fast path: single emoji, single guild — no UI needed
        if len(emojis) == 1 and len(guilds) == 1:
            animated, name, emoji_id = emojis[0]
            guild = guilds[0]
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                data = await _fetch_bytes(_emoji_url(emoji_id, animated))
                new_emoji = await guild.create_custom_emoji(name=_sanitize_name(name), image=data)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"I don't have **Manage Expressions** in **{guild.name}**.", ephemeral=True
                )
                return
            except discord.HTTPException as exc:
                await interaction.followup.send(f"Discord rejected it: {exc.text}", ephemeral=True)
                return
            except httpx.HTTPError as exc:
                await interaction.followup.send(
                    f"Couldn't download the emoji: {exc}", ephemeral=True
                )
                return
            await interaction.followup.send(
                f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!", ephemeral=True
            )
            return

        # Multi-emoji or multi-guild: show picker
        n_emoji = len(emojis)
        n_guild = len(guilds)
        if n_emoji > 1 and n_guild > 1:
            prompt = f"Found **{n_emoji}** emojis — pick one and a server:"
        elif n_emoji > 1:
            prompt = f"Found **{n_emoji}** emojis — pick one to add to **{guilds[0].name}**:"
        else:
            prompt = f"Add `:{emojis[0][1]}:` — which server?"

        view = _StealView(emojis, guilds, interaction.user.id)
        await interaction.response.send_message(prompt, view=view, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(EmojiStealerCog(bot, bot.ctx))
