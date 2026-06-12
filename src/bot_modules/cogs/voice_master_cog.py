"""Voice Master — member-owned voice channels created by joining a Hub."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.voice_master_commands import (
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
from bot_modules.services.moderation import write_audit
from bot_modules.services.voice_master_service import (
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
    get_owned_channel,
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
    try_dm,
)
from bot_modules.services.voice_master_service import (
    delete_profile,
    remove_member_from_all_lists,
    trusted_prune_loop,
)
from bot_modules.voice_master.embeds import build_profile_show_embed
from bot_modules.voice_master.logic import (
    build_hub_join_notes,
    build_skipped_payload,
    classify_claim_attempt,
    format_block_add_result,
    format_blocked_list,
    format_trust_add_result,
    format_trusted_list,
    hub_create_blocked_by_cooldown,
    plan_initial_overwrites,
    profile_reset_summary,
    select_effective_bitrate,
    select_effective_limit,
    validate_block_add,
    validate_trust_add,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

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

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        # owner_id → last hub-join wallclock (anti-spam create cooldown)
        self._last_create: dict[int, float] = {}
        # owner_id → asyncio.Lock to serialize their own Hub joins
        self._create_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        # channel_id → pending empty-grace cleanup task
        self._empty_timers: dict[int, asyncio.Task] = {}
        # (guild_id, user_id) → pending self-disconnect task
        self._sleepkick_tasks: dict[tuple[int, int], asyncio.Task] = {}
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
        asyncio.create_task(self._reconcile_state())

    async def _reconcile_state(self) -> None:
        """Resume tracked channels on startup across all guilds."""
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            try:
                await self._reconcile_guild(guild)
            except Exception:
                log.exception(
                    "voice_master: reconcile failed for guild %s", guild.id
                )

    async def _reconcile_guild(self, guild: discord.Guild) -> None:
        """Resume tracked channels for one guild; clean up empty/missing ones."""
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, guild.id)
            tracked = list_active_channels(conn, guild.id)

        if not cfg.hub_channel_id and not tracked:
            return  # unconfigured and nothing tracked

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
        row = None
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, channel.guild.id)
            row = get_active_channel(conn, channel.id)
            if row is not None:
                delete_active_channel(conn, channel.id)
                write_audit(
                    conn,
                    guild_id=channel.guild.id,
                    action="vm_channel_delete",
                    actor_id=0,
                    target_id=row.owner_id,
                    extra={"channel_id": channel.id, "reason": "external_delete"},
                )
        # Tracked channel deleted out from under us — already cleaned up in the block above.
        if row is not None:
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
        mod_channel_id = self.ctx.guild_config(guild.id).mod_channel_id
        if mod_channel_id == 0:
            return
        ch = guild.get_channel(mod_channel_id)
        if isinstance(ch, discord.TextChannel):
            try:
                await ch.send(content)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        await self._cleanup_for_departed_member(member)

    @commands.Cog.listener()
    async def on_member_ban(
        self, guild: discord.Guild, user: discord.User | discord.Member
    ) -> None:
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

        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, member.guild.id)
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
            # If the member already owns a live channel, return them to it
            # rather than kicking them out of the Hub.
            with self.ctx.open_db() as conn:
                existing = get_owned_channel(conn, guild.id, member.id)
            if existing is not None:
                live = guild.get_channel(existing.channel_id)
                if isinstance(live, discord.VoiceChannel):
                    with self._suppress_voice_errors():
                        await member.move_to(
                            live,
                            reason="Voice Master: returning to existing channel",
                        )
                    return
                # Stale DB row — clean it up so the cap check below is accurate.
                with self.ctx.open_db() as conn:
                    delete_active_channel(conn, existing.channel_id)

            now = time.time()
            last = self._last_create.get(member.id, 0.0)
            if hub_create_blocked_by_cooldown(
                now=now, last_create_at=last, cooldown_s=cfg.create_cooldown_s
            ):
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

            limit = select_effective_limit(
                saved_limit=profile.saved_limit,
                default_user_limit=cfg.default_user_limit,
            )
            bitrate = select_effective_bitrate(
                saved_bitrate=profile.bitrate,
                default_bitrate=cfg.default_bitrate,
            )

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
                skipped_payload = build_skipped_payload(
                    name_fell_back=name_fell_back,
                    missing_target_count=len(skipped_targets),
                )
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
            notes_text = build_hub_join_notes(
                name_fell_back=name_fell_back,
                fallback_name=name,
                missing_target_count=len(skipped_targets),
            )
            if notes_text is not None:
                await try_dm(member, content=notes_text)

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

        The plan itself is computed by ``plan_initial_overwrites`` against
        a snapshot of the current member roster; this wrapper resolves
        each plan entry to a live ``discord.Role``/``discord.Member`` and
        builds the matching ``PermissionOverwrite``. Lookups bypass any
        plan entry that no longer resolves (rare race: member left between
        the plan and the wrapper).
        """
        present_ids: set[int] = {m.id for m in guild.members}
        # Trust/block ids that match the owner or @everyone are pruned by
        # the plan implicitly because the cog never feeds them in here.
        plan = plan_initial_overwrites(
            owner_id=owner.id,
            everyone_role_id=guild.default_role.id,
            profile_locked=profile.locked,
            profile_hidden=profile.hidden,
            trusted_ids=trusted_ids,
            blocked_ids=blocked_ids,
            present_member_ids=present_ids,
        )
        ow: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
        for entry in plan.entries:
            key: discord.Role | discord.Member | None
            if entry.target_kind == "everyone":
                key = guild.default_role
            elif entry.target_kind == "owner":
                key = owner
            else:
                key = guild.get_member(entry.target_id)
            if key is None:
                # Member left between the snapshot and the resolve.
                continue
            ow[key] = discord.PermissionOverwrite(
                view_channel=entry.view_channel,
                connect=entry.connect,
            )
        return ow, plan.missing_target_ids

    # Convenience suppress-context for ignored voice-related Discord errors.
    @staticmethod
    def _suppress_voice_errors():
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
        owner = member.guild.get_member(row.owner_id)
        decision = classify_claim_attempt(
            owner_present=owner is not None,
            owner_left_at=row.owner_left_at,
            now=time.time(),
            owner_grace_s=cfg.owner_grace_s,
            caller_is_owner=row.owner_id == member.id,
        )
        if not decision.eligible:
            if decision.error_message is not None:
                await _ephemeral(interaction, decision.error_message)
            return

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

    # ── Sleep-kick (self-disconnect timer) ─────────────────────────────

    @voice.command(
        name="sleepkick",
        description="Disconnect yourself from voice after a set number of hours. Use 0 to cancel.",
    )
    @app_commands.describe(hours="Hours until you're disconnected (0–24). Use 0 to cancel a pending timer.")
    async def voice_sleepkick(
        self, interaction: discord.Interaction, hours: float
    ) -> None:
        if interaction.guild is None:
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        key = (interaction.guild.id, member.id)

        existing = self._sleepkick_tasks.pop(key, None)
        if existing is not None and not existing.done():
            existing.cancel()

        if hours == 0:
            if existing is not None:
                await _ephemeral(interaction, "Sleep-kick cancelled.")
            else:
                await _ephemeral(interaction, "No active sleep-kick to cancel.")
            return

        if not (0 < hours <= 24):
            await _ephemeral(interaction, "Hours must be between 0 and 24.")
            return

        task = self.bot.loop.create_task(
            self._sleepkick_fire(interaction.guild.id, member.id, int(hours * 3600))
        )
        self._sleepkick_tasks[key] = task

        mins = int(hours * 60)
        if mins < 60:
            time_str = f"{mins}m"
        elif hours == int(hours):
            time_str = f"{int(hours)}h"
        else:
            time_str = f"{int(hours)}h {int((hours % 1) * 60)}m"
        await _ephemeral(interaction, f"You'll be disconnected from voice in {time_str}.")

    async def _sleepkick_fire(self, guild_id: int, user_id: int, delay_s: int) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        self._sleepkick_tasks.pop((guild_id, user_id), None)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None or member.voice is None or member.voice.channel is None:
            return
        try:
            await member.move_to(None, reason="Voice Master: sleep-kick timer expired")
        except (discord.Forbidden, discord.HTTPException):
            log.warning("voice_master: sleepkick failed for member %d in guild %d", user_id, guild_id)

    # ── Trust / block list management ─────────────────────────────────

    @voice_trusted.command(name="list", description="Show your saved trusted members.")
    async def trusted_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            ids = list_trusted(conn, interaction.guild.id, interaction.user.id)
        await _ephemeral(interaction, format_trusted_list(ids))

    @voice_trusted.command(name="add", description="Add a member to your trust list.")
    @app_commands.describe(member="They'll auto-get access to every future channel of yours.")
    async def trusted_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, interaction.guild.id)
            err = validate_trust_add(
                target_is_bot=member.bot,
                target_is_self=member.id == interaction.user.id,
                disable_saves=cfg.disable_saves,
                saveable_fields=cfg.saveable_fields,
            )
            if err is not None:
                await _ephemeral(interaction, err)
                return
            added, evicted = add_trusted(
                conn,
                interaction.guild.id,
                interaction.user.id,
                member.id,
                cap=cfg.trust_cap,
            )
        await _ephemeral(
            interaction,
            format_trust_add_result(
                target_mention=member.mention,
                added=added,
                evicted_id=evicted,
            ),
        )

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
        await _ephemeral(interaction, format_blocked_list(ids))

    @voice_blocked.command(name="add", description="Add a member to your blocklist.")
    @app_commands.describe(member="They'll be auto-denied access to every future channel of yours.")
    async def blocked_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, interaction.guild.id)
            err = validate_block_add(
                target_is_bot=member.bot,
                target_is_self=member.id == interaction.user.id,
                disable_saves=cfg.disable_saves,
                saveable_fields=cfg.saveable_fields,
            )
            if err is not None:
                await _ephemeral(interaction, err)
                return
            added, evicted = add_blocked(
                conn,
                interaction.guild.id,
                interaction.user.id,
                member.id,
                cap=cfg.block_cap,
            )
        await _ephemeral(
            interaction,
            format_block_add_result(
                target_mention=member.mention,
                added=added,
                evicted_id=evicted,
            ),
        )

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
        embed = build_profile_show_embed(
            saved_name=profile.saved_name,
            saved_limit=profile.saved_limit,
            locked=profile.locked,
            hidden=profile.hidden,
            trusted_count=len(trusted),
            blocked_count=len(blocked),
        )
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
            elif target == "trusted":
                conn.execute(
                    "DELETE FROM voice_master_trusted WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
            elif target == "blocked":
                conn.execute(
                    "DELETE FROM voice_master_blocked WHERE guild_id = ? AND owner_id = ?",
                    (gid, uid),
                )
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
            summary = profile_reset_summary(target)
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

    @voice_admin.command(
        name="post-panel",
        description="Post (or repost) the persistent owner-control panel in the configured control channel.",
    )
    async def post_panel_cmd(self, interaction: discord.Interaction) -> None:
        if not _admin_only(self.ctx, interaction):
            await interaction.response.send_message("Administrator only.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        with self.ctx.open_db() as conn:
            cfg = load_voice_master_config(conn, interaction.guild.id)
        if not cfg.control_channel_id:
            await interaction.response.send_message(
                "No control channel set. Configure it in the web dashboard first.",
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


async def setup(bot: Bot) -> None:
    await bot.add_cog(VoiceMasterCog(bot, bot.ctx))
