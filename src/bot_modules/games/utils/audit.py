"""Audit entry point for the anonymous games.

``audit_anonymous`` is what call sites use. It does two things:

1. Writes a row to ``anon_audit_log`` — the durable trail. This is the part
   that matters: the audit channel below is unset by default and log.txt is
   truncated on every boot, so before this existed an anonymous submission
   could leave no trace anywhere.
2. Mirrors the submission to the guild's configured audit channel, which is
   the pre-existing behaviour moderators already watch.

The DB row stores **no content** — see migration 145. The channel embed does
show the text, because that is a Discord message subject to the same retention
as any other, not a second copy in our own database.
"""

import asyncio
import logging
import discord
from bot_modules.games.constants import WARNING_COLOR, GAME_ICONS
from bot_modules.services.anon_audit_service import record_event

log = logging.getLogger(__name__)


async def audit_anonymous(
    bot,
    db,
    guild: discord.Guild,
    *,
    game_type: str,
    user: discord.Member | discord.User,
    event: str,
    content: str | None = None,
    label: str = "Anonymous Submission",
    target_id: int | None = None,
    game_id: str | None = None,
    message_id: int | None = None,
    channel_id: int | None = None,
    extra: dict | None = None,
) -> None:
    """Record an anonymous action, and mirror it to the audit channel.

    ``message_id``/``channel_id`` are the pointer the dashboard joins against
    to recover the submission text; pass them once the message actually
    exists. Leave them None for actions that produce no guild message — a
    screened question the host rejects, a WYR question that is only queued,
    a Hot Takes or Fantasies entry held in the payload until the reveal.
    Those rows are who-and-when only, by design.

    ``content`` drives the channel mirror only, never the DB row. Omit it for
    metadata-only events — a pass, a vote, a hot-seat rotation — so the audit
    channel stays readable instead of filling with contentless embeds.
    """
    db_path = getattr(getattr(bot, "ctx", None), "db_path", None)
    if db_path is not None:
        await asyncio.to_thread(
            record_event,
            db_path,
            guild_id=guild.id,
            feature=game_type,
            event=event,
            actor_id=user.id,
            target_id=target_id,
            game_id=game_id,
            message_id=message_id,
            channel_id=channel_id,
            extra=extra,
        )

    if content:
        await send_audit_log(
            bot, db, guild,
            game_type=game_type, user=user, content=content, label=label,
        )


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
        description=f'"{discord.utils.escape_markdown(content)}"',
        color=WARNING_COLOR,
    )
    embed.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=True)
    embed.add_field(name="Game", value=game_type.upper(), inline=True)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        log.debug("Failed to send audit log: %s", e)
