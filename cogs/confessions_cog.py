"""Anonymous confessions cog — ported from openConfess."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from services.confessions_service import (
    ERROR_CONFIG_INVALID,
    ERROR_NOT_CONFIGURED,
    ERROR_PANIC_MODE,
    ERROR_REPLIES_DISABLED,
    ERROR_USER_BLOCKED,
    MAX_DISCORD_MESSAGE_LENGTH,
    MIN_REPLY_COOLDOWN_SECONDS,
    _ANON_CIRCLES,
    build_anon_reply,
    check_and_bump_limits,
    defang_everyone_here,
    get_config,
    get_discord_thread_id,
    get_or_assign_emoji_index,
    get_thread_info,
    init_db,
    jump_link,
    log_confession,
    log_reply,
    purge_old_thread_posts,
    thread_name_from_content,
    update_discord_thread_id,
    upsert_config,
    upsert_thread_post,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot
    from services.confessions_service import GuildConfig

log = logging.getLogger(__name__)

CONFESSION_HEADER_LENGTH = 2


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------

class DMRequestModal(discord.ui.Modal, title="New DM Request"):
    request = discord.ui.TextInput(
        label="What do you need help with?",
        style=discord.TextStyle.long,
        required=True,
        max_length=2000,
        placeholder="Describe what you'd like to discuss in DMs.",
    )

    def __init__(self, cog: ConfessionsCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and interaction.user
        cfg = get_config(self.cog.ctx.db_path, interaction.guild.id)
        if not cfg:
            await self.cog._safe_ephemeral(interaction, ERROR_NOT_CONFIGURED)
            return
        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await self.cog._safe_ephemeral(interaction, ERROR_CONFIG_INVALID)
            return
        request_text = str(self.request.value).strip()
        if not request_text:
            await self.cog._safe_ephemeral(interaction, "DM request can't be empty.")
            return
        emb = discord.Embed(
            title="New DM Request",
            description=defang_everyone_here(request_text),
            timestamp=discord.utils.utcnow(),
        )
        emb.add_field(name="Requester", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        emb.add_field(name="Guild", value=f"{interaction.guild.name} (`{interaction.guild.id}`)", inline=False)
        try:
            await log_channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            await self.cog._safe_ephemeral(interaction, "Failed to submit DM request (missing perms?).")
            return
        await self.cog._safe_ephemeral(interaction, "Your DM request was sent to moderators.")


class ConfessModal(discord.ui.Modal, title="Anonymous Confession"):
    confession = discord.ui.TextInput(
        label="Confession",
        style=discord.TextStyle.long,
        required=True,
        max_length=4000,
        placeholder="Confessions are logged by admins\nGrievances belong in tickets\nBe kind when mentioning people",
    )
    notify_pref = discord.ui.TextInput(
        label="Notify me on replies? (yes/no)",
        style=discord.TextStyle.short,
        required=False,
        default="yes",
        max_length=3,
        placeholder="yes",
    )

    def __init__(self, cog: ConfessionsCog) -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and interaction.user
        db_path = self.cog.ctx.db_path
        cfg = get_config(db_path, interaction.guild.id)
        if not cfg:
            await self.cog._safe_ephemeral(interaction, ERROR_NOT_CONFIGURED)
            return
        if cfg.panic:
            await self.cog._safe_ephemeral(interaction, ERROR_PANIC_MODE)
            return
        if interaction.user.id in cfg.blocked_set():
            await self.cog._safe_ephemeral(interaction, ERROR_USER_BLOCKED)
            return

        content = str(self.confession.value).strip()
        pref = str(self.notify_pref.value or "").strip().lower()
        if pref in ("", "y", "yes", "true", "1", "on"):
            ping_pref = True
        elif pref in ("n", "no", "false", "0", "off"):
            ping_pref = False
        else:
            await self.cog._safe_ephemeral(interaction, "Invalid notify setting. Use `yes` or `no`.")
            return

        if not content:
            await self.cog._safe_ephemeral(interaction, "Confession can't be empty.")
            return

        confession_max_chars = min(cfg.max_chars, max(1, MAX_DISCORD_MESSAGE_LENGTH - CONFESSION_HEADER_LENGTH))
        if len(content) > confession_max_chars:
            await self.cog._safe_ephemeral(
                interaction, f"That's too long (max **{confession_max_chars}** characters for this confession format)."
            )
            return

        ok, msg = check_and_bump_limits(
            db_path, interaction.guild.id, interaction.user.id,
            is_reply=False, cooldown_seconds=cfg.cooldown_seconds, per_day_limit=cfg.per_day_limit,
        )
        if not ok:
            await self.cog._safe_ephemeral(interaction, msg)
            return

        dest_channel = interaction.guild.get_channel(cfg.dest_channel_id)
        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(dest_channel, (discord.TextChannel, discord.ForumChannel)) or not isinstance(log_channel, discord.TextChannel):
            await self.cog._safe_ephemeral(interaction, "Bot config is invalid (missing destination or log channel).")
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return

        if isinstance(dest_channel, discord.ForumChannel):
            tag_kwargs: dict = {}
            if dest_channel.flags.require_tag and dest_channel.available_tags:
                tag_kwargs["applied_tags"] = [dest_channel.available_tags[0]]
            try:
                forum_result = await dest_channel.create_thread(
                    name=thread_name_from_content(content),
                    content=defang_everyone_here(content),
                    allowed_mentions=discord.AllowedMentions.none(),
                    auto_archive_duration=10080,
                    **tag_kwargs,
                )
            except discord.HTTPException:
                await self.cog._safe_ephemeral(interaction, "Failed to post confession (missing perms?).")
                return
            forum_thread = forum_result.thread
            root_message_id = forum_thread.id
            await log_confession(
                log_channel=log_channel, author=interaction.user, guild_id=interaction.guild.id,
                dest_channel_id=forum_thread.id, dest_message_id=forum_thread.id, content=content,
            )
            upsert_thread_post(
                db_path, guild_id=interaction.guild.id, message_id=root_message_id,
                channel_id=dest_channel.id, root_message_id=root_message_id,
                original_author_id=interaction.user.id,
                notify_original_author=1 if ping_pref else 0,
            )
            update_discord_thread_id(db_path, interaction.guild.id, root_message_id, forum_thread.id)
            try:
                await forum_result.message.edit(view=ConfessionsCog.build_reply_button_view(root_message_id))
            except discord.HTTPException:
                pass
            await self.cog.refresh_confess_launcher(interaction.guild.id, trigger_channel_id=dest_channel.id)
            await self.cog._safe_complete(interaction)
            return

        try:
            sent = await dest_channel.send(
                content=defang_everyone_here(content),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await self.cog._safe_ephemeral(interaction, "Failed to post confession (missing perms?).")
            return

        await log_confession(
            log_channel=log_channel, author=interaction.user, guild_id=interaction.guild.id,
            dest_channel_id=dest_channel.id, dest_message_id=sent.id, content=content,
        )
        upsert_thread_post(
            db_path, guild_id=interaction.guild.id, message_id=sent.id,
            channel_id=dest_channel.id, root_message_id=sent.id,
            original_author_id=interaction.user.id,
            notify_original_author=1 if ping_pref else 0,
        )
        try:
            thread = await sent.create_thread(name=thread_name_from_content(content), auto_archive_duration=10080)
            update_discord_thread_id(db_path, interaction.guild.id, sent.id, thread.id)
            try:
                await thread.send(view=ConfessionsCog.build_reply_button_view(sent.id))
            except discord.HTTPException:
                pass
        except discord.HTTPException:
            pass
        await self.cog.refresh_confess_launcher(interaction.guild.id, trigger_channel_id=dest_channel.id)
        await self.cog._safe_complete(interaction)


class ReplyModal(discord.ui.Modal, title="Anonymous Reply"):
    reply = discord.ui.TextInput(
        label="Reply",
        style=discord.TextStyle.long,
        required=True,
        max_length=4000,
        placeholder="Reply kindly. Keep it about the content, not the person.",
    )
    notify_pref = discord.ui.TextInput(
        label="Notify me on replies? (yes/no)",
        style=discord.TextStyle.short,
        required=False,
        default="yes",
        max_length=3,
        placeholder="yes",
    )

    def __init__(
        self,
        cog: ConfessionsCog,
        cfg: GuildConfig,
        parent_channel_id: int,
        parent_message_id: int,
        thread_id: int = 0,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.parent_channel_id = parent_channel_id
        self.parent_message_id = parent_message_id
        self.thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild and interaction.user
        db_path = self.cog.ctx.db_path
        cfg = get_config(db_path, interaction.guild.id)
        if not cfg:
            await self.cog._safe_ephemeral(interaction, "Bot is not configured.")
            return
        if cfg.panic:
            await self.cog._safe_ephemeral(interaction, ERROR_PANIC_MODE)
            return
        if not cfg.replies_enabled:
            await self.cog._safe_ephemeral(interaction, ERROR_REPLIES_DISABLED)
            return
        if interaction.user.id in cfg.blocked_set():
            await self.cog._safe_ephemeral(interaction, "You can't submit anonymous replies on this server.")
            return

        content = str(self.reply.value).strip()
        pref = str(self.notify_pref.value or "").strip().lower()
        if pref in ("", "y", "yes", "true", "1", "on"):
            my_notify_pref = 1
        elif pref in ("n", "no", "false", "0", "off"):
            my_notify_pref = 0
        else:
            await self.cog._safe_ephemeral(interaction, "Invalid notify setting. Use `yes` or `no`.")
            return

        if not content:
            await self.cog._safe_ephemeral(interaction, "Reply can't be empty.")
            return
        reply_max_chars = min(cfg.max_chars, MAX_DISCORD_MESSAGE_LENGTH)
        if len(content) > reply_max_chars:
            await self.cog._safe_ephemeral(
                interaction, f"That's too long (max **{reply_max_chars}** characters for replies)."
            )
            return

        reply_cooldown = max(MIN_REPLY_COOLDOWN_SECONDS, cfg.cooldown_seconds // 2)
        ok, msg = check_and_bump_limits(
            db_path, interaction.guild.id, interaction.user.id,
            is_reply=True, cooldown_seconds=reply_cooldown, per_day_limit=0,
        )
        if not ok:
            await self.cog._safe_ephemeral(interaction, msg)
            return

        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            await self.cog._safe_ephemeral(interaction, "Bot config is invalid.")
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return

        root_message_id = self.parent_message_id
        parent_author_id = 0
        parent_notify_pref = 1 if cfg.notify_op_on_reply else 0
        thread_info = get_thread_info(db_path, interaction.guild.id, self.parent_message_id)
        if thread_info:
            root_message_id, parent_author_id, parent_notify_pref = thread_info
            if parent_notify_pref not in (0, 1):
                parent_notify_pref = 1 if cfg.notify_op_on_reply else 0

        is_op = parent_author_id > 0 and interaction.user.id == parent_author_id
        circle = None
        if not is_op:
            emoji_idx = get_or_assign_emoji_index(db_path, interaction.guild.id, root_message_id, interaction.user.id)
            circle = _ANON_CIRCLES[emoji_idx]
        reply_content = build_anon_reply(content, interaction.user.id, root_message_id, is_op=is_op, circle=circle)

        if self.thread_id:
            reply_channel = self.cog.bot.get_channel(self.thread_id)
            if reply_channel is None:
                try:
                    reply_channel = await interaction.guild.fetch_channel(self.thread_id)
                except discord.HTTPException:
                    await self.cog._safe_ephemeral(interaction, "Couldn't access the confession thread.")
                    return
            if not isinstance(reply_channel, discord.Thread):
                await self.cog._safe_ephemeral(interaction, "Confession thread is unavailable.")
                return

            try:
                reply_msg = await reply_channel.send(content=reply_content, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                await self.cog._safe_ephemeral(interaction, "Failed to post reply (missing perms?).")
                return

            upsert_thread_post(
                db_path, guild_id=interaction.guild.id, message_id=reply_msg.id,
                channel_id=reply_channel.id, root_message_id=root_message_id,
                original_author_id=interaction.user.id, notify_original_author=my_notify_pref,
            )
            if parent_author_id > 0 and parent_author_id != interaction.user.id and parent_notify_pref:
                await self.cog.notify_original_poster(
                    guild=interaction.guild, original_author_id=parent_author_id,
                    reply_channel_id=reply_channel.id, reply_message_id=reply_msg.id,
                    root_message_id=root_message_id,
                    confession_channel_id=reply_channel.parent_id or cfg.dest_channel_id,
                )
            parent_channel_id = reply_channel.parent_id or cfg.dest_channel_id
            await log_reply(
                log_channel=log_channel, author=interaction.user, guild_id=interaction.guild.id,
                parent_channel_id=parent_channel_id, parent_message_id=self.parent_message_id,
                reply_channel_id=reply_channel.id, reply_message_id=reply_msg.id, content=content,
            )
            await self.cog.refresh_confess_launcher(interaction.guild.id, trigger_channel_id=parent_channel_id)
            await self.cog._safe_complete(interaction)
            return

        dest_channel = interaction.guild.get_channel(self.parent_channel_id)
        if not isinstance(dest_channel, discord.TextChannel):
            await self.cog._safe_ephemeral(interaction, "Bot config is invalid.")
            return
        try:
            parent_msg = await dest_channel.fetch_message(self.parent_message_id)
        except discord.NotFound:
            await self.cog._safe_ephemeral(interaction, "That message no longer exists.")
            return
        except discord.HTTPException:
            await self.cog._safe_ephemeral(interaction, "Couldn't load that message.")
            return

        try:
            reply_msg = await dest_channel.send(
                content=reply_content, reference=parent_msg,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await self.cog._safe_ephemeral(interaction, "Failed to post reply (missing perms?).")
            return

        upsert_thread_post(
            db_path, guild_id=interaction.guild.id, message_id=reply_msg.id,
            channel_id=dest_channel.id, root_message_id=root_message_id,
            original_author_id=interaction.user.id, notify_original_author=my_notify_pref,
        )
        if parent_author_id > 0 and parent_author_id != interaction.user.id and parent_notify_pref:
            await self.cog.notify_original_poster(
                guild=interaction.guild, original_author_id=parent_author_id,
                reply_channel_id=dest_channel.id, reply_message_id=reply_msg.id,
                root_message_id=root_message_id,
            )
        await log_reply(
            log_channel=log_channel, author=interaction.user, guild_id=interaction.guild.id,
            parent_channel_id=dest_channel.id, parent_message_id=parent_msg.id,
            reply_channel_id=dest_channel.id, reply_message_id=reply_msg.id, content=content,
        )
        await self.cog.refresh_confess_launcher(interaction.guild.id, trigger_channel_id=dest_channel.id)
        await self.cog._safe_complete(interaction)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class ConfessionsCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self._launcher_locks: dict[int, asyncio.Lock] = {}
        super().__init__()

    async def cog_load(self) -> None:
        init_db(self.ctx.db_path)
        self._cleanup_loop.start()

    async def cog_unload(self) -> None:
        self._cleanup_loop.cancel()

    # ── Periodic cleanup ─────────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def _cleanup_loop(self) -> None:
        try:
            purge_old_thread_posts(self.ctx.db_path)
        except Exception:
            log.exception("Error during confession thread purge")

    @_cleanup_loop.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # ── Launcher helpers ─────────────────────────────────────────────────────

    def _get_launcher_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._launcher_locks:
            self._launcher_locks[guild_id] = asyncio.Lock()
        return self._launcher_locks[guild_id]

    @staticmethod
    def _message_has_confess_launcher(message: discord.Message, guild_id: int) -> bool:
        target_id = f"nc|{guild_id}"
        return any(
            getattr(child, "custom_id", None) == target_id
            for row in message.components
            for child in getattr(row, "children", [])
        )

    async def _cleanup_duplicate_launchers(
        self, channel: discord.TextChannel, guild_id: int, *, keep_message_id: int
    ) -> None:
        if not self.bot.user:
            return
        try:
            async for msg in channel.history(limit=50):
                if msg.id == keep_message_id or msg.author.id != self.bot.user.id:
                    continue
                if not self._message_has_confess_launcher(msg, guild_id):
                    continue
                try:
                    await msg.delete()
                except discord.HTTPException:
                    continue
        except discord.HTTPException:
            pass

    async def _send_confess_launcher(self, channel: discord.TextChannel) -> Optional[discord.Message]:
        try:
            return await channel.send(
                view=self.build_confess_launcher_view(channel.guild.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            return None

    async def refresh_confess_launcher(
        self, guild_id: int, *, trigger_channel_id: Optional[int] = None
    ) -> None:
        async with self._get_launcher_lock(guild_id):
            cfg = get_config(self.ctx.db_path, guild_id)
            if not cfg or not cfg.launcher_channel_id:
                return
            if trigger_channel_id is not None and trigger_channel_id != cfg.launcher_channel_id:
                return
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            channel = guild.get_channel(cfg.launcher_channel_id)
            if not isinstance(channel, discord.TextChannel):
                return
            if cfg.launcher_message_id:
                try:
                    old = await channel.fetch_message(cfg.launcher_message_id)
                    await old.delete()
                except discord.HTTPException:
                    pass
            sent = await self._send_confess_launcher(channel)
            if sent is None:
                return
            cfg.launcher_channel_id = channel.id
            cfg.launcher_message_id = sent.id
            upsert_config(self.ctx.db_path, cfg)
            await self._cleanup_duplicate_launchers(channel, guild_id, keep_message_id=sent.id)

    # ── View builders ────────────────────────────────────────────────────────

    @staticmethod
    def build_reply_button_view(root_message_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Anonymously Reply",
            style=discord.ButtonStyle.secondary,
            custom_id=f"cr|{root_message_id}",
        ))
        return view

    @staticmethod
    def build_confess_launcher_view(guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Confess",
            style=discord.ButtonStyle.primary,
            custom_id=f"nc|{guild_id}",
        ))
        return view

    # ── DM notification ──────────────────────────────────────────────────────

    async def notify_original_poster(
        self,
        *,
        guild: discord.Guild,
        original_author_id: int,
        reply_channel_id: int,
        reply_message_id: int,
        root_message_id: int,
        confession_channel_id: Optional[int] = None,
    ) -> None:
        user = guild.get_member(original_author_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(original_author_id)
            except discord.HTTPException:
                return
        if user is None:
            return
        confession_ch = confession_channel_id or reply_channel_id
        text = (
            f"Someone replied to your anonymous confession in **{guild.name}**.\n"
            f"Reply: {jump_link(guild.id, reply_channel_id, reply_message_id)}\n"
            f"Confession: {jump_link(guild.id, confession_ch, root_message_id)}"
        )
        try:
            await user.send(text, allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── Interaction helpers ──────────────────────────────────────────────────

    async def _safe_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            pass

    async def _safe_complete(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            try:
                await interaction.delete_original_response()
            except Exception:
                pass

    def is_valid_reply_target_message(self, guild_id: int, msg: discord.Message) -> bool:
        if not self.bot.user or msg.author.id != self.bot.user.id:
            return False
        if get_thread_info(self.ctx.db_path, guild_id, msg.id):
            return True
        return any(
            isinstance(_cid := getattr(child, "custom_id", None), str) and _cid.startswith("cr|")
            for row in msg.components
            for child in getattr(row, "children", [])
        )

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _on_message_launcher_bump(self, message: discord.Message) -> None:
        if not message.guild or not self.bot.user or message.author.bot:
            return
        cfg = get_config(self.ctx.db_path, message.guild.id)
        if (
            cfg
            and cfg.launcher_channel_id
            and cfg.launcher_message_id
            and message.channel.id == cfg.launcher_channel_id
            and message.id != cfg.launcher_message_id
        ):
            await self.refresh_confess_launcher(message.guild.id, trigger_channel_id=message.channel.id)

    @commands.Cog.listener("on_interaction")
    async def _on_interaction_buttons(self, interaction: discord.Interaction) -> None:
        custom_id: Optional[str] = None
        action = "interaction"
        try:
            if interaction.type != discord.InteractionType.component:
                return
            if not interaction.data or not isinstance(interaction.data, dict):
                return
            custom_id = interaction.data.get("custom_id")
            if not isinstance(custom_id, str):
                return

            if custom_id.startswith("nc|"):
                action = "new confession"
                parts = custom_id.split("|")
                if len(parts) != 2 or not parts[1].isdigit():
                    await self._safe_ephemeral(interaction, "Invalid confession button.")
                    return
                if not interaction.guild or interaction.guild.id != int(parts[1]):
                    await self._safe_ephemeral(interaction, "Invalid confession button.")
                    return
                if not interaction.response.is_done():
                    await interaction.response.send_modal(ConfessModal(self))
                return

            if custom_id != "cr" and not custom_id.startswith("cr|"):
                return
            action = "anonymous reply"

            if not interaction.guild:
                await self._safe_ephemeral(interaction, "Invalid reply target.")
                return

            cfg = get_config(self.ctx.db_path, interaction.guild.id)
            if not cfg:
                await self._safe_ephemeral(interaction, "Bot is not configured.")
                return
            if cfg.panic:
                await self._safe_ephemeral(interaction, ERROR_PANIC_MODE)
                return
            if not cfg.replies_enabled:
                await self._safe_ephemeral(interaction, ERROR_REPLIES_DISABLED)
                return
            if interaction.user and interaction.user.id in cfg.blocked_set():
                await self._safe_ephemeral(interaction, "You can't submit anonymous replies on this server.")
                return

            if custom_id.startswith("cr|"):
                parts = custom_id.split("|")
                if len(parts) != 2 or not parts[1].isdigit():
                    await self._safe_ephemeral(interaction, "Invalid reply button.")
                    return
                root_message_id = int(parts[1])
                if not get_thread_info(self.ctx.db_path, interaction.guild.id, root_message_id):
                    await self._safe_ephemeral(interaction, "This confession can no longer be replied to.")
                    return
                discord_thread_id = get_discord_thread_id(self.ctx.db_path, interaction.guild.id, root_message_id)
                if not interaction.response.is_done():
                    await interaction.response.send_modal(
                        ReplyModal(
                            self, cfg,
                            parent_channel_id=cfg.dest_channel_id,
                            parent_message_id=root_message_id,
                            thread_id=discord_thread_id,
                        )
                    )
                return

            # Legacy plain "cr" button
            target_msg = interaction.message
            if target_msg is None:
                await self._safe_ephemeral(interaction, "That message no longer exists.")
                return
            target_channel = target_msg.channel
            if not isinstance(target_channel, discord.TextChannel):
                await self._safe_ephemeral(interaction, "That message no longer exists.")
                return
            if not self.is_valid_reply_target_message(interaction.guild.id, target_msg):
                await self._safe_ephemeral(interaction, "This message can't be replied to anonymously.")
                return
            if not interaction.response.is_done():
                await interaction.response.send_modal(
                    ReplyModal(self, cfg, parent_channel_id=target_channel.id, parent_message_id=target_msg.id)
                )

        except discord.Forbidden:
            log.exception(
                "Missing access during %s (custom_id=%r guild=%r user=%r)",
                action, custom_id, interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await self._safe_ephemeral(interaction, "I don't have enough access to handle that action.")
        except discord.HTTPException as exc:
            if exc.code in (40060, 10062):
                log.debug("Stale interaction during %s (code=%r)", action, exc.code)
                return
            log.exception(
                "HTTP error during %s (custom_id=%r guild=%r user=%r)",
                action, custom_id, interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await self._safe_ephemeral(interaction, "Discord rejected that interaction. Please try again.")
        except Exception:
            log.exception(
                "Unexpected error during %s (custom_id=%r guild=%r user=%r)",
                action, custom_id, interaction.guild_id,
                interaction.user.id if interaction.user else None,
            )
            await self._safe_ephemeral(interaction, f"Something went wrong handling that {action}.")

    # ── User commands ────────────────────────────────────────────────────────

    @app_commands.command(name="confess", description="Open the anonymous confession form.")
    @app_commands.guild_only()
    async def confess(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ConfessModal(self))

    @app_commands.command(name="dmrequest", description="Send moderators a private DM request.")
    @app_commands.guild_only()
    async def dmrequest(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(DMRequestModal(self))

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception(
            "Confession command error (guild=%r user=%r)",
            interaction.guild_id,
            interaction.user.id if interaction.user else None,
            exc_info=error,
        )
        await self._safe_ephemeral(interaction, "An unexpected error occurred. Please try again.")

    # ── Web panel action ─────────────────────────────────────────────────────

    async def web_post_launcher(self, guild_id: int, channel_id: int) -> bool:
        """Post or move the confession button to the given channel. Called from the web panel."""
        async with self._get_launcher_lock(guild_id):
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return False
            target_channel = guild.get_channel(channel_id)
            if not isinstance(target_channel, discord.TextChannel):
                return False
            cfg = get_config(self.ctx.db_path, guild_id)
            if cfg is None:
                return False
            if cfg.launcher_channel_id and cfg.launcher_message_id:
                old_ch = guild.get_channel(cfg.launcher_channel_id)
                if isinstance(old_ch, discord.TextChannel):
                    try:
                        old_msg = await old_ch.fetch_message(cfg.launcher_message_id)
                        await old_msg.delete()
                    except discord.HTTPException:
                        pass
            sent = await self._send_confess_launcher(target_channel)
            if sent is None:
                return False
            cfg.launcher_channel_id = target_channel.id
            cfg.launcher_message_id = sent.id
            upsert_config(self.ctx.db_path, cfg)
            await self._cleanup_duplicate_launchers(target_channel, guild_id, keep_message_id=sent.id)
            return True


async def setup(bot: Bot) -> None:
    await bot.add_cog(ConfessionsCog(bot, bot.ctx))
