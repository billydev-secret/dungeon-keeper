"""DM permission cog — ported from accord_bot (dm_perms_bot)."""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from services.embeds import DM_ACCEPT, DM_DENY, DM_PENDING, DM_PRIMARY
from services.dm_perms_service import (
    DM_ROLE_NAMES,
    add_consent_pair,
    build_panel_embed,
    get_consent_pair_meta,
    init_db,
    load_audit_channels,
    load_consent_pairs,
    load_panel_settings,
    load_request_channels,
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


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class AskConsentView(discord.ui.View):
    def __init__(
        self,
        cog: DmPermsCog,
        requester_id: int,
        target_id: int,
        guild_id: int,
        request_type: str = "dm",
        reason: str = "",
    ) -> None:
        super().__init__(timeout=86400)
        self.cog = cog
        self.requester_id = requester_id
        self.target_id = target_id
        self.guild_id = guild_id
        self.request_type = normalize_request_type(request_type)
        self.reason = (reason or "").strip()
        self.message: Optional[discord.Message] = None

    def _remove_from_state(self) -> None:
        guild_reqs = self.cog.dm_requests.get(self.guild_id, {})
        guild_reqs.pop((self.requester_id, self.target_id), None)
        remove_request(self.cog.ctx.db_path, self.guild_id, self.requester_id, self.target_id)

    async def on_timeout(self) -> None:
        if self.message:
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            timeout_embed = discord.Embed(
                title="⌛ Request expired",
                description="This one didn't get a response in time — it's been 24 hours.",
                color=DM_PENDING,
            )
            timeout_embed.add_field(name="Request Type", value=request_type_label(self.request_type), inline=True)
            timeout_embed.add_field(name="Reason", value=self.reason or "—", inline=False)
            try:
                await self.message.edit(embed=timeout_embed, view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        self._remove_from_state()

        guild = self.cog.bot.get_guild(self.guild_id)
        if guild:
            requester = guild.get_member(self.requester_id)
            target = guild.get_member(self.target_id)
            if requester:
                exp_embed = discord.Embed(
                    title="⌛ Request expired",
                    description=(
                        f"Your {request_type_label(self.request_type).lower()} request "
                        f"to **{target.display_name if target else self.target_id}** in **{guild.name}** "
                        "expired after 24 hours without a response."
                    ),
                    color=DM_PENDING,
                )
                exp_embed.add_field(name="Request Type", value=request_type_label(self.request_type), inline=True)
                exp_embed.add_field(name="Reason", value=self.reason or "—", inline=False)
                await _safe_dm(requester, embed=exp_embed)
            write_audit_log(
                self.cog.ctx.db_path, guild.id, "request_expired",
                user_a_id=self.requester_id, user_b_id=self.target_id,
                notes=f"type={self.request_type}",
            )
            audit_ch = self.cog.audit_channels.get(guild.id)
            requester_name = requester.display_name if requester else str(self.requester_id)
            target_name = target.display_name if target else str(self.target_id)
            await post_audit_event(
                guild, audit_ch,
                f"DM request expired: {requester_name} ➝ {target_name} ({request_type_label(self.request_type)})",
            )

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("This request isn't for you.", ephemeral=True)
            return

        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("Couldn't find the server for this request.", ephemeral=True)
            return
        requester = guild.get_member(self.requester_id)
        target = guild.get_member(self.target_id)
        if not requester or not target:
            await interaction.response.send_message(
                "Couldn't find one or both users in this server.", ephemeral=True
            )
            return

        self.cog.consent_pairs.setdefault(self.guild_id, set())
        self.cog.consent_pairs[self.guild_id].add((self.requester_id, self.target_id))
        self.cog.consent_pairs[self.guild_id].add((self.target_id, self.requester_id))

        source_channel_id = getattr(getattr(self.message, "channel", None), "id", None)
        source_msg_id = getattr(self.message, "id", None)
        add_consent_pair(
            self.cog.ctx.db_path, self.guild_id, self.requester_id, self.target_id,
            rel_type=self.request_type, reason=self.reason,
            source_msg_id=source_msg_id, source_channel_id=source_channel_id,
        )
        self._remove_from_state()

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        success_embed = discord.Embed(
            title="✅ Connection accepted!",
            color=DM_ACCEPT,
        )
        success_embed.description = (
            f"**{requester.display_name}** ↔ **{target.display_name}**\n\n"
            f"{requester.mention} and {target.mention} can now DM each other.\n\n"
            "Either of you can undo this at any time with `/dm_revoke`."
        )
        success_embed.add_field(name="Request Type", value=request_type_label(self.request_type), inline=True)
        success_embed.add_field(name="Reason", value=self.reason or "—", inline=False)

        await interaction.response.edit_message(embed=success_embed, view=self)
        self.stop()
        await _safe_dm(requester, embed=success_embed)
        await _safe_dm(target, embed=success_embed)

        write_audit_log(
            self.cog.ctx.db_path, guild.id, "request_accepted",
            actor_id=self.target_id, user_a_id=self.requester_id, user_b_id=self.target_id,
            notes=f"type={self.request_type}",
        )
        audit_ch = self.cog.audit_channels.get(guild.id)
        await post_audit_event(
            guild, audit_ch,
            f"DM request accepted: {requester.display_name} ↔ {target.display_name} ({request_type_label(self.request_type)})",
        )

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message("This request isn't for you.", ephemeral=True)
            return

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        deny_embed = discord.Embed(
            title="❌ Request declined",
            description="No worries — the request was turned down.",
            color=DM_DENY,
        )
        deny_embed.add_field(name="Request Type", value=request_type_label(self.request_type), inline=True)
        deny_embed.add_field(name="Reason", value=self.reason or "—", inline=False)

        await interaction.response.edit_message(embed=deny_embed, view=self)
        self.stop()
        self._remove_from_state()

        guild = self.cog.bot.get_guild(self.guild_id)
        if guild:
            requester = guild.get_member(self.requester_id)
            target = guild.get_member(self.target_id)
            if requester:
                req_embed = discord.Embed(
                    title="❌ Request declined",
                    description=(
                        f"Your {request_type_label(self.request_type).lower()} request "
                        f"to **{target.display_name if target else self.target_id}** in **{guild.name}** was declined."
                    ),
                    color=DM_DENY,
                )
                req_embed.add_field(name="Request Type", value=request_type_label(self.request_type), inline=True)
                req_embed.add_field(name="Reason", value=self.reason or "—", inline=False)
                await _safe_dm(requester, embed=req_embed)
            write_audit_log(
                self.cog.ctx.db_path, guild.id, "request_denied",
                actor_id=self.target_id, user_a_id=self.requester_id, user_b_id=self.target_id,
                notes=f"type={self.request_type}",
            )
            audit_ch = self.cog.audit_channels.get(guild.id)
            requester_name = requester.display_name if requester else str(self.requester_id)
            target_name = target.display_name if target else str(self.target_id)
            await post_audit_event(
                guild, audit_ch,
                f"DM request denied: {requester_name} ➝ {target_name} ({request_type_label(self.request_type)})",
            )


class DmRequestLookupView(discord.ui.View):
    """Ephemeral user-select + request type + continue button."""

    def __init__(self, cog: DmPermsCog) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self._selected_user: Optional[discord.Member | discord.User] = None
        self._request_type: str = "dm"

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Select a user", min_values=1, max_values=1)
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        self._selected_user = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="Direct Message", style=discord.ButtonStyle.secondary, row=1)
    async def type_dm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._request_type = "dm"
        button.style = discord.ButtonStyle.primary
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Friend Request":
                child.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Friend Request", style=discord.ButtonStyle.secondary, row=1)
    async def type_friend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._request_type = "friend"
        button.style = discord.ButtonStyle.primary
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Direct Message":
                child.style = discord.ButtonStyle.secondary
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
        max_length=256,
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
        self.audit_channels: dict[int, int] = {}
        self.panel_settings: dict[int, dict[str, Optional[int]]] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._panel_bump_guards: dict[int, datetime.datetime] = {}
        super().__init__()

    async def cog_load(self) -> None:
        init_db(self.ctx.db_path)
        self.consent_pairs = load_consent_pairs(self.ctx.db_path)
        self.dm_requests = load_requests(self.ctx.db_path)
        self.request_channels = load_request_channels(self.ctx.db_path)
        self.audit_channels = load_audit_channels(self.ctx.db_path)
        self.panel_settings = load_panel_settings(self.ctx.db_path)
        self.bot.add_view(DmRequestPanelView(self))

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
        reason_clean = reason[:253] + "..." if len(reason) > 256 else reason

        error = self._precheck_dm_request(guild, requester, user)  # type: ignore[arg-type]
        if error:
            if interaction.response.is_done():
                await interaction.followup.send(error, ephemeral=True)
            else:
                await interaction.response.send_message(error, ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="📨 Someone wants to connect with you",
            description=(
                f"A member of **{guild.name}** would like to connect.\n\n"
                "This request expires in 24 hours."
            ),
            color=DM_PRIMARY,
        )
        embed.set_author(name=requester.display_name, icon_url=requester.display_avatar.url)
        embed.set_footer(text="You can revoke this permission at any time with /dm_revoke")
        embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
        embed.add_field(name="Reason", value=reason_clean or "—", inline=False)

        view = AskConsentView(
            self, requester_id=requester.id, target_id=user.id,
            guild_id=guild.id, request_type=req_type, reason=reason_clean,
        )
        message = await _safe_dm(user, embed=embed, view=view)
        if message is None:
            await interaction.followup.send(
                "I couldn't DM that user — they may have DMs disabled.", ephemeral=True
            )
            return
        view.message = message

        self.dm_requests.setdefault(guild.id, {})
        self.dm_requests[guild.id][(requester.id, user.id)] = {
            "request_type": req_type, "reason": reason_clean,
            "message_id": message.id, "channel_id": None,
            "created_at": datetime.datetime.utcnow().timestamp(), "status": "pending",
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
                "The request expires in 24 hours."
            ),
            color=DM_PRIMARY,
        )
        sender_embed.add_field(name="Request Type", value=request_type_label(req_type), inline=True)
        sender_embed.add_field(name="Reason", value=reason_clean or "—", inline=False)
        await _safe_dm(requester, embed=sender_embed)

        write_audit_log(
            self.ctx.db_path, guild.id, "request_asked",
            actor_id=requester.id, user_a_id=requester.id, user_b_id=user.id,
            notes=f"type={req_type}",
        )
        audit_ch = self.audit_channels.get(guild.id)
        await post_audit_event(
            guild, audit_ch,
            f"DM request asked: {requester.display_name} ➝ {user.display_name} ({request_type_label(req_type)})",
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

        now = datetime.datetime.utcnow()
        last = self._panel_bump_guards.get(message.guild.id)
        if last and (now - last).total_seconds() < 2:
            return
        self._panel_bump_guards[message.guild.id] = now
        await self._ensure_panel(message.guild, panel_channel_id, force_repost=True)

    @commands.Cog.listener("on_member_update")
    async def _on_member_update_dm_roles(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        dm_roles = [r for r in after.roles if r.name in DM_ROLE_NAMES]
        if len(dm_roles) <= 1:
            return
        keep = max(dm_roles, key=lambda r: r.position)
        to_remove = [r for r in dm_roles if r != keep]
        try:
            await after.remove_roles(*to_remove, reason="DM role dedup")
        except (discord.Forbidden, discord.HTTPException):
            pass

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
            value="`/dm_request_panel_refresh` — Repost panel\n",
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
        removed = False
        if (interaction.user.id, user.id) in pair_set:
            pair_set.discard((interaction.user.id, user.id))
            removed = True
        if (user.id, interaction.user.id) in pair_set:
            pair_set.discard((user.id, interaction.user.id))
            removed = True
        if not removed:
            await interaction.response.send_message(
                f"You don't have a connection with {user.display_name}.", ephemeral=True
            )
            return

        meta = get_consent_pair_meta(self.ctx.db_path, guild_id, interaction.user.id, user.id)
        remove_consent_pair(self.ctx.db_path, guild_id, interaction.user.id, user.id)

        revoked_embed = discord.Embed(
            title="🚫 Connection removed",
            description=(
                f"**{interaction.user.display_name}** ↔ **{user.display_name}**\n\n"
                "The DM connection between you two has been removed."
            ),
            color=DM_DENY,
        )
        revoked_embed.add_field(name="Request Type", value=request_type_label(meta.get("type") if meta else None), inline=True)
        revoked_embed.add_field(name="Reason", value=(meta.get("reason") or "—") if meta else "—", inline=False)

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
        audit_ch = self.audit_channels.get(guild_id)
        await post_audit_event(
            interaction.guild, audit_ch,
            f"DM permission revoked: {interaction.user.display_name} ↔ {user.display_name} (by {interaction.user.display_name})",
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
