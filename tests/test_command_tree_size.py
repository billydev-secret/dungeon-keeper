"""Discord refuses a top-level command whose JSON exceeds 8000 bytes.

This is the test that was missing on 2026-09-06, when `/games` grew to 9,719
bytes: sync raised `CommandSyncFailure`, `setup_hook` let it escape, and prod
crash-looped five times — dashboard, games, economy and every scheduled job
down, because a command *description* got long.

Nothing static can catch it. The tree only exists once every cog is loaded, and
the limit lives at Discord's edge, so the failure otherwise arrives as an HTTP
400 in production.

**Measured in a subprocess, deliberately.** Loading every extension starts the
cogs' background task loops and mutates module-level state — the shared
`games` group most of all — which leaked into whatever else shared the xdist
worker and failed 25 unrelated tests the first time this ran in CI. A child
process cannot poison anyone.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Discord's documented per-top-level-command ceiling.
MAX_COMMAND_BYTES = 8000
#: Fail before Discord does, not with it. 95% leaves roughly three more options'
#: worth of runway — enough to notice and act, tight enough that nobody lands a
#: fifth game's worth of copy and finds out from a crash loop in production.
BUDGET = int(MAX_COMMAND_BYTES * 0.95)

_MEASURE = r"""
import asyncio, json, pkgutil, sys
sys.path.insert(0, "src")
import discord
from discord.ext import commands

async def main():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    import bot_modules.cogs as cogs
    for mod in pkgutil.iter_modules(cogs.__path__):
        try:
            await bot.load_extension(f"bot_modules.cogs.{mod.name}")
        except Exception:
            continue
    sizes = {}
    for cmd in bot.tree.get_commands():
        try:
            payload = cmd.to_dict(bot.tree)
        except Exception:
            continue
        sizes[cmd.name] = len(json.dumps(payload, separators=(",", ":")))
    print("SIZES:" + json.dumps(sizes))

asyncio.run(main())
"""


def _tree_sizes() -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-c", _MEASURE],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("SIZES:")), None
    )
    if line is None:  # pragma: no cover - only when the tree cannot be built at all
        pytest.fail(
            "could not measure the command tree:\n"
            f"stdout tail: {proc.stdout[-800:]}\nstderr tail: {proc.stderr[-800:]}"
        )
    return json.loads(line[len("SIZES:"):])


def test_no_top_level_command_exceeds_discords_size_limit():
    sizes = _tree_sizes()
    assert sizes, "no commands measured — the tree failed to build"
    oversized = {name: n for name, n in sizes.items() if n > BUDGET}
    assert not oversized, (
        "A top-level command is at or past Discord's 8000-byte ceiling, which "
        "makes command sync fail and (before the guard in app_context) "
        "crash-loop the bot:\n  "
        + "\n  ".join(f"/{n}: {b} bytes (budget {BUDGET})" for n, b in oversized.items())
        + "\nShorten command and option descriptions, or move subcommands out of "
        "the group. Look first for options that duplicate a dashboard dial — "
        "that is where the bytes went last time."
    )
