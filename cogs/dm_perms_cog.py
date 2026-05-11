"""DM permission cog — ported from accord_bot (dm_perms_bot)."""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import DM_ACCEPT, DM_DENY, DM_PENDING, DM_PRIMARY
from services.dm_perms_service import (
    DM_ROLE_NAMES,
    add_consent_pair,
    build_panel_embed,
    count_pending_for_requester,
    expire_stale_pending_requests,
    get_consent_pair_meta,
    init_db,
    load_audit_channels,
    load_consent_pairs,
    load_panel_settings,
    load_request_channels,
    load_request_by_message_id,
    load_requests,
    normalize_request_type,
    post_audit_event,
    remove_consent_pair,
    remove_request,
    request_type_label,
    resolve_mode,
    set_member_dm_mode,
    set_panel_settings,
    upsert_request,
    write_audit_log,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger(__name__)

DM_REQUEST_PANEL_CUSTOM_ID = "dm_request:open_modal"
DM_CONSENT_ACCEPT_CUSTOM_ID = "dm_consent:accept"
DM_CONSENT_DENY_CUSTOM_ID = "dm_consent:deny"

REQUEST_TIMEOUT_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_LABEL = "24 hours"
EXPIRY_SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly
MAX_PENDING_PER_REQUESTER = 5
MAX_REASON_LENGTH = 250  # leave headroom under the embed-field char ceiling
PANEL_BUMP_COOLDOWN_SECONDS = 2.0


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class AskConsentView(discord.ui.View):
    """Persistent view for DM consent buttons.

    A single instance is registered with ``bot.add_view()`` at cog load.
    All Accept/Deny clicks across the bot route to this instance, which
    looks up the underlying request from the DB by ``interaction.message.id``.
    This keeps in-flight requests usable across bot restarts.
    """

    def __init__(self, cog: DmPermsCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        custom_id=DM_CONSENT_ACCEPT_CUSTOM_ID,
    )
    async def accept(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_click(interaction, accepted=True)

    @discord.ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        custom_id=DM_CONSENT_DENY_CUSTOM_ID,
    )
    async def deny(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_click(interaction, accepted=False)

    async def _handle_click(
        self, interaction: discord.Interaction, *, accepted: bool
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "Couldn't find the request for this button.", ephemeral=True
            )
            return

        record = load_request_by_message_id(self.cog.ctx.db_path, message.id)
        if record is None:
            stale_embed = discord.Embed(
                title="⌛ Request no longer active",
                description=(
                    "This DM request has already been answered, expired, "
                    "or was cancelled."
                ),
                color=DM_PENDING,
            )
            try:
                await interaction.response.edit_message(
                    embed=stale_embed, view=None
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        target_id = record["target_id"]
        requester_id = record["requester_id"]
        guild_id = record["guild_id"]
        req_type = record["request_type"]
        reason = record["reason"]

        if interaction.user.id != target_id:
            await interaction.response.send_message(
                "This request isn't for you.", ephemeral=True
            )
            return

        guild = self.cog.bot.get_guild(guild_id)
        if guild is None:
            # Bot was removed from the guild after the request was sent.
            self.cog._drop_request_from_memory(guild_id, requester_id, target_id)
            remove_request(self.cog.ctx.db_path, guild_id, requester_id, target_id)
            stale_embed = discord.Embed(
                title="❌ Server unavailable",
                description=(
                    "The server this request belongs to is no longer reachable."
                ),
                color=DM_DENY,
            )
            try:
                await interaction.response.edit_message(
                    embed=stale_embed, view=None
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        requester = guild.get_member(requester_id)
        target = guild.get_member(target_id)

        # Disable buttons by removing the view; we'll edit with the result embed.
        if accepted:
            await self._handle_accept(
                interaction,
                guild=guild,
                requester=requester,
                target=target,
                requester_id=requester_id,
                target_id=target_id,
                req_type=req_type,
                reason=reason,
                source_msg_id=message.id,
                source_channel_id=getattr(message.channel, "id", None),
            )
        else:
            await self._handle_deny(
                interaction,
                guild=guild,
                requester=requester,
                target=target,
                requester_id=requester_id,
                target_id=target_id,
                req_type=req_type,
                reason=reason,
            )

    async def _handle_accept(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
        source_msg_id: Optional[int],
        source_channel_id: Optional[int],
    ) -> None:
        if requester is None or target is None:
            await interaction.response.send_message(
                "Couldn't find one or both users in this server.", ephemeral=True
            )
            return

        self.cog.consent_pairs.setdefault(guild.id, set())
        self.cog.consent_pairs[guild.id].add((requester_id, target_id))
        self.cog.consent_pairs[guild.id].add((target_id, requester_id))

        add_consent_pair(
            self.cog.ctx.db_path, guild.id, requester_id, target_id,
            rel_type=req_type, reason=reason,
            source_msg_id=source_msg_id, source_channel_id=source_channel_id,
        )
        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.ctx.db_path, guild.id, requester_id, target_id)

        success_embed = discord.Embed(
            title="✅ Connection accepted!",
            color=DM_ACCEPT,
        )
        success_embed.description = (
            f"**{requester.display_name}** ↔ **{target.display_name}**\n\n"
            f"{requester.mention} and {target.mention} can now DM each other.\n\n"
            "Either of you can undo this at any time with `/dm_revoke`."
        )
        success_embed.add_field(
            name="Request Type", value=request_type_label(req_type), inline=True
        )
        success_embed.add_field(
            name="Reason", value=_safe_field_text(reason), inline=False
        )

        await interaction.response.edit_message(embed=success_embed, view=None)
        await _safe_dm(requester, embed=success_embed)
        await _safe_dm(target, embed=success_embed)

        write_audit_log(
            self.cog.ctx.db_path, guild.id, "request_accepted",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}",
        )
        await self.cog._post_audit(
            guild,
            f"DM request accepted: {requester.display_name} ↔ "
            f"{target.display_name} ({request_type_label(req_type)})",
        )

    async def _handle_deny(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
    ) -> None:
        deny_embed = discord.Embed(
            title="❌ Request declined",
            description="No worries — the request was turned down.",
            color=DM_DENY,
        )
        deny_embed.add_field(
            name="Request Type", value=request_type_label(req_type), inline=True
        )
        deny_embed.add_field(
            name="Reason", value=_safe_field_text(reason), inline=False
        )

        await interaction.response.edit_message(embed=deny_embed, view=None)

        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.ctx.db_path, guild.id, requester_id, target_id)

        if requester:
            req_embed = discord.Embed(
                title="❌ Request declined",
                description=(
                    f"Your {request_type_label(req_type).lower()} request "
                    f"to **{target.display_name if target else target_id}** "
                    f"in **{guild.name}** was declined."
                ),
                color=DM_DENY,
            )
            req_embed.add_field(
                name="Request Type", value=request_type_label(req_type), inline=True
            )
            req_embed.add_field(
                name="Reason", value=_safe_field_text(reason), inline=False
            )
            await _safe_dm(requester, embed=req_embed)

        write_audit_log(
            self.cog.ctx.db_path, guild.id, "request_denied",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}",
        )
        requester_name = requester.display_name if requester else str(requester_id)
        target_name = target.display_name if target else str(target_id)
        await self.cog._post_audit(
            guild,
            f"DM request denied: {requester_name} ➝ {target_name} "
            f"({request_type_label(req_type)})",
        )


class DmRequestLookupView(discord.ui.View):
    """Ephemeral user-select + request type + continue button."""

    _TYPE_BUTTON_PREFIX = "dm_lookup_type:"

    def __init__(self, cog: DmPermsCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self._selected_user: Optional[discord.Member | discord.User] = None
        self._request_type: str = "dm"

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a user", min_values=1, max_values=1)
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self._selected_user = select.values[0]
        await interaction.response.defer()

    def _set_type_styles(self, selected: str) -> None:
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            cid = getattr(child, "custom_id", "") or ""
            if not cid.startswith(self._TYPE_BUTTON_PREFIX):
                continue
            this_type = cid[len(self._TYPE_BUTTON_PREFIX):]
            child.style = (
                discord.ButtonStyle.primary
                if this_type == selected
                else discord.ButtonStyle.secondary
            )

    @discord.ui.button(
        label="Direct Message",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="dm_lookup_type:dm",
    )
    async def type_dm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._request_type = "dm"
        self._set_type_styles("dm")
        await interaction.response.edit_message(view=self)

    @discord.ui.button(
        label="Friend Request",
        style=discord.ButtonStyle.secondary,
        row=1,
        custom_id="dm_lookup_type:friend",
    )
    async def type_friend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._request_type = "friend"
        self._set_type_styles("friend")
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.success, row=2)
    async def continue_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self._selected_user is None:
            await interaction.response.send_message("Please select a user first.", ephemeral=True)
            return
        await interaction.response.send_modal(
            DmRequestReasonModal(self.cog, self._selected_user, self._request_type)
        )


class DmRequestReasonModal(discord.ui.Modal, title="DM Request"):
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=MAX_REASON_LENGTH,
        placeholder="Why you'd like to connect...",
    )

    def __init__(self, cog: DmPermsCog, target: discord.Member | discord.User, request_type: str) -> None:
        super().__init__()
        self.cog = cog
        self.target = target
        self.request_type = request_type

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._submit_dm_request(
            interaction, self.target, self.request_type, str(self.reason.value or "").strip()
        )


class DmRequestPanelView(discord.ui.View):
    """Persistent panel button registered on startup."""

    def __init__(self, cog: DmPermsCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Open DM Request Form",
        style=discord.ButtonStyle.primary,
        custom_id=DM_REQUEST_PANEL_CUSTOM_ID,
    )
    async def open_request(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "Select who you'd like to contact and what type of request to send:",
            view=DmRequestLookupView(self.cog),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Module-level DM helper
# ---------------------------------------------------------------------------

async def _safe_dm(user: discord.abc.Messageable, **kwargs: Any) -> Optional[discord.Message]:
    try:
        return await user.send(**kwargs)
    except (discord.Forbidden, discord.HTTPException):
        return None


def _safe_field_text(text: str | None) -> str:
    """Escape markdown in user-supplied text before placing it into an embed.

    DM request reasons come from a Discord modal — without escaping, a
    requester could craft markdown links / formatting that the recipient
    sees as if the bot itself authored them.
    """
    if not text:
        return "—"
    return discord.utils.escape_markdown(text)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class DmPermsCog(commands.Cog):
    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        self.consent_pairs: dict[int, set[tuple[int, int]]] = {}
        self.dm_requests: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
        self.request_channels: dict[int, int] = {}
        self.panel_settings: dict[int, dict[str, Optional[int]]] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._panel_bump_guards: dict[int, float] = {}
        self._expiry_task: Optional[asyncio.Task[None]] = None
        super().__init__()

    async def cog_load(self) -> None:
        def _load_all() -> dict[str, Any]:
            init_db(self.ctx.db_path)
            return {
                "consent_pairs": load_consent_pairs(self.ctx.db_path),
                "dm_requests": load_requests(self.ctx.db_path),
                "request_channels": load_request_channels(self.ctx.db_path),
                "panel_settings": load_panel_settings(self.ctx.db_path),
            }

        loaded = await asyncio.to_thread(_load_all)
        self.consent_pairs = loaded["consent_pairs"]
        self.dm_requests = loaded["dm_requests"]
        self.request_channels = loaded["request_channels"]
        self.panel_settings = loaded["panel_settings"]

        # Persistent views: clicks on DM consent buttons across ALL DMs route
        # to this single instance, which recovers per-request state from the DB.
        self.bot.add_view(DmRequestPanelView(self))
        self.bot.add_view(AskConsentView(self))

        # The expiry loop sweeps stale 24h+ pending requests. Its first
        # iteration runs once the bot is ready, which handles any requests
        # that aged out while the bot was offline.
        self._expiry_task = asyncio.create_task(self._expiry_loop())

    async def cog_unload(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            self._expiry_task = None

    # ── Background tasks ─────────────────────────────────────────────────────

    async def _expiry_loop(self) -> None:
        """Periodic sweep that marks 24h+ pending DM requests as expired."""
        await self.bot.wait_until_ready()
        try:
            while not self.bot.is_closed():
                try:
                    await self._expire_stale_now()
                except Exception:
                    log.exception("DM request expiry sweep failed")
                await asyncio.sleep(EXPIRY_SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _expire_stale_now(self) -> None:
        expired = await asyncio.to_thread(
            expire_stale_pending_requests,
            self.ctx.db_path,
            max_age_seconds=REQUEST_TIMEOUT_SECONDS,
        )
        for row in expired:
            gid = row["guild_id"]
            self._drop_request_from_memory(gid, row["requester_id"], row["target_id"])
            guild = self.bot.get_guild(gid)
            if guild is None:
                continue
            requester = guild.get_member(row["requester_id"])
            target = guild.get_member(row["target_id"])
            requester_name = requester.display_name if requester else str(row["requester_id"])
            target_name = target.display_name if target else str(row["target_id"])
            req_type = row["request_type"]
            write_audit_log(
                self.ctx.db_path, gid, "request_expired",
                user_a_id=row["requester_id"], user_b_id=row["target_id"],
                notes=f"type={req_type}",
            )
            await self._post_audit(
                guild,
                f"DM request expired: {requester_name} ➝ {target_name} "
                f"({request_type_label(req_type)})",
            )
            if requester:
                exp_embed = discord.Embed(
                    title="⌛ Request expired",
                    description=(
                        f"Your {request_type_label(req_type).lower()} request to "
                        f"**{target_name}** in **{guild.name}** "
                        f"expired after {REQUEST_TIMEOUT_LABEL} without a response."
                    ),
                    color=DM_PENDING,
                )
                await _safe_dm(requester, embed=exp_embed)

    # ── State helpers ────────────────────────────────────────────────────────

    def _drop_request_from_memory(
        self, guild_id: int, requester_id: int, target_id: int
    ) -> None:
        guild_reqs = self.dm_requests.get(guild_id)
        if guild_reqs is not None:
            guild_reqs.pop((requester_id, target_id), None)

    def _audit_channel_for(self, guild_id: int) -> Optional[int]:
        """Read fresh from DB so changes via the web UI take effect immediately."""
        # Reuses load_audit_channels — small dict for all guilds; fine for the
        # expected scale and avoids a stale cache after web-side edits.
        channels = load_audit_channels(self.ctx.db_path)
        ch = channels.get(guild_id)
        return int(ch) if ch else None

    async def _post_audit(self, guild: discord.Guild, message: str) -> None:
        await post_audit_event(guild, self._audit_channel_for(guild.id), message)

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _is_mutual(self, guild_id: int, a: int, b: int) -> bool:
        pairs = self.consent_pairs.get(guild_id, set())
        return (a, b) in pairs and (b, a) in pairs

    def _has_pending_request(self, guild_id: int, a: int, b: int) -> bool:
        return (a, b) in self.dm_requests.get(guild_id, {})

    def _get_panel_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._panel_locks:
            self._panel_locks[guild_id] = asyncio.Lock()
        return self._panel_locks[guild_id]

    def _precheck_dm_request(self, guild: discord.Guild, requester: discord.Member, target: discord.Member | discord.User) -> Optional[str]:
        if not isinstance(target, discord.Member):
            return "I couldn't check that user's DM preference — they may not be in this server."
        if target.id == requester.id:
            return "You can't send a request to yourself!"
        if target.bot:
            return "Bots don't accept DM requests."
        mode = resolve_mode(target)
        if mode == "closed":
            return f"{target.display_name} isn't accepting DM requests right now."
        if mode == "open":
            return f"{target.display_name} has their DMs open — no request needed, just message them!"
        if self._is_mutual(guild.id, requester.id, target.id):
            return "You two already have a connection — no need to request again."
        if self._has_pending_request(guild.id, requester.id, target.id):
            return "You already have a pending request to them — wait for them to respond."
        return None

    async def _submit_dm_request(
        self,
        interaction: discord.Interaction,
        user: discord.Member | discord.User,
        request_type: str,
        reason: str,
    ) -> None:
        assert interaction.guild and interaction.user
        guild = interaction.guild
        requester = interaction.user
        req_type = normalize_request_type(request_type or "dm")
        # Modal already enforces MAX_REASON_LENGTH; this clamp is defence-in-depth
        # for callers that bypass the modal flow.
        if len(reason) > MAX_REASON_LENGTH:
            reason_clean = reason[: MAX_REASON_LENGTH - 1] + "…"
        else:
            reason_clean = reason

        error = self._precheck_dm_request(guild, requester, user)  # type: ignore[arg-type]
        if error:
            if interaction.response.is_done():
                await interaction.followup.send(error, ephemeral=True)
            else:
                await interaction.response.send_message(error, ephemeral=True)
            return

        # Per-requester rate limit: cap concurrent pending requests so a single
        # user can't spam DM prompts to dozens of targets at once.
        pending_count = count_pending_for_requester(
            self.ctx.db_path, guild.id, requester.id
        )
        if pending_count >= MAX_PENDING_PER_REQUESTER:
            limit_msg = (
                f"You already have {pending_count} pending DM requests. "
                f"Wait for some to be answered or expire (max {MAX_PENDING_PER_REQUESTER})."
            )
            if interaction.response.is_done():
                await interaction.followup.send(limit_msg, ephemeral=True)
            else:
                await interaction.response.send_message(limit_msg, ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="📨 Someone wants to connect with you",
            description=(
                f"A member of **{guild.name}** would like to connect.\n\n"
                f"This request expires in {REQUEST_TIMEOUT_LABEL}."
            ),
            color=DM_PRIMARY,
        )
        embed.set_author(name=requester.display_name, icon_url=requester.display_avatar.url)
        embed.set_footer(text="You can revoke this permission at any time with /dm_revoke")
        embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
        embed.add_field(name="Reason", value=_safe_field_text(reason_clean), inline=False)

        message = await _safe_dm(user, embed=embed, view=AskConsentView(self))
        if message is None:
            await interaction.followup.send(
                "I couldn't DM that user — they may have DMs disabled.", ephemeral=True
            )
            return

        now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
        self.dm_requests.setdefault(guild.id, {})
        self.dm_requests[guild.id][(requester.id, user.id)] = {
            "request_type": req_type, "reason": reason_clean,
            "message_id": message.id, "channel_id": None,
            "created_at": now_ts, "status": "pending",
        }
        upsert_request(
            self.ctx.db_path, guild.id, requester.id, user.id,
            req_type, reason_clean, message.id, None,
        )

        sender_embed = discord.Embed(
            title="📨 Request sent!",
            description=(
                f"Your {request_type_label(req_type).lower()} request to **{user.display_name}** "
                f"in **{guild.name}** has been delivered.\n\nYou'll get a DM when they respond. "
                f"The request expires in {REQUEST_TIMEOUT_LABEL}."
            ),
            color=DM_PRIMARY,
        )
        sender_embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
        sender_embed.add_field(name="Reason", value=_safe_field_text(reason_clean), inline=False)
        await _safe_dm(requester, embed=sender_embed)

        write_audit_log(
            self.ctx.db_path, guild.id, "request_asked",
            actor_id=requester.id, user_a_id=requester.id, user_b_id=user.id,
            notes=f"type={req_type}",
        )
        await self._post_audit(
            guild,
            f"DM request asked: {requester.display_name} ➝ "
            f"{user.display_name} ({request_type_label(req_type)})",
        )
        await interaction.followup.send(
            f"📨 Request sent to {user.display_name} via DM!", ephemeral=True
        )

    async def _ensure_panel(
        self, guild: discord.Guild, panel_channel_id: int, *, force_repost: bool = False
    ) -> Optional[int]:
        async with self._get_panel_lock(guild.id):
            channel = guild.get_channel(panel_channel_id)
            if not isinstance(channel, discord.TextChannel):
                return None

            settings = self.panel_settings.get(guild.id, {})
            old_msg_id = settings.get("panel_message_id")

            if force_repost and old_msg_id:
                try:
                    latest = None
                    async for msg in channel.history(limit=1):
                        latest = msg
                    if latest and latest.id == old_msg_id:
                        force_repost = False
                except (discord.Forbidden, discord.HTTPException):
                    pass

            if old_msg_id and not force_repost:
                try:
                    existing = await channel.fetch_message(old_msg_id)
                    await existing.edit(embed=build_panel_embed(), view=DmRequestPanelView(self))
                    return existing.id
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            try:
                new_msg = await channel.send(embed=build_panel_embed(), view=DmRequestPanelView(self))
            except (discord.Forbidden, discord.HTTPException):
                return None

            if old_msg_id and old_msg_id != new_msg.id:
                try:
                    old = await channel.fetch_message(old_msg_id)
                    await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

            self.panel_settings[guild.id] = {
                "panel_channel_id": panel_channel_id,
                "panel_message_id": new_msg.id,
            }
            set_panel_settings(self.ctx.db_path, guild.id, panel_channel_id, new_msg.id)
            return new_msg.id

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener("on_message")
    async def _on_message_panel_bump(self, message: discord.Message) -> None:
        if not message.guild or message.author.bot:
            return
        settings = self.panel_settings.get(message.guild.id)
        if not isinstance(settings, dict):
            return
        panel_channel_id = settings.get("panel_channel_id")
        panel_message_id = settings.get("panel_message_id")
        if panel_channel_id is None:
            return
        if message.channel.id != panel_channel_id:
            return
        if panel_message_id is not None and message.id == panel_message_id:
            return

        now = time.monotonic()
        last = self._panel_bump_guards.get(message.guild.id)
        if last is not None and (now - last) < PANEL_BUMP_COOLDOWN_SECONDS:
            return
        self._panel_bump_guards[message.guild.id] = now
        await self._ensure_panel(message.guild, panel_channel_id, force_repost=True)

    @commands.Cog.listener("on_member_update")
    async def _on_member_update_dm_roles(
        self, _before: discord.Member, after: discord.Member
    ) -> None:
        dm_roles = [r for r in after.roles if r.name in DM_ROLE_NAMES]
        if len(dm_roles) <= 1:
            return
        keep = max(dm_roles, key=lambda r: r.position)
        to_remove = [r for r in dm_roles if r != keep]
        try:
            await after.remove_roles(*to_remove, reason="DM role dedup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not dedup DM roles for member %s in guild %s: %s",
                after.id, after.guild.id, exc,
            )

    # ── User commands ────────────────────────────────────────────────────────

    @app_commands.command(name="dm_help", description="Show an overview of the DM request system.")
    @app_commands.guild_only()
    async def dm_help(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        embed = discord.Embed(
            title="📬 DM Request System",
            description="Control how users may request DM access with you.",
            color=DM_PRIMARY,
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.add_field(
            name="Your DM Modes",
            value="**OPEN** — Anyone may DM.\n**ASK** — You must approve requests.\n**CLOSED** — DM requests are blocked.",
            inline=False,
        )
        embed.add_field(
            name="Your Commands",
            value=(
                "`/dm_set_mode` — Set your DM preference\n"
                "`/dm_revoke @user` — Revoke relationship\n"
                "`/dm_status @user` — Check relationship status\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="Moderator Tools",
            value=(
                "`/dm_request_panel_refresh` — Repost the request panel as the newest "
                "message in its channel. Panels are set up via `/setup` and the web "
                "dashboard."
            ),
            inline=False,
        )
        embed.set_footer(text="DM relationships are logged for audit transparency.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="dm_set_mode", description="Set your DM request mode.")
    @app_commands.guild_only()
    @app_commands.choices(mode=[
        app_commands.Choice(name="open", value="open"),
        app_commands.Choice(name="ask", value="ask"),
        app_commands.Choice(name="closed", value="closed"),
    ])
    @app_commands.describe(mode="Choose your DM mode")
    async def dm_set_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        assert isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True)
        try:
            await set_member_dm_mode(interaction.user, mode.value)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to manage roles here.", ephemeral=True)
            return
        embed = discord.Embed(
            title="DM preference updated",
            description=f"You're now set to **{mode.value.upper()}**.",
            color=DM_PRIMARY,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="dm_revoke", description="Remove DM permission relationship with another user.")
    @app_commands.guild_only()
    @app_commands.describe(user="User to revoke permission with")
    async def dm_revoke(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild and interaction.user
        guild_id = interaction.guild.id
        pair_set = self.consent_pairs.get(guild_id, set())
        meta = get_consent_pair_meta(
            self.ctx.db_path, guild_id, interaction.user.id, user.id
        )
        db_removed = remove_consent_pair(
            self.ctx.db_path, guild_id, interaction.user.id, user.id
        )
        in_memory_removed = False
        if (interaction.user.id, user.id) in pair_set:
            pair_set.discard((interaction.user.id, user.id))
            in_memory_removed = True
        if (user.id, interaction.user.id) in pair_set:
            pair_set.discard((user.id, interaction.user.id))
            in_memory_removed = True

        if not (db_removed or in_memory_removed):
            await interaction.response.send_message(
                f"You don't have a connection with {user.display_name}.", ephemeral=True
            )
            return

        revoked_embed = discord.Embed(
            title="🚫 Connection removed",
            description=(
                f"**{interaction.user.display_name}** ↔ **{user.display_name}**\n\n"
                "The DM connection between you two has been removed."
            ),
            color=DM_DENY,
        )
        revoked_embed.add_field(
            name="Request Type",
            value=request_type_label(meta.get("type") if meta else None),
            inline=True,
        )
        revoked_embed.add_field(
            name="Reason",
            value=_safe_field_text(meta.get("reason") if meta else None),
            inline=False,
        )

        if meta and meta.get("source_msg_id") and meta.get("source_channel_id"):
            channel = interaction.guild.get_channel(meta["source_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(meta["source_msg_id"])
                    await msg.edit(embed=revoked_embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await _safe_dm(interaction.user, embed=revoked_embed)
        await _safe_dm(user, embed=revoked_embed)

        write_audit_log(
            self.ctx.db_path, guild_id, "relationship_revoked",
            actor_id=interaction.user.id, user_a_id=interaction.user.id, user_b_id=user.id,
        )
        await self._post_audit(
            interaction.guild,
            f"DM permission revoked: {interaction.user.display_name} ↔ "
            f"{user.display_name} (by {interaction.user.display_name})",
        )
        await interaction.response.send_message(
            f"Done — your connection with {user.mention} has been removed."
        )

    @app_commands.command(name="dm_status", description="Check whether mutual DM permission exists with a user.")
    @app_commands.guild_only()
    @app_commands.describe(user="User to check permission status with")
    async def dm_status(self, interaction: discord.Interaction, user: discord.Member) -> None:
        assert interaction.guild and interaction.user
        mutual = self._is_mutual(interaction.guild.id, interaction.user.id, user.id)
        result = "✅ You two are connected." if mutual else "❌ No connection yet."
        await interaction.response.send_message(
            f"**DM status — you & {user.display_name}**\n\n{result}", ephemeral=True
        )

    # ── Admin commands ────────────────────────────────────────────────────────

    @app_commands.command(name="dm_request_panel_refresh", description="Repost the DM request panel so it is the newest message.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    async def dm_request_panel_refresh(self, interaction: discord.Interaction) -> None:
        assert interaction.guild
        settings = self.panel_settings.get(interaction.guild.id, {})
        panel_channel_id = settings.get("panel_channel_id")
        if panel_channel_id is None:
            await interaction.response.send_message(
                "No panel is set up yet — use `/dm_request_panel_set` to get started.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        message_id = await self._ensure_panel(interaction.guild, panel_channel_id, force_repost=True)
        if message_id is None:
            await interaction.followup.send(
                "Couldn't refresh the panel — I may not have permission to post in that channel.", ephemeral=True
            )
            return
        await interaction.followup.send("✅ Panel bumped to the bottom.", ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(DmPermsCog(bot, bot.ctx))
