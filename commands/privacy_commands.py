"""Privacy commands — let users purge their own data, and let admins do the same for any user."""

from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.privacy")

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
        channel = guild.get_channel(channel_id)

        # Forum thread OPs: message_id == channel_id (Discord snowflake parity)
        if _is_forum_thread(channel) and channel_id in message_ids:
            try:
                op = await channel.fetch_message(channel_id)  # type: ignore[union-attr]
                content = op.content or "\u200b"
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
            if on_progress:
                await on_progress(deleted, failed, replaced)
            continue

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            failed += len(message_ids)
            if on_progress:
                await on_progress(deleted, failed, replaced)
            continue

        target_ids = set(message_ids)
        try:
            purged = await channel.purge(  # type: ignore[union-attr]
                limit=None,
                check=lambda m, _ids=target_ids: m.id in _ids,
            )
            deleted += len(purged)
            # Messages in target_ids that weren't found are already gone — not failures
        except discord.Forbidden as exc:
            log.warning("Purge forbidden in channel %d: %s", channel_id, exc)
            failed += len(message_ids)
        except discord.HTTPException as exc:
            log.warning("Purge failed in channel %d: %s", channel_id, exc)
            failed += len(message_ids)

        if on_progress:
            await on_progress(deleted, failed, replaced)

    return deleted, failed, replaced


def _purge_db(conn, guild_id: int, user_id: int) -> int:
    """Delete all user data from the DB. Returns message count removed."""
    msg_ids = [
        r[0]
        for r in conn.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        ).fetchall()
    ]

    if msg_ids:
        ph = ",".join("?" * len(msg_ids))
        for table in (
            "message_attachments",
            "message_mentions",
            "message_embeds",
            "message_reactions",
            "message_sentiment",
        ):
            conn.execute(f"DELETE FROM {table} WHERE message_id IN ({ph})", msg_ids)

        conn.execute(
            "DELETE FROM processed_messages WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        conn.execute(
            "DELETE FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild_id, user_id),
        )

    for table in (
        "member_xp",
        "voice_sessions",
        "member_activity",
        "quality_score_leaves",
        "member_gender",
        "member_events",
        "known_users",
    ):
        conn.execute(
            f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    conn.execute(
        "DELETE FROM xp_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    conn.execute(
        "DELETE FROM role_events WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )

    for col in ("from_user_id", "to_user_id"):
        conn.execute(
            f"DELETE FROM user_interactions WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
        )
        conn.execute(
            f"DELETE FROM user_interactions_log WHERE guild_id = ? AND {col} = ?",
            (guild_id, user_id),
        )

    # Wellness tables
    for table in (
        "wellness_users",
        "wellness_caps",
        "wellness_cap_counters",
        "wellness_cap_overages",
        "wellness_blackouts",
        "wellness_blackout_overages",
        "wellness_blackout_active",
        "wellness_slow_mode",
        "wellness_streaks",
        "wellness_streak_history",
        "wellness_away_rate_limit",
        "wellness_weekly_reports",
    ):
        try:
            conn.execute(
                f"DELETE FROM {table} WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        except Exception:
            pass

    for col in ("user_id_a", "user_id_b"):
        try:
            conn.execute(
                f"DELETE FROM wellness_partners WHERE guild_id = ? AND {col} = ?",
                (guild_id, user_id),
            )
        except Exception:
            pass

    return len(msg_ids)


async def _run_deletion(
    ctx: AppContext,
    guild: discord.Guild,
    user_id: int,
    original_interaction: discord.Interaction,
) -> None:
    with ctx.open_db() as conn:
        msg_rows = conn.execute(
            "SELECT message_id, channel_id FROM messages WHERE guild_id = ? AND author_id = ?",
            (guild.id, user_id),
        ).fetchall()

    msg_rows = [(int(r[0]), int(r[1])) for r in msg_rows]

    async def _progress(deleted: int, failed: int, replaced: int) -> None:
        try:
            await original_interaction.edit_original_response(
                content=f"Deleting… **{deleted}** messages removed so far.",
                view=None,
            )
        except discord.HTTPException:
            pass

    discord_deleted, discord_failed, discord_replaced = await _delete_discord_messages(
        guild, user_id, msg_rows, on_progress=_progress
    )

    lines = [
        "All done. Here's what was removed from Discord:",
        f"Messages deleted: **{discord_deleted}**",
    ]
    if discord_replaced:
        lines.append(
            f"Forum posts re-posted under this bot (content preserved): **{discord_replaced}**"
        )
    if discord_failed:
        lines.append(f"Messages that couldn't be deleted (no access): **{discord_failed}**")

    await original_interaction.edit_original_response(
        content="\n".join(lines), view=None
    )


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_privacy_commands(bot: Bot, ctx: AppContext) -> None:

    @bot.tree.command(
        name="delete_me",
        description="Permanently delete all your messages and data from this server.",
    )
    async def delete_me(interaction: discord.Interaction) -> None:
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

        await _run_deletion(ctx, interaction.guild, interaction.user.id, interaction)

    @bot.tree.command(
        name="delete_user",
        description="Permanently delete all messages and data for a user from this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="The user whose data should be erased (works for users who have left).")
    async def delete_user(
        interaction: discord.Interaction,
        member: discord.User,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        if not ctx.is_mod(interaction):
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

        await _run_deletion(ctx, interaction.guild, member.id, interaction)
