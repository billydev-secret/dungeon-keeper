"""Jail & Ticket moderation commands."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from commands.jail_commands import (
    CLR_INFO,
    CLR_JAIL,
    CLR_POLICY,
    CLR_SUCCESS,
    CLR_TICKET,
    CLR_WARNING,
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
    _do_jail,
    _do_unjail,
    _get_admin_role_ids,
    _get_config,
    _get_mod_role_ids,
    _is_admin,
    _is_mod,
    _post_audit,
    _setup_view,
    _ts_str,
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
    jail_expiry_loop,
    remove_ticket_participant,
    add_ticket_participant,
    reopen_ticket,
    start_policy_vote,
    write_audit,
)
from db_utils import get_config_value

if TYPE_CHECKING:
    from app_context import AppContext, Bot

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

        mod_role_ids = _get_mod_role_ids(ctx)
        admin_role_ids = _get_admin_role_ids(ctx)
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

        embed = discord.Embed(
            title=f"Policy Vote: {interaction.channel.name}",  # type: ignore[union-attr]
            color=CLR_POLICY,
        )
        embed.add_field(name="📜 Policy Text", value=vote_text_val, inline=False)
        embed.add_field(name="Votes Cast", value=f"0/{len(eligible)}", inline=True)
        embed.add_field(name="Status", value="🗳️ Voting", inline=True)
        embed.add_field(name="✅ Yes", value="—", inline=False)
        embed.add_field(name="❌ No", value="—", inline=False)
        embed.add_field(name="➖ Abstain", value="—", inline=False)
        embed.add_field(
            name="⏳ Awaiting",
            value=", ".join(f"<@{uid}>" for uid in eligible) or "—",
            inline=False,
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

        # Start jail expiry background task
        bot.startup_task_factories.append(lambda: jail_expiry_loop(bot, ctx))

    async def cog_unload(self) -> None:
        if hasattr(self, "_jail_context_menu"):
            self.bot.tree.remove_command(
                "Jail User", type=discord.AppCommandType.user
            )
        if hasattr(self, "_ticket_context_menu"):
            self.bot.tree.remove_command(
                "Open Ticket About This Message", type=discord.AppCommandType.message
            )

    # ── /setup ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="setup",
        description="First-time setup — creates jail role, channels, and mod config.",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        ctx = self.ctx
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return

        guild = interaction.guild
        if guild and guild.me:
            bot_perms = guild.me.guild_permissions
            required = {
                "Manage Roles": bot_perms.manage_roles,
                "Manage Channels": bot_perms.manage_channels,
                "Send Messages": bot_perms.send_messages,
                "Read Message History": bot_perms.read_message_history,
                "Embed Links": bot_perms.embed_links,
                "Attach Files": bot_perms.attach_files,
                "Manage Messages": bot_perms.manage_messages,
            }
            missing = [name for name, has in required.items() if not has]
            if missing:
                embed = discord.Embed(
                    title="⚠️ Missing Bot Permissions",
                    description=(
                        "The bot is missing the following required permissions:\n\n"
                        + "\n".join(f"• {p}" for p in missing)
                        + "\n\nGrant these in Server Settings → Integrations, then run `/setup` again."
                    ),
                    color=CLR_WARNING,
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        embed, view = _setup_view(ctx, 1)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /jail ─────────────────────────────────────────────────────────────

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
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📩 Support Tickets",
            description=(
                "Need help from the mod team? Click the button below to open a private ticket.\n\n"
                "A moderator will respond as soon as possible."
            ),
            color=CLR_TICKET,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(TicketPanelButton())
        msg = await channel.send(embed=embed, view=view)
        ctx.set_config_value("ticket_panel_channel_id", str(channel.id))
        ctx.set_config_value("ticket_panel_message_id", str(msg.id))
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
        name = f"ticket-{user.name[:16]}-{ts}"
        mod_role_ids = _get_mod_role_ids(ctx)

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

        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=desc_text,
            color=CLR_TICKET,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Opened by", value=user.mention, inline=True)
        embed.add_field(name="Status", value="🟢 Open", inline=True)
        view = discord.ui.View(timeout=None)
        view.add_item(TicketCloseButton(ticket_id))
        await channel.send(embed=embed, view=view)
        await interaction.followup.send(
            f"Ticket created → {channel.mention}", ephemeral=True
        )

        from commands.jail_commands import _dm_user
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
                from commands.jail_commands import _dm_user
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
                from commands.jail_commands import _dm_user
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

        admin_ids = _get_admin_role_ids(ctx)
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
    @app_commands.default_permissions(moderate_members=True)
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
        if not (_is_mod(user, ctx) or _is_admin(user, ctx)):
            await interaction.response.send_message(
                "Only mods and admins can open policy proposals.", ephemeral=True
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

        await interaction.response.defer(ephemeral=True)

        desc_text = description or "(no description)"
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
        safe_title = title[:20].lower().replace(" ", "-")
        name = f"policy-{safe_title}-{ts}"

        mod_role_ids = _get_mod_role_ids(ctx)
        admin_role_ids = _get_admin_role_ids(ctx)
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

        embed = discord.Embed(
            title=f"📋 Policy Proposal #{policy_id}: {title}",
            description=desc_text,
            color=CLR_POLICY,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Proposed by", value=user.mention, inline=True)
        embed.add_field(name="Status", value="💬 Open for Discussion", inline=True)
        embed.set_footer(text="Use /policy vote to start the formal vote when ready.")
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
            close_embed = discord.Embed(
                title="📋 Policy Proposal Closed",
                description=f"**{policy['title']}** was closed by {member.mention}.",
                color=CLR_INFO,
            )
            if reason_text:
                close_embed.add_field(name="Reason", value=reason_text, inline=False)
            await interaction.response.send_message(embed=close_embed)

            with ctx.open_db() as conn:
                adopted_policies = get_policies_by_ticket_id(conn, policy_id)
            if adopted_policies:
                adopted_embed = discord.Embed(
                    title="Adopted Policies from This Proposal", color=CLR_SUCCESS
                )
                for p in adopted_policies:
                    adopted_embed.add_field(
                        name=p["title"], value=p["description"][:1024], inline=False
                    )
                await channel.send(embed=adopted_embed)

            creator = guild.get_member(policy["creator_id"]) or member
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

        embed = discord.Embed(title="📋 Passed Policies", color=CLR_POLICY)
        for p in policies_list[:25]:
            passed_ts = f"<t:{int(p['passed_at'])}:d>"
            embed.add_field(
                name=f"#{p['id']} — {p['title']}",
                value=f"{p['description'][:100]}{'…' if len(p['description']) > 100 else ''}\nPassed: {passed_ts}",
                inline=False,
            )
        if len(policies_list) > 25:
            embed.set_footer(text=f"Showing 25 of {len(policies_list)} policies.")

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

        audit_embed = discord.Embed(
            title="⚠️ Warning Issued",
            description=f"{user.mention} warned by {member.mention}\n"
            + (f"**Reason:** {reason_text}\n" if reason_text else "")
            + f"**Active warnings:** {count}",
            color=CLR_WARNING,
        )
        await _post_audit(ctx, guild, audit_embed)

        with ctx.open_db() as conn:
            threshold = int(get_config_value(conn, "warning_threshold", "3"))
        if count == threshold:
            admin_ids = _get_admin_role_ids(ctx)
            pings = " ".join(f"<@&{rid}>" for rid in admin_ids) if admin_ids else ""
            alert = discord.Embed(
                title="🚨 Warning Threshold Reached",
                description=f"{user.mention} has reached **{count}** active warnings.\n{pings}",
                color=CLR_JAIL,
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

        lines: list[str] = []
        for w in warns:
            status = "~~Revoked~~" if w["revoked"] else "**Active**"
            dt = _ts_str(w["created_at"])
            line = f"#{w['id']} — {status} — {dt} — by <@{w['moderator_id']}>"
            if w["reason"]:
                line += f"\n  Reason: {w['reason']}"
            if w["revoked"] and w["revoke_reason"]:
                line += f"\n  Revoke reason: {w['revoke_reason']}"
            lines.append(line)

        embed = discord.Embed(
            title=f"Warnings for {user}",
            description="\n\n".join(lines[:20]),
            color=CLR_WARNING,
        )
        active = sum(1 for w in warns if not w["revoked"])
        embed.set_footer(text=f"{active} active / {len(warns)} total")
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        with ctx.open_db() as conn:
            active_jail = get_active_jail(conn, guild.id, user.id)
            jail_hist = get_jail_history(conn, guild.id, user.id)
            warns = get_warnings(conn, guild.id, user.id)
            tickets = get_ticket_history(conn, guild.id, user.id)

        embed = discord.Embed(title=f"Mod Info — {user}", color=CLR_INFO)

        if active_jail:
            jail_text = f"**Currently jailed** since {_ts_str(active_jail['created_at'])}"
            if active_jail["expires_at"]:
                jail_text += f"\nExpires: {_ts_str(active_jail['expires_at'])}"
            if active_jail["reason"]:
                jail_text += f"\nReason: {active_jail['reason']}"
        else:
            jail_text = "Not currently jailed"
        if len(jail_hist) > 1 or (len(jail_hist) == 1 and not active_jail):
            past = [j for j in jail_hist if j["status"] != "active"]
            jail_text += f"\n**Past jails:** {len(past)}"
            if past:
                recent = past[0]
                jail_text += f"\n  Most recent: {_ts_str(recent['created_at'])} — {recent.get('release_reason', '')}"
        embed.add_field(name="🔒 Jail", value=jail_text, inline=False)

        active_warns = [w for w in warns if not w["revoked"]]
        warn_text = f"**Active:** {len(active_warns)} / **Total:** {len(warns)}"
        for w in active_warns[:3]:
            warn_text += f"\n  #{w['id']} — {_ts_str(w['created_at'])} — {w['reason'] or 'no reason'}"
        embed.add_field(name="⚠️ Warnings", value=warn_text, inline=False)

        open_t = sum(1 for t in tickets if t["status"] == "open")
        closed_t = sum(1 for t in tickets if t["status"] in ("closed", "deleted"))
        ticket_text = f"**Open:** {open_t} / **Closed:** {closed_t}"
        if tickets:
            recent_ticket = tickets[0]
            ticket_text += f"\n  Most recent: #{recent_ticket['id']} — {recent_ticket['status']} — {_ts_str(recent_ticket['created_at'])}"
        embed.add_field(name="📩 Tickets", value=ticket_text, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: Bot) -> None:
    await bot.add_cog(JailCog(bot, bot.ctx))
