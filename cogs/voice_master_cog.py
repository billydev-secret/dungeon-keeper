"""Voice Master — member-owned voice channels created by joining a Hub."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.voice_master_commands import (
    PANEL_BUTTON_CLASSES,
    _apply_hide,
    _apply_invite,
    _apply_kick,
    _apply_limit,
    _apply_lock,
    _apply_rename,
    _apply_reset,
    _apply_transfer,
    _ephemeral,
    _gate_and_record_edit,
    _resolve_owned_channel,
    post_inline_panel,
    post_knock_request,
    post_panel,
)
from services.moderation import write_audit
from services.voice_master_service import (
    CATEGORY_CHANNEL_CAP,
    DEFAULT_NAME_TEMPLATE,
    VoiceMasterConfig,
    VoiceProfile,
    active_channel_count,
    add_blocked,
    add_trusted,
    compute_reconciliation_actions,
    default_profile,
    delete_active_channel,
    get_active_channel,
    insert_active_channel,
    list_active_channels,
    list_blocked,
    list_name_blocklist,
    list_trusted,
    load_profile,
    load_voice_master_config,
    remove_blocked,
    remove_trusted,
    resolve_channel_name,
    save_profile,
    set_owner,
    set_owner_left_at,
    set_voice_master_config_value,
    try_dm,
)
from services.voice_master_service import (
    add_name_blocklist,
    delete_profile,
    remove_member_from_all_lists,
    remove_name_blocklist,
    trusted_prune_loop,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.voice_master")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_only(ctx: AppContext, interaction: discord.Interaction) -> bool:
    """True if the interaction's user passes the admin gate."""
    return ctx.is_admin(interaction)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------


class VoiceMasterCog(commands.Cog):
    voice = app_commands.Group(
        name="voice", description="Manage your personal voice channel."
    )
    voice_trusted = app_commands.Group(
        name="trusted", description="Manage your saved trust list.", parent=voice
    )
    voice_blocked = app_commands.Group(
        name="blocked", description="Manage your saved block list.", parent=voice
    )
    voice_profile = app_commands.Group(
        name="profile", description="Inspect or reset your saved channel profile.", parent=voice
    )
    voice_admin = app_commands.Group(
        name="voice-admin",
        description="Voice Master admin configuration.",
        default_permissions=discord.Permissions(administrator=True),
    )
    voice_admin_blocklist = app_commands.Group(
        name="name-blocklist",
        description="Manage the per-server channel-name blocklist.",
        parent=voice_admin,
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # owner_id → last hub-join wallclock (anti-spam create cooldown)
        self._last_create: dict[int, float] = {}
        # owner_id → asyncio.Lock to serialize their own Hub joins
        self._create_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # channel_id → pending empty-grace cleanup task
        self._empty_timers: dict[int, asyncio.Task] = {}
        super().__init__()

    async def cog_load(self) -> None:
        # Expose the AppContext to button DynamicItem callbacks (which only
        # see ``interaction.client``). Mirrors the jail cog's _mod_ctx pattern.
        setattr(self.bot, "_vm_ctx", self.ctx)
        # Register persistent button classes so they survive bot restarts.
        for cls in PANEL_BUTTON_CLASSES:
            self.bot.add_dynamic_items(cls)
        # Background prune loop: runs daily, only does work when the per-guild
        # threshold is configured (default 0 = never).
        self.bot.startup_task_factories.append(
            lambda: trusted_prune_loop(self.bot, self.ctx.db_path)
        )
        # Reconciliation runs after the bot is connected; defer it onto the loop
        # so cog_load doesn't block bot startup.
        self.bot.loop.create_task(self._reconcile_state())

    async def _reconcile_state(self) -> None:
        """Resume tracked channels on startup; clean up empty/missing ones."""
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(self.ctx.guild_id)
        if guild is None:
            log.warning("voice_master: bot not in configured guild %s", self.ctx.guild_id)
            return

        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, self.ctx.guild_id)
            tracked = list_active_channels(conn, self.ctx.guild_id)

        present_ids: set[int] = set()
        with_humans: set[int] = set()
        for row in tracked:
            ch = guild.get_channel(row.channel_id)
            if ch is None:
                continue
            present_ids.add(row.channel_id)
            if isinstance(ch, discord.VoiceChannel):
                if any(not m.bot for m in ch.members):
                    with_humans.add(row.channel_id)

        category = guild.get_channel(cfg.category_id) if cfg.category_id else None
        category_voice_ids: set[int] = set()
        if isinstance(category, discord.CategoryChannel):
            category_voice_ids = {c.id for c in category.voice_channels}

        plan = compute_reconciliation_actions(
            tracked_channel_ids=[r.channel_id for r in tracked],
            present_channel_ids=present_ids,
            channels_with_humans=with_humans,
            category_voice_channel_ids=category_voice_ids,
            hub_channel_id=cfg.hub_channel_id,
        )

        # Delete empty Discord voice channels first (they're the slow path).
        for cid in plan.discord_to_delete:
            ch = guild.get_channel(cid)
            if isinstance(ch, discord.VoiceChannel):
                try:
                    await ch.delete(reason="Voice Master: empty on reconcile")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("voice_master: failed to delete channel %d", cid)

        if plan.db_to_delete:
            with self.ctx.open_db() as conn:
                for cid in plan.db_to_delete:
                    delete_active_channel(conn, cid)

        for cid in plan.orphan_warnings:
            log.warning(
                "voice_master: orphan voice channel %d in target category — leaving alone",
                cid,
            )

        if cfg.hub_channel_id and guild.get_channel(cfg.hub_channel_id) is None:
            log.error(
                "voice_master: configured Hub channel %d does not exist — feature disabled until reconfigured",
                cfg.hub_channel_id,
            )
        if cfg.category_id and category is None:
            log.error(
                "voice_master: configured target category %d does not exist — feature disabled until reconfigured",
                cfg.category_id,
            )

    # ── Member removed/banned cleanup ─────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_delete(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        if channel.guild.id != self.ctx.guild_id:
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, channel.guild.id)
            row = get_active_channel(conn, channel.id)
        # Tracked channel deleted out from under us — clean up DB.
        if row is not None:
            with self.ctx.open_db() as conn:
                delete_active_channel(conn, channel.id)
                write_audit(
                    conn,
                    guild_id=channel.guild.id,
                    action="vm_channel_delete",
                    actor_id=0,
                    target_id=row.owner_id,
                    extra={"channel_id": channel.id, "reason": "external_delete"},
                )
            return
        # Hub or target category deleted by an admin — feature is now broken
        # until it's reconfigured. Log loudly and post to mod log.
        if channel.id == cfg.hub_channel_id:
            log.error(
                "voice_master: Hub channel %d was deleted — feature disabled",
                channel.id,
            )
            await self._notify_admins(
                channel.guild,
                "⚠️ Voice Master Hub channel was deleted. "
                "Run `/voice-admin set-hub` to reconfigure.",
            )
        elif channel.id == cfg.category_id:
            log.error(
                "voice_master: target category %d was deleted — feature disabled",
                channel.id,
            )
            await self._notify_admins(
                channel.guild,
                "⚠️ Voice Master target category was deleted. "
                "Run `/voice-admin set-category` to reconfigure.",
            )
        elif channel.id == cfg.control_channel_id:
            log.warning(
                "voice_master: control channel %d was deleted — knock requests will fail",
                channel.id,
            )

    async def _notify_admins(self, guild: discord.Guild, content: str) -> None:
        if self.ctx.mod_channel_id == 0:
            return
        ch = guild.get_channel(self.ctx.mod_channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(content)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id != self.ctx.guild_id:
            return
        await self._cleanup_for_departed_member(member)

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, user: discord.User | discord.Member
    ) -> None:
        if guild.id != self.ctx.guild_id:
            return
        # Synthesize a "departed" cleanup using the user id; we don't need a
        # full Member object for the DB operations.
        await self._cleanup_for_departed_user(guild, user.id)

    async def _cleanup_for_departed_member(self, member: discord.Member) -> None:
        await self._cleanup_for_departed_user(member.guild, member.id)

    async def _cleanup_for_departed_user(
        self, guild: discord.Guild, user_id: int
    ) -> None:
        with self.ctx.open_db() as conn:
            owned_rows = conn.execute(
                "SELECT channel_id FROM voice_master_channels "
                "WHERE guild_id = ? AND owner_id = ?",
                (guild.id, user_id),
            ).fetchall()
        for row in owned_rows:
            cid = int(row["channel_id"])
            ch = guild.get_channel(cid)
            if not isinstance(ch, discord.VoiceChannel):
                with self.ctx.open_db() as conn:
                    delete_active_channel(conn, cid)
                continue
            humans = [m for m in ch.members if not m.bot]
            if humans:
                # Hand off to first non-bot human present.
                new_owner = humans[0]
                overwrite = ch.overwrites_for(new_owner)
                overwrite.connect = True
                overwrite.view_channel = True
                try:
                    await ch.set_permissions(
                        new_owner,
                        overwrite=overwrite,
                        reason="Voice Master: previous owner left server",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    log.exception(
                        "voice_master: failed to update perms for new owner of %d", cid
                    )
                with self.ctx.open_db() as conn:
                    set_owner(conn, cid, new_owner.id)
                    write_audit(
                        conn,
                        guild_id=guild.id,
                        action="vm_transfer",
                        actor_id=new_owner.id,
                        target_id=user_id,
                        extra={"channel_id": cid, "reason": "owner_left_server"},
                    )
            else:
                # Empty — delete it now.
                try:
                    await ch.delete(reason="Voice Master: owner left server, channel empty")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("voice_master: failed to delete %d", cid)
                with self.ctx.open_db() as conn:
                    delete_active_channel(conn, cid)
                    write_audit(
                        conn,
                        guild_id=guild.id,
                        action="vm_channel_delete",
                        actor_id=user_id,
                        extra={"channel_id": cid, "reason": "owner_left_server"},
                    )
        # Remove the departed member from every other owner's trust + block list.
        with self.ctx.open_db() as conn:
            n = remove_member_from_all_lists(conn, guild.id, user_id)
        if n:
            log.info(
                "voice_master: removed user %d from %d trust/block entries in guild %d",
                user_id, n, guild.id,
            )

    # ── Voice state listener ──────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if member.guild.id != self.ctx.guild_id:
            return

        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, self.ctx.guild_id)
        if cfg.hub_channel_id == 0:
            return  # feature unconfigured

        # A. Joined the Hub → spin up a new channel
        if after.channel is not None and after.channel.id == cfg.hub_channel_id:
            await self._handle_hub_join(member, cfg)

        # B. Left a tracked channel → maybe schedule cleanup, mark owner-left
        if before.channel is not None and (
            after.channel is None or after.channel.id != before.channel.id
        ):
            await self._handle_left_tracked(member, before.channel, cfg)

        # C. Joined a tracked channel → cancel pending cleanup, clear owner-left
        if after.channel is not None and (
            before.channel is None or before.channel.id != after.channel.id
        ):
            if after.channel.id != cfg.hub_channel_id:
                await self._handle_joined_tracked(member, after.channel)

    # ── Leave / cleanup branches ──────────────────────────────────────

    async def _handle_left_tracked(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
        cfg: VoiceMasterConfig,
    ) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
            if row is None:
                return
            if member.id == row.owner_id:
                set_owner_left_at(conn, channel.id, time.time())

        if not any(not m.bot for m in channel.members):
            self._schedule_empty_delete(channel, cfg.empty_grace_s)

    async def _handle_joined_tracked(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
            if row is None:
                return
            if member.id == row.owner_id and row.owner_left_at is not None:
                set_owner_left_at(conn, channel.id, None)
        self._cancel_empty_timer(channel.id)

    def _schedule_empty_delete(
        self, channel: discord.VoiceChannel, grace_s: int
    ) -> None:
        # Cancel any prior scheduled cleanup before queueing a fresh one.
        self._cancel_empty_timer(channel.id)
        task = self.bot.loop.create_task(
            self._delete_after_grace(channel, max(grace_s, 0))
        )
        self._empty_timers[channel.id] = task

    def _cancel_empty_timer(self, channel_id: int) -> None:
        task = self._empty_timers.pop(channel_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _delete_after_grace(
        self, channel: discord.VoiceChannel, grace_s: int
    ) -> None:
        try:
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        # Re-fetch to get current member state — the in-memory channel is the
        # source of truth via discord.py's voice cache.
        live = self.bot.get_channel(channel.id)
        if not isinstance(live, discord.VoiceChannel):
            with self.ctx.open_db() as conn:
                delete_active_channel(conn, channel.id)
            self._empty_timers.pop(channel.id, None)
            return
        if any(not m.bot for m in live.members):
            self._empty_timers.pop(channel.id, None)
            return  # someone returned during the grace period
        try:
            await live.delete(reason="Voice Master: empty after grace period")
        except (discord.Forbidden, discord.HTTPException):
            log.exception("voice_master: failed to delete empty channel %d", channel.id)
            return
        with self.ctx.open_db() as conn:
            delete_active_channel(conn, channel.id)
            write_audit(
                conn,
                guild_id=channel.guild.id,
                action="vm_channel_delete",
                actor_id=self.bot.user.id if self.bot.user else 0,
                extra={"channel_id": channel.id, "reason": "empty_grace"},
            )
        self._empty_timers.pop(channel.id, None)

    # ── Hub join → create channel ─────────────────────────────────────

    async def _handle_hub_join(
        self, member: discord.Member, cfg: VoiceMasterConfig
    ) -> None:
        guild = member.guild
        async with self._create_locks[member.id]:
            now = time.time()
            last = self._last_create.get(member.id, 0.0)
            if cfg.create_cooldown_s > 0 and (now - last) < cfg.create_cooldown_s:
                # Boot them out of the Hub silently — can't DM mid-event reliably.
                with self._suppress_voice_errors():
                    await member.move_to(None, reason="Voice Master: create cooldown")
                return
            self._last_create[member.id] = now

            with self.ctx.open_db() as conn:
                if active_channel_count(conn, guild.id, member.id) >= cfg.max_per_member:
                    with self._suppress_voice_errors():
                        await member.move_to(
                            None, reason="Voice Master: max channels reached"
                        )
                    return
                # Saves disabled? Treat every member as having no profile.
                if cfg.disable_saves:
                    profile = default_profile()
                    trusted_ids: list[int] = []
                    blocked_ids: list[int] = []
                else:
                    profile = (
                        load_profile(conn, guild.id, member.id) or default_profile()
                    )
                    trusted_ids = list_trusted(conn, guild.id, member.id)
                    blocked_ids = list_blocked(conn, guild.id, member.id)
                blocklist = list_name_blocklist(conn, guild.id)

            template = cfg.default_name_template or DEFAULT_NAME_TEMPLATE
            name, name_fell_back = resolve_channel_name(
                saved_name=profile.saved_name,
                template=template,
                display_name=member.display_name,
                username=member.name,
                blocklist_patterns=blocklist,
            )

            target_cat = guild.get_channel(cfg.category_id) if cfg.category_id else None
            if isinstance(target_cat, discord.CategoryChannel):
                if len(target_cat.channels) >= CATEGORY_CHANNEL_CAP:
                    log.warning(
                        "voice_master: target category %d at %d-channel cap — "
                        "creating outside category",
                        cfg.category_id, CATEGORY_CHANNEL_CAP,
                    )
                    target_cat = None
            else:
                target_cat = None

            limit = profile.saved_limit if profile.saved_limit > 0 else cfg.default_user_limit
            bitrate = profile.bitrate if profile.bitrate else cfg.default_bitrate

            overwrites, skipped_targets = self._build_initial_overwrites(
                guild=guild,
                profile=profile,
                trusted_ids=trusted_ids,
                blocked_ids=blocked_ids,
                owner=member,
            )

            create_kwargs: dict = {"name": name, "overwrites": overwrites}
            if isinstance(target_cat, discord.CategoryChannel):
                create_kwargs["category"] = target_cat
            if limit > 0:
                create_kwargs["user_limit"] = limit
            if bitrate > 0:
                create_kwargs["bitrate"] = bitrate

            try:
                channel = await guild.create_voice_channel(**create_kwargs)
            except discord.Forbidden:
                log.error("voice_master: missing Manage Channels permission")
                return
            except discord.HTTPException:
                log.exception("voice_master: failed to create channel for %s", member.id)
                return

            with self.ctx.open_db() as conn:
                insert_active_channel(
                    conn,
                    channel_id=channel.id,
                    guild_id=guild.id,
                    owner_id=member.id,
                    now=now,
                )
                skipped_payload: list[str] = []
                if name_fell_back:
                    skipped_payload.append("name")
                if skipped_targets:
                    skipped_payload.append("missing_members")
                write_audit(
                    conn,
                    guild_id=guild.id,
                    action="vm_channel_create",
                    actor_id=member.id,
                    extra={
                        "channel_id": channel.id,
                        "name": name,
                        "applied_skipped": skipped_payload,
                    },
                )

            try:
                await member.move_to(channel, reason="Voice Master: own channel ready")
            except (discord.Forbidden, discord.HTTPException):
                # Member disconnected before we could move them; the empty-grace
                # timer below will clean up the orphaned channel.
                log.info(
                    "voice_master: created channel %d but could not move %d in",
                    channel.id, member.id,
                )

            # Drop the control panel into the new channel's text chat so the
            # owner has the buttons right where they are. Non-fatal on failure
            # (perms missing, channel deleted out from under us, etc.).
            if cfg.post_inline_panel:
                await post_inline_panel(channel, member)

            # DM the owner about anything we had to skip.
            notes: list[str] = []
            if name_fell_back:
                notes.append(
                    f"Saved name was blocked by an admin filter — using `{name}` instead."
                )
            if skipped_targets:
                notes.append(
                    f"{len(skipped_targets)} member(s) on your trust/block list "
                    f"are no longer in this server and were skipped."
                )
            if notes:
                await try_dm(member, content="\n".join(notes))

            # Always arm the grace timer — if the move succeeded the channel
            # has the owner in it and the timer becomes a no-op; if it failed
            # the channel is empty and gets cleaned up after `empty_grace_s`.
            self._schedule_empty_delete(channel, cfg.empty_grace_s)

    @staticmethod
    def _build_initial_overwrites(
        *,
        guild: discord.Guild,
        profile: VoiceProfile,
        trusted_ids: list[int],
        blocked_ids: list[int],
        owner: discord.Member,
    ) -> tuple[
        dict[discord.Role | discord.Member, discord.PermissionOverwrite],
        list[int],
    ]:
        """Build create-time overwrites from a profile.

        Returns ``(overwrites, missing_member_ids)`` — missing members are
        ones in the trust/block list who are no longer in the server.
        """
        ow: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        everyone = guild.default_role
        ow[everyone] = discord.PermissionOverwrite(
            connect=False if profile.locked else None,
            view_channel=False if profile.hidden else None,
        )
        # Owner gets explicit access so locks/hides don't bite them.
        ow[owner] = discord.PermissionOverwrite(view_channel=True, connect=True)

        missing: list[int] = []
        for uid in trusted_ids:
            m = guild.get_member(uid)
            if m is None:
                missing.append(uid)
                continue
            ow[m] = discord.PermissionOverwrite(view_channel=True, connect=True)
        for uid in blocked_ids:
            m = guild.get_member(uid)
            if m is None:
                missing.append(uid)
                continue
            ow[m] = discord.PermissionOverwrite(connect=False)
        return ow, missing

    # Convenience suppress-context for ignored voice-related Discord errors.
    @staticmethod
    def _suppress_voice_errors():
        import contextlib
        return contextlib.suppress(discord.Forbidden, discord.HTTPException)

    # ── Owner slash commands ──────────────────────────────────────────

    @voice.command(name="lock", description="Lock your voice channel (denies @everyone Connect).")
    async def voice_lock(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_lock(interaction, channel, row, locked=True)

    @voice.command(name="unlock", description="Unlock your voice channel.")
    async def voice_unlock(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_lock(interaction, channel, row, locked=False)

    @voice.command(name="hide", description="Hide your voice channel from non-invited members.")
    async def voice_hide(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_hide(interaction, channel, row, hidden=True)

    @voice.command(name="unhide", description="Make your voice channel visible again.")
    async def voice_unhide(self, interaction: discord.Interaction) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_hide(interaction, channel, row, hidden=False)

    @voice.command(name="rename", description="Rename your voice channel.")
    @app_commands.describe(name="The new channel name (1–100 characters).")
    async def voice_rename(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 100],
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_rename(interaction, channel, row, new_name=name)

    @voice.command(name="limit", description="Set the user limit on your voice channel (0 = no cap).")
    @app_commands.describe(limit="0 to remove the cap, or 1–99.")
    async def voice_limit(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 0, 99],
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_limit(interaction, channel, row, new_limit=limit)

    @voice.command(name="invite", description="Grant a member access to your voice channel.")
    @app_commands.describe(
        member="The member to invite.",
        remember="Also add them to your trust list so future channels auto-grant them access.",
    )
    async def voice_invite(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        remember: bool = False,
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_invite(
            interaction, channel, row, target=member, remember=remember
        )

    @voice.command(name="kick", description="Remove a member from your voice channel.")
    @app_commands.describe(
        member="The member to kick.",
        remember="Also add them to your blocklist so they're auto-denied on future channels.",
    )
    async def voice_kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        remember: bool = False,
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_kick(
            interaction, channel, row, target=member, remember=remember
        )

    @voice.command(name="transfer", description="Transfer ownership to a member in your channel.")
    @app_commands.describe(member="They must currently be in the voice channel.")
    async def voice_transfer(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        await _apply_transfer(interaction, channel, row, new_owner=member)

    @voice.command(name="reset", description="Reset your channel's permissions. Optionally also reset your saved profile.")
    @app_commands.describe(
        also_profile="Also wipe your saved name/limit/lock/hide/trust/block lists.",
    )
    async def voice_reset(
        self, interaction: discord.Interaction, also_profile: bool = False
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        if not await _gate_and_record_edit(interaction, row):
            return
        await _apply_reset(interaction, channel, row, also_profile=also_profile)

    @voice.command(name="claim", description="Claim ownership of the channel you're in (if eligible).")
    async def voice_claim(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await _ephemeral(interaction, "You're not in a voice channel.")
            return
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "That isn't a managed voice channel.")
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, member.guild.id)
            row = get_active_channel(conn, channel.id)
        if row is None:
            await _ephemeral(interaction, "This channel isn't managed by Voice Master.")
            return
        if row.owner_id == member.id:
            await _ephemeral(interaction, "You already own this channel.")
            return
        # Eligibility: owner must be gone-from-server, OR owner_left_at must be
        # at least owner_grace_s ago.
        owner = member.guild.get_member(row.owner_id)
        eligible = False
        if owner is None:
            eligible = True
        elif row.owner_left_at is not None:
            elapsed = time.time() - row.owner_left_at
            if elapsed >= cfg.owner_grace_s:
                eligible = True
            else:
                wait = int(cfg.owner_grace_s - elapsed)
                await _ephemeral(
                    interaction,
                    f"The owner left {int(elapsed)}s ago — claim available in {wait}s.",
                )
                return
        else:
            await _ephemeral(
                interaction,
                "The owner is still active in or watching the channel.",
            )
            return

        if not eligible:
            return  # defensive

        # Defer because set_permissions can take >3s under load.
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=False)
            except discord.InteractionResponded:
                pass

        # Grant new owner explicit access.
        overwrite = channel.overwrites_for(member)
        overwrite.connect = True
        overwrite.view_channel = True
        try:
            await channel.set_permissions(
                member, overwrite=overwrite, reason="Voice Master: claim"
            )
        except (discord.Forbidden, discord.HTTPException):
            await _ephemeral(interaction, "Couldn't grant you ownership permissions.")
            return
        with self.ctx.open_db() as conn:
            set_owner(conn, channel.id, member.id)
            write_audit(
                conn,
                guild_id=member.guild.id,
                action="vm_claim",
                actor_id=member.id,
                target_id=row.owner_id,
                extra={"channel_id": channel.id},
            )
        await _ephemeral(interaction, "You're the new owner of this channel.")

    # ── Trust / block list management ─────────────────────────────────

    @voice_trusted.command(name="list", description="Show your saved trusted members.")
    async def trusted_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            ids = list_trusted(conn, interaction.guild.id, interaction.user.id)
        if not ids:
            await _ephemeral(interaction, "Your trust list is empty.")
            return
        rendered = ", ".join(f"<@{uid}>" for uid in ids)
        await _ephemeral(interaction, f"Trusted ({len(ids)}): {rendered}")

    @voice_trusted.command(name="add", description="Add a member to your trust list.")
    @app_commands.describe(member="They'll auto-get access to every future channel of yours.")
    async def trusted_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        if member.bot:
            await _ephemeral(interaction, "Can't trust bots.")
            return
        if member.id == interaction.user.id:
            await _ephemeral(interaction, "You're always trusted by yourself.")
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, interaction.guild.id)
            if cfg.disable_saves or "trusted" not in cfg.saveable_fields:
                await _ephemeral(
                    interaction,
                    "Saving the trust list is disabled by an admin on this server.",
                )
                return
            added, evicted = add_trusted(
                conn,
                interaction.guild.id,
                interaction.user.id,
                member.id,
                cap=cfg.trust_cap,
            )
        if not added:
            await _ephemeral(interaction, f"{member.mention} is already on your trust list.")
            return
        msg = f"Added {member.mention} to your trust list."
        if evicted is not None:
            msg += f" (Cap reached — removed <@{evicted}>.)"
        await _ephemeral(interaction, msg)

    @voice_trusted.command(name="remove", description="Remove a member from your trust list.")
    async def trusted_remove(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            removed = remove_trusted(
                conn, interaction.guild.id, interaction.user.id, member.id
            )
        if removed:
            await _ephemeral(interaction, f"Removed {member.mention} from your trust list.")
        else:
            await _ephemeral(interaction, f"{member.mention} wasn't on your trust list.")

    @voice_blocked.command(name="list", description="Show your saved blocked members.")
    async def blocked_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            ids = list_blocked(conn, interaction.guild.id, interaction.user.id)
        if not ids:
            await _ephemeral(interaction, "Your blocklist is empty.")
            return
        rendered = ", ".join(f"<@{uid}>" for uid in ids)
        await _ephemeral(interaction, f"Blocked ({len(ids)}): {rendered}")

    @voice_blocked.command(name="add", description="Add a member to your blocklist.")
    @app_commands.describe(member="They'll be auto-denied access to every future channel of yours.")
    async def blocked_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        if member.bot:
            await _ephemeral(interaction, "Can't block bots.")
            return
        if member.id == interaction.user.id:
            await _ephemeral(interaction, "Can't block yourself.")
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, interaction.guild.id)
            if cfg.disable_saves or "blocked" not in cfg.saveable_fields:
                await _ephemeral(
                    interaction,
                    "Saving the blocklist is disabled by an admin on this server.",
                )
                return
            added, evicted = add_blocked(
                conn,
                interaction.guild.id,
                interaction.user.id,
                member.id,
                cap=cfg.block_cap,
            )
        if not added:
            await _ephemeral(interaction, f"{member.mention} is already on your blocklist.")
            return
        msg = f"Added {member.mention} to your blocklist."
        if evicted is not None:
            msg += f" (Cap reached — removed <@{evicted}>.)"
        await _ephemeral(interaction, msg)

    @voice_blocked.command(name="remove", description="Remove a member from your blocklist.")
    async def blocked_remove(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            removed = remove_blocked(
                conn, interaction.guild.id, interaction.user.id, member.id
            )
        if removed:
            await _ephemeral(interaction, f"Removed {member.mention} from your blocklist.")
        else:
            await _ephemeral(interaction, f"{member.mention} wasn't on your blocklist.")

    @voice.command(
        name="knock",
        description="Ask the owner of a locked voice channel to let you in.",
    )
    @app_commands.describe(channel="The voice channel you'd like to join.")
    async def voice_knock(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None:
            await _ephemeral(
                interaction, "That channel isn't managed by Voice Master."
            )
            return
        if row.owner_id == interaction.user.id:
            await _ephemeral(interaction, "You already own that channel.")
            return
        owner = interaction.guild.get_member(row.owner_id)
        if owner is None:
            await _ephemeral(
                interaction,
                "The owner isn't in this server right now — try `/voice claim` if eligible.",
            )
            return
        if not isinstance(interaction.user, discord.Member):
            return
        ok = await post_knock_request(
            self.ctx, channel=channel, requester=interaction.user, owner=owner
        )
        if ok:
            await _ephemeral(
                interaction,
                f"Knock sent to {owner.mention} — they'll respond in the control channel.",
            )
        else:
            await _ephemeral(
                interaction,
                "Couldn't deliver the knock — control channel is unconfigured or unavailable.",
            )

    # ── Profile inspection / reset ────────────────────────────────────

    @voice_profile.command(name="show", description="Show your saved channel profile.")
    async def profile_show(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            profile = load_profile(conn, interaction.guild.id, interaction.user.id) or default_profile()
            trusted = list_trusted(conn, interaction.guild.id, interaction.user.id)
            blocked = list_blocked(conn, interaction.guild.id, interaction.user.id)
        embed = discord.Embed(
            title="Your Voice Master profile",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Saved name", value=profile.saved_name or "*(template default)*", inline=False)
        embed.add_field(name="User limit", value=str(profile.saved_limit) if profile.saved_limit else "no cap", inline=True)
        embed.add_field(name="Locked", value="yes" if profile.locked else "no", inline=True)
        embed.add_field(name="Hidden", value="yes" if profile.hidden else "no", inline=True)
        embed.add_field(name="Trusted (count)", value=str(len(trusted)), inline=True)
        embed.add_field(name="Blocked (count)", value=str(len(blocked)), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @voice_profile.command(name="reset", description="Reset your saved profile (or one specific field).")
    @app_commands.describe(field="Which field to clear. Omit to clear everything.")
    @app_commands.choices(
        field=[
            app_commands.Choice(name="all (full reset)", value="all"),
            app_commands.Choice(name="name", value="name"),
            app_commands.Choice(name="limit", value="limit"),
            app_commands.Choice(name="locked", value="locked"),
            app_commands.Choice(name="hidden", value="hidden"),
            app_commands.Choice(name="trusted", value="trusted"),
            app_commands.Choice(name="blocked", value="blocked"),
        ]
    )
    async def profile_reset(
        self,
        interaction: discord.Interaction,
        field: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        target = field.value if field is not None else "all"
        gid = interaction.guild.id
        uid = interaction.user.id
        with self.ctx.open_db() as conn:
            if target == "all":
                delete_profile(conn, gid, uid)
                conn.execute(
                    "DELETE FROM voice_master_trusted WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
                conn.execute(
                    "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
                summary = "Saved profile, trust list, and blocklist cleared."
            elif target == "trusted":
                conn.execute(
                    "DELETE FROM voice_master_trusted WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
                summary = "Trust list cleared."
            elif target == "blocked":
                conn.execute(
                    "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
                summary = "Blocklist cleared."
            else:
                profile = load_profile(conn, gid, uid) or default_profile()
                if target == "name":
                    profile = VoiceProfile(
                        saved_name=None, saved_limit=profile.saved_limit,
                        locked=profile.locked, hidden=profile.hidden, bitrate=profile.bitrate,
                    )
                elif target == "limit":
                    profile = VoiceProfile(
                        saved_name=profile.saved_name, saved_limit=0,
                        locked=profile.locked, hidden=profile.hidden, bitrate=profile.bitrate,
                    )
                elif target == "locked":
                    profile = VoiceProfile(
                        saved_name=profile.saved_name, saved_limit=profile.saved_limit,
                        locked=False, hidden=profile.hidden, bitrate=profile.bitrate,
                    )
                elif target == "hidden":
                    profile = VoiceProfile(
                        saved_name=profile.saved_name, saved_limit=profile.saved_limit,
                        locked=profile.locked, hidden=False, bitrate=profile.bitrate,
                    )
                save_profile(conn, gid, uid, profile)
                summary = f"`{target}` cleared from your saved profile."
            write_audit(
                conn,
                guild_id=gid,
                action="vm_reset_profile",
                actor_id=uid,
                extra={"field": target},
            )
        await _ephemeral(interaction, summary)

    @voice.command(name="owner", description="Show who owns the voice channel you're in.")
    async def voice_owner(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await _ephemeral(interaction, "You're not in a voice channel.")
            return
        channel = member.voice.channel
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None:
            await _ephemeral(
                interaction,
                "This channel isn't managed by Voice Master.",
            )
            return
        owner = interaction.guild.get_member(row.owner_id) if interaction.guild else None
        if owner is None:
            await _ephemeral(
                interaction, f"Owner: <@{row.owner_id}> (no longer in the server)."
            )
            return
        await _ephemeral(interaction, f"Owner: {owner.mention}")

    # ── Admin: set channels ───────────────────────────────────────────

    @voice_admin.command(name="set-hub", description="Set the click-to-create Hub voice channel.")
    @app_commands.describe(channel="A voice channel members will join to spin up their own room.")
    async def set_hub(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn, self.ctx.guild_id, "voice_master_hub_channel_id", str(channel.id)
            )
        await interaction.response.send_message(
            f"Hub set to {channel.mention}.", ephemeral=True
        )

    @voice_admin.command(name="set-category", description="Set the category where created channels live.")
    @app_commands.describe(category="The target category for new voice channels.")
    async def set_category(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn, self.ctx.guild_id, "voice_master_category_id", str(category.id)
            )
        await interaction.response.send_message(
            f"Target category set to **{category.name}**.", ephemeral=True
        )

    @voice_admin.command(
        name="set-control-channel",
        description="Set the text channel where the panel and join requests go.",
    )
    @app_commands.describe(channel="A text channel for the persistent control panel.")
    async def set_control_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn,
                self.ctx.guild_id,
                "voice_master_control_channel_id",
                str(channel.id),
            )
        await interaction.response.send_message(
            f"Control channel set to {channel.mention}.", ephemeral=True
        )

    @voice_admin.command(
        name="post-panel",
        description="Post (or repost) the persistent owner-control panel in the configured control channel.",
    )
    async def post_panel_cmd(self, interaction: discord.Interaction) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, self.ctx.guild_id)
        if not cfg.control_channel_id:
            await interaction.response.send_message(
                "No control channel set. Run `/voice-admin set-control-channel` first.",
                ephemeral=True,
            )
            return
        channel = (
            interaction.guild.get_channel(cfg.control_channel_id)
            if interaction.guild
            else None
        )
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Configured control channel is missing or not a text channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        msg = await post_panel(self.ctx, channel)
        await interaction.followup.send(
            f"Panel posted: {msg.jump_url}", ephemeral=True
        )

    # ── Admin: numeric / template knobs (one command, key-driven) ─────

    @voice_admin.command(
        name="set-default-name",
        description="Set the default channel name template ({display_name}, {username}).",
    )
    async def set_default_name(
        self, interaction: discord.Interaction, template: str
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn,
                self.ctx.guild_id,
                "voice_master_default_name_template",
                template or DEFAULT_NAME_TEMPLATE,
            )
        await interaction.response.send_message(
            f"Default name template set to: `{template or DEFAULT_NAME_TEMPLATE}`",
            ephemeral=True,
        )

    @voice_admin.command(
        name="set-int",
        description="Set a numeric Voice Master config value.",
    )
    @app_commands.describe(
        key="Which numeric setting to change.",
        value="The new value (non-negative integer).",
    )
    @app_commands.choices(
        key=[
            app_commands.Choice(name="default-user-limit", value="voice_master_default_user_limit"),
            app_commands.Choice(name="default-bitrate", value="voice_master_default_bitrate"),
            app_commands.Choice(name="create-cooldown-s", value="voice_master_create_cooldown_s"),
            app_commands.Choice(name="max-per-member", value="voice_master_max_per_member"),
            app_commands.Choice(name="trust-cap", value="voice_master_trust_cap"),
            app_commands.Choice(name="block-cap", value="voice_master_block_cap"),
            app_commands.Choice(name="owner-grace-s", value="voice_master_owner_grace_s"),
            app_commands.Choice(name="empty-grace-s", value="voice_master_empty_grace_s"),
            app_commands.Choice(name="trusted-prune-days", value="voice_master_trusted_prune_days"),
        ]
    )
    async def set_int(
        self,
        interaction: discord.Interaction,
        key: app_commands.Choice[str],
        value: app_commands.Range[int, 0, 100000],
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn, self.ctx.guild_id, key.value, str(value)
            )
        await interaction.response.send_message(
            f"`{key.name}` set to **{value}**.", ephemeral=True
        )

    @voice_admin.command(
        name="post-inline-panel",
        description="Toggle whether the panel is auto-posted in each new channel's text chat.",
    )
    async def post_inline_panel_toggle(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn,
                self.ctx.guild_id,
                "voice_master_post_inline_panel",
                "1" if enabled else "0",
            )
        await interaction.response.send_message(
            f"Inline panel auto-post: **{'enabled' if enabled else 'disabled'}**.",
            ephemeral=True,
        )

    @voice_admin.command(
        name="disable-saves",
        description="Force every channel to use server defaults (ignore per-member profiles).",
    )
    async def disable_saves(
        self, interaction: discord.Interaction, enabled: bool
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn,
                self.ctx.guild_id,
                "voice_master_disable_saves",
                "1" if enabled else "0",
            )
        await interaction.response.send_message(
            f"Per-member profile saves: **{'disabled' if enabled else 'enabled'}**.",
            ephemeral=True,
        )

    @voice_admin.command(
        name="saveable-fields",
        description="Comma-separated list of fields owners may save (name,limit,locked,hidden,trusted,blocked).",
    )
    async def saveable_fields(
        self, interaction: discord.Interaction, fields: str
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        valid = {"name", "limit", "locked", "hidden", "trusted", "blocked"}
        chosen = {s.strip().lower() for s in fields.split(",") if s.strip()}
        if not chosen.issubset(valid):
            bad = ", ".join(sorted(chosen - valid))
            await interaction.response.send_message(
                f"Unknown field(s): {bad}. Valid: {', '.join(sorted(valid))}.",
                ephemeral=True,
            )
            return
        with self.ctx.open_db() as conn:
            set_voice_master_config_value(
                conn,
                self.ctx.guild_id,
                "voice_master_saveable_fields",
                ",".join(sorted(chosen)),
            )
        await interaction.response.send_message(
            f"Saveable fields: `{','.join(sorted(chosen))}`.", ephemeral=True
        )

    # ── Admin: name blocklist ─────────────────────────────────────────

    @voice_admin_blocklist.command(name="add", description="Add a substring to the channel-name blocklist.")
    async def name_blocklist_add(
        self, interaction: discord.Interaction, pattern: str
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        cleaned = pattern.strip().lower()
        if not cleaned:
            await interaction.response.send_message("Pattern can't be empty.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            added = add_name_blocklist(
                conn, self.ctx.guild_id, cleaned, interaction.user.id
            )
            write_audit(
                conn,
                guild_id=self.ctx.guild_id,
                action="vm_name_blocklist_add",
                actor_id=interaction.user.id,
                extra={"pattern": cleaned},
            )
        if added:
            await interaction.response.send_message(
                f"Added `{cleaned}` to the name blocklist.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{cleaned}` was already on the blocklist.", ephemeral=True
            )

    @voice_admin_blocklist.command(name="remove", description="Remove a substring from the blocklist.")
    async def name_blocklist_remove(
        self, interaction: discord.Interaction, pattern: str
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        cleaned = pattern.strip().lower()
        with self.ctx.open_db() as conn:
            removed = remove_name_blocklist(conn, self.ctx.guild_id, cleaned)
            if removed:
                write_audit(
                    conn,
                    guild_id=self.ctx.guild_id,
                    action="vm_name_blocklist_remove",
                    actor_id=interaction.user.id,
                    extra={"pattern": cleaned},
                )
        if removed:
            await interaction.response.send_message(
                f"Removed `{cleaned}` from the blocklist.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"`{cleaned}` wasn't on the blocklist.", ephemeral=True
            )

    @voice_admin_blocklist.command(name="list", description="List all blocked name patterns.")
    async def name_blocklist_list(self, interaction: discord.Interaction) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            patterns = list_name_blocklist(conn, self.ctx.guild_id)
        if not patterns:
            await interaction.response.send_message(
                "No patterns on the name blocklist.", ephemeral=True
            )
            return
        rendered = "\n".join(f"• `{p}`" for p in patterns)
        await interaction.response.send_message(
            f"Name blocklist ({len(patterns)}):\n{rendered}", ephemeral=True
        )

    # ── Admin: force overrides (all mod-log mirrored) ─────────────────

    async def _post_admin_audit_mirror(
        self, interaction: discord.Interaction, *, action: str, summary: str
    ) -> None:
        """Post a brief admin-action embed to the mod log channel."""
        if interaction.guild is None or self.ctx.mod_channel_id == 0:
            return
        log_channel = interaction.guild.get_channel(self.ctx.mod_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title=f"Voice Master · {action}",
            description=summary,
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"by {interaction.user} ({interaction.user.id})")
        try:
            await log_channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("voice_master: failed to mirror admin audit to mod-log")

    @voice_admin.command(
        name="force-delete",
        description="Force-delete a member-owned voice channel.",
    )
    async def force_delete(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None:
            await interaction.response.send_message(
                "That channel isn't managed by Voice Master.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            await channel.delete(reason=f"Voice Master: admin force-delete by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Couldn't delete that channel.", ephemeral=True
            )
            return
        with self.ctx.open_db() as conn:
            delete_active_channel(conn, channel.id)
            write_audit(
                conn,
                guild_id=channel.guild.id,
                action="vm_admin_force_delete",
                actor_id=interaction.user.id,
                target_id=row.owner_id,
                extra={"channel_id": channel.id},
            )
        await self._post_admin_audit_mirror(
            interaction,
            action="force-delete",
            summary=f"Deleted channel `{channel.name}` (id `{channel.id}`) owned by <@{row.owner_id}>.",
        )
        await interaction.followup.send(
            f"Deleted `{channel.name}`.", ephemeral=True
        )

    @voice_admin.command(
        name="force-transfer",
        description="Force-transfer ownership of a managed channel to another member.",
    )
    async def force_transfer(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        new_owner: discord.Member,
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if new_owner.bot:
            await interaction.response.send_message(
                "Can't transfer ownership to a bot.", ephemeral=True
            )
            return
        with self.ctx.open_db() as conn:
            row = get_active_channel(conn, channel.id)
        if row is None:
            await interaction.response.send_message(
                "That channel isn't managed by Voice Master.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=False)
        overwrite = channel.overwrites_for(new_owner)
        overwrite.connect = True
        overwrite.view_channel = True
        try:
            await channel.set_permissions(
                new_owner,
                overwrite=overwrite,
                reason=f"Voice Master: admin force-transfer by {interaction.user}",
            )
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "Couldn't update channel permissions.", ephemeral=True
            )
            return
        with self.ctx.open_db() as conn:
            set_owner(conn, channel.id, new_owner.id)
            write_audit(
                conn,
                guild_id=channel.guild.id,
                action="vm_admin_force_transfer",
                actor_id=interaction.user.id,
                target_id=row.owner_id,
                extra={"channel_id": channel.id, "new_owner_id": new_owner.id},
            )
        await self._post_admin_audit_mirror(
            interaction,
            action="force-transfer",
            summary=(
                f"Channel `{channel.name}` (id `{channel.id}`): "
                f"<@{row.owner_id}> → {new_owner.mention}."
            ),
        )
        await interaction.followup.send(
            f"Ownership of `{channel.name}` transferred to {new_owner.mention}.",
            ephemeral=True,
        )

    @voice_admin.command(
        name="force-clear-profile",
        description="Wipe a member's saved Voice Master profile (logged).",
    )
    async def force_clear_profile(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        gid = self.ctx.guild_id
        with self.ctx.open_db() as conn:
            delete_profile(conn, gid, member.id)
            conn.execute(
                "DELETE FROM voice_master_trusted WHERE guild_id = ? AND owner_id = ?",
                (gid, member.id),
            )
            conn.execute(
                "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
                (gid, member.id),
            )
            write_audit(
                conn,
                guild_id=gid,
                action="vm_admin_clear_profile",
                actor_id=interaction.user.id,
                target_id=member.id,
                extra={},
            )
        await self._post_admin_audit_mirror(
            interaction,
            action="force-clear-profile",
            summary=f"Cleared saved profile for {member.mention}.",
        )
        await interaction.response.send_message(
            f"Profile cleared for {member.mention}.", ephemeral=True
        )

    @voice_admin.command(
        name="view-profile",
        description="Inspect a member's saved Voice Master profile (logged).",
    )
    async def view_profile(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        gid = self.ctx.guild_id
        with self.ctx.open_db() as conn:
            profile = load_profile(conn, gid, member.id) or default_profile()
            trusted = list_trusted(conn, gid, member.id)
            blocked = list_blocked(conn, gid, member.id)
            write_audit(
                conn,
                guild_id=gid,
                action="vm_admin_view_profile",
                actor_id=interaction.user.id,
                target_id=member.id,
                extra={},
            )
        embed = discord.Embed(
            title=f"Voice Master profile · {member.display_name}",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Saved name", value=profile.saved_name or "*(default)*", inline=False)
        embed.add_field(name="User limit", value=str(profile.saved_limit) if profile.saved_limit else "no cap", inline=True)
        embed.add_field(name="Locked", value="yes" if profile.locked else "no", inline=True)
        embed.add_field(name="Hidden", value="yes" if profile.hidden else "no", inline=True)
        embed.add_field(
            name=f"Trusted ({len(trusted)})",
            value="\n".join(f"<@{u}>" for u in trusted) or "*(empty)*",
            inline=False,
        )
        embed.add_field(
            name=f"Blocked ({len(blocked)})",
            value="\n".join(f"<@{u}>" for u in blocked) or "*(empty)*",
            inline=False,
        )
        await self._post_admin_audit_mirror(
            interaction,
            action="view-profile",
            summary=f"Viewed saved profile of {member.mention}.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Admin: show ───────────────────────────────────────────────────

    @voice_admin.command(name="show", description="Show the current Voice Master configuration.")
    async def show(self, interaction: discord.Interaction) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, self.ctx.guild_id)

        guild = interaction.guild
        hub = guild.get_channel(cfg.hub_channel_id) if guild and cfg.hub_channel_id else None
        cat = guild.get_channel(cfg.category_id) if guild and cfg.category_id else None
        ctrl = guild.get_channel(cfg.control_channel_id) if guild and cfg.control_channel_id else None

        def _ref(c: object, fallback_id: int) -> str:
            if c is None:
                return f"`{fallback_id or 'unset'}`"
            mention = getattr(c, "mention", None)
            if mention:
                return mention
            return f"**{getattr(c, 'name', fallback_id)}**"

        embed = discord.Embed(
            title="Voice Master configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Hub", value=_ref(hub, cfg.hub_channel_id), inline=True)
        embed.add_field(name="Category", value=_ref(cat, cfg.category_id), inline=True)
        embed.add_field(
            name="Control channel", value=_ref(ctrl, cfg.control_channel_id), inline=True
        )
        embed.add_field(name="Name template", value=f"`{cfg.default_name_template}`", inline=False)
        embed.add_field(name="Default user limit", value=str(cfg.default_user_limit), inline=True)
        embed.add_field(name="Default bitrate", value=str(cfg.default_bitrate or "guild default"), inline=True)
        embed.add_field(name="Create cooldown (s)", value=str(cfg.create_cooldown_s), inline=True)
        embed.add_field(name="Max per member", value=str(cfg.max_per_member), inline=True)
        embed.add_field(name="Trust cap", value=str(cfg.trust_cap), inline=True)
        embed.add_field(name="Block cap", value=str(cfg.block_cap), inline=True)
        embed.add_field(name="Owner grace (s)", value=str(cfg.owner_grace_s), inline=True)
        embed.add_field(name="Empty grace (s)", value=str(cfg.empty_grace_s), inline=True)
        embed.add_field(
            name="Trusted prune (days)",
            value=str(cfg.trusted_prune_days) if cfg.trusted_prune_days else "never",
            inline=True,
        )
        embed.add_field(name="Disable saves", value="yes" if cfg.disable_saves else "no", inline=True)
        embed.add_field(name="Inline panel", value="yes" if cfg.post_inline_panel else "no", inline=True)
        embed.add_field(
            name="Saveable fields",
            value=", ".join(sorted(cfg.saveable_fields)),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(VoiceMasterCog(bot, bot.ctx))
