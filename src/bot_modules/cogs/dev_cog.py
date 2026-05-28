"""Developer tools — hot-reload cog extensions."""
from __future__ import annotations

import ast
import importlib
import inspect
import logging
import os
import sys
import types
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.services.command_sync import sync_if_changed

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.dev")


def _is_stateful(val: object) -> bool:
    """Return True for mutable runtime values that should survive a module reload."""
    if val is None:
        return False
    if isinstance(val, (bool, int, float, str, bytes, tuple, frozenset)):
        return False
    if isinstance(val, type):
        return False
    if isinstance(val, types.ModuleType):
        return False
    if isinstance(val, (types.FunctionType, types.MethodType,
                        types.BuiltinFunctionType, types.BuiltinMethodType)):
        return False
    return True


def _snapshot(mod: types.ModuleType) -> dict[str, object]:
    return {
        name: val
        for name, val in vars(mod).items()
        if not name.startswith("__") and _is_stateful(val)
    }


def _restore(mod: types.ModuleType, snapshot: dict[str, object]) -> None:
    for name, val in snapshot.items():
        setattr(mod, name, val)


def _dep_names(extension: str) -> list[str]:
    """Return bot_modules.* dependency module names imported by the extension."""
    mod = sys.modules.get(extension)
    if mod is None:
        return []
    try:
        source = inspect.getsource(mod)
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return []

    seen: set[str] = set()
    ordered: list[str] = []

    def _add(name: str) -> None:
        if name not in seen and name in sys.modules:
            seen.add(name)
            ordered.append(name)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base.startswith("bot_modules."):
                _add(base)
                # `from pkg import submod` — submod may be a submodule
                for alias in node.names:
                    _add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("bot_modules."):
                    _add(alias.name)

    return ordered


class DevCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _ext_autocomplete(
        self, _interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=name, value=name)
            for name in self.bot.extensions
            if current.lower() in name.lower()
        ][:25]

    @app_commands.command(name="reload_cog", description="Reload a cog extension.")
    @app_commands.describe(extension="Extension to reload, e.g. cogs.mod_cog")
    @app_commands.autocomplete(extension=_ext_autocomplete)
    async def reload_cog(self, interaction: discord.Interaction, extension: str) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Bot owner only.", ephemeral=True)
            return
        if extension not in self.bot.extensions:
            await interaction.response.send_message(
                f"Unknown extension `{extension}`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            for dep_name in _dep_names(extension):
                dep_mod = sys.modules[dep_name]
                snap = _snapshot(dep_mod)
                try:
                    importlib.reload(dep_mod)
                except Exception:
                    log.exception("Deep reload failed for dependency %s", dep_name)
                else:
                    _restore(dep_mod, snap)
            await self.bot.reload_extension(extension)
            db_path = self.bot.ctx.db_path
            if self.bot.debug:
                guild = discord.Object(id=self.bot.guild_id)
                self.bot.tree.copy_global_to(guild=guild)
                _, did = await sync_if_changed(
                    self.bot.tree, db_path, guild=guild
                )
            else:
                _, did = await sync_if_changed(
                    self.bot.tree, db_path, guild=None
                )
        except Exception as exc:
            log.exception("Reload failed for %s", extension)
            await interaction.followup.send(
                f"Reload failed: `{type(exc).__name__}: {exc}`", ephemeral=True
            )
            return

        suffix = " (commands resynced)" if did else " (commands unchanged)"
        await interaction.followup.send(
            f"Reloaded `{extension}`.{suffix}", ephemeral=True
        )


    @app_commands.command(
        name="spotify_authorize",
        description="(Owner) Get a one-time link to authorize Spotify private-playlist access.",
    )
    async def spotify_authorize(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Bot owner only.", ephemeral=True)
            return
        base = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8080").rstrip("/")
        if base.endswith("/callback"):
            base = base[: -len("/callback")]
        url = f"{base}/spotify/authorize"
        await interaction.response.send_message(
            f"Click to authorize Spotify (admin login required): {url}",
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(DevCog(bot))
