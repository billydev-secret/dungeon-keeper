"""Jail & Ticket moderation commands.

Implements /setup, /jail, /unjail, /ticket, /pull, /remove, /warn, /warnings,
/revokewarn, /modinfo, and context menu commands per the spec.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from db_utils import get_config_value
from services.moderation import (
    add_policy,
    add_ticket_participant,
    cast_policy_vote,
    claim_ticket,
    close_policy_ticket,
    close_ticket,
    create_jail,
    create_policy_ticket,
    create_ticket,
    create_warning,
    delete_ticket,
    escalate_ticket,
    fmt_duration,
    generate_transcript,
    render_transcript_html,
    get_active_jail,
    get_active_warning_count,
    get_expired_jails,
    get_jail_by_channel,
    get_jail_history,
    get_policies,
    get_policies_by_ticket_id,
    get_policy_ticket,
    get_policy_ticket_by_channel,
    get_policy_votes,
    get_ticket_by_channel,
    get_ticket_history,
    get_warnings,
    parse_duration,
    release_jail,
    remove_ticket_participant,
    reopen_ticket,
    resolve_policy_vote,
    revoke_warning,
    start_policy_vote,
    store_transcript,
    write_audit,
)

if TYPE_CHECKING:
    from app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.jail_commands")

# Embed colours matching spec §12
CLR_JAIL = discord.Color.from_str("#E74C3C")
CLR_TICKET = discord.Color.from_str("#3498DB")
CLR_SUCCESS = discord.Color.from_str("#2ECC71")
CLR_INFO = discord.Color.from_str("#95A5A6")
CLR_WARNING = discord.Color.from_str("#F1C40F")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_mod_role_ids(ctx: AppContext) -> set[int]:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, "mod_role_ids", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def _get_admin_role_ids(ctx: AppContext) -> set[int]:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, "admin_role_ids", "")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


def _is_mod(member: discord.Member, ctx: AppContext) -> bool:
    """Check if member has mod access via configured roles or manage_guild."""
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    mod_ids = _get_mod_role_ids(ctx)
    return bool(mod_ids & {r.id for r in member.roles})


def _is_admin(member: discord.Member, ctx: AppContext) -> bool:
    """Check if member has admin access via the Discord ADMINISTRATOR bit or a configured admin role."""
    if member.guild_permissions.administrator:
        return True
    admin_ids = _get_admin_role_ids(ctx)
    return bool(admin_ids & {r.id for r in member.roles})


def _get_config(ctx: AppContext, key: str, default: str = "0") -> int:
    with ctx.open_db() as conn:
        return int(get_config_value(conn, key, default) or 0)


def _ts_str(ts: float | None) -> str:
    if ts is None:
        return "N/A"
    return f"<t:{int(ts)}:f>"


# ---------------------------------------------------------------------------
# DM helper — wraps DM sends with failure handling
# ---------------------------------------------------------------------------


async def _dm_user(
    user: discord.User | discord.Member,
    *,
    embed: discord.Embed | None = None,
    content: str | None = None,
    file: discord.File | None = None,
    fallback_channel=None,
) -> bool:
    """Send a DM; return True if successful.  Post note to fallback_channel on failure."""
    try:
        kwargs: dict = {}
        if embed:
            kwargs["embed"] = embed
        if content:
            kwargs["content"] = content
        if file:
            kwargs["file"] = file
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        if fallback_channel:
            await fallback_channel.send(
                f"⚠️ Could not DM {user.mention} — they may have DMs disabled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return False


# ---------------------------------------------------------------------------
# Audit embed helper
# ---------------------------------------------------------------------------


async def _post_audit(
    ctx: AppContext, guild: discord.Guild, embed: discord.Embed
) -> None:
    log_ch_id = _get_config(ctx, "log_channel_id")
    if not log_ch_id:
        return
    ch = guild.get_channel(log_ch_id)
    if ch and isinstance(ch, discord.TextChannel):
        await ch.send(embed=embed)


# ---------------------------------------------------------------------------
# Transcript helper
# ---------------------------------------------------------------------------


async def _collect_and_post_transcript(
    ctx: AppContext,
    channel: discord.TextChannel,
    *,
    record_type: str,
    record_id: int,
    user: discord.User | discord.Member,
    extra_meta: dict | None = None,
) -> None:
    """Generate transcript, store in DB, post to transcript channel, DM to user."""
    transcript = await generate_transcript(
        channel,
        record_type=record_type,
        record_id=record_id,
        extra_meta=extra_meta,
    )
    with ctx.open_db() as conn:
        store_transcript(
            conn,
            guild_id=channel.guild.id,
            record_type=record_type,
            record_id=record_id,
            content=transcript,
        )

    # Build HTML file
    html_bytes = render_transcript_html(transcript).encode("utf-8")
    filename = f"{record_type}-{record_id}-transcript.html"

    # Post to transcript channel
    transcript_ch_id = _get_config(ctx, "transcript_channel_id")
    if not transcript_ch_id:
        transcript_ch_id = _get_config(ctx, "log_channel_id")
    if transcript_ch_id:
        ch = channel.guild.get_channel(transcript_ch_id)
        if ch and isinstance(ch, discord.TextChannel):
            embed = discord.Embed(
                title=f"Transcript — {record_type.title()} #{record_id}",
                description=f"**Channel:** #{channel.name}\n**Messages:** {transcript['message_count']}",
                color=CLR_INFO,
            )
            await ch.send(
                embed=embed, file=discord.File(io.BytesIO(html_bytes), filename)
            )

    # DM to user
    await _dm_user(user, file=discord.File(io.BytesIO(html_bytes), filename))


# ═══════════════════════════════════════════════════════════════════════════
# SETUP WIZARD
# ═══════════════════════════════════════════════════════════════════════════


class _SetupRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self, config_key: str, ctx: AppContext, *, placeholder: str, max_values: int = 5
    ):
        super().__init__(placeholder=placeholder, min_values=0, max_values=max_values)
        self.config_key = config_key
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        ids = ",".join(str(r.id) for r in self.values)
        self.ctx.set_config_value(self.config_key, ids)
        names = ", ".join(f"@{r.name}" for r in self.values) or "(none)"
        await interaction.response.send_message(
            f"✅ Set **{self.config_key}** → {names}", ephemeral=True
        )


class _SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, ctx: AppContext, *, placeholder: str):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.config_key = config_key
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        ch = self.values[0]
        self.ctx.set_config_value(self.config_key, str(ch.id))
        await interaction.response.send_message(
            f"✅ Set **{self.config_key}** → #{ch}", ephemeral=True
        )


class _SetupCategorySelect(discord.ui.ChannelSelect):
    def __init__(self, config_key: str, ctx: AppContext, *, placeholder: str):
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.category],
        )
        self.config_key = config_key
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction) -> None:
        cat = self.values[0]
        self.ctx.set_config_value(self.config_key, str(cat.id))
        await interaction.response.send_message(
            f"✅ Set **{self.config_key}** → {cat}", ephemeral=True
        )


def _setup_view(ctx: AppContext, step: int) -> tuple[discord.Embed, discord.ui.View]:
    """Return the embed + view for a given setup step."""
    view = discord.ui.View(timeout=300)

    if step == 1:
        embed = discord.Embed(
            title="Setup — Step 1/6",
            description="Which roles should have **moderator** access?",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupRoleSelect("mod_role_ids", ctx, placeholder="Select mod roles…")
        )
    elif step == 2:
        embed = discord.Embed(
            title="Setup — Step 2/6",
            description="Which roles are **admin/senior staff**? (for escalations and warning alerts)",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupRoleSelect("admin_role_ids", ctx, placeholder="Select admin roles…")
        )
    elif step == 3:
        embed = discord.Embed(
            title="Setup — Step 3/6",
            description="Where should **jail channels** be created?",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupCategorySelect(
                "jail_category_id", ctx, placeholder="Select jail category…"
            )
        )
    elif step == 4:
        embed = discord.Embed(
            title="Setup — Step 4/6",
            description="Where should **ticket channels** be created?",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupCategorySelect(
                "ticket_category_id", ctx, placeholder="Select ticket category…"
            )
        )
    elif step == 5:
        embed = discord.Embed(
            title="Setup — Step 5/6",
            description="Where should **audit logs** be posted?",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupChannelSelect(
                "log_channel_id", ctx, placeholder="Select log channel…"
            )
        )
    elif step == 6:
        embed = discord.Embed(
            title="Setup — Step 6/6",
            description="Where should **transcripts** be posted? (can be the same as log channel)",
            color=CLR_TICKET,
        )
        view.add_item(
            _SetupChannelSelect(
                "transcript_channel_id", ctx, placeholder="Select transcript channel…"
            )
        )
    else:
        embed = discord.Embed(
            title="Setup Complete",
            description="All settings saved. Use `/config` to adjust later.",
            color=CLR_SUCCESS,
        )
        return embed, discord.ui.View()

    # Next / skip
    async def next_step(interaction: discord.Interaction):
        e, v = _setup_view(ctx, step + 1)
        await interaction.response.edit_message(embed=e, view=v)

    btn: discord.ui.Button = discord.ui.Button(
        label="Next →" if step < 6 else "Finish", style=discord.ButtonStyle.primary
    )  # type: ignore[assignment]
    btn.callback = next_step  # type: ignore[method-assign]
    view.add_item(btn)
    return embed, view


# ═══════════════════════════════════════════════════════════════════════════
# PERSISTENT TICKET VIEWS (survive restarts)
# ═══════════════════════════════════════════════════════════════════════════


class TicketPanelButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket_panel:open",
):
    """Persistent '📩 Open Ticket' button on the panel embed."""

    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(
                label="Open Ticket",
                emoji="📩",
                style=discord.ButtonStyle.success,
                custom_id="ticket_panel:open",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_TicketOpenModal())


class TicketCloseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket_action:close:(?P<tid>\d+)",
):
    """Persistent '🔒 Close Ticket' button inside open tickets."""

    def __init__(self, ticket_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Close Ticket",
                emoji="🔒",
                style=discord.ButtonStyle.danger,
                custom_id=f"ticket_action:close:{ticket_id}",
            )
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        tid = int((item.custom_id or "").split(":")[-1])
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(_TicketCloseModal(self.ticket_id))


class TicketReopenButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket_action:reopen:(?P<tid>\d+)",
):
    """Persistent '🔓 Reopen' button on closed tickets."""

    def __init__(self, ticket_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Reopen",
                emoji="🔓",
                style=discord.ButtonStyle.success,
                custom_id=f"ticket_action:reopen:{ticket_id}",
            )
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        tid = int((item.custom_id or "").split(":")[-1])
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get ctx from bot
        bot = interaction.client
        ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can reopen tickets.", ephemeral=True
            )
            return

        with ctx.open_db() as conn:
            reopen_ticket(conn, self.ticket_id)
            write_audit(
                conn,
                guild_id=interaction.guild_id or 0,
                action="ticket_reopen",
                actor_id=member.id,
                extra={"ticket_id": self.ticket_id},
            )

        # Restore send permission for creator
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            with ctx.open_db() as conn:
                ticket = get_ticket_by_channel(conn, channel.id)
            if ticket:
                creator = interaction.guild.get_member(ticket["user_id"])  # type: ignore[union-attr]
                if creator:
                    await channel.set_permissions(
                        creator,
                        view_channel=True,
                        send_messages=True,
                        read_message_history=True,
                        attach_files=True,
                    )
                await _dm_user(
                    creator or interaction.user,
                    embed=discord.Embed(
                        description=f"Your ticket in **{interaction.guild.name}** has been reopened.",  # type: ignore[union-attr]
                        color=CLR_TICKET,
                    ),
                )

            # Swap to close button
            view = discord.ui.View(timeout=None)
            view.add_item(TicketCloseButton(self.ticket_id))
            await interaction.response.edit_message(view=view)
            await channel.send(
                f"🔓 Ticket reopened by {member.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )


class TicketDeleteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"ticket_action:delete:(?P<tid>\d+)",
):
    """Persistent '🗑️ Delete' button on closed tickets."""

    def __init__(self, ticket_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Delete",
                emoji="🗑️",
                style=discord.ButtonStyle.danger,
                custom_id=f"ticket_action:delete:{ticket_id}",
            )
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        tid = int((item.custom_id or "").split(":")[-1])
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can delete tickets.", ephemeral=True
            )
            return

        # Confirm
        confirm_view = discord.ui.View(timeout=30)
        confirmed = False

        async def do_confirm(inter: discord.Interaction):
            nonlocal confirmed
            confirmed = True
            await inter.response.defer()
            confirm_view.stop()

        async def do_cancel(inter: discord.Interaction):
            await inter.response.edit_message(content="Deletion cancelled.", view=None)
            confirm_view.stop()

        btn_yes: discord.ui.Button = discord.ui.Button(
            label="Confirm Delete", style=discord.ButtonStyle.danger
        )  # type: ignore[assignment]
        btn_no: discord.ui.Button = discord.ui.Button(
            label="Cancel", style=discord.ButtonStyle.secondary
        )  # type: ignore[assignment]
        btn_yes.callback = do_confirm  # type: ignore[method-assign,assignment]
        btn_no.callback = do_cancel  # type: ignore[method-assign,assignment]
        confirm_view.add_item(btn_yes)
        confirm_view.add_item(btn_no)
        await interaction.response.edit_message(
            content="⚠️ This will permanently delete this ticket and generate a transcript. Continue?",
            view=confirm_view,
        )
        await confirm_view.wait()

        if not confirmed:
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, channel.id)

        if not ticket:
            return

        # Transcript
        creator = interaction.guild.get_member(ticket["user_id"]) or interaction.user  # type: ignore[union-attr]
        await _collect_and_post_transcript(
            ctx,
            channel,
            record_type="ticket",
            record_id=self.ticket_id,
            user=creator,
            extra_meta={
                "closed_by": member.id,
                "close_reason": ticket.get("close_reason", ""),
                "transcript_stage": "delete",
            },
        )

        with ctx.open_db() as conn:
            delete_ticket(conn, self.ticket_id)
            write_audit(
                conn,
                guild_id=interaction.guild_id or 0,
                action="ticket_delete",
                actor_id=member.id,
                target_id=ticket["user_id"],
                extra={"ticket_id": self.ticket_id, "message_count": 0},
            )

        audit_embed = discord.Embed(
            title="🗑️ Ticket Deleted",
            description=f"**Ticket #{self.ticket_id}** by <@{ticket['user_id']}> deleted by {member.mention}",
            color=CLR_TICKET,
        )
        await _post_audit(ctx, interaction.guild, audit_embed)  # type: ignore[arg-type]
        await channel.delete(reason=f"Ticket #{self.ticket_id} deleted by {member}")


# ---------------------------------------------------------------------------
# Policy vote persistent buttons
# ---------------------------------------------------------------------------

CLR_POLICY = discord.Color.from_str("#9B59B6")  # purple for policy actions


async def _handle_policy_vote(
    interaction: discord.Interaction, policy_id: int, vote: str
) -> None:
    """Shared handler for all three policy vote buttons."""
    bot = interaction.client
    ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
    member = interaction.user
    guild = interaction.guild
    if not isinstance(member, discord.Member) or not guild:
        await interaction.response.send_message("Server-only.", ephemeral=True)
        return
    if not (_is_mod(member, ctx) or _is_admin(member, ctx)):
        await interaction.response.send_message(
            "Only mods and admins can vote.", ephemeral=True
        )
        return

    with ctx.open_db() as conn:
        policy = get_policy_ticket(conn, policy_id)
    if not policy or policy["status"] != "voting":
        await interaction.response.send_message(
            "This vote is no longer active.", ephemeral=True
        )
        return

    # Cast or update vote
    with ctx.open_db() as conn:
        cast_policy_vote(conn, policy_id=policy_id, user_id=member.id, vote=vote)
        votes = get_policy_votes(conn, policy_id)

    # Build eligible voter set
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

    vote_map = {v["user_id"]: v["vote"] for v in votes}
    voted_ids = set(vote_map.keys()) & eligible
    yes_ids = [uid for uid in voted_ids if vote_map[uid] == "yes"]
    no_ids = [uid for uid in voted_ids if vote_map[uid] == "no"]
    abstain_ids = [uid for uid in voted_ids if vote_map[uid] == "abstain"]
    awaiting_ids = eligible - voted_ids

    # Build updated embed
    embed = discord.Embed(
        title=f"Policy Vote: {policy['title']}",
        color=CLR_POLICY,
    )
    embed.add_field(
        name="📜 Policy Text",
        value=policy["vote_text"] or policy["description"] or "(no text)",
        inline=False,
    )
    embed.add_field(
        name="Votes Cast", value=f"{len(voted_ids)}/{len(eligible)}", inline=True
    )
    embed.add_field(name="Status", value="🗳️ Voting", inline=True)
    embed.add_field(
        name="✅ Yes",
        value=", ".join(f"<@{uid}>" for uid in yes_ids) or "—",
        inline=False,
    )
    embed.add_field(
        name="❌ No",
        value=", ".join(f"<@{uid}>" for uid in no_ids) or "—",
        inline=False,
    )
    embed.add_field(
        name="➖ Abstain",
        value=", ".join(f"<@{uid}>" for uid in abstain_ids) or "—",
        inline=False,
    )
    embed.add_field(
        name="⏳ Awaiting",
        value=", ".join(f"<@{uid}>" for uid in awaiting_ids) or "—",
        inline=False,
    )

    # Check if all eligible voters have voted
    all_voted = len(awaiting_ids) == 0
    if all_voted:
        has_no = len(no_ids) > 0
        if has_no:
            # Failed
            with ctx.open_db() as conn:
                resolve_policy_vote(conn, policy_id, status="failed")
                write_audit(
                    conn,
                    guild_id=guild.id,
                    action="policy_vote_failed",
                    actor_id=member.id,
                    extra={
                        "policy_id": policy_id,
                        "yes": len(yes_ids),
                        "no": len(no_ids),
                        "abstain": len(abstain_ids),
                    },
                )
            embed.color = discord.Color.from_str("#E74C3C")
            embed.set_field_at(2, name="Status", value="❌ Rejected", inline=True)
            view = discord.ui.View(timeout=None)  # No more buttons
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(
                f"Your vote ({vote}) has been recorded.", ephemeral=True
            )
            channel = interaction.channel
            vote_text = policy["vote_text"] or policy["title"]
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    f"❌ **Policy rejected.** The proposal did not achieve unanimous support.\n"
                    f"**Rejected policy:** {vote_text}"
                )
                # Generate transcript before deleting
                creator = guild.get_member(policy["creator_id"]) or member
                await _collect_and_post_transcript(
                    ctx,
                    channel,
                    record_type="policy_ticket",
                    record_id=policy_id,
                    user=creator,
                    extra_meta={
                        "resolution": "failed",
                        "policy_title": policy["title"],
                        "vote_yes": len(yes_ids),
                        "vote_no": len(no_ids),
                        "vote_abstain": len(abstain_ids),
                    },
                )
                await channel.delete(reason=f"Policy #{policy_id} rejected")
            audit_embed = discord.Embed(
                title="❌ Policy Rejected",
                description=f"**{policy['title']}**\n📜 {vote_text}\n\nVote: {len(yes_ids)} yes, {len(no_ids)} no, {len(abstain_ids)} abstain",
                color=discord.Color.from_str("#E74C3C"),
            )
            await _post_audit(ctx, guild, audit_embed)
        else:
            # Passed — store the vote_text as the adopted policy
            adopted_text = policy["vote_text"] or policy["description"]
            with ctx.open_db() as conn:
                resolve_policy_vote(conn, policy_id, status="passed")
                policy_row_id = add_policy(
                    conn,
                    guild_id=guild.id,
                    policy_ticket_id=policy_id,
                    title=policy["title"],
                    description=adopted_text,
                )
                write_audit(
                    conn,
                    guild_id=guild.id,
                    action="policy_passed",
                    actor_id=member.id,
                    extra={
                        "policy_id": policy_id,
                        "policy_row_id": policy_row_id,
                        "vote_text": adopted_text,
                        "yes": len(yes_ids),
                        "no": 0,
                        "abstain": len(abstain_ids),
                    },
                )
            embed.color = CLR_SUCCESS
            embed.set_field_at(2, name="Status", value="✅ Adopted", inline=True)
            view = discord.ui.View(timeout=None)
            await interaction.response.edit_message(embed=embed, view=view)
            await interaction.followup.send(
                f"Your vote ({vote}) has been recorded.", ephemeral=True
            )
            channel = interaction.channel
            if isinstance(channel, discord.TextChannel):
                await channel.send(
                    f'✅ **Policy adopted!** "{policy["title"]}" is now in effect.\n'
                    f"**Adopted policy:** {adopted_text}\n"
                    f"({len(yes_ids)} yes, {len(abstain_ids)} abstain)"
                )
                # List adopted policies
                with ctx.open_db() as conn:
                    adopted_policies = get_policies_by_ticket_id(conn, policy_id)
                if adopted_policies:
                    adopted_embed = discord.Embed(
                        title="Adopted Policies",
                        color=CLR_SUCCESS,
                    )
                    for p in adopted_policies:
                        adopted_embed.add_field(
                            name=p["title"],
                            value=p["description"][:1024],
                            inline=False,
                        )
                    await channel.send(embed=adopted_embed)
                # Generate transcript before deleting
                creator = guild.get_member(policy["creator_id"]) or member
                await _collect_and_post_transcript(
                    ctx,
                    channel,
                    record_type="policy_ticket",
                    record_id=policy_id,
                    user=creator,
                    extra_meta={
                        "resolution": "passed",
                        "policy_title": policy["title"],
                        "adopted_text": adopted_text,
                        "vote_yes": len(yes_ids),
                        "vote_no": 0,
                        "vote_abstain": len(abstain_ids),
                    },
                )
                await channel.delete(reason=f"Policy #{policy_id} adopted")
            audit_embed = discord.Embed(
                title="✅ Policy Adopted",
                description=f"**{policy['title']}**\n📜 {adopted_text}\n\nVote: {len(yes_ids)} yes, {len(abstain_ids)} abstain",
                color=CLR_SUCCESS,
            )
            await _post_audit(ctx, guild, audit_embed)
    else:
        # Still waiting for votes — update embed, keep buttons
        view = discord.ui.View(timeout=None)
        view.add_item(PolicyVoteYesButton(policy_id))
        view.add_item(PolicyVoteNoButton(policy_id))
        view.add_item(PolicyVoteAbstainButton(policy_id))
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(
            f"Your vote ({vote}) has been recorded.", ephemeral=True
        )


class PolicyVoteYesButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"policy_vote:yes:(?P<pid>\d+)",
):
    def __init__(self, policy_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Yes",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"policy_vote:yes:{policy_id}",
            )
        )
        self.policy_id = policy_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        pid = int((item.custom_id or "").split(":")[-1])
        return cls(pid)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_policy_vote(interaction, self.policy_id, "yes")


class PolicyVoteNoButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"policy_vote:no:(?P<pid>\d+)",
):
    def __init__(self, policy_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="No",
                emoji="❌",
                style=discord.ButtonStyle.danger,
                custom_id=f"policy_vote:no:{policy_id}",
            )
        )
        self.policy_id = policy_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        pid = int((item.custom_id or "").split(":")[-1])
        return cls(pid)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_policy_vote(interaction, self.policy_id, "no")


class PolicyVoteAbstainButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"policy_vote:abstain:(?P<pid>\d+)",
):
    def __init__(self, policy_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Abstain",
                emoji="➖",
                style=discord.ButtonStyle.secondary,
                custom_id=f"policy_vote:abstain:{policy_id}",
            )
        )
        self.policy_id = policy_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        pid = int((item.custom_id or "").split(":")[-1])
        return cls(pid)

    async def callback(self, interaction: discord.Interaction) -> None:
        await _handle_policy_vote(interaction, self.policy_id, "abstain")


# Modals


class _TicketOpenModal(discord.ui.Modal, title="Open a Ticket"):
    description: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="What do you need help with?",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return

        cat_id = _get_config(ctx, "ticket_category_id")
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category is not configured. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        # Create channel
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

        desc_text = self.description.value or "(no description)"
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

        # Post ticket embed
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

        # DM the creator
        await _dm_user(
            user,
            embed=discord.Embed(
                description=f"Your ticket has been created in **{guild.name}** → [Go to ticket]({channel.jump_url})",
                color=CLR_TICKET,
            ),
        )

        # Notify mods
        with ctx.open_db() as conn:
            notify = get_config_value(conn, "ticket_notify_on_create", "1")
        if notify != "0":
            for rid in mod_role_ids:
                role = guild.get_role(rid)
                if not role:
                    continue
                for m in role.members:
                    if m.bot or m.id == user.id:
                        continue
                    await _dm_user(
                        m,
                        embed=discord.Embed(
                            title="📩 New Ticket",
                            description=f"**{user}** opened a ticket → [Jump to ticket]({channel.jump_url})\n\n{desc_text}",
                            color=CLR_TICKET,
                        ),
                    )

        # Audit
        audit_embed = discord.Embed(
            title="📩 Ticket Opened",
            description=f"**Ticket #{ticket_id}** by {user.mention} in {channel.mention}",
            color=CLR_TICKET,
        )
        await _post_audit(ctx, guild, audit_embed)


class _TicketCloseModal(discord.ui.Modal, title="Close Ticket"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)", required=False, max_length=500
    )  # type: ignore[assignment]

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
        member = interaction.user
        guild = interaction.guild
        if not isinstance(member, discord.Member) or guild is None:
            return
        if not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can close tickets.", ephemeral=True
            )
            return

        reason = self.reason.value or ""
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
            if not ticket or ticket["status"] != "open":
                await interaction.response.send_message(
                    "This ticket is not open.", ephemeral=True
                )
                return
            close_ticket(conn, self.ticket_id, closed_by=member.id, reason=reason)
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_close",
                actor_id=member.id,
                target_id=ticket["user_id"],
                extra={"ticket_id": self.ticket_id, "reason": reason},
            )

        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            # Lock channel — creator can view but not send
            creator = guild.get_member(ticket["user_id"])
            if creator:
                await channel.set_permissions(
                    creator,
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
                )

            # Swap buttons to Reopen + Delete
            view = discord.ui.View(timeout=None)
            view.add_item(TicketReopenButton(self.ticket_id))
            view.add_item(TicketDeleteButton(self.ticket_id))
            await interaction.response.edit_message(view=view)

            close_msg = f"🔒 Ticket closed by {member.mention}."
            if reason:
                close_msg += f"\n**Reason:** {reason}"
            await channel.send(
                close_msg, allowed_mentions=discord.AllowedMentions.none()
            )

            # DM creator
            if creator:
                await _dm_user(
                    creator,
                    embed=discord.Embed(
                        description=f"Your ticket in **{guild.name}** has been closed.\n{f'**Reason:** {reason}' if reason else ''}\nYou can still view the channel.",
                        color=CLR_TICKET,
                    ),
                    fallback_channel=channel,
                )

        audit_embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=f"**Ticket #{self.ticket_id}** closed by {member.mention}"
            + (f"\nReason: {reason}" if reason else ""),
            color=CLR_TICKET,
        )
        await _post_audit(ctx, guild, audit_embed)


class _JailModal(discord.ui.Modal, title="Jail User"):
    duration_input: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Duration (e.g. 24h, 7d, leave blank for indefinite)",
        required=False,
        max_length=20,
    )
    reason_input: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Reason",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, target: discord.Member, ctx: AppContext):
        super().__init__()
        self.target = target
        self.ctx = ctx

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await _do_jail(
            interaction,
            self.ctx,
            self.target,
            duration_str=self.duration_input.value,
            reason=self.reason_input.value or "",
        )


# ═══════════════════════════════════════════════════════════════════════════
# JAIL LOGIC
# ═══════════════════════════════════════════════════════════════════════════


async def _do_jail(
    interaction: discord.Interaction,
    ctx: AppContext,
    target: discord.Member,
    *,
    duration_str: str = "",
    reason: str = "",
) -> None:
    guild = interaction.guild
    mod = interaction.user
    if guild is None or not isinstance(mod, discord.Member):
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return

    # Validation
    if target.bot:
        await interaction.response.send_message("Cannot jail a bot.", ephemeral=True)
        return
    if target.id == mod.id:
        await interaction.response.send_message("Cannot jail yourself.", ephemeral=True)
        return
    if _is_admin(target, ctx):
        await interaction.response.send_message("Cannot jail an admin.", ephemeral=True)
        return
    if _is_mod(target, ctx) and not _is_admin(mod, ctx):
        await interaction.response.send_message(
            "Only admins can jail a moderator.", ephemeral=True
        )
        return
    with ctx.open_db() as conn:
        if get_active_jail(conn, guild.id, target.id):
            await interaction.response.send_message(
                f"{target} is already jailed.", ephemeral=True
            )
            return

    duration_seconds = parse_duration(duration_str) if duration_str else None

    await interaction.response.defer(ephemeral=True)

    # Ensure @Jailed role exists
    jailed_role_id = _get_config(ctx, "jailed_role_id")
    jailed_role = guild.get_role(jailed_role_id) if jailed_role_id else None
    if not jailed_role:
        jailed_role = await guild.create_role(
            name="Jailed",
            reason="Dungeon Keeper jail system setup",
            permissions=discord.Permissions.none(),
        )
        ctx.set_config_value("jailed_role_id", str(jailed_role.id))
        # Deny view + send on all channels
        for channel in guild.channels:
            try:
                await channel.set_permissions(
                    jailed_role, view_channel=False, send_messages=False
                )
            except discord.Forbidden:
                pass

    # Snapshot roles
    stored_roles = [
        r.id for r in target.roles if r != guild.default_role and r.id != jailed_role.id
    ]

    # Strip roles + assign Jailed
    try:
        await target.edit(roles=[jailed_role], reason=f"Jailed by {mod}")
    except discord.Forbidden:
        await interaction.followup.send(
            "I don't have permission to manage this user's roles.", ephemeral=True
        )
        return

    # Create jail channel
    cat_id = _get_config(ctx, "jail_category_id")
    category = guild.get_channel(cat_id) if cat_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    ch_name = f"jail-{target.name[:16]}-{ts}"
    mod_role_ids = _get_mod_role_ids(ctx)

    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False, send_messages=False
        ),
        jailed_role: discord.PermissionOverwrite(view_channel=False),
        target: discord.PermissionOverwrite(
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

    jail_channel = await guild.create_text_channel(
        ch_name, category=category, overwrites=overwrites  # type: ignore[arg-type]
    )

    # DB record
    with ctx.open_db() as conn:
        jail_id = create_jail(
            conn,
            guild_id=guild.id,
            user_id=target.id,
            moderator_id=mod.id,
            reason=reason,
            stored_roles=stored_roles,
            channel_id=jail_channel.id,
            duration_seconds=duration_seconds,
        )
        write_audit(
            conn,
            guild_id=guild.id,
            action="jail_create",
            actor_id=mod.id,
            target_id=target.id,
            extra={
                "jail_id": jail_id,
                "reason": reason,
                "duration": fmt_duration(duration_seconds)
                if duration_seconds
                else "indefinite",
            },
        )

    # Post embed in jail channel
    duration_text = fmt_duration(duration_seconds) if duration_seconds else "Indefinite"
    now_ts = int(datetime.now(timezone.utc).timestamp())
    expiry_ts = now_ts + duration_seconds if duration_seconds else None
    countdown_line = (
        f"**Releases:** <t:{expiry_ts}:R> (<t:{expiry_ts}:f>)\n"
        if expiry_ts
        else ""
    )
    embed = discord.Embed(
        title="Moderation Hold",
        description=(
            f"{target.mention}, you have been placed in a moderation hold.\n\n"
            f"**Moderator:** {mod.mention}\n"
            f"**Duration:** {duration_text}\n"
            + countdown_line
            + (f"**Reason:** {reason}\n" if reason else "")
            + "\nA moderator will review your case here."
        ),
        color=CLR_JAIL,
        timestamp=datetime.now(timezone.utc),
    )
    await jail_channel.send(embed=embed)

    # DM the user
    dm_embed = discord.Embed(
        title="You've been placed in a moderation hold",
        description=(
            f"**Server:** {guild.name}\n"
            f"**Moderator:** {mod}\n"
            f"**Duration:** {duration_text}\n"
            + (f"**Reason:** {reason}\n" if reason else "")
            + "\nPlease check the jail channel — a moderator will review your situation."
        ),
        color=CLR_JAIL,
    )
    await _dm_user(target, embed=dm_embed, fallback_channel=jail_channel)

    await interaction.followup.send(
        f"✅ {target} has been jailed → {jail_channel.mention}", ephemeral=True
    )

    # Audit
    audit_embed = discord.Embed(
        title="🔒 Member Jailed",
        description=f"{target.mention} jailed by {mod.mention}\n**Duration:** {duration_text}"
        + (f"\n**Reason:** {reason}" if reason else ""),
        color=CLR_JAIL,
    )
    await _post_audit(ctx, guild, audit_embed)


async def _do_unjail(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    *,
    reason: str = "",
    actor: discord.Member | None = None,
) -> str:
    """Core unjail logic.  Returns a status message."""
    with ctx.open_db() as conn:
        jail = get_active_jail(conn, guild.id, target.id)
    if not jail:
        return f"{target} is not currently jailed."

    # Restore roles
    stored = json.loads(jail["stored_roles"])
    roles_to_add: list[discord.Role] = []
    missing: list[int] = []
    for rid in stored:
        role = guild.get_role(rid)
        if role:
            roles_to_add.append(role)
        else:
            missing.append(rid)

    try:
        await target.edit(roles=roles_to_add, reason=f"Unjailed: {reason}")
    except discord.Forbidden:
        return "Could not restore roles — missing permissions."

    # Transcript
    jail_channel = guild.get_channel(jail["channel_id"])
    if isinstance(jail_channel, discord.TextChannel):
        duration_served = time.time() - jail["created_at"]
        await _collect_and_post_transcript(
            ctx,
            jail_channel,
            record_type="jail",
            record_id=jail["id"],
            user=target,
            extra_meta={
                "reason": reason,
                "duration_served": fmt_duration(int(duration_served)),
            },
        )
        await jail_channel.delete(reason=f"Jail #{jail['id']} released")

    # Update DB
    actor_id = actor.id if actor else 0
    with ctx.open_db() as conn:
        release_jail(conn, jail["id"], reason=reason)
        write_audit(
            conn,
            guild_id=guild.id,
            action="jail_release",
            actor_id=actor_id,
            target_id=target.id,
            extra={"jail_id": jail["id"], "reason": reason},
        )

    # DM
    dm_embed = discord.Embed(
        title="You've been released",
        description=f"Your moderation hold in **{guild.name}** has been lifted.\n"
        + (f"**Reason:** {reason}" if reason else ""),
        color=CLR_SUCCESS,
    )
    await _dm_user(target, embed=dm_embed)

    # Audit
    audit_embed = discord.Embed(
        title="🔓 Member Released",
        description=f"{target.mention} released"
        + (f" by {actor.mention}" if actor else " (auto-expired)")
        + (f"\n**Reason:** {reason}" if reason else ""),
        color=CLR_SUCCESS,
    )
    await _post_audit(ctx, guild, audit_embed)

    note = ""
    if missing:
        note = f"\n⚠️ Could not restore {len(missing)} deleted role(s)."
    return f"✅ {target} has been released from jail.{note}"


# ═══════════════════════════════════════════════════════════════════════════
# REJOIN DETECTION (called from events.py on_member_join)
# ═══════════════════════════════════════════════════════════════════════════


async def check_jail_rejoin(ctx: AppContext, member: discord.Member) -> bool:
    """If the member has an active jail, re-apply it. Returns True if jailed."""
    with ctx.open_db() as conn:
        jail = get_active_jail(conn, member.guild.id, member.id)
    if not jail:
        return False

    jailed_role_id = _get_config(ctx, "jailed_role_id")
    jailed_role = member.guild.get_role(jailed_role_id)
    if jailed_role:
        try:
            await member.edit(
                roles=[jailed_role], reason="Rejoin while jailed — re-applying jail"
            )
        except discord.Forbidden:
            log.warning("Could not re-jail %s on rejoin", member)

    jail_channel = member.guild.get_channel(jail["channel_id"])
    if isinstance(jail_channel, discord.TextChannel):
        await jail_channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )
        await jail_channel.send(
            f"⚠️ {member.mention} left and rejoined. Jail has been re-applied."
        )
    return True


# ═══════════════════════════════════════════════════════════════════════════
# AUTO-EXPIRY BACKGROUND TASK
# ═══════════════════════════════════════════════════════════════════════════


async def jail_expiry_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Background task that checks for expired jails every 60 seconds."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(ctx.guild_id)
            if guild:
                with ctx.open_db() as conn:
                    expired = get_expired_jails(conn, guild.id)
                for jail in expired:
                    member = guild.get_member(jail["user_id"])
                    if member:
                        await _do_unjail(
                            ctx, guild, member, reason="Jail duration expired"
                        )
                    else:
                        # User left — just release the record
                        with ctx.open_db() as conn:
                            release_jail(
                                conn,
                                jail["id"],
                                reason="Jail duration expired (user left)",
                            )
        except Exception:
            log.exception("Error in jail expiry loop")
        await asyncio.sleep(60)


# ═══════════════════════════════════════════════════════════════════════════
# COMMAND REGISTRATION
# ═══════════════════════════════════════════════════════════════════════════


def register_jail_commands(bot: Bot, ctx: AppContext) -> None:
    # Stash ctx on bot so persistent views can access it
    bot._mod_ctx = ctx  # type: ignore[attr-defined]

    # Register persistent dynamic items
    bot.add_dynamic_items(TicketPanelButton)
    bot.add_dynamic_items(TicketCloseButton)
    bot.add_dynamic_items(TicketReopenButton)
    bot.add_dynamic_items(TicketDeleteButton)
    bot.add_dynamic_items(PolicyVoteYesButton)
    bot.add_dynamic_items(PolicyVoteNoButton)
    bot.add_dynamic_items(PolicyVoteAbstainButton)

    # Start expiry loop
    bot.startup_task_factories.append(lambda: jail_expiry_loop(bot, ctx))

    # ── /setup ────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="setup",
        description="First-time setup — creates jail role, channels, and mod config.",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup_cmd(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:  # type: ignore[union-attr]
            await interaction.response.send_message("Admin only.", ephemeral=True)
            return
        embed, view = _setup_view(ctx, 1)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ── /jail ─────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="jail", description="Place a member in a private jail channel."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="Member to jail",
        duration="How long (e.g. 24h, 7d). Leave blank for indefinite.",
        reason="Reason for jailing",
    )
    async def jail_cmd(
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str | None = None,
        reason: str | None = None,
    ):
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

    @bot.tree.command(
        name="unjail", description="Release a jailed member and restore their roles."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to release", reason="Release reason")
    async def unjail_cmd(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ):
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

    ticket_group = app_commands.Group(
        name="ticket", description="Ticket management commands."
    )

    @ticket_group.command(
        name="panel", description="Post the ticket-creation button in a channel."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(channel="Channel to post the panel in")
    async def ticket_panel(
        interaction: discord.Interaction, channel: discord.TextChannel
    ):
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

    @ticket_group.command(
        name="open", description="Open a private support ticket with the mod team."
    )
    @app_commands.describe(description="Brief description of your issue")
    async def ticket_open(
        interaction: discord.Interaction, description: str | None = None
    ):
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

    @ticket_group.command(name="close", description="Close the current ticket.")
    @app_commands.describe(reason="Reason for closing")
    async def ticket_close_cmd(
        interaction: discord.Interaction, reason: str | None = None
    ):
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
                    creator,
                    view_channel=True,
                    send_messages=False,
                    read_message_history=True,
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

            # Find the original ticket embed message and update its buttons
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

    @ticket_group.command(name="reopen", description="Reopen a closed ticket.")
    async def ticket_reopen_cmd(interaction: discord.Interaction):
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

    @ticket_group.command(
        name="delete",
        description="Permanently delete a closed ticket. A transcript is saved first.",
    )
    async def ticket_delete_cmd(interaction: discord.Interaction):
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

    @ticket_group.command(
        name="claim",
        description="Mark yourself as handling this ticket. You'll get DM pings on new activity.",
    )
    async def ticket_claim_cmd(interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        with ctx.open_db() as conn:
            ticket = get_ticket_by_channel(conn, interaction.channel_id or 0)
        if not ticket:
            await interaction.response.send_message(
                "Not a ticket channel.", ephemeral=True
            )
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

    @ticket_group.command(
        name="escalate", description="Bring admin roles into this ticket and ping them."
    )
    @app_commands.describe(reason="Reason for escalation")
    async def ticket_escalate_cmd(
        interaction: discord.Interaction, reason: str | None = None
    ):
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
            await interaction.response.send_message(
                "Not a ticket channel.", ephemeral=True
            )
            return
        if ticket["escalated"]:
            await interaction.response.send_message(
                "This ticket is already escalated.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return

        # Grant admin roles visibility
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

    bot.tree.add_command(ticket_group)

    # ── /policy ──────────────────────────────────────────────────────────

    policy_group = app_commands.Group(
        name="policy", description="Policy proposal and voting commands."
    )

    @policy_group.command(
        name="open", description="Open a new policy proposal for discussion."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        title="Short title for the policy",
        description="Detailed description of the proposed policy",
    )
    async def policy_open(
        interaction: discord.Interaction, title: str, description: str | None = None
    ):
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

        # Ping all mod and admin roles
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

    class _PolicyVoteModal(discord.ui.Modal, title="Start Policy Vote"):
        vote_text: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
            label="Exact policy text to vote on",
            style=discord.TextStyle.paragraph,
            placeholder="Type the exact wording of the policy being voted on...",
            required=True,
            max_length=2000,
        )

        def __init__(self, policy_id: int) -> None:
            super().__init__()
            self.policy_id = policy_id

        async def on_submit(self, interaction: discord.Interaction) -> None:
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

            # Build eligible voter list
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
                # Ping roles
                role_mentions = []
                for rid in all_role_ids:
                    role = guild.get_role(rid)
                    if role:
                        role_mentions.append(role.mention)
                if role_mentions:
                    await channel.send(f"🗳️ Vote now! {' '.join(role_mentions)}")

    @policy_group.command(
        name="vote", description="Start the formal vote on this policy proposal."
    )
    @app_commands.default_permissions(moderate_members=True)
    async def policy_vote_cmd(interaction: discord.Interaction):
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

        await interaction.response.send_modal(_PolicyVoteModal(policy["id"]))

    @policy_group.command(
        name="close", description="Close a policy proposal without voting (admin only)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(reason="Reason for closing without a vote")
    async def policy_close_cmd(
        interaction: discord.Interaction, reason: str | None = None
    ):
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

            # List any adopted policies from this proposal
            with ctx.open_db() as conn:
                adopted_policies = get_policies_by_ticket_id(conn, policy_id)
            if adopted_policies:
                adopted_embed = discord.Embed(
                    title="Adopted Policies from This Proposal",
                    color=CLR_SUCCESS,
                )
                for p in adopted_policies:
                    adopted_embed.add_field(
                        name=p["title"],
                        value=p["description"][:1024],
                        inline=False,
                    )
                await channel.send(embed=adopted_embed)

            # Generate transcript before deleting
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

    @policy_group.command(name="list", description="List all passed policies.")
    @app_commands.default_permissions(moderate_members=True)
    async def policy_list_cmd(interaction: discord.Interaction):
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

        embed = discord.Embed(
            title="📋 Passed Policies",
            color=CLR_POLICY,
        )
        for p in policies_list[:25]:  # Discord embed field limit
            passed_ts = f"<t:{int(p['passed_at'])}:d>"
            embed.add_field(
                name=f"#{p['id']} — {p['title']}",
                value=f"{p['description'][:100]}{'…' if len(p['description']) > 100 else ''}\nPassed: {passed_ts}",
                inline=False,
            )
        if len(policies_list) > 25:
            embed.set_footer(text=f"Showing 25 of {len(policies_list)} policies.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    bot.tree.add_command(policy_group)

    # ── /pull ─────────────────────────────────────────────────────────────

    @bot.tree.command(
        name="pull", description="Bring someone into this jail or ticket channel."
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="User to add to the channel")
    async def pull_cmd(interaction: discord.Interaction, user: discord.Member):
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

        # Check it's a jail or ticket channel
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

    @bot.tree.command(
        name="remove",
        description="Remove someone you pulled into this jail or ticket channel.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="User to remove from the channel")
    async def remove_cmd(interaction: discord.Interaction, user: discord.Member):
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

        # Can't remove the primary user
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

    @bot.tree.command(
        name="warn",
        description="Issue a formal warning. The action is logged (user is not notified).",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to warn", reason="Reason for warning")
    async def warn_cmd(
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ):
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

        # Audit
        audit_embed = discord.Embed(
            title="⚠️ Warning Issued",
            description=f"{user.mention} warned by {member.mention}\n"
            + (f"**Reason:** {reason_text}\n" if reason_text else "")
            + f"**Active warnings:** {count}",
            color=CLR_WARNING,
        )
        await _post_audit(ctx, guild, audit_embed)

        # Threshold alert
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

    @bot.tree.command(
        name="warnings",
        description="List all warnings (active and revoked) for a member.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to check")
    async def warnings_cmd(interaction: discord.Interaction, user: discord.Member):
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

    # ── /revokewarn ───────────────────────────────────────────────────────

    @bot.tree.command(
        name="revokewarn",
        description="Cancel a warning by ID. It stays in history but no longer counts.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(
        user="Member", warning_id="Warning ID to revoke", reason="Reason for revoking"
    )
    async def revokewarn_cmd(
        interaction: discord.Interaction,
        user: discord.Member,
        warning_id: int,
        reason: str | None = None,
    ):
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
            ok = revoke_warning(
                conn, warning_id, revoked_by=member.id, reason=reason or ""
            )
            if not ok:
                await interaction.response.send_message(
                    f"Warning #{warning_id} not found or already revoked.",
                    ephemeral=True,
                )
                return
            remaining = get_active_warning_count(conn, guild.id, user.id)
            write_audit(
                conn,
                guild_id=guild.id,
                action="warning_revoke",
                actor_id=member.id,
                target_id=user.id,
                extra={
                    "warning_id": warning_id,
                    "reason": reason or "",
                    "remaining": remaining,
                },
            )

        await interaction.response.send_message(
            f"✅ Warning #{warning_id} revoked. {user.mention} now has **{remaining}** active warning(s).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ── /modinfo ──────────────────────────────────────────────────────────

    @bot.tree.command(
        name="modinfo",
        description="Full mod profile — jail history, warnings, and tickets for a member.",
    )
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to inspect")
    async def modinfo_cmd(interaction: discord.Interaction, user: discord.Member):
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

        # Jail status
        if active_jail:
            jail_text = (
                f"**Currently jailed** since {_ts_str(active_jail['created_at'])}"
            )
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

        # Warnings
        active_warns = [w for w in warns if not w["revoked"]]
        warn_text = f"**Active:** {len(active_warns)} / **Total:** {len(warns)}"
        for w in active_warns[:3]:
            warn_text += f"\n  #{w['id']} — {_ts_str(w['created_at'])} — {w['reason'] or 'no reason'}"
        embed.add_field(name="⚠️ Warnings", value=warn_text, inline=False)

        # Tickets
        open_t = sum(1 for t in tickets if t["status"] == "open")
        closed_t = sum(1 for t in tickets if t["status"] in ("closed", "deleted"))
        ticket_text = f"**Open:** {open_t} / **Closed:** {closed_t}"
        if tickets:
            recent_ticket = tickets[0]
            ticket_text += f"\n  Most recent: #{recent_ticket['id']} — {recent_ticket['status']} — {_ts_str(recent_ticket['created_at'])}"
        embed.add_field(name="📩 Tickets", value=ticket_text, inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Context menus ─────────────────────────────────────────────────────

    @bot.tree.context_menu(name="Jail User")
    @app_commands.default_permissions(moderate_members=True)
    async def jail_context_menu(interaction: discord.Interaction, user: discord.Member):
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("Mod only.", ephemeral=True)
            return
        await interaction.response.send_modal(_JailModal(user, ctx))

    @bot.tree.context_menu(name="Open Ticket About This Message")
    async def ticket_message_context(
        interaction: discord.Interaction, message: discord.Message
    ):
        # Pre-fill the modal with a link to the source message
        modal = _TicketFromMessageModal(message)
        await interaction.response.send_modal(modal)

    # Rejoin detection is handled by events.py calling check_jail_rejoin()


class _TicketFromMessageModal(discord.ui.Modal, title="Open Ticket About This Message"):
    description: discord.ui.TextInput = discord.ui.TextInput(  # type: ignore[assignment]
        label="Additional context",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, source_message: discord.Message):
        super().__init__()
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = bot._mod_ctx  # type: ignore[attr-defined]
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        cat_id = _get_config(ctx, "ticket_category_id")
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category not configured.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        desc_text = self.description.value or "(no description)"
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
                source_message_url=self.source_message.jump_url,
            )
            write_audit(
                conn,
                guild_id=guild.id,
                action="ticket_open",
                actor_id=user.id,
                extra={
                    "ticket_id": ticket_id,
                    "description": desc_text,
                    "source": self.source_message.jump_url,
                },
            )

        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=desc_text,
            color=CLR_TICKET,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Opened by", value=user.mention, inline=True)
        embed.add_field(name="Status", value="🟢 Open", inline=True)
        embed.add_field(
            name="Source message",
            value=f"[Jump to message]({self.source_message.jump_url})",
            inline=False,
        )

        view = discord.ui.View(timeout=None)
        view.add_item(TicketCloseButton(ticket_id))
        await channel.send(embed=embed, view=view)
        await interaction.followup.send(
            f"Ticket created → {channel.mention}", ephemeral=True
        )
