"""Jail & Ticket moderation commands."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.jail.embeds import (
    build_adopted_policies_embed,
    build_modinfo_embed,
    build_policy_close_embed,
    build_policy_list_embed,
    build_policy_proposal_embed,
    build_policy_vote_initial_embed,
    build_ticket_open_embed,
    build_ticket_panel_embed,
    build_warning_audit_embed,
    build_warning_revoke_audit_embed,
    build_warning_threshold_embed,
    build_warnings_list_embed,
)
from bot_modules.jail.logic import sanitize_channel_name

from bot_modules.commands.jail_commands import (
    CLR_INFO,
    CLR_POLICY,
    CLR_TICKET,
    PolicyVoteAbstainButton,
    PolicyVoteNoButton,
    PolicyVoteYesButton,
    TicketCloseButton,
    TicketDeleteButton,
    TicketPanelButton,
    TicketReopenButton,
    _JailModal,
    _TicketFromMessageModal,
    _collect_and_post_transcript,
    _dm_user,
    _do_jail,
    _do_unjail,
    _get_admin_role_ids,
    _add_ticket_panel,
    _get_config,
    _get_mod_role_ids,
    _is_admin,
    _is_mod,
    _post_audit,
    _ts_str,
    jail_expiry_loop,
    policy_vote_timeout_loop,
)
from bot_modules.services.moderation import (
    add_ticket_participant,
    claim_ticket,
    close_policy_ticket,
    close_ticket,
    create_policy_ticket,
    create_ticket,
    create_warning,
    delete_ticket,
    escalate_ticket,
    get_active_jail,
    get_active_warning_count,
    get_jail_by_channel,
    get_jail_history,
    get_policies,
    get_policies_by_ticket_id,
    get_policy_ticket_by_channel,
    get_ticket_by_channel,
    get_ticket_history,
    get_warnings,
    remove_ticket_participant,
    reopen_ticket,
    revoke_warning,
    start_policy_vote,
    write_audit,
)
from bot_modules.core.db_utils import get_config_value, get_tz_offset_hours
from bot_modules.services.activity_graphs import query_message_activity, render_activity_chart


# Discord caps the embed *title* at 256 chars; we trim our policy titles
# below that so the "Policy Proposal #N: …" prefix still fits.
_POLICY_TITLE_MAX = 200

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.jail_commands")


class _PolicyVoteModal(discord.ui.Modal, title="Start Policy Vote"):
    vote_text: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Exact policy text to vote on",
        style=discord.TextStyle.paragraph,
        placeholder="Type the exact wording of the policy being voted on...",
        required=True,
        max_length=2000,
    )

    def __init__(self, policy_id: int, ctx: AppContext) -> None:
        super().__init__()
        self.policy_id = policy_id
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ctx = self._ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return

        vote_text_val = self.vote_text.value.strip()
        with ctx.open_db() as conn:
            start_policy_vote(conn, self.policy_id, vote_text=vote_text_val)
            write_audit(
                conn,
                guild_id=guild.id,
                action="policy_vote_started",
                actor_id=member.id,
                extra={"policy_id": self.policy_id, "vote_text": vote_text_val},
            )

        mod_role_ids = _get_mod_role_ids(ctx, guild.id)
        admin_role_ids = _get_admin_role_ids(ctx, guild.id)
        all_role_ids = mod_role_ids | admin_role_ids
        eligible: set[int] = set()
        for m in guild.members:
            if m.bot:
                continue
            if m.guild_permissions.administrator:
                eligible.add(m.id)
                continue
            if all_role_ids & {r.id for r in m.roles}:
                eligible.add(m.id)

        embed = build_policy_vote_initial_embed(
            channel_name=interaction.channel.name,  # type: ignore[union-attr]
            vote_text=vote_text_val,
            eligible_ids=sorted(eligible),
        )

        view = discord.ui.View(timeout=None)
        view.add_item(PolicyVoteYesButton(self.policy_id))
        view.add_item(PolicyVoteNoButton(self.policy_id))
        view.add_item(PolicyVoteAbstainButton(self.policy_id))

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "🗳️ **Voting has begun!** All mods and admins must vote.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await channel.send(embed=embed, view=view)
            role_mentions = []
            for rid in all_role_ids:
                role = guild.get_role(rid)
                if role:
                    role_mentions.append(role.mention)
            if role_mentions:
                await channel.send(f"🗳️ Vote now! {' '.join(role_mentions)}")


class _WarnFromMessageModal(discord.ui.Modal, title="Warn User — Message Context"):
    notes: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Moderator notes (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, source_message: discord.Message, ctx: "AppContext") -> None:
        super().__init__()
        self.source_message = source_message
        self._ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ctx = self._ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        target = guild.get_member(self.source_message.author.id)
        if target is None:
            await interaction.response.send_message(
                "That user is no longer in this server.", ephemeral=True
            )
            return

        reason_text = self.source_message.content.strip()
        notes_text = self.notes.value.strip()
        full_reason = reason_text
        if notes_text:
            full_reason = f"{reason_text}\n\n**Mod notes:** {notes_text}"

        with ctx.open_db() as conn:
            warning_id = create_warning(
                conn,
                guild_id=guild.id,
                user_id=target.id,
                moderator_id=member.id,
                reason=full_reason,
            )
            count = get_active_warning_count(conn, guild.id, target.id)
            write_audit(
                conn,
                guild_id=guild.id,
                action="warning_issue",
                actor_id=member.id,
                target_id=target.id,
                extra={
                    "warning_id": warning_id,
                    "reason": full_reason,
                    "count": count,
                    "source_message_id": self.source_message.id,
                    "source_channel_id": self.source_message.channel.id,
                },
            )

        await interaction.response.send_message(
            f"⚠️ Warning issued to {target.mention}. They now have **{count}** active warning(s).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        audit_embed = build_warning_audit_embed(
            target_mention=target.mention,
            moderator_mention=member.mention,
            active_count=count,
            reason=reason_text,
            notes=notes_text,
            source_jump_url=self.source_message.jump_url,
        )
        await _post_audit(ctx, guild, audit_embed)

        with ctx.open_db() as conn:
            threshold = int(get_config_value(conn, "warning_threshold", "3"))
        if count >= threshold and (count - 1) < threshold:
            alert = build_warning_threshold_embed(
                target_mention=target.mention,
                active_count=count,
                admin_role_ids=sorted(_get_admin_role_ids(ctx, guild.id)),
            )
            await _post_audit(ctx, guild, alert)


class JailCog(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Ticket management commands.")
    policy = app_commands.Group(
        name="policy", description="Policy proposal and voting commands."
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        bot = self.bot
        ctx = self.ctx

        # Store ctx on bot so persistent view callbacks can reach it
        bot._mod_ctx = ctx  # type: ignore[attr-defined]

        # Register persistent dynamic items
        bot.add_dynamic_items(TicketPanelButton)
        bot.add_dynamic_items(TicketCloseButton)
        bot.add_dynamic_items(TicketReopenButton)
        bot.add_dynamic_items(TicketDeleteButton)
        bot.add_dynamic_items(PolicyVoteYesButton)
        bot.add_dynamic_items(PolicyVoteNoButton)
        bot.add_dynamic_items(PolicyVoteAbstainButton)

        # Context menus — add to tree; stored for removal in cog_unload
        async def _jail_ctx_cb(
            interaction: discord.Interaction, user: discord.Member
        ) -> None:
            member = interaction.user
            if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
                await interaction.response.send_message("Mod only.", ephemeral=True)
                return
            await interaction.response.send_modal(_JailModal(user, ctx))

        jail_ctx_menu = app_commands.ContextMenu(name="Jail User", callback=_jail_ctx_cb)
        jail_ctx_menu.default_permissions = discord.Permissions(moderate_members=True)
        bot.tree.add_command(jail_ctx_menu)
        self._jail_context_menu = jail_ctx_menu

        async def ticket_message_context(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            await interaction.response.send_modal(_TicketFromMessageModal(message))

        ticket_ctx_menu = app_commands.ContextMenu(
            name="Open Ticket About This Message", callback=ticket_message_context
        )
        bot.tree.add_command(ticket_ctx_menu)
        self._ticket_context_menu = ticket_ctx_menu

        async def warn_message_ctx(
            interaction: discord.Interaction, message: discord.Message
        ) -> None:
            invoker = interaction.user
            if not isinstance(invoker, discord.Member) or not _is_mod(invoker, ctx):
                await interaction.response.send_message("Mod only.", ephemeral=True)
                return
            if message.author.bot:
                await interaction.response.send_message(
                    "Can't warn a bot.", ephemeral=True
                )
                return
            if not message.content or not message.content.strip():
                await interaction.response.send_message(
                    "That message has no text content to use as a warning reason.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(_WarnFromMessageModal(message, ctx))

        warn_msg_ctx_menu = app_commands.ContextMenu(
            name="Warn User (Message)", callback=warn_message_ctx
        )
        warn_msg_ctx_menu.default_permissions = discord.Permissions(moderate_members=True)
        bot.tree.add_command(warn_msg_ctx_menu)
        self._warn_msg_context_menu = warn_msg_ctx_menu

        # Start jail expiry background task
        bot.startup_task_factories.append(lambda: jail_expiry_loop(bot, ctx))
        # Resolve policy votes whose 72h (or configured) window has passed.
        bot.startup_task_factories.append(
            lambda: policy_vote_timeout_loop(bot, ctx)
        )

    async def cog_unload(self) -> None:
        if hasattr(self, "_jail_context_menu"):
            self.bot.tree.remove_command(
                "Jail User", type=discord.AppCommandType.user
            )
        if hasattr(self, "_ticket_context_menu"):
            self.bot.tree.remove_command(
                "Open Ticket About This Message", type=discord.AppCommandType.message
            )
        if hasattr(self, "_warn_msg_context_menu"):
            self.bot.tree.remove_command(
                "Warn User (Message)", type=discord.AppCommandType.message
            )

    # ── /jail ─────────────────────────────────────────────────────────────
    # Note: the /setup command lives in cogs/setup_cog.py, which runs both
    # the channel-creation phase and this cog's role/category wizard.

    @app_commands.command(name="jail", description="Place a member in a private jail channel.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="Member to jail",
        duration="How long (e.g. 24h, 7d). Leave blank for indefinite.",
        reason="Reason for jailing",
    )
    async def jail_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str | None = None,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "You don't have permission.", ephemeral=True
            )
            return
        await _do_jail(
            interaction, ctx, user, duration_str=duration or "", reason=reason or ""
        )

    # ── /unjail ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="unjail", description="Release a jailed member and restore their roles."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to release", reason="Release reason")
    async def unjail_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message(
                "You don't have permission.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        result = await _do_unjail(ctx, guild, user, reason=reason or "", actor=member)
        await interaction.followup.send(result, ephemeral=True)

    # ── /ticket ───────────────────────────────────────────────────────────

    @ticket.command(name="panel", description="Post the ticket-creation button in a channel.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(channel="Channel to post the panel in")
    async def ticket_panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        embed = build_ticket_panel_embed()
        view = discord.ui.View(timeout=None)
        view.add_item(TicketPanelButton())
        msg = await channel.send(embed=embed, view=view)
        _add_ticket_panel(ctx, guild.id, channel.id, msg.id)
        await interaction.response.send_message(
            f"✅ Ticket panel posted in {channel.mention}", ephemeral=True
        )

    @ticket.command(
        name="open", description="Open a private support ticket with the mod team."
    )
    @app_commands.describe(description="Brief description of your issue")
    async def ticket_open(
        self, interaction: discord.Interaction, description: str | None = None
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return

        cat_id = _get_config(ctx, "ticket_category_id")
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category not configured. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        desc_text = description or "(no description)"
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
        name = f"ticket-{sanitize_channel_name(user.name)[:16]}-{ts}"
        mod_role_ids = _get_mod_role_ids(ctx, guild.id)

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
            ),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            )
        for rid in mod_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                )

        channel = await guild.create_text_channel(
            name, category=category, overwrites=overwrites  # type: ignore[arg-type]
        )

        with ctx.open_db() as conn:
            ticket_id = create_ticket(
                conn,
                guild_id=guild.id,
                user_id=user.id,
                channel_id=channel.id,
                description=desc_text,
            )
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_open",
                actor_id=user.id,
                extra={"ticket_id": ticket_id, "description": desc_text},
            )

        embed = build_ticket_open_embed(
            ticket_id=ticket_id,
            description=desc_text,
            opener_mention=user.mention,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(TicketCloseButton(ticket_id))
        await channel.send(embed=embed, view=view)
        await interaction.followup.send(
            f"Ticket created → {channel.mention}", ephemeral=True
        )

        await _dm_user(
            user,
            embed=discord.Embed(
                description=f"Your ticket has been created → [Go to ticket]({channel.jump_url})",
                color=CLR_TICKET,
            ),
        )

        audit_embed = discord.Embed(
            title="📩 Ticket Opened",
            description=f"**Ticket #{ticket_id}** by {user.mention} in {channel.mention}",
            color=CLR_TICKET,
        )
        await _post_audit(ctx, guild, audit_embed)

    @ticket.command(name="close", description="Close the current ticket.")
    @app_commands.describe(reason="Reason for closing")
    async def ticket_close_cmd(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can close tickets.", ephemeral=True
            )
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket or ticket["status"] != "open":
            await interaction.response.send_message(
                "This is not an open ticket channel.", ephemeral=True
            )
            return
        tid = ticket["id"]
        reason_text = reason or ""
        guild = interaction.guild
        if not guild:
            return

        with ctx.open_db() as conn:
            close_ticket(conn, tid, closed_by=member.id, reason=reason_text)
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_close",
                actor_id=member.id,
                target_id=ticket["user_id"],
                extra={"ticket_id": tid, "reason": reason_text},
            )

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            creator = guild.get_member(ticket["user_id"])
            if creator:
                await channel.set_permissions(
                    creator, view_channel=True, send_messages=False, read_message_history=True
                )

            view = discord.ui.View(timeout=None)
            view.add_item(TicketReopenButton(tid))
            view.add_item(TicketDeleteButton(tid))

            close_msg = f"🔒 Ticket closed by {member.mention}."
            if reason_text:
                close_msg += f"\n**Reason:** {reason_text}"

            await interaction.response.send_message(
                close_msg, allowed_mentions=discord.AllowedMentions.none()
            )

            async for msg in channel.history(limit=5, oldest_first=True):
                if msg.author == guild.me and msg.embeds:
                    await msg.edit(view=view)
                    break

            if creator:
                await _dm_user(
                    creator,
                    embed=discord.Embed(
                        description=f"Your ticket in **{guild.name}** has been closed."
                        + (f"\n**Reason:** {reason_text}" if reason_text else "")
                        + "\nYou can still view the channel.",
                        color=CLR_TICKET,
                    ),
                    fallback_channel=channel,
                )

    @ticket.command(name="reopen", description="Reopen a closed ticket.")
    async def ticket_reopen_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can reopen tickets.", ephemeral=True
            )
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket or ticket["status"] != "closed":
            await interaction.response.send_message(
                "This is not a closed ticket channel.", ephemeral=True
            )
            return

        guild = interaction.guild
        if not guild:
            return
        tid = ticket["id"]
        with ctx.open_db() as conn:
            reopen_ticket(conn, tid)
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_reopen",
                actor_id=member.id,
                extra={"ticket_id": tid},
            )

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            creator = guild.get_member(ticket["user_id"])
            if creator:
                await channel.set_permissions(
                    creator,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )
            view = discord.ui.View(timeout=None)
            view.add_item(TicketCloseButton(tid))
            async for msg in channel.history(limit=5, oldest_first=True):
                if msg.author == guild.me and msg.embeds:
                    await msg.edit(view=view)
                    break
            await interaction.response.send_message(
                f"🔓 Ticket reopened by {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if creator:
                await _dm_user(
                    creator,
                    embed=discord.Embed(
                        description=f"Your ticket in **{guild.name}** has been reopened.",
                        color=CLR_TICKET,
                    ),
                )

    @ticket.command(
        name="delete",
        description="Permanently delete a closed ticket. A transcript is saved first.",
    )
    async def ticket_delete_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can delete tickets.", ephemeral=True
            )
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket or ticket["status"] != "closed":
            await interaction.response.send_message(
                "Ticket must be closed before deleting.", ephemeral=True
            )
            return

        guild = interaction.guild
        channel = interaction.channel
        if not guild or not isinstance(channel, discord.TextChannel):
            return
        tid = ticket["id"]

        await interaction.response.defer(ephemeral=True)
        creator = guild.get_member(ticket["user_id"]) or interaction.user
        await _collect_and_post_transcript(
            ctx,
            channel,
            record_type="ticket",
            record_id=tid,
            user=creator,
            extra_meta={"close_reason": ticket.get("close_reason", "")},
        )
        with ctx.open_db() as conn:
            delete_ticket(conn, tid)
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_delete",
                actor_id=member.id,
                target_id=ticket["user_id"],
                extra={"ticket_id": tid},
            )

        audit_embed = discord.Embed(
            title="🗑️ Ticket Deleted",
            description=f"**Ticket #{tid}** deleted by {member.mention}",
            color=CLR_TICKET,
        )
        await _post_audit(ctx, guild, audit_embed)
        await channel.delete(reason=f"Ticket #{tid} deleted")

    @ticket.command(
        name="claim",
        description="Mark yourself as handling this ticket. You'll get DM pings on new activity.",
    )
    async def ticket_claim_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        with ctx.open_db() as conn:
            claim_ticket(conn, ticket["id"], member.id)
            write_audit(
                conn,
                guild_id=interaction.guild_id or 0,
                action="ticket_claim",
                actor_id=member.id,
                extra={"ticket_id": ticket["id"]},
            )
        await interaction.response.send_message(
            f"✅ {member.mention} claimed this ticket. You'll get DM notifications for new activity.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @ticket.command(
        name="escalate", description="Bring admin roles into this ticket and ping them."
    )
    @app_commands.describe(reason="Reason for escalation")
    async def ticket_escalate_cmd(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket:
            await interaction.response.send_message("Not a ticket channel.", ephemeral=True)
            return
        if ticket["escalated"]:
            await interaction.response.send_message(
                "This ticket is already escalated.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        admin_ids = _get_admin_role_ids(ctx, guild.id)
        pings: list[str] = []
        for rid in admin_ids:
            role = guild.get_role(rid)
            if role:
                await channel.set_permissions(
                    role,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                )
                pings.append(role.mention)

        with ctx.open_db() as conn:
            escalate_ticket(conn, ticket["id"])
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_escalate",
                actor_id=member.id,
                extra={"ticket_id": ticket["id"], "reason": reason or ""},
            )

        msg = f"⚠️ **Ticket escalated** by {member.mention}."
        if reason:
            msg += f"\n**Reason:** {reason}"
        if pings:
            msg += f"\n{' '.join(pings)}"
        await interaction.response.send_message(msg)

    # ── /policy ───────────────────────────────────────────────────────────

    @policy.command(
        name="open", description="Open a new policy proposal for discussion."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        title="Short title for the policy",
        description="Detailed description of the proposed policy",
    )
    async def policy_open(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str | None = None,
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        if not _is_admin(user, ctx):
            await interaction.response.send_message(
                "Only admins can open policy proposals.", ephemeral=True
            )
            return

        cat_id = _get_config(ctx, "ticket_category_id")
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category not configured. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        # Cap the title so the resulting embed-title (which prefixes "Policy
        # Proposal #N: ") stays under Discord's 256-char embed-title limit.
        if len(title) > _POLICY_TITLE_MAX:
            title = title[:_POLICY_TITLE_MAX].rstrip() + "…"

        await interaction.response.defer(ephemeral=True)

        desc_text = description or "(no description)"
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
        safe_title = sanitize_channel_name(title[:20])
        name = f"policy-{safe_title}-{ts}"

        mod_role_ids = _get_mod_role_ids(ctx, guild.id)
        admin_role_ids = _get_admin_role_ids(ctx, guild.id)
        all_role_ids = mod_role_ids | admin_role_ids

        overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me:
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            )
        for rid in all_role_ids:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True,
                )

        channel = await guild.create_text_channel(
            name, category=category, overwrites=overwrites  # type: ignore[arg-type]
        )

        with ctx.open_db() as conn:
            policy_id = create_policy_ticket(
                conn,
                guild_id=guild.id,
                creator_id=user.id,
                channel_id=channel.id,
                title=title,
                description=desc_text,
            )
            write_audit(
                conn,
                guild_id=guild.id,
                action="policy_open",
                actor_id=user.id,
                extra={"policy_id": policy_id, "title": title},
            )

        embed = build_policy_proposal_embed(
            policy_id=policy_id,
            title=title,
            description=desc_text,
            proposer_mention=user.mention,
        )
        await channel.send(embed=embed)

        role_mentions = []
        for rid in all_role_ids:
            role = guild.get_role(rid)
            if role:
                role_mentions.append(role.mention)
        if role_mentions:
            await channel.send(
                f"📋 New policy proposal from {user.mention}: **{title}**\n"
                f"Attention: {' '.join(role_mentions)}",
            )

        await interaction.followup.send(
            f"Policy proposal created → {channel.mention}", ephemeral=True
        )

        audit_embed = discord.Embed(
            title="📋 Policy Proposal Opened",
            description=f"**{title}** by {user.mention} in {channel.mention}",
            color=CLR_POLICY,
        )
        await _post_audit(ctx, guild, audit_embed)

    @policy.command(
        name="vote", description="Start the formal vote on this policy proposal."
    )
    @app_commands.default_permissions(moderate_members=True)
    async def policy_vote_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        if not (_is_mod(member, ctx) or _is_admin(member, ctx)):
            await interaction.response.send_message(
                "Only mods and admins can start a policy vote.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            policy = get_policy_ticket_by_channel(conn, interaction.channel_id or 0)
        if not policy:
            await interaction.response.send_message(
                "This is not an active policy proposal channel.", ephemeral=True
            )
            return
        if policy["status"] != "open":
            await interaction.response.send_message(
                f"This policy is already in '{policy['status']}' state.", ephemeral=True
            )
            return

        await interaction.response.send_modal(_PolicyVoteModal(policy["id"], ctx))

    @policy.command(
        name="close",
        description="Close a policy proposal without voting (admin only).",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(reason="Reason for closing without a vote")
    async def policy_close_cmd(
        self, interaction: discord.Interaction, reason: str | None = None
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        if not _is_admin(member, ctx):
            await interaction.response.send_message(
                "Only admins can close policy proposals.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            policy = get_policy_ticket_by_channel(conn, interaction.channel_id or 0)
        if not policy:
            await interaction.response.send_message(
                "This is not an active policy proposal channel.", ephemeral=True
            )
            return

        policy_id = policy["id"]
        reason_text = reason or "Closed without vote"

        with ctx.open_db() as conn:
            close_policy_ticket(conn, policy_id)
            write_audit(
                conn,
                guild_id=guild.id,
                action="policy_closed",
                actor_id=member.id,
                extra={"policy_id": policy_id, "reason": reason_text},
            )

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            close_embed = build_policy_close_embed(
                title=policy["title"],
                moderator_mention=member.mention,
                reason=reason_text,
            )
            await interaction.response.send_message(embed=close_embed)

            with ctx.open_db() as conn:
                adopted_policies = get_policies_by_ticket_id(conn, policy_id)
            if adopted_policies:
                adopted_embed = build_adopted_policies_embed(adopted_policies)
                await channel.send(embed=adopted_embed)

            creator = guild.get_member(policy["creator_id"]) or member
            try:
                await _collect_and_post_transcript(
                    ctx,
                    channel,
                    record_type="policy_ticket",
                    record_id=policy_id,
                    user=creator,
                    extra_meta={
                        "resolution": "closed",
                        "reason": reason_text,
                        "policy_title": policy["title"],
                        "adopted_policies": [
                            {"id": p["id"], "title": p["title"], "description": p["description"]}
                            for p in adopted_policies
                        ],
                    },
                )
            except Exception:
                # Don't delete the channel if we couldn't archive the
                # discussion — losing both the transcript and the source is
                # the worst possible outcome here.
                log.exception(
                    "Policy transcript save failed for policy %s; "
                    "leaving channel %s intact.",
                    policy_id, channel.id,
                )
                await channel.send(
                    "⚠️ Failed to archive this policy's transcript. "
                    "The channel has been **kept** so the discussion isn't lost. "
                    "An admin can retry by running `/policy close` again, "
                    "or delete the channel manually once a transcript is saved."
                )
                return
            await channel.delete(reason=f"Policy #{policy_id} closed by {member}")

        audit_embed = discord.Embed(
            title="📋 Policy Proposal Closed",
            description=f"**{policy['title']}** closed by {member.mention}"
            + (f"\nReason: {reason_text}" if reason_text else ""),
            color=CLR_INFO,
        )
        await _post_audit(ctx, guild, audit_embed)

    @policy.command(name="list", description="List all passed policies.")
    @app_commands.default_permissions(moderate_members=True)
    async def policy_list_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("Server-only.", ephemeral=True)
            return
        if not (_is_mod(member, ctx) or _is_admin(member, ctx)):
            await interaction.response.send_message(
                "Only mods and admins can view policies.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            policies_list = get_policies(conn, guild.id)

        if not policies_list:
            await interaction.response.send_message(
                "No passed policies yet.", ephemeral=True
            )
            return

        embed = build_policy_list_embed(policies_list)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /pull ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="pull", description="Bring someone into this jail or ticket channel."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="User to add to the channel")
    async def pull_cmd(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        channel = interaction.channel
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this inside a jail or ticket channel.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            jail = get_jail_by_channel(conn, channel.id)
            ticket = get_ticket_by_channel(conn, channel.id)
        if not jail and not ticket:
            await interaction.response.send_message(
                "This is not a jail or ticket channel.", ephemeral=True
            )
            return

        await channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )

        record_type = "jail" if jail else "ticket"
        record_id = jail["id"] if jail else ticket["id"]  # type: ignore[index]
        if ticket:
            with ctx.open_db() as conn:
                add_ticket_participant(conn, ticket["id"], user.id, member.id)

        with ctx.open_db() as conn:
            write_audit(
                conn,
                guild_id=guild.id,
                action="channel_pull",
                actor_id=member.id,
                target_id=user.id,
                extra={"channel_type": record_type, "record_id": record_id},
            )

        await interaction.response.send_message(
            f"{user.mention} has been added by {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ── /remove ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="remove",
        description="Remove someone you pulled into this jail or ticket channel.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="User to remove from the channel")
    async def remove_cmd(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        channel = interaction.channel
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        if not isinstance(channel, discord.TextChannel):
            return

        with ctx.open_db() as conn:
            jail = get_jail_by_channel(conn, channel.id)
            ticket = get_ticket_by_channel(conn, channel.id)
        if not jail and not ticket:
            await interaction.response.send_message(
                "Not a jail or ticket channel.", ephemeral=True
            )
            return

        primary_id = jail["user_id"] if jail else ticket["user_id"]  # type: ignore[index]
        if user.id == primary_id:
            await interaction.response.send_message(
                "Cannot remove the primary user from their own channel.", ephemeral=True
            )
            return

        await channel.set_permissions(user, overwrite=None)

        record_type = "jail" if jail else "ticket"
        record_id = jail["id"] if jail else ticket["id"]  # type: ignore[index]
        if ticket:
            with ctx.open_db() as conn:
                remove_ticket_participant(conn, ticket["id"], user.id)

        with ctx.open_db() as conn:
            write_audit(
                conn,
                guild_id=guild.id,
                action="channel_remove",
                actor_id=member.id,
                target_id=user.id,
                extra={"channel_type": record_type, "record_id": record_id},
            )

        await interaction.response.send_message(
            f"{user.mention} has been removed by {member.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ── /warn ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="warn",
        description="Issue a formal warning. The action is logged (user is not notified).",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to warn", reason="Reason for warning")
    async def warn_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        reason_text = reason or ""
        with ctx.open_db() as conn:
            warning_id = create_warning(
                conn,
                guild_id=guild.id,
                user_id=user.id,
                moderator_id=member.id,
                reason=reason_text,
            )
            count = get_active_warning_count(conn, guild.id, user.id)
            write_audit(
                conn,
                guild_id=guild.id,
                action="warning_issue",
                actor_id=member.id,
                target_id=user.id,
                extra={"warning_id": warning_id, "reason": reason_text, "count": count},
            )

        await interaction.response.send_message(
            f"⚠️ Warning issued to {user.mention}. They now have **{count}** active warning(s).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        audit_embed = build_warning_audit_embed(
            target_mention=user.mention,
            moderator_mention=member.mention,
            active_count=count,
            reason=reason_text,
        )
        await _post_audit(ctx, guild, audit_embed)

        with ctx.open_db() as conn:
            threshold = int(get_config_value(conn, "warning_threshold", "3"))
        # Fire the threshold alert when this warning is the one that crosses
        # the line — i.e. count was below threshold before, and is at or
        # above it now. Equality-only comparison would miss bulk additions.
        if count >= threshold and (count - 1) < threshold:
            alert = build_warning_threshold_embed(
                target_mention=user.mention,
                active_count=count,
                admin_role_ids=sorted(_get_admin_role_ids(ctx, guild.id)),
            )
            await _post_audit(ctx, guild, alert)

    # ── /warnings ─────────────────────────────────────────────────────────

    @app_commands.command(
        name="warnings",
        description="List all warnings (active and revoked) for a member.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to check")
    async def warnings_cmd(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        guild = interaction.guild
        if not guild:
            return
        with ctx.open_db() as conn:
            warns = get_warnings(conn, guild.id, user.id)

        if not warns:
            await interaction.response.send_message(
                f"{user} has no warnings.", ephemeral=True
            )
            return

        embed = build_warnings_list_embed(str(user), warns, ts_formatter=_ts_str)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /revokewarn ───────────────────────────────────────────────────────

    @app_commands.command(
        name="revokewarn",
        description="Revoke a warning by ID. Stays in history but stops counting.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="Member the warning belongs to",
        warning_id="The warning's numeric ID (see /warnings).",
        reason="Why this warning is being revoked.",
    )
    async def revokewarn_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        warning_id: int,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        reason_text = reason or ""
        with ctx.open_db() as conn:
            # Verify the warning belongs to this user in this guild before
            # touching the row — services.revoke_warning looks up by id only.
            warns = get_warnings(conn, guild.id, user.id)
            match = next((w for w in warns if w["id"] == warning_id), None)
            if match is None:
                await interaction.response.send_message(
                    f"Warning #{warning_id} doesn't belong to {user.mention} "
                    f"in this server.",
                    ephemeral=True,
                )
                return
            if match["revoked"]:
                await interaction.response.send_message(
                    f"Warning #{warning_id} is already revoked.", ephemeral=True
                )
                return
            revoked = revoke_warning(
                conn, warning_id, revoked_by=member.id, reason=reason_text
            )
            if not revoked:
                await interaction.response.send_message(
                    "Couldn't revoke that warning — it may have just been "
                    "revoked by someone else.",
                    ephemeral=True,
                )
                return
            count = get_active_warning_count(conn, guild.id, user.id)
            write_audit(
                conn,
                guild_id=guild.id,
                action="warning_revoke",
                actor_id=member.id,
                target_id=user.id,
                extra={
                    "warning_id": warning_id,
                    "reason": reason_text,
                    "count": count,
                },
            )

        await interaction.response.send_message(
            f"✅ Warning #{warning_id} revoked. {user.mention} now has "
            f"**{count}** active warning(s).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

        audit_embed = build_warning_revoke_audit_embed(
            warning_id=warning_id,
            target_mention=user.mention,
            moderator_mention=member.mention,
            active_count=count,
            reason=reason_text,
        )
        await _post_audit(ctx, guild, audit_embed)

    # ── /modinfo ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="modinfo",
        description="Full mod profile — jail history, warnings, and tickets for a member.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to inspect")
    async def modinfo_cmd(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        ctx = self.ctx
        member = interaction.user
        guild = interaction.guild
        if (
            not isinstance(member, discord.Member)
            or guild is None
            or not _is_mod(member, ctx)
        ):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        since_30d = datetime.now(timezone.utc).timestamp() - 30 * 86400

        def _fetch():
            with ctx.open_db() as conn:
                active_jail = get_active_jail(conn, guild.id, user.id)
                jail_hist = get_jail_history(conn, guild.id, user.id)
                warns = get_warnings(conn, guild.id, user.id)
                tickets = get_ticket_history(conn, guild.id, user.id)

                xp_row = conn.execute(
                    "SELECT total_xp, level FROM member_xp WHERE guild_id = ? AND user_id = ?",
                    (guild.id, user.id),
                ).fetchone()

                watcher_count = conn.execute(
                    "SELECT COUNT(*) FROM watched_users WHERE guild_id = ? AND watched_user_id = ?",
                    (guild.id, user.id),
                ).fetchone()[0]

                last_ts = conn.execute(
                    "SELECT MAX(created_at) FROM processed_messages WHERE guild_id = ? AND user_id = ?",
                    (guild.id, user.id),
                ).fetchone()[0]

                top_channels = conn.execute(
                    """
                    SELECT channel_id, COUNT(*) AS cnt
                    FROM processed_messages
                    WHERE guild_id = ? AND user_id = ? AND created_at >= ?
                    GROUP BY channel_id ORDER BY cnt DESC LIMIT 3
                    """,
                    (guild.id, user.id, since_30d),
                ).fetchall()

                labels, msg_counts, member_counts = query_message_activity(
                    conn, guild.id, "day", user_id=user.id,
                    utc_offset_hours=get_tz_offset_hours(conn, guild.id),
                )

            return (
                active_jail, jail_hist, warns, tickets,
                xp_row, watcher_count, last_ts, top_channels,
                labels, msg_counts, member_counts,
            )

        (
            active_jail, jail_hist, warns, tickets,
            xp_row, watcher_count, last_ts, top_channels,
            labels, msg_counts, member_counts,
        ) = await asyncio.to_thread(_fetch)

        chart_bytes = await asyncio.to_thread(
            render_activity_chart,
            labels,
            msg_counts,
            member_counts,
            f"{user.display_name} — 30-Day Activity",
            "day",
            show_members=False,
        )

        embed = build_modinfo_embed(
            user_label=str(user),
            user_avatar_url=user.display_avatar.url if user.display_avatar else None,
            account_created=user.created_at,
            account_age_days=(datetime.now(timezone.utc) - user.created_at).days,
            joined_at=user.joined_at,
            xp_row=xp_row,
            watcher_count=watcher_count,
            active_jail=active_jail,
            jail_history=jail_hist,
            warns=warns,
            tickets=tickets,
            last_seen_ts=last_ts,
            top_channels=top_channels,
            msgs_30d_total=sum(msg_counts),
            ts_formatter=_ts_str,
        )

        await interaction.followup.send(
            embed=embed,
            file=discord.File(io.BytesIO(chart_bytes), filename="modinfo_activity.png"),
            ephemeral=True,
        )


async def setup(bot: Bot) -> None:
    await bot.add_cog(JailCog(bot, bot.ctx))
