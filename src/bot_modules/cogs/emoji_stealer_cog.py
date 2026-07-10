"""Emoji stealer cog.

Two entry points:
  - Right-click message → Apps → "Steal Emoji"  (picks from custom emojis in the message)
  - /steal_emoji url:<url> name:<name>           (any direct image URL)

Both show a guild picker when DungeonKeeper is in multiple emoji-capable servers.

Before adding, every single-emoji path runs a duplicate check against the target
guild's existing emojis (exact byte match + perceptual "looks the same" match,
plus a name collision). A hit warns the user and offers "Add anyway"; the
Steal-All button silently skips duplicates and reports the count instead.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
import httpx
from discord import app_commands
from discord.ext import commands

from bot_modules.emoji_stealer.compress import compress_gif_for_emoji
from bot_modules.emoji_stealer.dedupe import (
    hamming,
    perceptual_hash,
    sha256_hex,
    DUPE_THRESHOLD,
)
from bot_modules.emoji_stealer.logic import (
    build_steal_prompt,
    emoji_cdn_url,
    extract_emojis_from_text,
    format_steal_all_summary,
    is_https_url,
    looks_like_image,
    sanitize_emoji_name,
    validate_emoji_name,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.emoji_stealer")

# How many guild-emoji images to download at once when warming the hash cache.
_HASH_FETCH_CONCURRENCY = 8


async def _fetch_bytes(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        r = await client.get(url, timeout=10)
        r.raise_for_status()
        return r.content


def _eligible_guilds(bot: Bot, user_id: int) -> list[discord.Guild]:
    result = []
    for g in bot.guilds:
        if not (g.me and g.me.guild_permissions.manage_expressions):
            continue
        member = g.get_member(user_id)
        if member and (
            member.guild_permissions.administrator
            or member.guild_permissions.manage_expressions
            or member.guild_permissions.manage_guild
        ):
            result.append(g)
    return result


_NOT_AN_IMAGE_MSG = (
    "That doesn't look like an image — give me a direct image URL "
    "(ending in .png, .gif, or .webp), not a webpage or message link."
)


async def _upload(
    guild: discord.Guild, name: str, data: bytes
) -> discord.Emoji:
    if not looks_like_image(data):
        # Raise before discord.py does, which would throw a bare ValueError
        # the callers' except blocks don't catch.
        raise ValueError(_NOT_AN_IMAGE_MSG)
    return await guild.create_custom_emoji(
        name=sanitize_emoji_name(name), image=compress_gif_for_emoji(data)
    )


def _dupe_notice(kind: str, existing: discord.Emoji, guild: discord.Guild) -> str:
    """User-facing warning for a detected duplicate, keyed by match kind."""
    if kind == "exact":
        detail = f"already has this exact emoji as {existing} `:{existing.name}:`"
    elif kind == "similar":
        detail = f"already has a very similar emoji {existing} `:{existing.name}:`"
    else:  # name
        detail = (
            f"already has an emoji named `:{existing.name}:` "
            f"(a different image)"
        )
    return f"⚠️ **{guild.name}** {detail}. Add it anyway?"


# ---------------------------------------------------------------------------
# Confirm view (shown when a single steal hits a duplicate)
# ---------------------------------------------------------------------------

class _ConfirmDupeView(discord.ui.View):
    """"Add anyway / Cancel" after a duplicate warning. Holds the already-
    fetched bytes so confirming doesn't re-download."""

    def __init__(
        self,
        cog: EmojiStealerCog,
        guild: discord.Guild,
        name: str,
        data: bytes,
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._cog = cog
        self._guild = guild
        self._name = name
        self._data = data
        self._invoker_id = invoker_id

        add: discord.ui.Button = discord.ui.Button(label="Add anyway", style=discord.ButtonStyle.primary)  # type: ignore[type-arg]
        add.callback = self._on_add
        self.add_item(add)

        cancel: discord.ui.Button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)  # type: ignore[type-arg]
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._invoker_id:
            await interaction.response.send_message("This menu isn't for you.", ephemeral=True)
            return False
        return True

    async def _on_add(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Adding emoji...", view=None)
        await self._cog._upload_and_report(interaction, self._guild, self._name, self._data)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", view=None)


# ---------------------------------------------------------------------------
# Guild picker view (used by both entry points after emoji/URL is resolved)
# ---------------------------------------------------------------------------

class _GuildPickView(discord.ui.View):
    """Pick which DungeonKeeper server to add a pre-fetched emoji to."""

    def __init__(
        self,
        cog: EmojiStealerCog,
        url: str,
        name: str,
        guilds: list[discord.Guild],
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._cog = cog
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
        await interaction.response.edit_message(content="Downloading emoji...", view=None)
        await self._cog._do_steal(interaction, self._sel_guild, self._name, self._url)


# ---------------------------------------------------------------------------
# Context-menu picker view (emoji from message → guild)
# ---------------------------------------------------------------------------

class _StealView(discord.ui.View):
    """Emoji picker + guild picker for the message context menu."""

    def __init__(
        self,
        cog: EmojiStealerCog,
        emojis: list[tuple[bool, str, int]],
        guilds: list[discord.Guild],
        invoker_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._cog = cog
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
        await interaction.response.edit_message(content="Downloading emoji...", view=None)
        await self._cog._do_steal(
            interaction, self._sel_guild, name, emoji_cdn_url(emoji_id, animated)
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
        skipped: list[str] = []
        for animated, name, emoji_id in all_emojis:
            try:
                data = await _fetch_bytes(emoji_cdn_url(emoji_id, animated))
            except httpx.HTTPError as exc:
                failed.append((name, str(exc)))
                continue
            # Batch UX: silently skip duplicates rather than 25 confirm prompts.
            if await self._cog._find_duplicate(guild, name, data) is not None:
                skipped.append(name)
                continue
            try:
                added.append(await self._cog._upload_and_add(guild, name, data))
            except discord.Forbidden:
                await interaction.edit_original_response(
                    content=f"I don't have **Manage Expressions** in **{guild.name}**."
                )
                return
            except discord.HTTPException as exc:
                failed.append((name, exc.text or str(exc)))
            except ValueError as exc:
                failed.append((name, str(exc)))

        await interaction.edit_original_response(
            content=format_steal_all_summary(
                added_mentions=[str(e) for e in added],
                guild_name=guild.name,
                failed=failed,
                skipped=skipped,
            ),
        )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class EmojiStealerCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # guild id -> {emoji id -> (sha256_hex, perceptual_hash | None)}.
        # Lazily warmed per guild and self-heals by id-set diff on each check.
        self._hash_cache: dict[int, dict[int, tuple[str, int | None]]] = {}
        self.ctx_menu = app_commands.ContextMenu(
            name="Steal Emoji",
            callback=self._steal_from_message,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------

    async def _guild_hashes(
        self, guild: discord.Guild
    ) -> dict[int, tuple[str, int | None]]:
        """Return {emoji id -> (sha, phash)} for the guild, downloading only
        emoji not already cached and dropping any that have since vanished."""
        cache = self._hash_cache.setdefault(guild.id, {})
        current = {e.id: e for e in guild.emojis}
        for stale in [eid for eid in cache if eid not in current]:
            del cache[stale]
        missing = [e for eid, e in current.items() if eid not in cache]
        if missing:
            sem = asyncio.Semaphore(_HASH_FETCH_CONCURRENCY)

            async def load(e: discord.Emoji) -> tuple[int, tuple[str, int | None]] | None:
                async with sem:
                    try:
                        blob = await e.read()
                    except (discord.HTTPException, discord.NotFound):
                        return None
                return e.id, (sha256_hex(blob), perceptual_hash(blob))

            for res in await asyncio.gather(*(load(e) for e in missing)):
                if res is not None:
                    cache[res[0]] = res[1]
        return cache

    async def _find_duplicate(
        self, guild: discord.Guild, name: str, data: bytes
    ) -> tuple[str, discord.Emoji] | None:
        """Detect whether ``data``/``name`` is already in ``guild``.

        Returns ``(kind, existing_emoji)`` where kind is ``"exact"`` (identical
        bytes), ``"similar"`` (perceptual match), or ``"name"`` (name collision,
        different image), in that priority order. None if nothing matches.
        """
        by_id = {e.id: e for e in guild.emojis}
        src_sha = sha256_hex(data)
        src_ph = perceptual_hash(data)
        hashes = await self._guild_hashes(guild)
        similar: discord.Emoji | None = None
        for eid, (sha, ph) in hashes.items():
            existing = by_id.get(eid)
            if existing is None:
                continue
            if sha == src_sha:
                return "exact", existing
            if src_ph is not None and ph is not None and hamming(src_ph, ph) <= DUPE_THRESHOLD:
                similar = similar or existing
        if similar is not None:
            return "similar", similar
        name_match = discord.utils.get(guild.emojis, name=sanitize_emoji_name(name))
        if name_match is not None:
            return "name", name_match
        return None

    # ------------------------------------------------------------------
    # Upload + report (shared by every single-emoji path)
    # ------------------------------------------------------------------

    async def _upload_and_add(
        self, guild: discord.Guild, name: str, data: bytes
    ) -> discord.Emoji:
        """Upload and record the new emoji's hashes so an immediate re-steal is
        caught. Propagates discord/ValueError errors to the caller."""
        new_emoji = await _upload(guild, name, data)
        # We hash the pre-compression source bytes, whereas _upload may store a
        # recompressed GIF — so a cold re-warm (e.read()) can yield a different
        # exact sha. Harmless: the perceptual tier is built to survive exactly
        # that recompression, so an immediate re-steal is still caught.
        self._hash_cache.setdefault(guild.id, {})[new_emoji.id] = (
            sha256_hex(data),
            perceptual_hash(data),
        )
        return new_emoji

    async def _upload_and_report(
        self, interaction: discord.Interaction, guild: discord.Guild, name: str, data: bytes
    ) -> None:
        """Upload the (already-fetched, already-dedup-checked) bytes and report
        the outcome via ``edit_original_response``. The caller must have already
        sent an initial response (defer/thinking or edit_message)."""
        try:
            new_emoji = await self._upload_and_add(guild, name, data)
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=f"I don't have **Manage Expressions** in **{guild.name}**."
            )
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(content=f"Discord rejected it: {exc.text}")
            return
        except ValueError as exc:
            await interaction.edit_original_response(content=str(exc))
            return
        await interaction.edit_original_response(
            content=f"Added {new_emoji} `:{new_emoji.name}:` to **{guild.name}**!"
        )

    async def _do_steal(
        self, interaction: discord.Interaction, guild: discord.Guild, name: str, url: str
    ) -> None:
        """Fetch → dedup-check → confirm-or-upload for a single emoji. The
        caller must have already sent an initial response."""
        try:
            data = await _fetch_bytes(url)
        except httpx.HTTPError as exc:
            await interaction.edit_original_response(content=f"Couldn't download the emoji: {exc}")
            return
        dupe = await self._find_duplicate(guild, name, data)
        if dupe is not None:
            kind, existing = dupe
            await interaction.edit_original_response(
                content=_dupe_notice(kind, existing, guild),
                view=_ConfirmDupeView(self, guild, name, data, interaction.user.id),
            )
            return
        await self._upload_and_report(interaction, guild, name, data)

    # ------------------------------------------------------------------
    # Context menu: right-click a message
    # ------------------------------------------------------------------

    async def _steal_from_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        guilds = _eligible_guilds(self.bot, interaction.user.id)
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
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._do_steal(
                interaction, guilds[0], name, emoji_cdn_url(emoji_id, animated)
            )
            return

        prompt = build_steal_prompt(
            n_emoji=len(emojis),
            guild_count=len(guilds),
            first_emoji_name=emojis[0][1],
            first_guild_name=guilds[0].name,
        )

        await interaction.response.send_message(
            prompt, view=_StealView(self, emojis, guilds, interaction.user.id), ephemeral=True
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

        guilds = _eligible_guilds(self.bot, interaction.user.id)
        if not guilds:
            await interaction.response.send_message(
                "I don't have **Manage Expressions** in any server.", ephemeral=True
            )
            return

        # Fast path: one guild
        if len(guilds) == 1:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await self._do_steal(interaction, guilds[0], clean_name, url)
            return

        await interaction.response.send_message(
            f"Add `:{clean_name}:` — which server?",
            view=_GuildPickView(self, url, clean_name, guilds, interaction.user.id),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(EmojiStealerCog(bot, bot.ctx))
