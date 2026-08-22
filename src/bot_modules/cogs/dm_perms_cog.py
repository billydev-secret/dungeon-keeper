"""DM permission cog — ported from accord_bot (dm_perms_bot)."""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import discord
from discord.ext import commands

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.services.dm_branding import send_branded_dm
from bot_modules.core.sticky import PanelContent, StickyPanel
from bot_modules.dm_perms.embeds import (
    build_acceptance_embed,
    build_denial_embed_for_requester,
    build_denial_embed_for_view,
    build_dm_settings_embed,
    build_expired_embed,
    build_guild_unavailable_embed,
    build_request_dm_embed,
    build_request_sent_embed,
    build_revoked_embed,
    build_stale_request_embed,
)
from bot_modules.dm_perms.logic import (
    audit_line_accepted,
    audit_line_asked,
    audit_line_denied,
    audit_line_expired,
    audit_line_revoked,
    clamp_reason,
    classify_dm_request,
    discard_consent_pair,
    display_name_for,
    dm_status_text,
    pick_dm_roles_to_remove,
)
from bot_modules.services.dm_perms_service import (
    add_consent_pair,
    build_panel_embed,
    count_pending_for_requester,
    expire_stale_pending_requests,
    get_consent_pair_meta,
    init_db,
    is_dm_mode_role,
    load_audit_channels,
    load_consent_pairs,
    load_dm_mode_roles,
    load_panel_settings,
    load_request_by_message_id,
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
from bot_modules.services.no_contact_service import is_no_contact

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

DM_REQUEST_PANEL_CUSTOM_ID = "dm_request:open_modal"
DM_SETTINGS_PANEL_CUSTOM_ID = "dm_request:open_settings"
DM_CONSENT_ACCEPT_CUSTOM_ID = "dm_consent:accept"
DM_CONSENT_DENY_CUSTOM_ID = "dm_consent:deny"
DM_CONSENT_DENY_REPLY_CUSTOM_ID = "dm_consent:deny_reply"

REQUEST_TIMEOUT_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_LABEL = "24 hours"
EXPIRY_SWEEP_INTERVAL_SECONDS = 60 * 60  # hourly
MAX_PENDING_PER_REQUESTER = 5
MAX_REASON_LENGTH = 250  # leave headroom under the embed-field char ceiling


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

    @discord.ui.button(
        label="Deny With Reply",
        style=discord.ButtonStyle.secondary,
        custom_id=DM_CONSENT_DENY_REPLY_CUSTOM_ID,
    )
    async def deny_with_reply(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._open_deny_reply_modal(interaction)

    async def _open_deny_reply_modal(
        self, interaction: discord.Interaction
    ) -> None:
        """Validate the click, then open a modal for the denier's note.

        Resolution/validation here must not consume the interaction response
        (``send_modal`` needs an un-responded interaction), so it only does DB
        reads before either erroring out or sending the modal.
        """
        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "❌ Couldn't find the request for this button.", ephemeral=True
            )
            return

        record = load_request_by_message_id(self.cog.bot.ctx.db_path, message.id)
        if record is None:
            try:
                await interaction.response.edit_message(
                    embed=build_stale_request_embed(), view=None
                )
            except (discord.NotFound, discord.HTTPException):
                pass
            return

        if interaction.user.id != record["target_id"]:
            await interaction.response.send_message(
                "❌ This request isn't for you.", ephemeral=True
            )
            return

        await interaction.response.send_modal(DmDenyReplyModal(self, message))

    async def _handle_click(
        self, interaction: discord.Interaction, *, accepted: bool
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(
                "❌ Couldn't find the request for this button.", ephemeral=True
            )
            return

        record = load_request_by_message_id(self.cog.bot.ctx.db_path, message.id)
        if record is None:
            try:
                await interaction.response.edit_message(
                    embed=build_stale_request_embed(), view=None
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
                "❌ This request isn't for you.", ephemeral=True
            )
            return

        guild = self.cog.bot.get_guild(guild_id)
        if guild is None:
            # Bot was removed from the guild after the request was sent.
            self.cog._drop_request_from_memory(guild_id, requester_id, target_id)
            remove_request(self.cog.bot.ctx.db_path, guild_id, requester_id, target_id)
            try:
                await interaction.response.edit_message(
                    embed=build_guild_unavailable_embed(), view=None
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
                "❌ Couldn't find one or both users in this server.", ephemeral=True
            )
            return

        self.cog.consent_pairs.setdefault(guild.id, set())
        self.cog.consent_pairs[guild.id].add((requester_id, target_id))
        self.cog.consent_pairs[guild.id].add((target_id, requester_id))

        add_consent_pair(
            self.cog.bot.ctx.db_path, guild.id, requester_id, target_id,
            rel_type=req_type, reason=reason,
            source_msg_id=source_msg_id, source_channel_id=source_channel_id,
        )
        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.bot.ctx.db_path, guild.id, requester_id, target_id)

        type_label = request_type_label(req_type)
        success_embed = build_acceptance_embed(
            requester_display_name=requester.display_name,
            target_display_name=target.display_name,
            requester_mention=requester.mention,
            target_mention=target.mention,
            type_label=type_label,
            reason=reason,
        )

        await interaction.response.edit_message(embed=success_embed, view=None)
        await _dm(requester, self.cog.bot.ctx.db_path, guild, embed=success_embed)
        await _dm(target, self.cog.bot.ctx.db_path, guild, embed=success_embed)

        write_audit_log(
            self.cog.bot.ctx.db_path, guild.id, "request_accepted",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}",
        )
        await self.cog._post_audit(
            guild,
            audit_line_accepted(requester.display_name, target.display_name, type_label),
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
        type_label = request_type_label(req_type)
        deny_embed = build_denial_embed_for_view(type_label=type_label, reason=reason)

        # Edit first: if this raises we bail before touching the DB, leaving the
        # request pending rather than half-resolved.
        await interaction.response.edit_message(embed=deny_embed, view=None)

        await self._finalize_deny(
            guild=guild,
            requester=requester,
            target=target,
            requester_id=requester_id,
            target_id=target_id,
            req_type=req_type,
            reason=reason,
        )

    async def _finalize_deny(
        self,
        *,
        guild: discord.Guild,
        requester: Optional[discord.Member],
        target: Optional[discord.Member],
        requester_id: int,
        target_id: int,
        req_type: str,
        reason: str,
        reply: str = "",
    ) -> None:
        """Drop the request, notify the requester, and audit the denial.

        Shared by the plain Deny button and the "Deny with reply" modal; the
        caller is responsible for having already updated the target's own DM
        message. ``reply`` is the denier's optional note to the requester.
        """
        type_label = request_type_label(req_type)

        self.cog._drop_request_from_memory(guild.id, requester_id, target_id)
        remove_request(self.cog.bot.ctx.db_path, guild.id, requester_id, target_id)

        if requester:
            target_name = target.display_name if target else str(target_id)
            req_embed = build_denial_embed_for_requester(
                target_display_name=target_name,
                guild_name=guild.name,
                type_label=type_label,
                reason=reason,
                reply=reply,
            )
            await _dm(requester, self.cog.bot.ctx.db_path, guild, embed=req_embed)

        write_audit_log(
            self.cog.bot.ctx.db_path, guild.id, "request_denied",
            actor_id=target_id, user_a_id=requester_id, user_b_id=target_id,
            notes=f"type={req_type}" + ("; replied" if reply else ""),
        )
        requester_name = display_name_for(requester, requester_id)
        target_name = display_name_for(target, target_id)
        await self.cog._post_audit(
            guild,
            audit_line_denied(requester_name, target_name, type_label),
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
        # Pin the pick as the select's default value. A UserSelect carries its
        # chosen user only in the client until the message is edited: the type
        # buttons below re-send this same view with ``edit_message``, and a
        # payload with no ``default_values`` redraws the select empty. The
        # Python-side ``_selected_user`` survived that, so Continue still
        # worked -- but the member watched their pick vanish every time they
        # switched between Direct Message and Friend Request, and picked it
        # again.
        select.default_values = [
            discord.SelectDefaultValue.from_user(self._selected_user)
        ]
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
            await interaction.response.send_message("❌ Please select a user first.", ephemeral=True)
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
        placeholder="Why you'd like to connect…",
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


class DmDenyReplyModal(discord.ui.Modal, title="Decline With a Reply"):
    """Lets the target deny a request while sending a short note to the requester.

    Opened from the "Deny with reply" button. Carries the source DM message so
    the bot can edit it directly on submit (a version-independent alternative to
    editing via the modal-submit interaction), and re-resolves the request by
    that message id so a request answered in the meantime resolves as stale.
    """

    reply = discord.ui.TextInput(
        label="Message to the requester",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=MAX_REASON_LENGTH,
        placeholder="Add a short note to send back with your decline…",
    )

    def __init__(self, view: AskConsentView, message: discord.Message) -> None:
        super().__init__()
        self._view = view
        self._message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reply = str(self.reply.value or "").strip()
        view = self._view

        record = load_request_by_message_id(view.cog.bot.ctx.db_path, self._message.id)
        if record is None:
            await interaction.response.send_message(
                "❌ That request is no longer pending.", ephemeral=True
            )
            return

        guild = view.cog.bot.get_guild(record["guild_id"])
        if guild is None:
            await interaction.response.send_message(
                "❌ That server is no longer available.", ephemeral=True
            )
            return

        requester_id = record["requester_id"]
        target_id = record["target_id"]
        req_type = record["request_type"]
        reason = record["reason"]
        requester = guild.get_member(requester_id)
        target = guild.get_member(target_id)

        deny_embed = build_denial_embed_for_view(
            type_label=request_type_label(req_type), reason=reason, reply=reply
        )
        # Edit the bot's own DM first: on failure, bail before the DB delete so
        # the request stays pending (mirrors the plain-Deny fail-safe ordering).
        try:
            await self._message.edit(embed=deny_embed, view=None)
        except discord.HTTPException:
            log.warning("dm_perms: failed to edit source DM on deny-with-reply")
            await interaction.response.send_message(
                "❌ Something went wrong updating the request — nothing was changed, "
                "please try again.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Declined — your reply was sent to them.", ephemeral=True
        )

        await view._finalize_deny(
            guild=guild,
            requester=requester,
            target=target,
            requester_id=requester_id,
            target_id=target_id,
            req_type=req_type,
            reason=reason,
            reply=reply,
        )


class DmSettingsView(discord.ui.View):
    """Ephemeral, per-member DM settings panel.

    Replaced ``/dm_help``, ``/dm_set_mode``, ``/dm_status`` and ``/dm_revoke``
    (2026-07-28): four top-level commands for one feature, which CLAUDE.md's
    "prefer one ephemeral panel over a sprawl of subcommands" rule calls out
    directly. Reached from the "My DM Settings" button on the public request
    panel.

    Ephemeral and short-lived, so unlike ``DmRequestPanelView`` this holds
    per-member state in memory rather than recovering it from a custom_id.
    """

    _MODE_BUTTON_PREFIX = "dm_settings_mode:"

    def __init__(self, cog: DmPermsCog, member: discord.Member) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.member = member
        self._selected: Optional[discord.Member | discord.User] = None
        self._revoke_button: Optional[discord.ui.Button] = None
        self._pending_mode: Optional[str] = None
        self._sync_mode_styles()

    # ── Rendering ────────────────────────────────────────────────────────

    def _current_mode(self) -> str:
        """The mode to render.

        ``_pending_mode`` shadows the role lookup for the rest of this panel's
        life once the member has set a mode here. ``set_member_dm_mode`` goes
        out over REST; discord.py does not write the result back into the
        cached member, so ``resolve_mode`` keeps returning the *old* mode until
        a MEMBER_UPDATE arrives over the gateway. Without the shadow the panel
        re-renders immediately after a change and contradicts its own "your DM
        mode is now X" confirmation.
        """
        if self._pending_mode is not None:
            return self._pending_mode
        return resolve_mode(self.member, self.cog._mode_roles_for(self.member.guild.id))

    def _sync_mode_styles(self) -> None:
        """Mark the active mode. Same idiom as DmRequestLookupView's type buttons."""
        current = self._current_mode()
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            cid = getattr(child, "custom_id", "") or ""
            if not cid.startswith(self._MODE_BUTTON_PREFIX):
                continue
            this_mode = cid[len(self._MODE_BUTTON_PREFIX):]
            child.style = (
                discord.ButtonStyle.primary
                if this_mode == current
                else discord.ButtonStyle.secondary
            )

    async def _embed(self) -> discord.Embed:
        guild = self.member.guild
        accent = await safe_resolve_accent(self.cog.bot.ctx, guild, log_label="dm perms")
        return build_dm_settings_embed(
            self._current_mode(),
            guild.icon.url if guild.icon else None,
            color=accent,
        )

    def _set_revoke_visible(self, visible: bool, target_name: str = "") -> None:
        """Show the revoke button only when the selected user is actually
        connected — a disabled-but-present button invites a pointless click.

        The relabel on the already-visible path is load-bearing: picking a
        second connected member reuses the existing button, and a stale label
        would name the *previous* member on a button that revokes the current
        one. The button is destructive and names a person, so the label has to
        track ``_selected`` exactly.
        """
        if not visible:
            if self._revoke_button is not None:
                self.remove_item(self._revoke_button)
                self._revoke_button = None
            return

        label = f"Remove connection with {target_name}"[:80]
        if self._revoke_button is not None:
            self._revoke_button.label = label
            return

        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.danger,
            row=2,
        )
        button.callback = self._on_revoke  # type: ignore[method-assign]
        self._revoke_button = button
        self.add_item(button)

    def _sync_select_default(self) -> None:
        """Keep the member being looked at showing in the select on a redraw.

        Every path in this view redraws the whole thing with ``edit_message``,
        and a UserSelect re-sent without ``default_values`` comes back empty —
        so the pick vanished on every mode-button press while the status line
        and the revoke button underneath still named that person. Same defect
        the request picker carried; pinned here rather than in the select's own
        callback because the mode buttons redraw without going through it.
        """
        if self._selected is not None:
            self.user_select.default_values = [
                discord.SelectDefaultValue.from_user(self._selected)
            ]

    async def _rerender(self, interaction: discord.Interaction, note: str = "") -> None:
        """Redraw the panel. Falls back to editing the original response when
        the interaction has already been deferred or answered — ``_on_revoke``
        defers first, and ``edit_message`` is only valid on a fresh one."""
        self._sync_mode_styles()
        self._sync_select_default()
        embed = await self._embed()
        if interaction.response.is_done():
            await interaction.edit_original_response(
                content=note or None, embed=embed, view=self
            )
            return
        await interaction.response.edit_message(
            content=note or None, embed=embed, view=self
        )

    # ── Mode buttons ─────────────────────────────────────────────────────

    async def _set_mode(self, interaction: discord.Interaction, mode: str) -> None:
        # Re-seat the cached member on every click. ``self.member`` is captured
        # when the panel opens and discord.py never writes REST results back
        # into it, so a *second* mode change in one sitting computed its
        # removals from a role set that predated the first — leaving the old
        # mode's role in place, which then raced the dedup listener and could
        # revert the choice the member had just made. The interaction carries a
        # member resolved by the gateway at click time, which is as fresh as
        # this process can get without a REST fetch per click.
        if isinstance(interaction.user, discord.Member):
            self.member = interaction.user
        try:
            await set_member_dm_mode(
                self.member, mode, self.cog._mode_roles_for(self.member.guild.id)
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to manage roles here.", ephemeral=True
            )
            return
        self._pending_mode = mode
        # A mode change left no trace anywhere before this: dm_audit_log only
        # held requests, so "did the bot set this, or did something else take
        # it away?" could only be answered by trawling role_events.
        await asyncio.to_thread(
            write_audit_log,
            self.cog.bot.ctx.db_path,
            self.member.guild.id,
            "mode_set",
            actor_id=self.member.id,
            user_a_id=self.member.id,
            notes=f"mode={mode}",
        )
        await self._rerender(interaction, f"✅ Your DM mode is now **{mode.upper()}**.")

    @discord.ui.button(
        label="Open", style=discord.ButtonStyle.secondary, row=0,
        custom_id="dm_settings_mode:open",
    )
    async def mode_open(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_mode(interaction, "open")

    @discord.ui.button(
        label="Ask", style=discord.ButtonStyle.secondary, row=0,
        custom_id="dm_settings_mode:ask",
    )
    async def mode_ask(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_mode(interaction, "ask")

    @discord.ui.button(
        label="Closed", style=discord.ButtonStyle.secondary, row=0,
        custom_id="dm_settings_mode:closed",
    )
    async def mode_closed(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._set_mode(interaction, "closed")

    # ── Connection lookup / revoke ───────────────────────────────────────

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="Check or remove a connection…",
        min_values=1,
        max_values=1,
        row=1,
    )
    async def user_select(
        self, interaction: discord.Interaction, select: discord.ui.UserSelect
    ) -> None:
        target = select.values[0]
        self._selected = target
        if target.id == self.member.id:
            self._set_revoke_visible(False)
            await self._rerender(interaction, "You can't connect with yourself.")
            return
        mutual = self.cog._is_mutual(self.member.guild.id, self.member.id, target.id)
        self._set_revoke_visible(mutual, target.display_name)
        await self._rerender(
            interaction,
            f"**You & {target.display_name}** — {dm_status_text(mutual)}",
        )

    async def _on_revoke(self, interaction: discord.Interaction) -> None:
        target = self._selected
        if target is None:
            await interaction.response.send_message(
                "Pick someone first.", ephemeral=True
            )
            return
        # Defer first: revoke_connection does several DB reads plus a
        # fetch_message, a message edit, and two DM sends before we could
        # otherwise answer. That runs well past Discord's 3s initial-response
        # window, and a late edit_message raises NotFound — the revoke would
        # succeed while the member saw "This interaction failed" next to a
        # panel still offering the button.
        await interaction.response.defer()
        removed = await self.cog.revoke_connection(
            self.member.guild, self.member, target
        )
        self._set_revoke_visible(False)
        note = (
            f"Done — your connection with {target.display_name} has been removed."
            if removed
            else f"❌ You don't have a connection with {target.display_name}."
        )
        await self._rerender(interaction, note)


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

    @discord.ui.button(
        label="My DM Settings",
        style=discord.ButtonStyle.secondary,
        custom_id=DM_SETTINGS_PANEL_CUSTOM_ID,
    )
    async def open_settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "❌ Use this in the server.", ephemeral=True
            )
            return
        view = DmSettingsView(self.cog, member)
        await interaction.response.send_message(
            embed=await view._embed(), view=view, ephemeral=True
        )


# ---------------------------------------------------------------------------
# Module-level DM helper
# ---------------------------------------------------------------------------

async def _dm(
    user: discord.abc.Messageable,
    db_path: Path,
    guild: Optional[discord.Guild],
    **kwargs: Any,
) -> Optional[discord.Message]:
    """DM a consent notice branded for ``guild``.

    Positional ``db_path``/``guild`` keep the eight call sites terse. These
    embeds put the requesting member in the author slot — the useful thing
    to see on a "someone wants to connect" card — so the server name goes
    in the footer, alongside the audit-transparency line these builders
    already set.
    """
    return await send_branded_dm(user, db_path=db_path, guild=guild, **kwargs)


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class DmPermsCog(commands.Cog):
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self.consent_pairs: dict[int, set[tuple[int, int]]] = {}
        self.dm_requests: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
        self.request_channels: dict[int, int] = {}
        self.panel_settings: dict[int, dict[str, Optional[int]]] = {}
        # Per-guild mode→role-id overrides ({"open"/"ask"/"closed": id}).
        # Loaded at cog_load; the web config route pokes this cache on save.
        self.mode_role_ids: dict[int, dict[str, int]] = {}
        self.panel = StickyPanel(
            "dm perms",
            bot,
            load_ids=self._panel_ids,
            save_ids=self._save_panel_ids,
            build=self._build_panel,
        )
        self._expiry_task: Optional[asyncio.Task[None]] = None
        self._autopost_task: Optional[asyncio.Task[None]] = None
        super().__init__()

    async def cog_load(self) -> None:
        def _load_all() -> dict[str, Any]:
            init_db(self.bot.ctx.db_path)
            return {
                "consent_pairs": load_consent_pairs(self.bot.ctx.db_path),
                "dm_requests": load_requests(self.bot.ctx.db_path),
                "request_channels": load_request_channels(self.bot.ctx.db_path),
                "panel_settings": load_panel_settings(self.bot.ctx.db_path),
                "mode_role_ids": load_dm_mode_roles(self.bot.ctx.db_path),
            }

        loaded = await asyncio.to_thread(_load_all)
        self.consent_pairs = loaded["consent_pairs"]
        self.dm_requests = loaded["dm_requests"]
        self.request_channels = loaded["request_channels"]
        self.panel_settings = loaded["panel_settings"]
        self.mode_role_ids = loaded["mode_role_ids"]
        self._publish_panel_guilds()

        # Persistent views: clicks on DM consent buttons across ALL DMs route
        # to this single instance, which recovers per-request state from the DB.
        self.bot.add_view(DmRequestPanelView(self))
        self.bot.add_view(AskConsentView(self))

        # The expiry loop sweeps stale 24h+ pending requests. Its first
        # iteration runs once the bot is ready, which handles any requests
        # that aged out while the bot was offline.
        self._expiry_task = asyncio.create_task(self._expiry_loop())
        self._autopost_task = asyncio.create_task(self._autopost_panels())

    async def _autopost_panels(self) -> None:
        """Make sure every configured guild's request panel exists on boot.

        The panel is the *only* route to DM settings now that the /dm_* commands
        are gone, so it can't depend on an admin remembering to press "post
        panel" on the dashboard. ``place_or_refresh`` edits in place when the
        panel is already the configured channel's, so this re-runs safely on
        every restart instead of stacking duplicates.

        A guild with no configured panel channel is skipped — there is nowhere
        to put it, and inventing a channel is not this method's call.
        """
        await self.bot.wait_until_ready()
        for guild_id, settings in list(self.panel_settings.items()):
            channel_id = (settings or {}).get("panel_channel_id")
            if not channel_id:
                continue
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                log.warning(
                    "dm_perms: panel channel %s for guild %s is missing or not "
                    "a text channel — skipping autopost",
                    channel_id, guild_id,
                )
                continue
            try:
                await self.panel.place_or_refresh(guild, channel)
            except Exception:
                # Deliberately broad, and per-guild: the loop body also reaches
                # sqlite (accent lookup, panel-id save), so a DB hiccup on one
                # guild must not abort the bootstrap for every guild after it.
                # This runs in a bare task nobody awaits, so an escaping
                # exception would only ever surface as "exception was never
                # retrieved" — and the panel is the only route to DM settings.
                log.exception(
                    "dm_perms: failed to autopost the panel in guild %s", guild_id
                )

    async def cog_unload(self) -> None:
        if self._expiry_task is not None:
            self._expiry_task.cancel()
            self._expiry_task = None
        if self._autopost_task is not None:
            self._autopost_task.cancel()
            self._autopost_task = None
        self.panel.cancel_all()

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
            self.bot.ctx.db_path,
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
            requester_name = display_name_for(requester, row["requester_id"])
            target_name = display_name_for(target, row["target_id"])
            req_type = row["request_type"]
            type_label = request_type_label(req_type)
            write_audit_log(
                self.bot.ctx.db_path, gid, "request_expired",
                user_a_id=row["requester_id"], user_b_id=row["target_id"],
                notes=f"type={req_type}",
            )
            await self._post_audit(
                guild,
                audit_line_expired(requester_name, target_name, type_label),
            )
            if requester:
                exp_embed = build_expired_embed(
                    target_display_name=target_name,
                    guild_name=guild.name,
                    type_label=type_label,
                    request_timeout_label=REQUEST_TIMEOUT_LABEL,
                )
                await _dm(requester, self.bot.ctx.db_path, guild, embed=exp_embed)

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
        channels = load_audit_channels(self.bot.ctx.db_path)
        ch = channels.get(guild_id)
        return int(ch) if ch else None

    async def _post_audit(self, guild: discord.Guild, message: str) -> None:
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="dm perms")
        await post_audit_event(
            guild, self._audit_channel_for(guild.id), message, color=accent
        )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _is_mutual(
        self, guild_id: int, a: int, b: int, *, check_no_contact: bool = True
    ) -> bool:
        # A no-contact pair reads as unconnected everywhere consent is checked,
        # even when a consent row still exists. The row is SUPPRESSED, not
        # deleted: it keeps the provenance of when and why they were connected,
        # which is exactly what a mod reviewing the case wants, while granting
        # none of the access it used to.
        #
        # Note this is the one direction where the in-memory cache is safe to
        # bypass — the DB read below can only ever turn a True into a False.
        #
        # ``check_no_contact=False`` is for the one caller that has already
        # made this exact query and folds the result in itself; it must never
        # be passed by a caller that hasn't.
        if check_no_contact and is_no_contact(self.bot.ctx.db_path, guild_id, a, b):
            return False
        pairs = self.consent_pairs.get(guild_id, set())
        return (a, b) in pairs and (b, a) in pairs

    def _has_pending_request(self, guild_id: int, a: int, b: int) -> bool:
        return (a, b) in self.dm_requests.get(guild_id, {})

    def _mode_roles_for(self, guild_id: int) -> dict[str, int]:
        """The guild's configured mode→role-id overrides (empty dict if none)."""
        return self.mode_role_ids.get(guild_id, {})

    def _mode_role_names_for(self, guild: discord.Guild) -> dict[str, str]:
        """Display names for the guild's DM-mode roles (for the panel embed).

        Configured overrides that resolve to a live role use that role's
        name; everything else keeps the default "DMs: …" label.
        """
        overrides = self._mode_roles_for(guild.id)
        names: dict[str, str] = {}
        for mode, rid in overrides.items():
            role = guild.get_role(rid) if rid else None
            if role is not None:
                names[mode] = role.name
        return names

    def _precheck_dm_request(self, guild: discord.Guild, requester: discord.Member, target: discord.Member | discord.User) -> Optional[str]:
        # ``classify_dm_request`` takes primitives, not discord objects, so it
        # remains testable without spinning up Discord. The cog observes the
        # facts here and the classifier picks the right message.
        target_in_guild = isinstance(target, discord.Member)
        target_mode = (
            resolve_mode(target, self._mode_roles_for(guild.id))
            if isinstance(target, discord.Member)
            else ""
        )
        # Suppressing the consent pair alone would leave the request path OPEN
        # — "not connected" is precisely the state that invites a new request.
        # Present the pair as though her DMs were closed instead. That reuses
        # an existing refusal verbatim, and it is the right kind of lie: it
        # describes a general setting of hers rather than anything about him,
        # so it reads the same whether or not he is the reason.
        no_contact = is_no_contact(self.bot.ctx.db_path, guild.id, requester.id, target.id)
        if target_in_guild and no_contact:
            target_mode = "closed"
        return classify_dm_request(
            target_in_guild=target_in_guild,
            is_self=target.id == requester.id,
            target_is_bot=target.bot,
            target_mode=target_mode,
            # Reuse the lookup above rather than letting ``_is_mutual`` run the
            # identical query a second time.
            is_mutual=(
                not no_contact
                and self._is_mutual(guild.id, requester.id, target.id, check_no_contact=False)
            ),
            has_pending=self._has_pending_request(guild.id, requester.id, target.id),
            target_display_name=getattr(target, "display_name", str(target.id)),
        )

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
        reason_clean = clamp_reason(reason, MAX_REASON_LENGTH)

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
            self.bot.ctx.db_path, guild.id, requester.id
        )
        if pending_count >= MAX_PENDING_PER_REQUESTER:
            limit_msg = (
                f"❌ You already have {pending_count} pending DM requests. "
                f"Wait for some to be answered or expire (max {MAX_PENDING_PER_REQUESTER})."
            )
            if interaction.response.is_done():
                await interaction.followup.send(limit_msg, ephemeral=True)
            else:
                await interaction.response.send_message(limit_msg, ephemeral=True)
            return

        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        type_label = request_type_label(req_type)
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="dm perms")
        embed = build_request_dm_embed(
            guild_name=guild.name,
            requester_display_name=requester.display_name,
            requester_avatar_url=requester.display_avatar.url,
            request_timeout_label=REQUEST_TIMEOUT_LABEL,
            type_label=type_label,
            reason=reason_clean,
            color=accent,
        )

        message = await _dm(
            user, self.bot.ctx.db_path, guild, embed=embed, view=AskConsentView(self)
        )
        if message is None:
            await interaction.followup.send(
                "❌ I couldn't DM that user — they may have DMs disabled.", ephemeral=True
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
            self.bot.ctx.db_path, guild.id, requester.id, user.id,
            req_type, reason_clean, message.id, None,
        )

        sender_embed = build_request_sent_embed(
            target_display_name=user.display_name,
            guild_name=guild.name,
            request_timeout_label=REQUEST_TIMEOUT_LABEL,
            type_label=type_label,
            reason=reason_clean,
            color=accent,
        )
        await _dm(requester, self.bot.ctx.db_path, guild, embed=sender_embed)

        write_audit_log(
            self.bot.ctx.db_path, guild.id, "request_asked",
            actor_id=requester.id, user_a_id=requester.id, user_b_id=user.id,
            notes=f"type={req_type}",
        )
        await self._post_audit(
            guild,
            audit_line_asked(requester.display_name, user.display_name, type_label),
        )
        await interaction.followup.send(
            f"📨 Request sent to {user.display_name} via DM!", ephemeral=True
        )

    # ── request panel (core.sticky) ──────────────────────────────────────

    def _panel_ids(self, guild_id: int) -> tuple[int, int]:
        settings = self.panel_settings.get(guild_id) or {}
        return (
            int(settings.get("panel_channel_id") or 0),
            int(settings.get("panel_message_id") or 0),
        )

    def _save_panel_ids(self, guild_id: int, channel_id: int, message_id: int) -> None:
        self.panel_settings[guild_id] = {
            "panel_channel_id": channel_id or None,
            "panel_message_id": message_id or None,
        }
        set_panel_settings(self.bot.ctx.db_path, guild_id, channel_id, message_id)
        self._publish_panel_guilds()

    def _publish_panel_guilds(self) -> None:
        """Keep the listener's fast path in sync with the in-memory settings, so
        a guild with no panel costs a set lookup rather than a DB read."""
        self.panel.set_known_guilds(
            {
                gid
                for gid, s in self.panel_settings.items()
                if s and s.get("panel_channel_id")
            }
        )

    async def _build_panel(self, guild: discord.Guild) -> PanelContent:
        accent = await safe_resolve_accent(self.bot.ctx, guild, log_label="dm perms")
        return PanelContent(
            embed=build_panel_embed(
                color=accent, role_names=self._mode_role_names_for(guild)
            ),
            view=DmRequestPanelView(self),
        )

    async def post_panel(
        self, guild: discord.Guild, panel_channel_id: int
    ) -> Optional[int]:
        """Post the request panel (or refresh it in place if it's already
        there). Returns the live message id, or None if the channel is
        unusable. Public: the dashboard route calls this."""
        channel = guild.get_channel(panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        message = await self.panel.place_or_refresh(guild, channel)
        return message.id if message else None

    # ── Listeners ────────────────────────────────────────────────────────────

    @commands.Cog.listener("on_guild_channel_delete")
    async def _forget_deleted_panel_channel(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """Clear the request panel's ids if its channel was deleted, so the
        dashboard stops reporting a panel that cannot exist."""
        await self.panel.on_channel_delete(channel)

    @commands.Cog.listener("on_message")
    async def _on_message_panel_bump(self, message: discord.Message) -> None:
        """Keep the panel at the bottom of its channel.

        Trailing-edge: a burst of chat settles into one repost once the channel
        falls quiet, rather than the panel jumping on the first message of every
        burst (the old leading-edge cooldown).
        """
        await self.panel.on_message(message)

    @commands.Cog.listener("on_member_update")
    async def _on_member_update_dm_roles(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Enforce one DM-mode role, keeping the one that just arrived.

        ``before`` is the whole point: the role added *in this update* is the
        member's new choice, and stripping anything else is the dedup doing its
        job. Ignoring it and keeping the highest-positioned role instead undid
        real choices — see ``pick_dm_roles_to_remove``.
        """
        dm_roles = [
            r for r in after.roles
            if is_dm_mode_role(r, self._mode_roles_for(after.guild.id))
        ]
        before_ids = {r.id for r in before.roles}
        just_added = next((r for r in dm_roles if r.id not in before_ids), None)
        to_remove = pick_dm_roles_to_remove(dm_roles, keep=just_added)
        if not to_remove:
            return
        try:
            await after.remove_roles(*to_remove, reason="DM role dedup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "Could not dedup DM roles for member %s in guild %s: %s",
                after.id, after.guild.id, exc,
            )

    # ── Member self-service ──────────────────────────────────────────────────
    #
    # There are no /dm_* slash commands. /dm_help, /dm_set_mode, /dm_status and
    # /dm_revoke collapsed into the ephemeral DmSettingsView on 2026-07-28,
    # reached from the request panel's "My DM Settings" button. This method is
    # the shared teardown the panel's revoke button calls.

    async def revoke_connection(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member | discord.User,
    ) -> bool:
        """Tear down the consent pair between ``actor`` and ``target``.

        Returns False when there was nothing to remove, so the caller can say
        "you have no connection with them" rather than claiming success. On a
        real removal this also retires the original request message, DMs both
        parties, and writes both the DB audit row and the mod-feed line.
        """
        guild_id = guild.id
        pair_set = self.consent_pairs.get(guild_id, set())
        meta = get_consent_pair_meta(self.bot.ctx.db_path, guild_id, actor.id, target.id)
        db_removed = remove_consent_pair(
            self.bot.ctx.db_path, guild_id, actor.id, target.id
        )
        in_memory_removed = discard_consent_pair(pair_set, actor.id, target.id)

        if not (db_removed or in_memory_removed):
            return False

        type_label = request_type_label(meta.get("type") if meta else None)
        revoked_embed = build_revoked_embed(
            requester_display_name=actor.display_name,
            target_display_name=target.display_name,
            type_label=type_label,
            reason=meta.get("reason") if meta else None,
        )

        if meta and meta.get("source_msg_id") and meta.get("source_channel_id"):
            channel = guild.get_channel(meta["source_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    msg = await channel.fetch_message(meta["source_msg_id"])
                    await msg.edit(embed=revoked_embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await _dm(actor, self.bot.ctx.db_path, guild, embed=revoked_embed)
        await _dm(target, self.bot.ctx.db_path, guild, embed=revoked_embed)

        write_audit_log(
            self.bot.ctx.db_path, guild_id, "relationship_revoked",
            actor_id=actor.id, user_a_id=actor.id, user_b_id=target.id,
        )
        await self._post_audit(
            guild,
            audit_line_revoked(
                actor.display_name, target.display_name, actor.display_name
            ),
        )
        return True


async def setup(bot: Bot) -> None:
    await bot.add_cog(DmPermsCog(bot))
