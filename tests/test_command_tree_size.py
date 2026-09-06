"""Discord refuses a top-level command whose JSON exceeds 8000 bytes.

This is the test that was missing on 2026-09-06, when `/games` grew to 9719
bytes: sync raised `CommandSyncFailure`, `setup_hook` let it escape, and prod
crash-looped five times — dashboard, games, economy and every scheduled job
down, because a command *description* got long.

Nothing else catches it. The tree is only assembled at startup, the limit lives
at Discord's edge, and the failure arrives as an HTTP 400 in production.
"""

from __future__ import annotations

import json
import pkgutil

import discord
import pytest
from discord.ext import commands

#: Discord's documented per-top-level-command ceiling.
MAX_COMMAND_BYTES = 8000
#: Fail before Discord does, not with it. 95% leaves roughly three more options'
#: worth of runway — enough to notice and act, tight enough that nobody lands a
#: fifth game's worth of copy and finds out from a crash loop in production.
BUDGET = int(MAX_COMMAND_BYTES * 0.95)


async def _build_tree() -> commands.Bot:
    import bot_modules.cogs as cogs

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    for mod in pkgutil.iter_modules(cogs.__path__):
        try:
            await bot.load_extension(f"bot_modules.cogs.{mod.name}")
        except Exception:  # noqa: BLE001 - a cog that needs a live bot is not our concern
            continue
    return bot


@pytest.mark.asyncio
async def test_no_top_level_command_exceeds_discords_size_limit():
    bot = await _build_tree()
    oversized = []
    for cmd in bot.tree.get_commands():
        try:
            payload = cmd.to_dict(bot.tree)
        except Exception:  # noqa: BLE001
            continue
        size = len(json.dumps(payload, separators=(",", ":")))
        if size > BUDGET:
            oversized.append((cmd.name, size))

    assert not oversized, (
        "A top-level command is at or past Discord's 8000-byte ceiling, which "
        "makes command sync fail and (before the guard) crash-loop the bot:\n  "
        + "\n  ".join(f"/{name}: {size} bytes (budget {BUDGET})" for name, size in oversized)
        + "\nShorten command and option descriptions, or move subcommands out "
        "of the group. The bytes are mostly descriptions."
    )
