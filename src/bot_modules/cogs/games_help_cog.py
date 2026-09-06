import asyncio
import logging

import discord

from bot_modules.games.command_groups import games
from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import open_db
from bot_modules.games.mahjong.mahjong_service import mahjong_help_line
from bot_modules.games_help.logic import room_help_lines, survivor_help_line
from bot_modules.games_help.views import GameHelpView
from bot_modules.games.utils.game_manager import channel_name

log = logging.getLogger(__name__)


async def open_room_lines(db_path, guild_id: int, now: float) -> list[str]:
    """The channel-native rooms open on this server, for the overview's
    Rooms & Tables group: Survivor while its door is open, Mahjong while its
    dial is on, Guess Who and the casino when a channel is wired."""
    def _lines() -> list[str]:
        with open_db(db_path) as conn:
            return [
                line for line in (
                    survivor_help_line(conn, guild_id, now),
                    mahjong_help_line(conn, guild_id),
                    *room_help_lines(conn, guild_id),
                ) if line
            ]

    return await asyncio.to_thread(_lines)


@games.command(name="help", description="Browse every game — rules, players needed, and a Start button.")
async def help_command(interaction: discord.Interaction):
    log.info("%s used /games help in #%s", interaction.user.display_name, channel_name(interaction.channel))
    guild = interaction.guild
    color = await safe_resolve_accent(interaction.client, guild, log_label="games help")
    extra_lines: list[str] = []
    if guild is not None:
        db_path = interaction.client.ctx.db_path  # type: ignore[attr-defined]
        extra_lines = await open_room_lines(db_path, guild.id, discord.utils.utcnow().timestamp())
    view = GameHelpView(color=color, extra_lines=extra_lines)
    await interaction.response.send_message(embed=view.overview_embed(), view=view, ephemeral=True)


async def setup(bot) -> None:
    pass
