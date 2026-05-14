import logging
import discord
from bot_modules.games.constants import WARNING_COLOR, GAME_ICONS

log = logging.getLogger(__name__)


async def send_audit_log(
    bot,
    db,
    guild: discord.Guild,
    *,
    game_type: str,
    user: discord.Member | discord.User,
    content: str,
    label: str = "Anonymous Submission",
):
    """Send an audit log entry to the configured audit channel, if any."""
    row = await db.fetchone(
        "SELECT channel_id FROM games_audit_channel WHERE guild_id = ?",
        (guild.id,),
    )
    if not row:
        return

    channel = bot.get_channel(row[0])
    if not channel:
        return

    icon = GAME_ICONS.get(game_type, "")
    embed = discord.Embed(
        title=f"{icon} {label}",
        description=f'"{content}"',
        color=WARNING_COLOR,
    )
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    embed.add_field(name="Game", value=game_type.upper(), inline=True)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        log.debug("Failed to send audit log: %s", e)
