"""Privacy commands — let users clear their own Discord messages, and let admins
do the same for any user.

These commands only remove messages from Discord. The bot's server-side records
(XP, activity, profile, and its own copy of the messages) are always retained for
moderation. The genuine hard-erasure path lives in ``privacy_service.purge_user_data``,
which is deliberately not wired to any command.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot_modules.core.utils import disable_all_items
from discord import app_commands
from discord.ext import commands

from bot_modules.privacy.logic import (
    MODE_ALL,
    MODE_MEDIA,
    MODE_TEXT,
    chunk_for_bulk_delete,
    confirm_button_label,
    group_messages_by_channel,
    is_forum_thread,
    message_matches_mode,
    partition_by_bulk_delete_window,
    render_confirm_prompt,
    render_deletion_summary,
    render_empty_summary,
    render_progress_bar,
    render_scan_status,
    should_throttle,
)
from bot_modules.services.discord_scan import find_user_messages

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.privacy")

# ---------------------------------------------------------------------------
# Confirmation view
# ---------------------------------------------------------------------------

class _ConfirmDeleteView(discord.ui.View):
    def __init__(self, actor_id: int, *, confirm_label: str = "Yes, delete my messages") -> None:
        super().__init__(timeout=60)
        self.actor_id = actor_id
        self.confirmed: bool | None = None
        # The label is the last thing read before an irreversible click, so it
        # names the actual scope — a broader claim than "messages" would be a lie.
        self.confirm.label = confirm_label

    @discord.ui.button(label="Yes, delete my messages", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return
        self.confirmed = True
        disable_all_items(self)
        await interaction.response.edit_message(
            content="Working on it — this may take a moment…", view=self
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


async def _delete_discord_messages(
    guild: discord.Guild,
    user_id: int,
    msg_rows: list[tuple[int, int]],
    on_progress=None,
) -> tuple[int, int, int]:
    """Delete Discord messages for *user_id*. Returns (deleted, failed, replaced).

    Forum thread OPs (message_id == channel_id) are kept as threads: the original
    message is deleted and the bot posts a [deleted] tombstone so the thread and
    its replies survive under the bot's name.

    on_progress: optional async callable(deleted, failed, replaced) called after each channel.
    """
    by_channel = group_messages_by_channel(msg_rows)

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
        if is_forum_thread(channel) and channel_id in message_ids:
            try:
                op = await channel.fetch_message(channel_id)  # type: ignore[union-attr]
                await op.delete()
                await channel.send("[deleted]")  # type: ignore[union-attr]
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

        recent, old = partition_by_bulk_delete_window(message_ids)

        for chunk in chunk_for_bulk_delete(recent):
            batch = [discord.Object(id=mid) for mid in chunk]
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
                deleted += 1
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
    expires (long scans can hit this); fall back to a DM so the result
    doesn't appear publicly in the channel."""
    try:
        await interaction.edit_original_response(content=content, view=None)
        return
    except (discord.HTTPException, discord.NotFound):
        pass
    try:
        await interaction.user.send(content)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def _run_deletion(
    ctx: AppContext,
    guild: discord.Guild,
    user_id: int,
    original_interaction: discord.Interaction,
    *,
    mode: str = MODE_ALL,
) -> None:
    # Every mode only clears the member's *Discord* messages; nothing on the
    # server side is touched. XP, activity, profile, and the bot's own copy of
    # the messages are retained for moderation — the modes differ only in which
    # Discord messages (all / images / text) are removed.

    # Phase 1 — scan Discord directly. Authoritative: doesn't depend on what
    # the local index has captured, so messages from before the bot joined,
    # from downtime, or from channels that were never backfilled all show up.
    last_scan_update = 0.0

    async def _scan_progress(done: int, total: int, found: int) -> None:
        nonlocal last_scan_update
        # Throttle edits — Discord rate-limits edit_original_response.
        now = asyncio.get_running_loop().time()
        if should_throttle(last_scan_update, now, done=done, total=total, interval=2.0):
            return
        last_scan_update = now
        await _edit_or_send(
            original_interaction, render_scan_status(done, total, found)
        )

    msg_rows = await find_user_messages(
        guild,
        user_id,
        on_progress=_scan_progress,
        # Filter during the scan: the returned ids carry no attachment detail,
        # so a mode that selects on content has to decide while the Message is
        # still in hand.
        predicate=(
            None if mode == MODE_ALL else lambda m: message_matches_mode(m, mode)
        ),
    )
    total = len(msg_rows)

    if total == 0:
        await _edit_or_send(
            original_interaction,
            render_empty_summary(mode=mode),
        )
        return

    # Phase 2 — delete what we found on Discord. The local archive is safe:
    # on_raw_message_delete no longer touches the messages table, so the
    # user's records here survive the Discord-side deletion.
    last_delete_update = 0.0

    async def _delete_progress(deleted: int, failed: int, replaced: int) -> None:
        nonlocal last_delete_update
        done = deleted + failed + replaced
        now = asyncio.get_running_loop().time()
        if should_throttle(last_delete_update, now, done=done, total=total, interval=1.5):
            return
        last_delete_update = now
        await _edit_or_send(
            original_interaction, f"Deleting… {render_progress_bar(done, total)}"
        )

    discord_deleted, discord_failed, discord_replaced = await _delete_discord_messages(
        guild, user_id, msg_rows, on_progress=_delete_progress
    )

    await _edit_or_send(
        original_interaction,
        render_deletion_summary(
            deleted=discord_deleted,
            failed=discord_failed,
            replaced=discord_replaced,
            mode=mode,
        ),
    )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class PrivacyCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self._active_deletions: set[int] = set()
        super().__init__()

    @app_commands.command(
        name="delete_me",
        description="Delete your messages from this server — all of them, or just images or text.",
    )
    @app_commands.describe(
        mode="What to clear from Discord. Your XP, profile, and the server's own records always stay.",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="All my messages", value=MODE_ALL),
            app_commands.Choice(name="Images & files only", value=MODE_MEDIA),
            app_commands.Choice(name="Text messages only", value=MODE_TEXT),
        ]
    )
    async def delete_me(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if interaction.user.id in self._active_deletions:
            await interaction.response.send_message(
                "A deletion is already running for your account — please wait for it to finish.",
                ephemeral=True,
            )
            return

        # Omitting the option clears all of the member's Discord messages (their
        # server-side data is kept either way), so muscle-memory /delete_me still
        # does the broadest message clear.
        mode_value = mode.value if mode else MODE_ALL

        view = _ConfirmDeleteView(
            actor_id=interaction.user.id,
            confirm_label=confirm_button_label(mode_value, self_service=True),
        )
        await interaction.response.send_message(
            render_confirm_prompt(mode=mode_value),
            view=view,
            ephemeral=True,
        )

        self._active_deletions.add(interaction.user.id)
        try:
            await view.wait()
            if not view.confirmed:
                return
            await _run_deletion(
                self.ctx,
                interaction.guild,
                interaction.user.id,
                interaction,
                mode=mode_value,
            )
        finally:
            self._active_deletions.discard(interaction.user.id)

    @app_commands.command(
        name="delete_user",
        description="Delete a user's messages from this server — all of them, or just images or text.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        member="The user whose Discord messages should be cleared (works for users who have left).",
        mode="What to clear from Discord. Their XP, profile, and the server's own records always stay.",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="All their messages", value=MODE_ALL),
            app_commands.Choice(name="Images & files only", value=MODE_MEDIA),
            app_commands.Choice(name="Text messages only", value=MODE_TEXT),
        ]
    )
    async def delete_user(
        self,
        interaction: discord.Interaction,
        member: discord.User,
        mode: app_commands.Choice[str] | None = None,
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

        if member.id in self._active_deletions:
            await interaction.response.send_message(
                f"A deletion is already running for {member.mention} — please wait for it to finish.",
                ephemeral=True,
            )
            return

        mode_value = mode.value if mode else MODE_ALL

        view = _ConfirmDeleteView(
            actor_id=interaction.user.id,
            confirm_label=confirm_button_label(mode_value, self_service=False),
        )
        await interaction.response.send_message(
            render_confirm_prompt(
                mode=mode_value,
                subject=member.mention,
            ),
            view=view,
            ephemeral=True,
        )

        self._active_deletions.add(member.id)
        try:
            await view.wait()
            if not view.confirmed:
                return
            await _run_deletion(
                self.ctx,
                interaction.guild,
                member.id,
                interaction,
                mode=mode_value,
            )
        finally:
            self._active_deletions.discard(member.id)


async def setup(bot: Bot) -> None:
    await bot.add_cog(PrivacyCog(bot, bot.ctx))
