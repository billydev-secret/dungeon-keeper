"""Anonymous confessions cog — ported from openConfess."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_modules.core.branding import resolve_accent_color
from bot_modules.confessions.logic import (
    HELP_TEXT,
    build_dm_notification_text,
    compute_confession_max_chars,
    compute_reply_cooldown,
    compute_reply_max_chars,
    is_op_reply,
    is_stale_interaction_error_code,
    message_exposes_reply_buttons,
    message_has_confess_launcher,
    parse_button_custom_id,
    parse_notify_pref,
    resolve_thread_root_info,
    should_notify_op,
)
from bot_modules.services.confessions_service import (
    ERROR_NOT_CONFIGURED,
    ERROR_PANIC_MODE,
    ERROR_REPLIES_DISABLED,
    ERROR_USER_BLOCKED,
    anon_circle_from_index,
    anon_name_from_index,
    build_anon_reply,
    build_confession_embed,
    check_and_bump_limits,
    get_config,
    get_discord_thread_id,
    get_ephemeral_anon_identity,
    get_or_assign_anon_identity,
    get_thread_info,
    init_db,
    log_confession,
    log_reply,
    purge_old_thread_posts,
    thread_name_from_content,
    update_discord_thread_id,
    upsert_config,
    upsert_thread_post,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot
    from bot_modules.services.confessions_service import GuildConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


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
        pref_parsed = parse_notify_pref(self.notify_pref.value)
        if pref_parsed is None:
            await self.cog._safe_ephemeral(interaction, "Invalid notify setting. Use `yes` or `no`.")
            return
        ping_pref = pref_parsed

        if not content:
            await self.cog._safe_ephemeral(interaction, "Confession can't be empty.")
            return

        confession_max_chars = compute_confession_max_chars(cfg.max_chars)
        if len(content) > confession_max_chars:
            await self.cog._safe_ephemeral(
                interaction, f"That's too long (max **{confession_max_chars}** characters for this confession format)."
            )
            return

        dest_channel = interaction.guild.get_channel(cfg.dest_channel_id)
        log_channel = interaction.guild.get_channel(cfg.log_channel_id)
        if not isinstance(dest_channel, (discord.TextChannel, discord.ForumChannel)):
            await self.cog._safe_ephemeral(interaction, "Bot config is invalid (missing destination channel).")
            return

        ok, msg = check_and_bump_limits(
            db_path, interaction.guild.id, interaction.user.id,
            is_reply=False, cooldown_seconds=cfg.cooldown_seconds, per_day_limit=cfg.per_day_limit,
        )
        if not ok:
            await self.cog._safe_ephemeral(interaction, msg)
            return

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return

        accent = await resolve_accent_color(self.cog.ctx.db_path, interaction.guild)
        confession_embed = build_confession_embed(content, colour=accent)

        if isinstance(dest_channel, discord.ForumChannel):
            tag_kwargs: dict = {}
            if dest_channel.flags.require_tag and dest_channel.available_tags:
                tag_kwargs["applied_tags"] = [dest_channel.available_tags[0]]
            try:
                forum_result = await dest_channel.create_thread(
                    name=thread_name_from_content(content),
                    embed=confession_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                    auto_archive_duration=10080,
                    **tag_kwargs,
                )
            except discord.HTTPException:
                await self.cog._safe_ephemeral(interaction, "Failed to post confession (missing perms?).")
                return
            forum_thread = forum_result.thread
            root_message_id = forum_thread.id
            if isinstance(log_channel, discord.TextChannel):
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
                embed=confession_embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            await self.cog._safe_ephemeral(interaction, "Failed to post confession (missing perms?).")
            return

        if isinstance(log_channel, discord.TextChannel):
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
        placeholder="Logged by admins for moderation.\nReply kindly. Keep it about the content, not the person.",
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
        ephemeral: bool = False,
    ) -> None:
        super().__init__()
        self.cog = cog
        self.cfg = cfg
        self.parent_channel_id = parent_channel_id
        self.parent_message_id = parent_message_id
        self.thread_id = thread_id
        self.ephemeral = ephemeral

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
        pref_parsed = parse_notify_pref(self.notify_pref.value)
        if pref_parsed is None:
            await self.cog._safe_ephemeral(interaction, "Invalid notify setting. Use `yes` or `no`.")
            return
        my_notify_pref = 1 if pref_parsed else 0

        if not content:
            await self.cog._safe_ephemeral(interaction, "Reply can't be empty.")
            return
        reply_max_chars = compute_reply_max_chars(cfg.max_chars)
        if len(content) > reply_max_chars:
            await self.cog._safe_ephemeral(
                interaction, f"That's too long (max **{reply_max_chars}** characters for replies)."
            )
            return

        log_channel = interaction.guild.get_channel(cfg.log_channel_id)

        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except discord.HTTPException:
            return

        reply_cooldown = compute_reply_cooldown(cfg.cooldown_seconds)
        ok, msg = check_and_bump_limits(
            db_path, interaction.guild.id, interaction.user.id,
            is_reply=True, cooldown_seconds=reply_cooldown, per_day_limit=0,
        )
        if not ok:
            await self.cog._safe_ephemeral(interaction, msg)
            return

        thread_info = get_thread_info(db_path, interaction.guild.id, self.parent_message_id)
        root_info = resolve_thread_root_info(
            thread_info,
            fallback_parent_message_id=self.parent_message_id,
            fallback_notify_op_on_reply=cfg.notify_op_on_reply,
        )
        root_message_id = root_info.root_message_id
        parent_author_id = root_info.parent_author_id
        parent_notify_pref = root_info.parent_notify_pref

        is_op = is_op_reply(
            ephemeral=self.ephemeral,
            parent_author_id=parent_author_id,
            replier_id=interaction.user.id,
        )
        circle = None
        anon_name = None
        if not is_op:
            if self.ephemeral:
                name_idx, emoji_idx = get_ephemeral_anon_identity(
                    db_path, interaction.guild.id, root_message_id
                )
            else:
                name_idx, emoji_idx = get_or_assign_anon_identity(
                    db_path, interaction.guild.id, root_message_id, interaction.user.id
                )
            circle = anon_circle_from_index(emoji_idx)
            anon_name = anon_name_from_index(name_idx)
        reply_content = build_anon_reply(content, is_op=is_op, circle=circle, anon_name=anon_name)

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
            if reply_channel.locked:
                await self.cog._safe_ephemeral(interaction, "This confession thread is locked.")
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
            if should_notify_op(
                parent_author_id=parent_author_id,
                replier_id=interaction.user.id,
                parent_notify_pref=parent_notify_pref,
            ):
                await self.cog.notify_original_poster(
                    guild=interaction.guild, original_author_id=parent_author_id,
                    reply_channel_id=reply_channel.id, reply_message_id=reply_msg.id,
                    root_message_id=root_message_id,
                    confession_channel_id=reply_channel.parent_id or cfg.dest_channel_id,
                )
            parent_channel_id = reply_channel.parent_id or cfg.dest_channel_id
            if isinstance(log_channel, discord.TextChannel):
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
        if should_notify_op(
            parent_author_id=parent_author_id,
            replier_id=interaction.user.id,
            parent_notify_pref=parent_notify_pref,
        ):
            await self.cog.notify_original_poster(
                guild=interaction.guild, original_author_id=parent_author_id,
                reply_channel_id=dest_channel.id, reply_message_id=reply_msg.id,
                root_message_id=root_message_id,
            )
        if isinstance(log_channel, discord.TextChannel):
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
        return message_has_confess_launcher(message.components, guild_id)

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
                    last = [m async for m in channel.history(limit=1)]
                    if last and last[0].id == cfg.launcher_message_id:
                        return  # already the most recent message, nothing to do
                except discord.HTTPException:
                    pass
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
            label="🎭 Reply Anonymously",
            style=discord.ButtonStyle.secondary,
            custom_id=f"cr|{root_message_id}",
        ))
        view.add_item(discord.ui.Button(
            label="🎲 Reply as Someone New",
            style=discord.ButtonStyle.secondary,
            custom_id=f"crn|{root_message_id}",
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
        view.add_item(discord.ui.Button(
            label="❓ What's this?",
            style=discord.ButtonStyle.secondary,
            custom_id=f"crh|{guild_id}",
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
        text = build_dm_notification_text(
            guild_name=guild.name,
            guild_id=guild.id,
            reply_channel_id=reply_channel_id,
            reply_message_id=reply_message_id,
            confession_channel_id=confession_ch,
            root_message_id=root_message_id,
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
        except discord.HTTPException:
            pass

    async def _safe_complete(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass

    def is_valid_reply_target_message(self, guild_id: int, msg: discord.Message) -> bool:
        if not self.bot.user or msg.author.id != self.bot.user.id:
            return False
        if get_thread_info(self.ctx.db_path, guild_id, msg.id):
            return True
        return message_exposes_reply_buttons(msg.components)

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

            decoded = parse_button_custom_id(custom_id)
            if decoded.kind == "ignore":
                return

            if decoded.kind == "new_confession":
                action = "new confession"
                if not interaction.guild or interaction.guild.id != decoded.guild_id:
                    await self._safe_ephemeral(interaction, "Invalid confession button.")
                    return
                if not interaction.response.is_done():
                    await interaction.response.send_modal(ConfessModal(self))
                return

            action = "anonymous reply"

            if decoded.kind == "invalid":
                assert decoded.error is not None
                await self._safe_ephemeral(interaction, decoded.error)
                return

            if not interaction.guild:
                await self._safe_ephemeral(interaction, "Invalid reply target.")
                return

            if decoded.kind == "reply_help":
                action = "help request"
                await self._safe_ephemeral(interaction, HELP_TEXT)
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

            if decoded.kind in ("reply", "reply_new"):
                assert decoded.root_id is not None
                ephemeral_identity = decoded.kind == "reply_new"
                if ephemeral_identity:
                    action = "ephemeral anonymous reply"
                root_message_id = decoded.root_id
                if not get_thread_info(self.ctx.db_path, interaction.guild.id, root_message_id):
                    await self._safe_ephemeral(interaction, "This confession can no longer be replied to.")
                    return
                discord_thread_id = get_discord_thread_id(self.ctx.db_path, interaction.guild.id, root_message_id)
                if discord_thread_id:
                    thread_obj = self.bot.get_channel(discord_thread_id)
                    if isinstance(thread_obj, discord.Thread) and thread_obj.locked:
                        await self._safe_ephemeral(interaction, "This confession thread is locked.")
                        return
                if not interaction.response.is_done():
                    await interaction.response.send_modal(
                        ReplyModal(
                            self, cfg,
                            parent_channel_id=cfg.dest_channel_id,
                            parent_message_id=root_message_id,
                            thread_id=discord_thread_id,
                            ephemeral=ephemeral_identity,
                        )
                    )
                return

            # decoded.kind == "legacy_reply" — plain "cr" button on old posts
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
            if is_stale_interaction_error_code(exc.code):
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
