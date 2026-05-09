"""/beta-ambient-* slash commands — start, stop, status for the ambient sim."""
from __future__ import annotations

import time
import logging

import discord

from beta_tools.slash._base import reject_if_not_mod

log = logging.getLogger("beta_tools.slash.ambient")


async def _ambient_start_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    if sim.is_running:
        await interaction.response.send_message("Ambient sim is already running.", ephemeral=True)
        return
    sim.start()
    base = sim._base_interval()
    await interaction.response.send_message(
        f"Ambient sim started — base interval `{base:.0f}s`, burst `5s` for `30s` after each post.",
        ephemeral=True,
    )


async def _ambient_stop_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    if not sim.is_running:
        await interaction.response.send_message("Ambient sim is not running.", ephemeral=True)
        return
    posts = sim.posts_since_start
    await sim.stop()
    await interaction.response.send_message(
        f"Ambient sim stopped — `{posts}` post{'s' if posts != 1 else ''} sent this session.",
        ephemeral=True,
    )


async def _ambient_status_handler(bot, interaction: discord.Interaction) -> None:
    if not await reject_if_not_mod(interaction):
        return
    sim = bot.ambient_sim
    state = "Running" if sim.is_running else "Stopped"
    lines = [f"**Ambient Sim** — {state}"]
    lines.append(f"Posts this session: `{sim.posts_since_start}`")
    if sim.last_post:
        key, channel_name, ts = sim.last_post
        ago = int(time.time() - ts)
        lines.append(f"Last post: `{key}` in `#{channel_name}` ({ago}s ago)")
    else:
        lines.append("Last post: —")
    lines.append(
        f"Corpus: `{sim._chain.corpus_size}` messages / `{sim._chain.vocab_size}` bigrams"
    )
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


def register(bot) -> None:
    guild_obj = discord.Object(id=bot.main_cfg.guild_id)

    @bot.tree.command(
        name="beta-ambient-start",
        description="Start the ambient puppet sim loop",
        guild=guild_obj,
    )
    async def start_cmd(interaction: discord.Interaction) -> None:
        await _ambient_start_handler(bot, interaction)

    @bot.tree.command(
        name="beta-ambient-stop",
        description="Stop the ambient puppet sim loop",
        guild=guild_obj,
    )
    async def stop_cmd(interaction: discord.Interaction) -> None:
        await _ambient_stop_handler(bot, interaction)

    @bot.tree.command(
        name="beta-ambient-status",
        description="Show ambient sim state, post count, and corpus info",
        guild=guild_obj,
    )
    async def status_cmd(interaction: discord.Interaction) -> None:
        await _ambient_status_handler(bot, interaction)
