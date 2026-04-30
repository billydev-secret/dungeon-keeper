"""Privacy commands — let users purge their own data, and let admins do the same for any user."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from services.discord_scan import find_user_messages
from services.privacy_service import purge_user_data

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.privacy")

_FOURTEEN_DAYS = timedelta(days=14)

# ---------------------------------------------------------------------------
# Confirmation view
# ---------------------------------------------------------------------------

class _ConfirmDeleteView(discord.ui.View):
    def __init__(self, actor_id: int) -> None:
        super().__init__(timeout=60)
        self.actor_id = actor_id
        self.confirmed: bool | None = None

    @discord.ui.button(label="Yes, delete everything", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        self.confirmed = True
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            content="Deleting your data — this may take a moment…", view=self
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ---------------------------------------------------------------------------
# Deletion logic
# ---------------------------------------------------------------------------

def _is_forum_thread(channel: discord.abc.GuildChannel | discord.Thread | None) -> bool:
    return isinstance(channel, discord.Thread) and isinstance(
        channel.parent, discord.ForumChannel
    )


async def _delete_discord_messages(
    guild: discord.Guild,
    user_id: int,
    msg_rows: list[tuple[int, int]],
    on_progress=None,
) -> tuple[int, int, int]:
    """Delete Discord messages for *user_id*. Returns (deleted, failed, replaced).

    Forum thread OPs (message_id == channel_id) are kept as threads: the original
    message is deleted and the bot re-posts the same content so the post survives
    under the bot's name.

    on_progress: optional async callable(deleted, failed, replaced) called after each channel.
    """
    by_channel: dict[int, list[int]] = {}
    for message_id, channel_id in msg_rows:
        by_channel.setdefault(channel_id, []).append(message_id)

    deleted = 0
    failed = 0
    replaced = 0

    for channel_id, message_ids in by_channel.items():
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                # Channel deleted on Discord — its messages are gone with it.
                log.info(
                    "Channel %d no longer exists — counting %d messages as already gone",
                    channel_id,
                    len(message_ids),
                )
                deleted += len(message_ids)
                if on_progress:
                    await on_progress(deleted, failed, replaced)
                continue
            except (discord.Forbidden, discord.HTTPException) as exc:
                # Bot lost access or transient error — try direct deletion as a fallback.
                log.warning("Cannot resolve channel %d (%s) — attempting direct deletion", channel_id, exc)
                partial = discord.PartialMessageable(
                    state=guild._state, id=channel_id, guild_id=guild.id  # type: ignore[arg-type]
                )
                for mid in message_ids:
                    try:
                        await partial.get_partial_message(mid).delete()
                        deleted += 1
                    except discord.NotFound:
                        deleted += 1
                    except (discord.Forbidden, discord.HTTPException) as del_exc:
                        log.warning("Direct delete failed for message %d: %s", mid, del_exc)
                        failed += 1
                    if on_progress:
                        await on_progress(deleted, failed, replaced)
                    await asyncio.sleep(0.5)
                continue

        if not isinstance(
            channel,
            (discord.TextChannel, discord.VoiceChannel, discord.StageChannel, discord.Thread),
        ):
            # Unsupported channel type — count as gone
            deleted += len(message_ids)
            if on_progress:
                await on_progress(deleted, failed, replaced)
            continue

        # Unarchive threads so we can delete from them (and so forum-OP re-post
        # works — Discord rejects sends to archived threads with code 50083).
        # Re-archive at the end of the per-channel block.
        was_archived = isinstance(channel, discord.Thread) and channel.archived
        if was_archived:
            try:
                await channel.edit(archived=False)  # type: ignore[union-attr]
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Cannot unarchive thread %d: %s", channel_id, exc)
                failed += len(message_ids)
                if on_progress:
                    await on_progress(deleted, failed, replaced)
                continue

        # Forum thread OPs: message_id == channel_id (Discord snowflake parity).
        # Delete the OP and re-post its content under the bot so the thread —
        # and any replies from other members — survives.
        if _is_forum_thread(channel) and channel_id in message_ids:
            try:
                op = await channel.fetch_message(channel_id)  # type: ignore[union-attr]
                content = op.content or "​"
                await op.delete()
                await channel.send(content)  # type: ignore[union-attr]
                replaced += 1
            except discord.NotFound:
                replaced += 1
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Forum OP re-post failed in thread %d: %s", channel_id, exc)
                failed += 1
            message_ids = [mid for mid in message_ids if mid != channel_id]
            await asyncio.sleep(0.5)

        if not message_ids:
            if was_archived:
                try:
                    await channel.edit(archived=True)  # type: ignore[union-attr]
                except (discord.Forbidden, discord.HTTPException):
                    pass
            continue

        cutoff = datetime.now(timezone.utc) - _FOURTEEN_DAYS
        recent: list[int] = []
        old: list[int] = []
        for mid in message_ids:
            if discord.utils.snowflake_time(mid) > cutoff:
                recent.append(mid)
            else:
                old.append(mid)

        for i in range(0, len(recent), 100):
            batch = [discord.Object(id=mid) for mid in recent[i : i + 100]]
            try:
                await channel.delete_messages(batch)  # type: ignore[union-attr]
                deleted += len(batch)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Bulk delete failed in channel %d: %s", channel_id, exc)
                failed += len(batch)
            if on_progress:
                await on_progress(deleted, failed, replaced)
            await asyncio.sleep(1)

        for mid in old:
            try:
                await channel.get_partial_message(mid).delete()  # type: ignore[union-attr]
                deleted += 1
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Delete failed for message %d: %s", mid, exc)
                failed += 1
            if on_progress:
                await on_progress(deleted, failed, replaced)
            await asyncio.sleep(0.5)

        if was_archived:
            try:
                await channel.edit(archived=True)  # type: ignore[union-attr]
            except (discord.Forbidden, discord.HTTPException):
                pass

    return deleted, failed, replaced


async def _edit_or_send(
    interaction: discord.Interaction, content: str
) -> None:
    """Update the deletion status. After ~15 minutes the interaction token
    expires (long scans can hit this); fall back to a regular channel send."""
    try:
        await interaction.edit_original_response(content=content, view=None)
        return
    except (discord.HTTPException, discord.NotFound):
        pass
    channel = interaction.channel
    if isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel)):
        try:
            await channel.send(f"<@{interaction.user.id}> {content}")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def _run_deletion(
    ctx: AppContext,
    guild: discord.Guild,
    user_id: int,
    original_interaction: discord.Interaction,
) -> None:
    # Phase 1 — scan Discord directly. Authoritative: doesn't depend on what
    # the local index has captured, so messages from before the bot joined,
    # from downtime, or from channels that were never backfilled all show up.
    last_scan_update = 0.0

    async def _scan_progress(done: int, total: int, found: int) -> None:
        nonlocal last_scan_update
        # Throttle edits — Discord rate-limits edit_original_response.
        now = asyncio.get_event_loop().time()
        if done < total and now - last_scan_update < 2.0:
            return
        last_scan_update = now
        await _edit_or_send(
            original_interaction,
            f"Scanning the server for your messages — channel **{done}/{total}** "
            f"(**{found}** found so far)…",
        )

    msg_rows = await find_user_messages(
        guild, user_id, on_progress=_scan_progress
    )
    total = len(msg_rows)

    if total == 0:
        with ctx.open_db() as conn:
            purge_user_data(conn, guild.id, user_id, keep_messages=True)
        await _edit_or_send(
            original_interaction,
            "All done. No messages found in any channel I can read. "
            "Server-side data (XP, activity, profile): **cleared** "
            "(your message archive is preserved).",
        )
        return

    # Phase 2 — delete what we found on Discord. The local archive is safe:
    # on_raw_message_delete no longer touches the messages table, so the
    # user's records here survive the Discord-side deletion.
    def _render_bar(done: int) -> str:
        width = 20
        filled = round(width * done / total) if total else width
        bar = "█" * filled + "░" * (width - filled)
        return f"`[{bar}]` {done}/{total}"

    last_delete_update = 0.0

    async def _delete_progress(deleted: int, failed: int, replaced: int) -> None:
        nonlocal last_delete_update
        done = deleted + failed + replaced
        now = asyncio.get_event_loop().time()
        if done < total and now - last_delete_update < 1.5:
            return
        last_delete_update = now
        await _edit_or_send(
            original_interaction, f"Deleting… {_render_bar(done)}"
        )

    discord_deleted, discord_failed, discord_replaced = await _delete_discord_messages(
        guild, user_id, msg_rows, on_progress=_delete_progress
    )

    # Purge XP, activity, known_users, wellness — but keep the messages table
    # rows so the user retains a local archive of what they posted.
    with ctx.open_db() as conn:
        purge_user_data(conn, guild.id, user_id, keep_messages=True)

    lines = [
        "All done. Here's what was removed:",
        f"Discord messages deleted: **{discord_deleted}**",
        "Server-side data (XP, activity, profile): **cleared** "
        "(your message archive is preserved)",
    ]
    if discord_replaced:
        lines.append(
            f"Forum posts re-posted under this bot (content preserved): **{discord_replaced}**"
        )
    if discord_failed:
        lines.append(f"Messages that couldn't be deleted (no access): **{discord_failed}**")

    await _edit_or_send(original_interaction, "\n".join(lines))


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class PrivacyCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    @app_commands.command(
        name="delete_me",
        description="Permanently delete all your messages and data from this server.",
    )
    async def delete_me(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        view = _ConfirmDeleteView(actor_id=interaction.user.id)
        await interaction.response.send_message(
            "⚠️ **This will permanently delete everything you have done in this server** — "
            "all your messages, XP, activity history, and profile data. "
            "This cannot be undone.\n\nAre you sure?",
            view=view,
            ephemeral=True,
        )

        await view.wait()
        if not view.confirmed:
            return

        await _run_deletion(self.ctx, interaction.guild, interaction.user.id, interaction)

    @app_commands.command(
        name="delete_user",
        description="Permanently delete all messages and data for a user from this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The user whose data should be erased (works for users who have left).")
    async def delete_user(
        self,
        interaction: discord.Interaction,
        member: discord.User,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if not self.ctx.is_mod(interaction):
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        view = _ConfirmDeleteView(actor_id=interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ **This will permanently delete everything {member.mention} has done in this server** — "
            f"all their messages, XP, activity history, and profile data. "
            f"This cannot be undone.\n\nAre you sure?",
            view=view,
            ephemeral=True,
        )

        await view.wait()
        if not view.confirmed:
            return

        await _run_deletion(self.ctx, interaction.guild, member.id, interaction)


async def setup(bot: Bot) -> None:
    await bot.add_cog(PrivacyCog(bot, bot.ctx))
