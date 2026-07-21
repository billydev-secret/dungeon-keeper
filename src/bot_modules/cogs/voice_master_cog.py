"""Voice Master — member-owned voice channels created by joining a Hub."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.voice_master_commands import (
    PANEL_DYNAMIC_ITEM_CLASSES,
    _apply_access_state,
    _apply_invite,
    _apply_kick,
    _apply_limit,
    _apply_rename,
    _apply_reset,
    _apply_transfer,
    _ephemeral,
    _grant_speaker_if_spectating,
    _resolve_owned_channel,
    post_claim_prompt,
    post_inline_panel,
    post_knock_request,
    post_panel,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.services.economy_rentals_service import entitlements
from bot_modules.services.economy_service import load_econ_settings
from bot_modules.services.moderation import write_audit
from bot_modules.services.voice_master_service import (
    ACCESS_LOCKED,
    ACCESS_NSFW,
    ACCESS_OPEN,
    ACCESS_SPECTATE,
    CATEGORY_CHANNEL_CAP,
    DEFAULT_NAME_TEMPLATE,
    VoiceMasterConfig,
    VoiceProfile,
    access_state_profile_flags,
    access_status_text,
    active_channel_count,
    profile_access_state,
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
    style_lease_blocks,
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
        # channel_id → pending post-grace "owner left, claim me" prompt task
        self._claim_timers: dict[int, asyncio.Task] = {}
        # (guild_id, user_id) → pending self-disconnect task
        self._sleepkick_tasks: dict[tuple[int, int], asyncio.Task] = {}
        # channel_id → voice_room_host quest already fired this room lifetime
        self._host_quest_fired: set[int] = set()
        super().__init__()

    async def cog_load(self) -> None:
        # Register persistent panel dropdown classes so they survive restarts.
        for cls in PANEL_DYNAMIC_ITEM_CLASSES:
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
        guild_id = guild.id

        def _load():
            with self.ctx.open_db() as conn:
                return (
                    load_voice_master_config(conn, guild_id),
                    list_active_channels(conn, guild_id),
                )

        cfg, tracked = await asyncio.to_thread(_load)

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
            db_to_delete = list(plan.db_to_delete)

            def _del_db():
                with self.ctx.open_db() as conn:
                    for cid in db_to_delete:
                        delete_active_channel(conn, cid)

            await asyncio.to_thread(_del_db)

        for cid in plan.orphan_warnings:
            log.warning(
                "voice_master: orphan voice channel %d in target category — leaving alone",
                cid,
            )

        # Re-arm claim prompts for channels orphaned (owner gone, members still
        # inside) across the downtime. In-memory timers don't survive a restart,
        # same as the empty-grace timers; reconcile heals them. Past-grace
        # channels get the prompt now — a rare duplicate (if one was already
        # posted before the restart) is harmless: first claim wins, and the
        # other button then refuses (claiming clears owner_left_at).
        now = time.time()
        accent = await resolve_accent_color(self.ctx.db_path, guild)
        for row in tracked:
            if (
                row.channel_id not in present_ids
                or row.channel_id not in with_humans
                or row.owner_left_at is None
            ):
                continue
            ch = guild.get_channel(row.channel_id)
            if not isinstance(ch, discord.VoiceChannel):
                continue
            remaining = cfg.owner_grace_s - (now - row.owner_left_at)
            if remaining <= 0:
                await post_claim_prompt(ch, color=accent)
            else:
                self._schedule_claim_prompt(ch, int(remaining))

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
        _del_guild_id = channel.guild.id
        _del_channel_id = channel.id
        self._host_quest_fired.discard(_del_channel_id)

        def _fetch_and_delete():
            with self.ctx.open_db() as conn:
                cfg_ = load_voice_master_config(conn, _del_guild_id)
                row_ = get_active_channel(conn, _del_channel_id)
                if row_ is not None:
                    delete_active_channel(conn, _del_channel_id)
                    write_audit(
                        conn,
                        guild_id=_del_guild_id,
                        action="vm_channel_delete",
                        actor_id=0,
                        target_id=row_.owner_id,
                        extra={"channel_id": _del_channel_id, "reason": "external_delete"},
                    )
                return cfg_, row_

        cfg, row = await asyncio.to_thread(_fetch_and_delete)
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
        _guild_id = guild.id

        def _get_owned_rows():
            with self.ctx.open_db() as conn:
                return conn.execute(
                    "SELECT channel_id FROM voice_master_channels "
                    "WHERE guild_id = ? AND owner_id = ?",
                    (_guild_id, user_id),
                ).fetchall()

        owned_rows = await asyncio.to_thread(_get_owned_rows)
        for row in owned_rows:
            cid = int(row["channel_id"])
            ch = guild.get_channel(cid)
            if not isinstance(ch, discord.VoiceChannel):
                def _del_missing(cid_: int) -> None:
                    with self.ctx.open_db() as conn:
                        delete_active_channel(conn, cid_)

                await asyncio.to_thread(_del_missing, cid)
                continue
            humans = [m for m in ch.members if not m.bot]
            if humans:
                # Hand off to first non-bot human present.
                new_owner = humans[0]
                overwrite = ch.overwrites_for(new_owner)
                overwrite.connect = True
                overwrite.view_channel = True
                _grant_speaker_if_spectating(self.ctx, ch, overwrite)
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
                _new_owner_id = new_owner.id

                def _set_owner(cid_: int, new_owner_id_: int) -> None:
                    with self.ctx.open_db() as conn:
                        set_owner(conn, cid_, new_owner_id_)
                        write_audit(
                            conn,
                            guild_id=_guild_id,
                            action="vm_transfer",
                            actor_id=new_owner_id_,
                            target_id=user_id,
                            extra={"channel_id": cid_, "reason": "owner_left_server"},
                        )

                await asyncio.to_thread(_set_owner, cid, _new_owner_id)
            else:
                # Empty — delete it now.
                try:
                    await ch.delete(reason="Voice Master: owner left server, channel empty")
                except (discord.Forbidden, discord.HTTPException):
                    log.exception("voice_master: failed to delete %d", cid)

                def _del_active(cid_: int) -> None:
                    with self.ctx.open_db() as conn:
                        delete_active_channel(conn, cid_)
                        write_audit(
                            conn,
                            guild_id=_guild_id,
                            action="vm_channel_delete",
                            actor_id=user_id,
                            extra={"channel_id": cid_, "reason": "owner_left_server"},
                        )

                await asyncio.to_thread(_del_active, cid)
        # Remove the departed member from every other owner's trust + block list.
        def _remove_lists():
            with self.ctx.open_db() as conn:
                return remove_member_from_all_lists(conn, _guild_id, user_id)

        n = await asyncio.to_thread(_remove_lists)
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

        def _load_cfg():
            with self.ctx.open_db() as conn:
                return load_voice_master_config(conn, member.guild.id)

        cfg = await asyncio.to_thread(_load_cfg)
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

        def _mark_left():
            with self.ctx.open_db() as conn:
                row = get_active_channel(conn, channel.id)
                if row is None:
                    return None
                if member.id == row.owner_id:
                    set_owner_left_at(conn, channel.id, time.time())
                return row

        row = await asyncio.to_thread(_mark_left)
        if row is None:
            return

        if not any(not m.bot for m in channel.members):
            self._schedule_empty_delete(channel, cfg.empty_grace_s)
        elif member.id == row.owner_id:
            # Owner walked out but others remain. Arm a claim prompt that posts
            # only if they don't come back within the grace window — a brief
            # disconnect stays quiet (the join handler cancels this).
            self._schedule_claim_prompt(channel, cfg.owner_grace_s)

    async def _handle_joined_tracked(
        self,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        if not isinstance(channel, discord.VoiceChannel):
            return

        def _mark_joined():
            with self.ctx.open_db() as conn:
                row = get_active_channel(conn, channel.id)
                if row is None:
                    return None
                owner_returned = (
                    member.id == row.owner_id and row.owner_left_at is not None
                )
                if owner_returned:
                    set_owner_left_at(conn, channel.id, None)
                return row, owner_returned

        marked = await asyncio.to_thread(_mark_joined)
        if marked is None:
            return
        row, owner_returned = marked

        # Quest hook: the room became a real hangout — 2+ non-bot guests with
        # the owner in the room. Once per room lifetime via the in-memory set
        # (restarts forgive; the event occurrence key still blocks a re-pay
        # of the same channel id). Guarded wrapper, never raises.
        guests = [
            m for m in channel.members if not m.bot and m.id != row.owner_id
        ]
        owner_present = any(m.id == row.owner_id for m in channel.members)
        if (
            len(guests) >= 2
            and owner_present
            and channel.id not in self._host_quest_fired
        ):
            self._host_quest_fired.add(channel.id)
            from bot_modules.economy.game_rewards import fire_member_trigger  # noqa: PLC0415

            await fire_member_trigger(
                self.bot, channel.guild.id, row.owner_id, "voice_room_host",
                occurrence=str(channel.id),
            )
        self._cancel_empty_timer(channel.id)
        # The owner coming back disarms a pending claim prompt; a random member
        # joining does not (the channel is still ownerless).
        if owner_returned:
            self._cancel_claim_timer(channel.id)

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

    def _schedule_claim_prompt(
        self, channel: discord.VoiceChannel, grace_s: int
    ) -> None:
        # Mirror of _schedule_empty_delete: post a claim prompt once the owner
        # has been gone past the grace window, cancellable if they return.
        self._cancel_claim_timer(channel.id)
        task = self.bot.loop.create_task(
            self._post_claim_after_grace(channel, max(grace_s, 0))
        )
        self._claim_timers[channel.id] = task

    def _cancel_claim_timer(self, channel_id: int) -> None:
        task = self._claim_timers.pop(channel_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _post_claim_after_grace(
        self, channel: discord.VoiceChannel, grace_s: int
    ) -> None:
        try:
            await asyncio.sleep(grace_s)
        except asyncio.CancelledError:
            return
        self._claim_timers.pop(channel.id, None)
        live = self.bot.get_channel(channel.id)
        if not isinstance(live, discord.VoiceChannel):
            return
        if not any(not m.bot for m in live.members):
            return  # everyone left during grace; empty-cleanup will delete it
        _pca_channel_id = channel.id

        def _fetch_pca():
            with self.ctx.open_db() as conn:
                return get_active_channel(conn, _pca_channel_id)

        row = await asyncio.to_thread(_fetch_pca)
        if row is None or row.owner_left_at is None:
            return  # owner returned (cancel raced) — nothing to claim
        accent = await resolve_accent_color(self.ctx.db_path, live.guild)
        await post_claim_prompt(live, color=accent)

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
            cid = channel.id

            def _del_stale():
                with self.ctx.open_db() as conn:
                    delete_active_channel(conn, cid)

            await asyncio.to_thread(_del_stale)
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
        self._cancel_claim_timer(channel.id)
        _actor_id = self.bot.user.id if self.bot.user else 0
        _ch_id = channel.id
        _guild_id_del = channel.guild.id

        def _del_empty():
            with self.ctx.open_db() as conn:
                delete_active_channel(conn, _ch_id)
                write_audit(
                    conn,
                    guild_id=_guild_id_del,
                    action="vm_channel_delete",
                    actor_id=_actor_id,
                    extra={"channel_id": _ch_id, "reason": "empty_grace"},
                )

        await asyncio.to_thread(_del_empty)
        self._empty_timers.pop(channel.id, None)

    # ── Hub join → create channel ─────────────────────────────────────

    async def _handle_hub_join(
        self, member: discord.Member, cfg: VoiceMasterConfig
    ) -> None:
        guild = member.guild
        async with self._create_locks[member.id]:
            # If the member already owns a live channel, return them to it
            # rather than kicking them out of the Hub.
            guild_id = guild.id
            member_id = member.id

            def _get_existing():
                with self.ctx.open_db() as conn:
                    return get_owned_channel(conn, guild_id, member_id)

            existing = await asyncio.to_thread(_get_existing)
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
                stale_cid = existing.channel_id

                def _del_stale_hub(cid_: int) -> None:
                    with self.ctx.open_db() as conn:
                        delete_active_channel(conn, cid_)

                await asyncio.to_thread(_del_stale_hub, stale_cid)

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

            def _load_profile_data() -> tuple[bool, VoiceProfile, list[int], list[int], list[str]]:
                with self.ctx.open_db() as conn:
                    if active_channel_count(conn, guild_id, member_id) >= cfg.max_per_member:
                        return True, default_profile(), [], [], []
                    # Saves disabled? Treat every member as having no profile.
                    if cfg.disable_saves:
                        profile_ = default_profile()
                        trusted_ids_: list[int] = []
                        blocked_ids_: list[int] = []
                    else:
                        profile_ = (
                            load_profile(conn, guild_id, member_id) or default_profile()
                        )
                        trusted_ids_ = list_trusted(conn, guild_id, member_id)
                        blocked_ids_ = list_blocked(conn, guild_id, member_id)
                    # Voice-style lease (economy sinks round 3, stage 3): the
                    # saved name/limit only re-apply while leased — the profile
                    # stays stored (dormant), so re-renting restores the setup.
                    econ = load_econ_settings(conn, guild_id)
                    if style_lease_blocks(
                        economy_enabled=econ.enabled,
                        price=econ.price_voice_style,
                        entitled=(
                            econ.enabled
                            and econ.price_voice_style > 0
                            and "voice_style" in entitlements(conn, guild_id, member_id)
                        ),
                    ):
                        profile_ = replace(profile_, saved_name=None, saved_limit=0)
                    blocklist_ = list_name_blocklist(conn, guild_id)
                    return False, profile_, trusted_ids_, blocked_ids_, blocklist_

            cap_exceeded, profile, trusted_ids, blocked_ids, blocklist = await asyncio.to_thread(_load_profile_data)
            if cap_exceeded:
                with self._suppress_voice_errors():
                    await member.move_to(
                        None, reason="Voice Master: max channels reached"
                    )
                return

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
                guild_max_bitrate=int(guild.bitrate_limit),
            )

            overwrites, skipped_targets = self._build_initial_overwrites(
                guild=guild,
                profile=profile,
                trusted_ids=trusted_ids,
                blocked_ids=blocked_ids,
                owner=member,
                gate_role_id=cfg.spectator_gate_role_id,
            )

            create_kwargs: dict = {"name": name, "overwrites": overwrites}
            if isinstance(target_cat, discord.CategoryChannel):
                create_kwargs["category"] = target_cat
            if limit > 0:
                create_kwargs["user_limit"] = limit
            if bitrate > 0:
                create_kwargs["bitrate"] = bitrate
            # Every access state but plain "open" is age-gated (mirrors
            # _apply_access_state), so carry Discord's age gate at creation.
            initial_state = profile_access_state(profile)
            if initial_state != ACCESS_OPEN:
                create_kwargs["nsfw"] = True

            try:
                channel = await guild.create_voice_channel(**create_kwargs)
            except discord.Forbidden:
                log.error("voice_master: missing Manage Channels permission")
                return
            except discord.HTTPException:
                log.exception("voice_master: failed to create channel for %s", member.id)
                return

            new_channel_id = channel.id
            skipped_payload = build_skipped_payload(
                name_fell_back=name_fell_back,
                missing_target_count=len(skipped_targets),
            )

            def _insert_channel():
                with self.ctx.open_db() as conn:
                    insert_active_channel(
                        conn,
                        channel_id=new_channel_id,
                        guild_id=guild_id,
                        owner_id=member_id,
                        now=now,
                    )
                    write_audit(
                        conn,
                        guild_id=guild_id,
                        action="vm_channel_create",
                        actor_id=member_id,
                        extra={
                            "channel_id": new_channel_id,
                            "name": name,
                            "applied_skipped": skipped_payload,
                        },
                    )

            await asyncio.to_thread(_insert_channel)

            try:
                await member.move_to(channel, reason="Voice Master: own channel ready")
            except (discord.Forbidden, discord.HTTPException):
                # Member disconnected before we could move them; the empty-grace
                # timer below will clean up the orphaned channel.
                log.info(
                    "voice_master: created channel %d but could not move %d in",
                    channel.id, member.id,
                )

            # Advertise the room's access state on its status line. create_voice_channel
            # can't carry a status, so set it here in a follow-up edit (separate
            # endpoint, not subject to the name rate limit). Best-effort.
            with self._suppress_voice_errors():
                status_mode = {
                    ACCESS_OPEN: "open",
                    ACCESS_NSFW: "nsfw",
                    ACCESS_LOCKED: "lock",
                    ACCESS_SPECTATE: "spectate",
                }[initial_state]
                await channel.edit(
                    status=access_status_text(mode=status_mode),
                    reason="Voice Master: initial access-state status",
                )

            # Drop the control panel into the new channel's text chat so the
            # owner has the buttons right where they are. Non-fatal on failure
            # (perms missing, channel deleted out from under us, etc.).
            if cfg.post_inline_panel:
                accent = await resolve_accent_color(self.ctx.db_path, guild)
                await post_inline_panel(channel, member, color=accent)

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
        gate_role_id: int = 0,
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
        gate_role = (
            guild.get_role(gate_role_id) if gate_role_id else None
        )
        # Derive the flags from the single access state so legacy rows behave:
        # the locked state always implies hidden, even for profiles saved before
        # the states were unified.
        flags = access_state_profile_flags(profile_access_state(profile))
        plan = plan_initial_overwrites(
            owner_id=owner.id,
            everyone_role_id=guild.default_role.id,
            profile_locked=flags["locked"],
            profile_hidden=flags["hidden"],
            profile_spectator=flags["spectator"],
            # Only treat spectating as gated if the role still exists.
            gate_role_id=gate_role.id if gate_role is not None else None,
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
            elif entry.target_kind == "gate_role":
                key = gate_role
            else:
                key = guild.get_member(entry.target_id)
            if key is None:
                # Member/role left between the snapshot and the resolve.
                continue
            ow[key] = discord.PermissionOverwrite(
                view_channel=entry.view_channel,
                connect=entry.connect,
                speak=entry.speak,
                stream=entry.stream,
                send_messages=entry.send_messages,
                send_messages_in_threads=entry.send_messages_in_threads,
            )
        return ow, plan.missing_target_ids

    # Convenience suppress-context for ignored voice-related Discord errors.
    @staticmethod
    def _suppress_voice_errors():
        return contextlib.suppress(discord.Forbidden, discord.HTTPException)

    # ── Owner slash commands ──────────────────────────────────────────

    @voice.command(
        name="access",
        description="Set who can see and join your channel (open / NSFW / locked / spectator).",
    )
    @app_commands.describe(state="The access state to apply.")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="🔓 Open — anyone can see and join", value=ACCESS_OPEN),
            app_commands.Choice(
                name="🔞 NSFW — age-gated, but open", value=ACCESS_NSFW
            ),
            app_commands.Choice(
                name="🔒 NSFW locked — age-gated, hidden, invite-only",
                value=ACCESS_LOCKED,
            ),
            app_commands.Choice(
                name="🎭 Spectator — age-gated muted audience", value=ACCESS_SPECTATE
            ),
        ]
    )
    async def voice_access(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str],
    ) -> None:
        resolved = await _resolve_owned_channel(interaction)
        if resolved is None:
            return
        channel, row = resolved
        await _apply_access_state(interaction, channel, row, state=state.value)

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
        await _apply_reset(interaction, channel, row, also_profile=also_profile)

    @voice.command(name="claim", description="Claim ownership of the channel you're in (if eligible).")
    async def voice_claim(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await _ephemeral(interaction, "❌ You're not in a voice channel.")
            return
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await _ephemeral(interaction, "❌ That isn't a managed voice channel.")
            return
        _guild_id = member.guild.id
        _channel_id = channel.id

        def _fetch_claim_data():
            with self.ctx.open_db() as conn:
                return (
                    load_voice_master_config(conn, _guild_id),
                    get_active_channel(conn, _channel_id),
                )

        cfg, row = await asyncio.to_thread(_fetch_claim_data)
        if row is None:
            await _ephemeral(interaction, "❌ This channel isn't managed by Voice Master.")
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
        _grant_speaker_if_spectating(self.ctx, channel, overwrite)
        try:
            await channel.set_permissions(
                member, overwrite=overwrite, reason="Voice Master: claim"
            )
        except (discord.Forbidden, discord.HTTPException):
            await _ephemeral(interaction, "❌ Couldn't grant you ownership permissions.")
            return
        _prev_owner_id = row.owner_id
        _claimer_id = member.id

        def _save_claim():
            with self.ctx.open_db() as conn:
                set_owner(conn, _channel_id, _claimer_id)
                write_audit(
                    conn,
                    guild_id=_guild_id,
                    action="vm_claim",
                    actor_id=_claimer_id,
                    target_id=_prev_owner_id,
                    extra={"channel_id": _channel_id},
                )

        await asyncio.to_thread(_save_claim)
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
                await _ephemeral(interaction, "❌ No active sleep-kick to cancel.")
            return

        if not (0 < hours <= 24):
            await _ephemeral(interaction, "❌ Hours must be between 0 and 24.")
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
        _tl_guild_id = interaction.guild.id
        _tl_user_id = interaction.user.id

        def _fetch_trusted():
            with self.ctx.open_db() as conn:
                return list_trusted(conn, _tl_guild_id, _tl_user_id)

        ids = await asyncio.to_thread(_fetch_trusted)
        await _ephemeral(interaction, format_trusted_list(ids))

    @voice_trusted.command(name="add", description="Add a member to your trust list.")
    @app_commands.describe(member="They'll auto-get access to every future channel of yours.")
    async def trusted_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        _ta_guild_id = interaction.guild.id
        _ta_user_id = interaction.user.id

        def _do_trust_add() -> str | tuple[bool, int | None]:
            with self.ctx.open_db() as conn:
                cfg = load_voice_master_config(conn, _ta_guild_id)
                err = validate_trust_add(
                    target_is_bot=member.bot,
                    target_is_self=member.id == _ta_user_id,
                    disable_saves=cfg.disable_saves,
                    saveable_fields=cfg.saveable_fields,
                )
                if err is not None:
                    return err
                added, evicted = add_trusted(
                    conn,
                    _ta_guild_id,
                    _ta_user_id,
                    member.id,
                    cap=cfg.trust_cap,
                )
                return added, evicted

        _result = await asyncio.to_thread(_do_trust_add)
        if isinstance(_result, str):
            await _ephemeral(interaction, _result)
            return
        added, evicted = _result
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
        _tr_guild_id = interaction.guild.id
        _tr_user_id = interaction.user.id

        def _do_trust_remove():
            with self.ctx.open_db() as conn:
                return remove_trusted(conn, _tr_guild_id, _tr_user_id, member.id)

        removed = await asyncio.to_thread(_do_trust_remove)
        if removed:
            await _ephemeral(interaction, f"Removed {member.mention} from your trust list.")
        else:
            await _ephemeral(interaction, f"❌ {member.mention} wasn't on your trust list.")

    @voice_blocked.command(name="list", description="Show your saved blocked members.")
    async def blocked_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        _bl_guild_id = interaction.guild.id
        _bl_user_id = interaction.user.id

        def _fetch_blocked():
            with self.ctx.open_db() as conn:
                return list_blocked(conn, _bl_guild_id, _bl_user_id)

        ids = await asyncio.to_thread(_fetch_blocked)
        await _ephemeral(interaction, format_blocked_list(ids))

    @voice_blocked.command(name="add", description="Add a member to your blocklist.")
    @app_commands.describe(member="They'll be auto-denied access to every future channel of yours.")
    async def blocked_add(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        if interaction.guild is None:
            return
        _ba_guild_id = interaction.guild.id
        _ba_user_id = interaction.user.id

        def _do_block_add() -> str | tuple[bool, int | None]:
            with self.ctx.open_db() as conn:
                cfg = load_voice_master_config(conn, _ba_guild_id)
                err = validate_block_add(
                    target_is_bot=member.bot,
                    target_is_self=member.id == _ba_user_id,
                    disable_saves=cfg.disable_saves,
                    saveable_fields=cfg.saveable_fields,
                )
                if err is not None:
                    return err
                added, evicted = add_blocked(
                    conn,
                    _ba_guild_id,
                    _ba_user_id,
                    member.id,
                    cap=cfg.block_cap,
                )
                return added, evicted

        _result = await asyncio.to_thread(_do_block_add)
        if isinstance(_result, str):
            await _ephemeral(interaction, _result)
            return
        added, evicted = _result
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
        _br_guild_id = interaction.guild.id
        _br_user_id = interaction.user.id

        def _do_block_remove():
            with self.ctx.open_db() as conn:
                return remove_blocked(conn, _br_guild_id, _br_user_id, member.id)

        removed = await asyncio.to_thread(_do_block_remove)
        if removed:
            await _ephemeral(interaction, f"Removed {member.mention} from your blocklist.")
        else:
            await _ephemeral(interaction, f"❌ {member.mention} wasn't on your blocklist.")

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
        _knock_channel_id = channel.id

        def _fetch_knock_row():
            with self.ctx.open_db() as conn:
                return get_active_channel(conn, _knock_channel_id)

        row = await asyncio.to_thread(_fetch_knock_row)
        if row is None:
            await _ephemeral(
                interaction, "❌ That channel isn't managed by Voice Master."
            )
            return
        if row.owner_id == interaction.user.id:
            await _ephemeral(interaction, "❌ You already own that channel.")
            return
        owner = interaction.guild.get_member(row.owner_id)
        if owner is None:
            await _ephemeral(
                interaction,
                "❌ The owner isn't in this server right now — try `/voice claim` if eligible.",
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
                f"Knock sent to {owner.mention} — you'll hear back if they let you in.",
            )
        else:
            await _ephemeral(
                interaction,
                "❌ Couldn't deliver the knock — the owner's DMs are closed and "
                "there's no control channel to fall back to.",
            )

    # ── Profile inspection / reset ────────────────────────────────────

    @voice_profile.command(name="show", description="Show your saved channel profile.")
    async def profile_show(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            return
        _ps_guild_id = interaction.guild.id
        _ps_user_id = interaction.user.id

        def _fetch_profile():
            with self.ctx.open_db() as conn:
                return (
                    load_profile(conn, _ps_guild_id, _ps_user_id) or default_profile(),
                    list_trusted(conn, _ps_guild_id, _ps_user_id),
                    list_blocked(conn, _ps_guild_id, _ps_user_id),
                )

        profile, trusted, blocked = await asyncio.to_thread(_fetch_profile)
        accent = await resolve_accent_color(self.ctx.db_path, interaction.guild)
        embed = build_profile_show_embed(
            saved_name=profile.saved_name,
            saved_limit=profile.saved_limit,
            access_state=profile_access_state(profile),
            trusted_count=len(trusted),
            blocked_count=len(blocked),
            color=accent,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @voice_profile.command(name="reset", description="Reset your saved profile (or one specific field).")
    @app_commands.describe(field="Which field to clear. Omit to clear everything.")
    @app_commands.choices(
        field=[
            app_commands.Choice(name="all (full reset)", value="all"),
            app_commands.Choice(name="name", value="name"),
            app_commands.Choice(name="limit", value="limit"),
            app_commands.Choice(name="access", value="access"),
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

        def _do_reset():
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
                        profile = replace(profile, saved_name=None)
                    elif target == "limit":
                        profile = replace(profile, saved_limit=0)
                    elif target == "access":
                        # One dial now — clear every underlying access flag.
                        profile = replace(
                            profile,
                            locked=False,
                            hidden=False,
                            spectator=False,
                            age_gated=False,
                        )
                    save_profile(conn, gid, uid, profile)
                write_audit(
                    conn,
                    guild_id=gid,
                    action="vm_reset_profile",
                    actor_id=uid,
                    extra={"field": target},
                )

        await asyncio.to_thread(_do_reset)
        summary = profile_reset_summary(target)
        await _ephemeral(interaction, summary)

    @voice.command(name="owner", description="Show who owns the voice channel you're in.")
    async def voice_owner(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await _ephemeral(interaction, "❌ You're not in a voice channel.")
            return
        channel = member.voice.channel
        _owner_channel_id = channel.id

        def _fetch_owner_row():
            with self.ctx.open_db() as conn:
                return get_active_channel(conn, _owner_channel_id)

        row = await asyncio.to_thread(_fetch_owner_row)
        if row is None:
            await _ephemeral(
                interaction,
                "❌ This channel isn't managed by Voice Master.",
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
            await interaction.response.send_message("❌ Administrator only.", ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        _pp_guild_id = interaction.guild.id

        def _fetch_pp_cfg():
            with self.ctx.open_db() as conn:
                return load_voice_master_config(conn, _pp_guild_id)

        cfg = await asyncio.to_thread(_fetch_pp_cfg)
        if not cfg.control_channel_id:
            await interaction.response.send_message(
                "❌ No control channel set. Configure it in the web dashboard first.",
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
                "❌ Configured control channel is missing or not a text channel.",
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
