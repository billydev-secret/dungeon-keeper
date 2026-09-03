import asyncio
import logging

import discord

from bot_modules.games.command_groups import games
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import open_db
from bot_modules.games_help.embeds import build_help_embed
from bot_modules.games_help.logic import survivor_help_line
from bot_modules.games.utils.game_manager import channel_name

log = logging.getLogger(__name__)


@games.command(name="help", description="List all game modes and how to use them.")
async def help_command(interaction: discord.Interaction):
    log.info("%s used /games help in #%s", interaction.user.display_name, channel_name(interaction.channel))
    guild = interaction.guild
    color = await safe_resolve_accent(interaction.client, guild, log_label="games help")
    extra_lines: list[str] = []
    if guild is not None:
        db_path = interaction.client.ctx.db_path  # type: ignore[attr-defined]
        now = discord.utils.utcnow().timestamp()

        def _survivor():
            with open_db(db_path) as conn:
                return survivor_help_line(conn, guild.id, now)

        line = await asyncio.to_thread(_survivor)
        if line:
            extra_lines.append(line)
    embed = build_help_embed(color=color, extra_lines=extra_lines)
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot) -> None:
    pass
