"""Jail & Ticket moderation commands.

Implements /jail, /unjail, /ticket, /pull, /remove, /warn, /warnings,
/revokewarn, /modinfo, and context menu commands per the spec.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.services.dm_branding import (
    brand_dm_embed,
    guild_icon_url,
    resolve_dm_accent,
)

from bot_modules.core.branding import safe_resolve_accent
from bot_modules.core.db_utils import get_config_value
from bot_modules.services.embeds import (
    MOD_JAIL as CLR_JAIL,  # noqa: F401  re-exported for jail_cog
    MOD_POLICY as CLR_POLICY,  # noqa: F401  re-exported for jail_cog
    MOD_SUCCESS as CLR_SUCCESS,
)
from bot_modules.services.moderation import (
    add_policy,
    cast_policy_vote,
    close_ticket,
    compute_roles_to_restore,
    create_ticket,
    delete_ticket,
    find_expired_policy_votes,
    fmt_duration,
    generate_transcript,
    render_transcript_markdown,
    get_active_jail,
    get_expired_jails,
    get_policies_by_ticket_id,
    get_policy_ticket,
    get_policy_votes,
    get_ticket_by_channel,
    get_tickets_to_autodelete,
    parse_duration,
    release_jail,
    reopen_ticket,
    resolve_policy_vote,
    store_transcript,
    ticket_notify_on_create_enabled,
    write_audit,
    PolicyTicketRow,
    TicketRow,
)
from bot_modules.jail.apply import create_jail_channel
from bot_modules.jail.embeds import (
    build_policy_vote_update_embed,
)
from bot_modules.jail.logic import (
    channel_needs_jail_deny,
    channels_needing_jail_deny,
    vote_outcome as _vote_outcome,
)

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.jail_commands")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_mod_role_ids(ctx: AppContext, guild_id: int) -> set[int]:
    return set(ctx.guild_config(guild_id).mod_role_ids)


def _get_admin_role_ids(ctx: AppContext, guild_id: int) -> set[int]:
    return set(ctx.guild_config(guild_id).admin_role_ids)


def _is_mod(member: discord.Member, ctx: AppContext) -> bool:
    """Check if member has mod access via configured roles or manage_guild."""
    if member.guild_permissions.manage_guild or member.guild_permissions.administrator:
        return True
    return ctx.guild_config(member.guild.id).member_is_mod(member)


def _is_admin(member: discord.Member, ctx: AppContext) -> bool:
    """Check if member has admin access via the Discord ADMINISTRATOR bit or a configured admin role."""
    if member.guild_permissions.administrator:
        return True
    return ctx.guild_config(member.guild.id).member_is_admin(member)


def _get_config(ctx: AppContext, key: str, default: str = "0", guild_id: int = 0) -> int:
    with ctx.open_db() as conn:
        return int(get_config_value(conn, key, default, guild_id) or 0)


def _add_ticket_panel(ctx: AppContext, guild_id: int, channel_id: int, message_id: int) -> None:
    import time as _time
    with ctx.open_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ticket_panels (guild_id, channel_id, message_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, message_id, _time.time()),
        )


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
    db_path: Path | None = None,
    guild: discord.Guild | None = None,
) -> bool:
    """Send a DM; return True if successful.  Post note to fallback_channel on failure.

    Branded with ``guild``'s attribution when the caller supplies it, but
    with ``keep_color=True``: these embeds already choose their own color
    deliberately — the resolved accent at most sites, and a semantic green
    on the release notice — so branding adds the server, not a repaint.
    """
    if embed is not None and guild is not None:
        brand_dm_embed(
            embed,
            guild_name=guild.name,
            guild_icon_url=guild_icon_url(guild),
            color=await resolve_dm_accent(db_path, guild) if db_path else None,
            keep_color=True,
        )
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
    log_ch_id = _get_config(ctx, "log_channel_id", guild_id=guild.id)
    if not log_ch_id:
        return
    ch = guild.get_channel(log_ch_id)
    if ch and isinstance(ch, discord.TextChannel):
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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
    def _store():
        with ctx.open_db() as conn:
            store_transcript(
                conn,
                guild_id=channel.guild.id,
                record_type=record_type,
                record_id=record_id,
                content=transcript,
            )

    await asyncio.to_thread(_store)

    # Build Markdown file
    md_bytes = render_transcript_markdown(transcript).encode("utf-8")
    filename = f"{record_type}-{record_id}-transcript.md"

    # Post to transcript channel
    transcript_ch_id = _get_config(ctx, "transcript_channel_id", guild_id=channel.guild.id)
    if not transcript_ch_id:
        transcript_ch_id = _get_config(ctx, "log_channel_id", guild_id=channel.guild.id)
    if transcript_ch_id:
        ch = channel.guild.get_channel(transcript_ch_id)
        if ch and isinstance(ch, discord.TextChannel):
            accent = await safe_resolve_accent(ctx, channel.guild, log_label="jail")
            embed = discord.Embed(
                title=f"Transcript — {record_type.title()} #{record_id}",
                description=f"**Channel:** #{channel.name}\n**Messages:** {transcript['message_count']}",
                color=accent,
            )
            await ch.send(
                embed=embed, file=discord.File(io.BytesIO(md_bytes), filename)
            )

    # DM to user
    await _dm_user(user, file=discord.File(io.BytesIO(md_bytes), filename))


# ---------------------------------------------------------------------------
# Ticket status embed helper
# ---------------------------------------------------------------------------

# The ticket embed carries a "Status" field that must track the ticket's
# lifecycle. Every open-embed builder seeds it with ``TICKET_STATUS_OPEN``;
# close/reopen/escalate rewrite it. Both the button flows here and the slash
# ``/ticket`` flows in ``jail_cog`` share these so the wording stays in sync.
TICKET_STATUS_OPEN = "🟢 Open"
TICKET_STATUS_CLOSED = "🔒 Closed"
TICKET_STATUS_ESCALATED = "⚠️ Escalated"


def _apply_ticket_status(embed: discord.Embed, status_value: str) -> discord.Embed:
    """Rewrite the ticket embed's ``Status`` field in place, returning it.

    Matches on the field *name* rather than a fixed index so it survives extra
    fields (e.g. the "Source message" field on message-context tickets); adds
    the field if a legacy embed somehow lacks it.
    """
    for i, field in enumerate(embed.fields):
        if field.name == "Status":
            embed.set_field_at(
                i, name="Status", value=status_value, inline=field.inline
            )
            return embed
    embed.add_field(name="Status", value=status_value, inline=True)
    return embed


async def _finalize_ticket_delete(
    ctx: AppContext,
    channel: discord.TextChannel,
    ticket: TicketRow,
    *,
    actor_id: int,
    auto: bool = False,
) -> None:
    """Archive and permanently delete a ticket channel.

    Shared by the Delete button, ``/ticket delete`` and the 24 h auto-delete
    sweep. The transcript is generated and posted *first*: if that raises, the
    exception propagates before the row is marked deleted or the channel is
    removed, so we never destroy a conversation we failed to archive. ``auto``
    only changes the audit wording and the channel-delete audit reason.
    """
    guild = channel.guild
    ticket_id = ticket["id"]
    user_id = ticket["user_id"]
    # Fall back to the bot itself so the transcript DM step always has a target
    # (the send simply fails-soft if the creator has left or DMs are closed).
    transcript_user = guild.get_member(user_id) or guild.me

    await _collect_and_post_transcript(
        ctx,
        channel,
        record_type="ticket",
        record_id=ticket_id,
        user=transcript_user,
        extra_meta={
            "closed_by": ticket.get("closed_by"),
            "close_reason": ticket.get("close_reason", ""),
            "auto_deleted": auto,
        },
    )

    guild_id = guild.id

    def _mark_deleted() -> None:
        with ctx.open_db() as conn:
            delete_ticket(conn, ticket_id)
            write_audit(
                conn,
                guild_id=guild_id,
                action="ticket_delete",
                actor_id=actor_id,
                target_id=user_id,
                extra={"ticket_id": ticket_id, "auto": auto},
            )

    await asyncio.to_thread(_mark_deleted)

    accent = await safe_resolve_accent(ctx, guild, log_label="jail")
    if auto:
        desc = f"**Ticket #{ticket_id}** by <@{user_id}> auto-deleted 24h after close."
        reason = f"Ticket #{ticket_id} auto-deleted 24h after close"
    else:
        desc = f"**Ticket #{ticket_id}** by <@{user_id}> deleted by <@{actor_id}>."
        reason = f"Ticket #{ticket_id} deleted"
    await _post_audit(
        ctx,
        guild,
        discord.Embed(title="🗑️ Ticket Deleted", description=desc, color=accent),
    )
    await channel.delete(reason=reason)


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
        tid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Capture the ticket embed message here (a component interaction, where
        # ``interaction.message`` is reliably set) and hand it to the modal — a
        # modal's own ``on_submit`` interaction has ``message = None``, so the
        # close handler couldn't otherwise find the embed to update.
        await interaction.response.send_modal(
            _TicketCloseModal(self.ticket_id, interaction.message)
        )


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
        tid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get ctx from bot
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "❌ Only moderators can reopen tickets.", ephemeral=True
            )
            return

        ticket_id = self.ticket_id
        guild_id = interaction.guild_id or 0
        member_id = member.id

        def _reopen():
            with ctx.open_db() as conn:
                reopen_ticket(conn, ticket_id)
                write_audit(
                    conn,
                    guild_id=guild_id,
                    action="ticket_reopen",
                    actor_id=member_id,
                    extra={"ticket_id": ticket_id},
                )

        await asyncio.to_thread(_reopen)

        # Restore send permission for creator
        channel = interaction.channel
        if isinstance(channel, discord.TextChannel):
            accent = await safe_resolve_accent(ctx, channel.guild, log_label="jail")
            reopen_ch_id = channel.id

            def _fetch_reopened_ticket():
                with ctx.open_db() as conn:
                    return get_ticket_by_channel(conn, reopen_ch_id)

            ticket = await asyncio.to_thread(_fetch_reopened_ticket)
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
                    db_path=ctx.db_path,
                    guild=interaction.guild,
                    embed=discord.Embed(
                        description=f"Your ticket in **{interaction.guild.name}** has been reopened.",  # type: ignore[union-attr]
                        color=accent,
                    ),
                )

            # Swap to close button and restore the embed's Status field. An
            # escalated ticket keeps its ⚠️ marker (the flag survives reopen);
            # otherwise it goes back to 🟢 Open.
            view = discord.ui.View(timeout=None)
            view.add_item(TicketCloseButton(self.ticket_id))
            reopen_status = (
                TICKET_STATUS_ESCALATED
                if ticket and ticket.get("escalated")
                else TICKET_STATUS_OPEN
            )
            msg = interaction.message
            if msg is not None and msg.embeds:
                await interaction.response.edit_message(
                    embed=_apply_ticket_status(msg.embeds[0], reopen_status),
                    view=view,
                )
            else:
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
        tid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "❌ Only moderators can delete tickets.", ephemeral=True
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
        del_ch_id = channel.id

        def _fetch_del_ticket():
            with ctx.open_db() as conn:
                return get_ticket_by_channel(conn, del_ch_id)

        ticket = await asyncio.to_thread(_fetch_del_ticket)

        if not ticket:
            return

        await _finalize_ticket_delete(ctx, channel, ticket, actor_id=member.id)


# ---------------------------------------------------------------------------
# Policy vote persistent buttons
# ---------------------------------------------------------------------------

# CLR_POLICY now imported above from services.embeds


async def finalize_policy_vote(
    ctx: AppContext,
    guild: discord.Guild,
    policy_id: int,
    outcome: str,
    *,
    channel: discord.TextChannel | None,
    yes_ids: list[int],
    no_ids: list[int],
    abstain_ids: list[int],
    actor_id: int,
    timed_out: bool,
) -> bool:
    """Commit a policy vote resolution: DB, audit, channel announcement, transcript, delete.

    Guarded against double-finalization: returns False if the policy row's
    status has already moved out of 'voting' (a concurrent finalizer won).
    Returns True after the row is resolved and side-effects have been issued.

    ``outcome`` must be one of: 'adopted', 'rejected', 'rejected_no_quorum'.
    'rejected_no_quorum' only makes sense when ``timed_out=True``.
    """
    db_status = "passed" if outcome == "adopted" else "failed"
    guild_id = guild.id

    def _db_commit():
        with ctx.open_db() as conn:
            won = resolve_policy_vote(conn, policy_id, status=db_status)
            if not won:
                return None
            pol = get_policy_ticket(conn, policy_id)
            if pol is None:
                return None
            pol_row_id: int | None = None
            pol_adopted_text = pol["vote_text"] or pol["description"]
            if outcome == "adopted":
                pol_row_id = add_policy(
                    conn,
                    guild_id=guild_id,
                    policy_ticket_id=policy_id,
                    title=pol["title"],
                    description=pol_adopted_text,
                )
            pol_audit_extra: dict = {
                "policy_id": policy_id,
                "yes": len(yes_ids),
                "no": len(no_ids),
                "abstain": len(abstain_ids),
                "timed_out": timed_out,
            }
            if outcome == "rejected_no_quorum":
                pol_audit_extra["no_quorum"] = True
            if outcome == "adopted":
                pol_audit_extra["policy_row_id"] = pol_row_id
                pol_audit_extra["vote_text"] = pol_adopted_text
                pol_audit_action = "policy_passed"
            else:
                pol_audit_action = "policy_vote_failed"
            write_audit(
                conn,
                guild_id=guild_id,
                action=pol_audit_action,
                actor_id=actor_id,
                extra=pol_audit_extra,
            )
        return {"policy": pol, "adopted_text": pol_adopted_text}

    commit = await asyncio.to_thread(_db_commit)
    if commit is None:
        return False
    policy = commit["policy"]
    adopted_text = commit["adopted_text"]

    vote_text = policy["vote_text"] or policy["title"]

    if channel is not None:
        creator = guild.get_member(policy["creator_id"])
        if outcome == "adopted":
            adopted_suffix = (
                f"({len(yes_ids)} yes, {len(abstain_ids)} abstain"
                + (", absentees ignored after timeout)" if timed_out else ")")
            )
            await channel.send(
                f'✅ **Policy adopted!** "{policy["title"]}" is now in effect.\n'
                f"**Adopted policy:** {adopted_text}\n"
                f"{adopted_suffix}"
            )
            def _get_adopted():
                with ctx.open_db() as conn:
                    return get_policies_by_ticket_id(conn, policy_id)

            adopted_policies = await asyncio.to_thread(_get_adopted)
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
            extra_meta = {
                "resolution": "passed",
                "policy_title": policy["title"],
                "adopted_text": adopted_text,
                "vote_yes": len(yes_ids),
                "vote_no": 0,
                "vote_abstain": len(abstain_ids),
                "timed_out": timed_out,
            }
            delete_reason = f"Policy #{policy_id} adopted"
        elif outcome == "rejected_no_quorum":
            await channel.send(
                "❌ **Policy timed out.** Nobody voted within the timeout window.\n"
                f"**Rejected policy:** {vote_text}"
            )
            extra_meta = {
                "resolution": "failed",
                "policy_title": policy["title"],
                "vote_yes": 0,
                "vote_no": 0,
                "vote_abstain": 0,
                "timed_out": True,
                "no_quorum": True,
            }
            delete_reason = f"Policy #{policy_id} timed out (no quorum)"
        else:
            reject_reason = (
                "did not achieve unanimous support before the timeout"
                if timed_out
                else "did not achieve unanimous support"
            )
            await channel.send(
                f"❌ **Policy rejected.** The proposal {reject_reason}.\n"
                f"**Rejected policy:** {vote_text}"
            )
            extra_meta = {
                "resolution": "failed",
                "policy_title": policy["title"],
                "vote_yes": len(yes_ids),
                "vote_no": len(no_ids),
                "vote_abstain": len(abstain_ids),
                "timed_out": timed_out,
            }
            delete_reason = f"Policy #{policy_id} rejected"

        transcript_user = creator or guild.me
        await _collect_and_post_transcript(
            ctx,
            channel,
            record_type="policy_ticket",
            record_id=policy_id,
            user=transcript_user,
            extra_meta=extra_meta,
        )
        await channel.delete(reason=delete_reason)

    if outcome == "adopted":
        audit_embed = discord.Embed(
            title="✅ Policy Adopted",
            description=(
                f"**{policy['title']}**\n📜 {adopted_text}\n\n"
                f"Vote: {len(yes_ids)} yes, {len(abstain_ids)} abstain"
                + (" (timed out)" if timed_out else "")
            ),
            color=CLR_SUCCESS,
        )
    elif outcome == "rejected_no_quorum":
        audit_embed = discord.Embed(
            title="❌ Policy Timed Out",
            description=f"**{policy['title']}**\n📜 {vote_text}\n\nNo votes were cast.",
            color=discord.Color.from_str("#E74C3C"),
        )
    else:
        audit_embed = discord.Embed(
            title="❌ Policy Rejected",
            description=(
                f"**{policy['title']}**\n📜 {vote_text}\n\n"
                f"Vote: {len(yes_ids)} yes, {len(no_ids)} no, {len(abstain_ids)} abstain"
                + (" (timed out)" if timed_out else "")
            ),
            color=discord.Color.from_str("#E74C3C"),
        )
    await _post_audit(ctx, guild, audit_embed)
    return True


async def _handle_policy_vote(
    interaction: discord.Interaction, policy_id: int, vote: str
) -> None:
    """Shared handler for all three policy vote buttons."""
    bot = interaction.client
    ctx: AppContext = cast("Bot", bot).ctx
    member = interaction.user
    guild = interaction.guild
    if not isinstance(member, discord.Member) or not guild:
        await interaction.response.send_message("❌ Server-only.", ephemeral=True)
        return
    if not (_is_mod(member, ctx) or _is_admin(member, ctx)):
        await interaction.response.send_message(
            "❌ Only mods and admins can vote.", ephemeral=True
        )
        return

    def _get_policy():
        with ctx.open_db() as conn:
            return get_policy_ticket(conn, policy_id)

    policy = await asyncio.to_thread(_get_policy)
    if not policy or policy["status"] != "voting":
        await interaction.response.send_message(
            "❌ This vote is no longer active.", ephemeral=True
        )
        return

    # Cast or update vote
    member_id = member.id

    def _cast_vote():
        with ctx.open_db() as conn:
            cast_policy_vote(conn, policy_id=policy_id, user_id=member_id, vote=vote)
            return get_policy_votes(conn, policy_id)

    votes = await asyncio.to_thread(_cast_vote)

    # Build eligible voter set
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

    vote_map = {v["user_id"]: v["vote"] for v in votes}
    voted_ids = set(vote_map.keys()) & eligible
    yes_ids = [uid for uid in voted_ids if vote_map[uid] == "yes"]
    no_ids = [uid for uid in voted_ids if vote_map[uid] == "no"]
    abstain_ids = [uid for uid in voted_ids if vote_map[uid] == "abstain"]
    awaiting_ids = list(eligible - voted_ids)

    # A 'no' alone does not finalize — we wait until every eligible mod has
    # voted (or the timeout sweeper takes over). This preserves the existing
    # "unanimous required, no early-reject" rule.
    outcome: str | None = None
    if not awaiting_ids:
        outcome = "rejected" if no_ids else "adopted"

    embed = build_policy_vote_update_embed(
        policy_title=policy["title"],
        vote_text=policy["vote_text"] or policy["description"] or "",
        yes_ids=yes_ids,
        no_ids=no_ids,
        abstain_ids=abstain_ids,
        awaiting_ids=awaiting_ids,
        outcome=outcome,
    )

    if outcome is not None:
        view = discord.ui.View(timeout=None)  # No more buttons
        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(
            f"Your vote ({vote}) has been recorded.", ephemeral=True
        )
        channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        await finalize_policy_vote(
            ctx,
            guild,
            policy_id,
            outcome,
            channel=channel,
            yes_ids=yes_ids,
            no_ids=no_ids,
            abstain_ids=abstain_ids,
            actor_id=member.id,
            timed_out=False,
        )
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
        pid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
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
        pid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
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
        pid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
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
        ctx: AppContext = cast("Bot", bot).ctx
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message(
                "❌ This only works in a server.", ephemeral=True
            )
            return

        cat_id = _get_config(ctx, "ticket_category_id", guild_id=guild.id)
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "❌ Ticket category is not configured. Ask an admin to set one on the dashboard (Config → Moderation).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await safe_resolve_accent(ctx, guild, log_label="jail")

        # Create channel
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
        name = f"ticket-{user.name[:16]}-{ts}"
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

        desc_text = self.description.value or "(no description)"
        guild_id = guild.id

        def _create_ticket():
            with ctx.open_db() as conn:
                tid = create_ticket(
                    conn,
                    guild_id=guild_id,
                    user_id=user.id,
                    channel_id=channel.id,
                    description=desc_text,
                )
                write_audit(
                    conn,
                    guild_id=guild_id,
                    action="ticket_open",
                    actor_id=user.id,
                    extra={"ticket_id": tid, "description": desc_text},
                )
            return tid

        ticket_id = await asyncio.to_thread(_create_ticket)

        # Post ticket embed
        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=desc_text,
            color=accent,
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
                    db_path=ctx.db_path,
                    guild=guild,
            embed=discord.Embed(
                description=f"Your ticket has been created in **{guild.name}** → [Go to ticket]({channel.jump_url})",
                color=accent,
            ),
        )

        # Notify mods
        def _get_notify():
            with ctx.open_db() as conn:
                return ticket_notify_on_create_enabled(conn, guild.id)

        notify = await asyncio.to_thread(_get_notify)
        if notify:
            for rid in mod_role_ids:
                role = guild.get_role(rid)
                if not role:
                    continue
                for m in role.members:
                    if m.bot or m.id == user.id:
                        continue
                    await _dm_user(
                        m,
                    db_path=ctx.db_path,
                    guild=guild,
                        embed=discord.Embed(
                            title="📩 New Ticket",
                            description=f"**{user}** opened a ticket → [Jump to ticket]({channel.jump_url})\n\n{desc_text}",
                            color=accent,
                        ),
                    )

        # Audit
        audit_embed = discord.Embed(
            title="📩 Ticket Opened",
            description=f"**Ticket #{ticket_id}** by {user.mention} in {channel.mention}",
            color=accent,
        )
        await _post_audit(ctx, guild, audit_embed)


class _TicketCloseModal(discord.ui.Modal, title="Close Ticket"):
    reason: discord.ui.TextInput = discord.ui.TextInput(
        label="Reason (optional)", required=False, max_length=500
    )  # type: ignore[assignment]

    def __init__(self, ticket_id: int, ticket_message: discord.Message | None = None):
        super().__init__()
        self.ticket_id = ticket_id
        self.ticket_message = ticket_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
        member = interaction.user
        guild = interaction.guild
        if not isinstance(member, discord.Member) or guild is None:
            return
        if not _is_mod(member, ctx):
            await interaction.response.send_message(
                "❌ Only moderators can close tickets.", ephemeral=True
            )
            return

        reason = self.reason.value or ""
        accent = await safe_resolve_accent(ctx, guild, log_label="jail")
        close_guild_id = guild.id
        close_channel_id = interaction.channel_id or 0
        close_ticket_id = self.ticket_id
        close_member_id = member.id

        def _close():
            with ctx.open_db() as conn:
                t = get_ticket_by_channel(conn, close_channel_id)
                if not t or t["status"] != "open":
                    return None
                close_ticket(conn, close_ticket_id, closed_by=close_member_id, reason=reason)
                write_audit(
                    conn,
                    guild_id=close_guild_id,
                    action="ticket_close",
                    actor_id=close_member_id,
                    target_id=t["user_id"],
                    extra={"ticket_id": close_ticket_id, "reason": reason},
                )
                return t

        ticket = await asyncio.to_thread(_close)
        if ticket is None:
            await interaction.response.send_message(
                "❌ This ticket is not open.", ephemeral=True
            )
            return

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

            # Swap buttons to Reopen + Delete and flip the embed's Status
            # field to Closed in the same edit. ``edit_message`` on a modal
            # submit targets the message the Close button lived on; we read the
            # embed off the captured ``ticket_message`` because the modal's own
            # ``interaction.message`` is None.
            view = discord.ui.View(timeout=None)
            view.add_item(TicketReopenButton(self.ticket_id))
            view.add_item(TicketDeleteButton(self.ticket_id))
            tmsg = self.ticket_message
            if tmsg is not None and tmsg.embeds:
                await interaction.response.edit_message(
                    embed=_apply_ticket_status(tmsg.embeds[0], TICKET_STATUS_CLOSED),
                    view=view,
                )
            else:
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
                    db_path=ctx.db_path,
                    guild=guild,
                    embed=discord.Embed(
                        description=f"Your ticket in **{guild.name}** has been closed.\n{f'**Reason:** {reason}' if reason else ''}\nYou can still view the channel.",
                        color=accent,
                    ),
                    fallback_channel=channel,
                )

        audit_embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=f"**Ticket #{self.ticket_id}** closed by {member.mention}"
            + (f"\nReason: {reason}" if reason else ""),
            color=accent,
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
    """Slash-command entry to the canonical jail flow.

    Translates ``interaction`` context into a call to :func:`apply_jail` and
    surfaces the structured result as an ephemeral interaction response.
    Precondition rejections (bot/self/admin/mod/already-jailed) come back
    as initial responses so they appear immediately; everything else is a
    followup so the user sees the "thinking" indicator while role/channel
    creation runs.
    """
    from bot_modules.jail.apply import apply_jail, check_jail_preconditions

    guild = interaction.guild
    mod = interaction.user
    if guild is None or not isinstance(mod, discord.Member):
        await interaction.response.send_message("❌ Server-only command.", ephemeral=True)
        return

    # Cheap precondition checks → initial response (no defer required).
    precheck = check_jail_preconditions(ctx, guild, target, mod)
    if precheck is not None:
        await interaction.response.send_message(
            "❌ " + (precheck.error_message or "Cannot jail this user."), ephemeral=True
        )
        return

    duration_seconds = parse_duration(duration_str) if duration_str else None

    await interaction.response.defer(ephemeral=True)

    result = await apply_jail(
        ctx,
        guild,
        target,
        mod,
        reason=reason,
        duration_seconds=duration_seconds,
        source="command",
    )

    if not result.ok:
        await interaction.followup.send(
            "❌ " + (result.error_message or "Failed to jail user."), ephemeral=True
        )
        return

    channel_mention = f"<#{result.channel_id}>" if result.channel_id else "(channel)"
    await interaction.followup.send(
        f"✅ {target} has been jailed → {channel_mention}", ephemeral=True
    )


async def resolve_release_target(
    bot: discord.Client,
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | discord.User | None:
    """Resolve ``user_id`` to something :func:`_do_unjail` can release.

    Prefers the guild member, so a present user still gets full role
    restoration. Falls back to the global user object — cache first, then a
    REST fetch — which is what lets a departed member be released with a
    transcript, channel cleanup, and audit trail instead of a bare DB update.

    Returns ``None`` only when Discord positively reports no such user (a
    deleted account, or an id that never existed); callers close the row out
    directly in that case rather than leaving it active forever.

    Only ``NotFound`` is caught, deliberately. ``NotFound`` subclasses
    ``HTTPException``, so catching the parent would turn every 5xx and
    rate-limit failure into "this account doesn't exist" — and callers would
    then close a live user's hold with no transcript, no DM, and an orphaned
    jail channel. A transient failure must propagate: the expiry loop logs it
    and retries a minute later, and the dashboard route surfaces it to the mod.
    """
    member = guild.get_member(user_id)
    if member is not None:
        return member
    user = bot.get_user(user_id)
    if user is not None:
        return user
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        return None


async def _do_unjail(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member | discord.User,
    *,
    reason: str = "",
    actor: discord.Member | None = None,
) -> str:
    """Core unjail logic.  Returns a status message.

    ``target`` may be a :class:`discord.User` rather than a
    :class:`discord.Member` — a jailed member who leaves the server keeps their
    ``active`` row (``check_jail_rejoin`` re-applies the hold if they return),
    so releasing them has to work without a guild member to act on. Role
    restoration is the *only* member-specific step; the transcript, channel
    cleanup, DM, audit row, and audit embed all run either way, which is what
    makes this the single release path for the slash command, the dashboard
    route, and the expiry loop alike.

    Releasing an absent user is a real decision, not bookkeeping: their stored
    roles are dropped for good, since a later rejoin sees no active jail and
    restores nothing. The returned message says so explicitly.
    """
    present = isinstance(target, discord.Member)

    def _fetch_jail():
        with ctx.open_db() as conn:
            return get_active_jail(conn, guild.id, target.id)

    jail = await asyncio.to_thread(_fetch_jail)
    if not jail:
        return f"{target} is not currently jailed."

    stored = json.loads(jail["stored_roles"])
    missing: list[int] = []

    if present:
        # Restore roles — use remove/add rather than edit(roles=...) so that any
        # managed roles the member holds are left in place instead of causing 403.
        member = cast(discord.Member, target)
        available_role_ids = {r.id for r in guild.roles}
        restorable_ids, missing = compute_roles_to_restore(stored, available_role_ids)
        roles_to_add: list[discord.Role] = [
            r for r in (guild.get_role(rid) for rid in restorable_ids) if r is not None
        ]

        jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=guild.id)
        jailed_role = guild.get_role(jailed_role_id)

        try:
            if jailed_role:
                await member.remove_roles(jailed_role, reason=f"Unjailed: {reason}")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason=f"Unjailed: {reason}")
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
    jail_id_rel = jail["id"]
    audit_extra: dict = {"jail_id": jail_id_rel, "reason": reason}
    if not present:
        # Matches the note the dashboard route used to write on its own
        # absent-release path, so existing audit consumers keep working.
        audit_extra["note"] = "user_left_guild"

    def _release():
        with ctx.open_db() as conn:
            release_jail(conn, jail_id_rel, reason=reason)
            write_audit(
                conn,
                guild_id=guild.id,
                action="jail_release",
                actor_id=actor_id,
                target_id=target.id,
                extra=audit_extra,
            )

    await asyncio.to_thread(_release)

    # DM
    dm_embed = discord.Embed(
        title="You've been released",
        description=f"Your moderation hold in **{guild.name}** has been lifted.\n"
        + (f"**Reason:** {reason}" if reason else ""),
        color=CLR_SUCCESS,
    )
    await _dm_user(target, embed=dm_embed, db_path=ctx.db_path, guild=guild)

    # Audit
    audit_embed = discord.Embed(
        title="🔓 Member Released",
        description=f"{target.mention} released"
        + (f" by {actor.mention}" if actor else " (auto-expired)")
        + ("" if present else "\n*(had already left the server)*")
        + (f"\n**Reason:** {reason}" if reason else ""),
        color=CLR_SUCCESS,
    )
    await _post_audit(ctx, guild, audit_embed)

    if not present:
        note = ""
        if stored:
            note = (
                f"\n⚠️ They aren't in the server, so their {len(stored)} stored "
                "role(s) were not restored — rejoining now gives them a clean slate."
            )
        return f"✅ Jail #{jail_id_rel} closed for {target} (no longer in the server).{note}"

    note = ""
    if missing:
        note = f"\n⚠️ Could not restore {len(missing)} deleted role(s)."
    return f"✅ {target} has been released from jail.{note}"


# ═══════════════════════════════════════════════════════════════════════════
# REJOIN DETECTION (called from events.py on_member_join)
# ═══════════════════════════════════════════════════════════════════════════


async def check_jail_rejoin(
    ctx: AppContext,
    member: discord.Member,
    *,
    note: str | None = None,
) -> bool:
    """If the member has an active jail, re-apply it. Returns True if jailed.

    ``note`` overrides the line posted in the jail channel, so the startup
    reconcile can say *why* the hold was re-applied instead of claiming a
    rejoin it didn't observe.
    """

    def _fetch_rejoin_jail():
        with ctx.open_db() as conn:
            return get_active_jail(conn, member.guild.id, member.id)

    jail = await asyncio.to_thread(_fetch_rejoin_jail)
    if not jail:
        return False

    jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=member.guild.id)
    jailed_role = member.guild.get_role(jailed_role_id)
    if jailed_role:
        # remove/add rather than edit(roles=[...]), matching ``apply_jail``:
        # a member holding an integration-managed role (Nitro Booster, a bot
        # role) makes a wholesale role set 403, and Discord re-grants the
        # booster role automatically on rejoin — precisely the members most
        # likely to land here. Managed roles and @everyone are left alone.
        strip = [
            r for r in member.roles
            if not r.managed
            and r.id != member.guild.default_role.id
            and r.id != jailed_role.id
        ]
        try:
            if strip:
                await member.remove_roles(
                    *strip, reason="Rejoin while jailed — re-applying jail"
                )
            # Adding a role the member already holds is a no-op server-side,
            # so this doesn't need a cache check that may be stale after the
            # remove above.
            await member.add_roles(
                jailed_role, reason="Rejoin while jailed — re-applying jail"
            )
        except discord.HTTPException:
            # Report the failure instead of posting "jail re-applied" over it:
            # the member is walking around unjailed and a mod needs to know.
            log.warning("Could not re-jail %s on rejoin", member, exc_info=True)
            await _post_audit(
                ctx,
                member.guild,
                discord.Embed(
                    title="⚠️ Could not re-apply jail",
                    description=(
                        f"{member.mention} returned with an active hold "
                        f"(jail #{jail['id']}) but I couldn't re-apply the "
                        "Jailed role. They are **not** currently jailed — check "
                        "my role position and permissions."
                    ),
                    color=CLR_JAIL,
                ),
            )
            # Still report "jailed" to the join pipeline. The hold is active in
            # the database even though the role didn't stick, and handing this
            # member a welcome card plus auto-roles would compound the problem.
            return True

    jail_channel = member.guild.get_channel(jail["channel_id"])
    if not isinstance(jail_channel, discord.TextChannel):
        # No channel to return to: it was deleted while they were away, or was
        # never created because the original jail hit a missing Manage Channels
        # (``apply_jail`` stores ``channel_id`` 0 in that case). Rebuild it —
        # without one the member is re-jailed into a server the Jailed role
        # hides completely, with nowhere to appeal and no notice that anything
        # happened. The new id is written back to the row by the helper.
        jail_channel = await create_jail_channel(
            ctx,
            member.guild,
            member,
            jail_id=jail["id"],
            jailed_role=jailed_role,
        )
        if jail_channel is None:
            log.warning(
                "Re-jailed %s but could not recreate jail channel for jail #%s",
                member,
                jail["id"],
            )
            await _post_audit(
                ctx,
                member.guild,
                discord.Embed(
                    title="⚠️ Jail channel missing",
                    description=(
                        f"{member.mention} returned while jailed (jail #{jail['id']}), "
                        "but I couldn't recreate their jail channel — grant "
                        "**Manage Channels**. They're re-jailed with nowhere to appeal."
                    ),
                    color=CLR_JAIL,
                ),
            )
            return True

    await jail_channel.set_permissions(
        member,
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
    )
    await jail_channel.send(
        note or f"⚠️ {member.mention} left and rejoined. Jail has been re-applied."
    )
    return True


# ═══════════════════════════════════════════════════════════════════════════
# JAILED-ROLE CHANNEL VISIBILITY
# ═══════════════════════════════════════════════════════════════════════════
#
# The Jailed role's per-channel view-deny is only stamped when the role is
# first created. Channels created afterward have no deny, so a jailed member
# (who keeps @everyone) can see them. ``stamp_channel_jail_deny`` closes one
# channel; the cog calls it from ``on_guild_channel_create`` for new channels,
# and ``jail_channel_deny_sweep`` backfills any that already leaked at startup.


async def stamp_channel_jail_deny(
    channel: discord.abc.GuildChannel,
    role: discord.Role,
) -> bool:
    """Deny ``@Jailed`` view+send on *channel* unless it already denies view.

    Returns True if an overwrite was written. Uses the same
    ``view_channel=False, send_messages=False`` shape as the initial stamp in
    ``jail/apply.py`` so categories, text, and voice channels are all hidden.
    A missing-permission failure is logged and swallowed — best-effort, matching
    the original creation-time sweep.
    """
    if not channel_needs_jail_deny(channel.overwrites_for(role).view_channel):
        return False
    try:
        await channel.set_permissions(
            role,
            view_channel=False,
            send_messages=False,
            reason="Dungeon Keeper: hide channel from jailed members",
        )
        return True
    except discord.Forbidden:
        log.warning(
            "Missing permission to deny @Jailed on channel %s (%s)",
            channel.id,
            getattr(channel, "name", "?"),
        )
        return False
    except discord.HTTPException:
        log.exception("Failed to deny @Jailed on channel %s", channel.id)
        return False


async def jail_channel_deny_sweep(bot: discord.Client, ctx: AppContext) -> None:
    """One-shot startup backfill: stamp the Jailed deny on every exposed channel.

    Backstops the ``on_guild_channel_create`` listener for the case it can't
    cover — channels created while the bot was offline (including the very
    channels that leaked before this fix shipped). Runs once after the gateway
    is ready and exits; no-ops cleanly when no Jailed role is configured yet.
    """
    await bot.wait_until_ready()
    guild = bot.get_guild(ctx.guild_id)
    if guild is None:
        return
    jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=guild.id)
    role = guild.get_role(jailed_role_id) if jailed_role_id else None
    if role is None:
        return

    states = [
        (ch.id, ch.overwrites_for(role).view_channel) for ch in guild.channels
    ]
    exposed = channels_needing_jail_deny(states)
    if not exposed:
        return

    stamped = 0
    for cid in exposed:
        ch = guild.get_channel(cid)
        if ch is None:
            continue
        if await stamp_channel_jail_deny(ch, role):
            stamped += 1
    if stamped:
        log.info(
            "Jail channel sweep: stamped @Jailed view-deny on %d channel(s).",
            stamped,
        )


async def jail_rejoin_reconcile_sweep(bot: discord.Client, ctx: AppContext) -> None:
    """One-shot startup sweep: re-apply holds that lapsed while the bot was down.

    ``check_jail_rejoin`` only fires on ``on_member_join``. A member who left
    while jailed and rejoined during downtime never triggers it — Discord
    restores no roles on rejoin, so they come back with @everyone, full
    visibility, and an ``active`` jails row nobody reads. From the member's side
    that is indistinguishable from being released.

    A hold is only re-applied when the member demonstrably **rejoined after it
    was imposed** — ``member.joined_at`` later than the jail's ``created_at``.
    That is the whole population this sweep exists for, and nothing else is
    safe to act on: a member who is merely missing the Jailed role, without a
    fresh join, was most likely released by hand by a moderator, and re-jailing
    them strips every role they hold to enforce a hold nobody wants.

    Role *count* can't make that distinction, which an earlier version of this
    tried: jailing strips every non-managed role, so a hand-released member
    (mod removes @Jailed, nothing else) looks exactly like a fresh rejoiner —
    the guard would have missed the precise case it was written for. Ambiguous
    rows get a mod-log line and are left strictly alone.

    Holds that already expired during the downtime are skipped: re-applying one
    only for the expiry loop to release it a minute later would generate a
    spurious transcript, DM, and channel delete.
    """
    await bot.wait_until_ready()
    guild = bot.get_guild(ctx.guild_id)
    if guild is None:
        return
    jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=guild.id)
    jailed_role = guild.get_role(jailed_role_id) if jailed_role_id else None
    if jailed_role is None:
        return

    guild_id = guild.id
    now = time.time()

    def _fetch_active():
        with ctx.open_db() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT id, user_id, created_at FROM jails "
                    "WHERE guild_id = ? AND status = 'active' "
                    "AND (expires_at IS NULL OR expires_at > ?)",
                    (guild_id, now),
                ).fetchall()
            ]

    active = await asyncio.to_thread(_fetch_active)
    if not active:
        return

    reapplied = 0
    for jail in active:
        # One member's failure must not abandon the rest of the sweep: an
        # uncaught error here would crash the startup task, and the resilient
        # runner would restart it only to fail on the same member each time.
        try:
            member = guild.get_member(jail["user_id"])
            if member is None:
                continue  # still absent — the rejoin listener covers their return
            if jailed_role in member.roles:
                continue  # hold is intact

            joined_at = getattr(member, "joined_at", None)
            rejoined_after_jail = (
                joined_at is not None
                and joined_at.timestamp() > jail["created_at"]
            )
            if not rejoined_after_jail:
                log.warning(
                    "Jail #%s is active for %s, who has no Jailed role and no "
                    "join since it was imposed — leaving alone, needs a human.",
                    jail["id"],
                    member,
                )
                await _post_audit(
                    ctx,
                    guild,
                    discord.Embed(
                        title="⚠️ Jail state mismatch",
                        description=(
                            f"{member.mention} has an active hold (jail #{jail['id']}) "
                            "but isn't wearing the Jailed role, and hasn't rejoined "
                            "since it was imposed. They may have been released by "
                            "hand — I left their roles alone.\n"
                            f"Use `/unjail` to close jail #{jail['id']} out."
                        ),
                        color=CLR_JAIL,
                    ),
                )
                continue
            await check_jail_rejoin(
                ctx,
                member,
                note=(
                    f"⚠️ {member.mention} rejoined while the bot was offline. "
                    "Jail has been re-applied."
                ),
            )
            reapplied += 1
        except Exception:
            log.exception(
                "Jail reconcile sweep failed on jail #%s — continuing",
                jail["id"],
            )

    if reapplied:
        log.info("Jail reconcile sweep: re-applied %d lapsed hold(s).", reapplied)


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
                el_guild_id = guild.id

                def _get_expired():
                    with ctx.open_db() as conn:
                        return get_expired_jails(conn, el_guild_id)

                expired = await asyncio.to_thread(_get_expired)
                for jail in expired:
                    target = await resolve_release_target(
                        bot, guild, jail["user_id"]
                    )
                    if target is not None:
                        # One release path for present and departed members
                        # alike: a departed one still gets their transcript,
                        # channel cleanup, and audit row, none of which the
                        # old DB-only shortcut here wrote.
                        await _do_unjail(
                            ctx,
                            guild,
                            target,
                            reason="Jail duration expired"
                            + ("" if isinstance(target, discord.Member) else " (user left)"),
                        )
                    else:
                        # Discord has no such user (deleted account). Nothing
                        # to transcript or DM — close the row so it stops
                        # being re-examined every minute.
                        expired_jail_id = jail["id"]
                        expired_user_id = jail["user_id"]

                        def _release_left(
                            jid: int = expired_jail_id,
                            uid: int = expired_user_id,
                        ) -> None:
                            with ctx.open_db() as conn:
                                release_jail(
                                    conn,
                                    jid,
                                    reason="Jail duration expired (user unreachable)",
                                )
                                write_audit(
                                    conn,
                                    guild_id=el_guild_id,
                                    action="jail_release",
                                    actor_id=0,
                                    target_id=uid,
                                    extra={
                                        "jail_id": jid,
                                        "reason": "expired",
                                        "note": "user_unresolvable",
                                    },
                                )

                        await asyncio.to_thread(_release_left)
        except Exception:
            log.exception("Error in jail expiry loop")
        await asyncio.sleep(60)


# How long a closed ticket lingers before the sweep archives + deletes it, and
# how often the sweep runs. The lingering window lets a mod hit Reopen for a
# "one more thing" without spawning a fresh ticket; after 24 h untouched we
# transcript and remove the channel.
_TICKET_AUTODELETE_SECONDS = 24 * 3600
_TICKET_AUTODELETE_POLL_SECONDS = 3600


async def ticket_autodelete_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Permanently delete tickets 24 h after they were closed.

    Reopening a ticket clears ``closed_at`` and flips its status back to
    ``open``, so it silently drops out of the sweep — the countdown only runs
    while a ticket sits closed. Each deletion goes through
    :func:`_finalize_ticket_delete`, so the transcript is archived and DM'd
    before the channel is removed. Per-ticket errors are logged and retried on
    the next pass rather than aborting the whole sweep.
    """
    await bot.wait_until_ready()
    me = bot.user
    actor_id = me.id if me else 0
    while not bot.is_closed():
        try:
            cutoff = time.time() - _TICKET_AUTODELETE_SECONDS

            def _fetch(cutoff: float = cutoff):
                with ctx.open_db() as conn:
                    return get_tickets_to_autodelete(conn, closed_before=cutoff)

            due = await asyncio.to_thread(_fetch)
            for ticket in due:
                try:
                    guild = bot.get_guild(ticket["guild_id"])
                    if guild is None:
                        continue
                    raw = guild.get_channel(ticket["channel_id"])
                    if not isinstance(raw, discord.TextChannel):
                        # Channel was already removed by hand — mark the row
                        # deleted so it stops matching the sweep every hour.
                        gone_id = ticket["id"]

                        def _mark_gone(tid: int = gone_id) -> None:
                            with ctx.open_db() as conn:
                                delete_ticket(conn, tid)

                        await asyncio.to_thread(_mark_gone)
                        continue
                    await _finalize_ticket_delete(
                        ctx, raw, ticket, actor_id=actor_id, auto=True
                    )
                    log.info(
                        "Auto-deleted ticket %s (24h after close)", ticket["id"]
                    )
                except Exception:
                    log.exception(
                        "Auto-delete failed for ticket %s", ticket.get("id")
                    )
        except Exception:
            log.exception("Error in ticket auto-delete loop")
        await asyncio.sleep(_TICKET_AUTODELETE_POLL_SECONDS)


# Default if no per-guild override is set. Kept in sync with the
# `policy_vote_timeout_hours` admin setting (see web_server/routes/config.py).
_POLICY_VOTE_TIMEOUT_DEFAULT_HOURS = 72


def _policy_vote_timeout_seconds(ctx: AppContext, guild_id: int) -> float:
    with ctx.open_db() as conn:
        raw = get_config_value(
            conn,
            "policy_vote_timeout_hours",
            str(_POLICY_VOTE_TIMEOUT_DEFAULT_HOURS),
            guild_id,
        )
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        hours = _POLICY_VOTE_TIMEOUT_DEFAULT_HOURS
    return max(hours, 0) * 3600.0


async def sweep_expired_policy_votes(bot: discord.Client, ctx: AppContext) -> None:
    """One pass over **every** guild the bot is in.

    ``/policy open`` works on any server, and each server sets its own voting
    deadline on its own dashboard, so the sweep can't be home-guild-only — a
    proposal on a second server would hang in 'voting' forever at any deadline.
    """
    for guild in list(getattr(bot, "guilds", [])):
        timeout_secs = _policy_vote_timeout_seconds(ctx, guild.id)
        if timeout_secs <= 0:
            continue
        pvt_guild_id = guild.id

        def _get_expired_votes(
            pvt_guild_id: int = pvt_guild_id, timeout_secs: float = timeout_secs
        ):
            with ctx.open_db() as conn:
                return find_expired_policy_votes(
                    conn, pvt_guild_id, timeout_seconds=timeout_secs
                )

        expired = await asyncio.to_thread(_get_expired_votes)
        for policy in expired:
            try:
                await _resolve_expired_policy(bot, ctx, guild, policy)
            except Exception:
                log.exception(
                    "Failed to resolve expired policy %s",
                    policy.get("id"),
                )


async def policy_vote_timeout_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Background task that resolves policy votes past their deadline."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await sweep_expired_policy_votes(bot, ctx)
        except Exception:
            log.exception("Error in policy vote timeout loop")
        await asyncio.sleep(60)


async def _resolve_expired_policy(
    bot: discord.Client,
    ctx: AppContext,
    guild: discord.Guild,
    policy: PolicyTicketRow,
) -> None:
    policy_id = policy["id"]
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

    def _get_votes():
        with ctx.open_db() as conn:
            return get_policy_votes(conn, policy_id)

    votes = await asyncio.to_thread(_get_votes)
    vote_map = {v["user_id"]: v["vote"] for v in votes}
    voted_ids = set(vote_map.keys()) & eligible
    yes_ids = [uid for uid in voted_ids if vote_map[uid] == "yes"]
    no_ids = [uid for uid in voted_ids if vote_map[uid] == "no"]
    abstain_ids = [uid for uid in voted_ids if vote_map[uid] == "abstain"]
    awaiting_ids = eligible - voted_ids

    tally = {
        "yes": yes_ids,
        "no": no_ids,
        "abstain": abstain_ids,
        "awaiting": list(awaiting_ids),
    }
    outcome = _vote_outcome(tally, eligible, expired=True)
    if outcome == "pending":
        # vote_outcome never returns "pending" when expired=True, but guard
        # so we never finalize the wrong way if the rule ever changes.
        return

    raw_channel = guild.get_channel(policy["channel_id"]) if policy["channel_id"] else None
    channel = raw_channel if isinstance(raw_channel, discord.TextChannel) else None

    bot_user = bot.user
    actor_id = bot_user.id if bot_user is not None else 0
    await finalize_policy_vote(
        ctx,
        guild,
        policy_id,
        outcome,
        channel=channel,
        yes_ids=yes_ids,
        no_ids=no_ids,
        abstain_ids=abstain_ids,
        actor_id=actor_id,
        timed_out=True,
    )


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
        ctx: AppContext = cast("Bot", bot).ctx
        guild = interaction.guild
        user = interaction.user
        if guild is None or not isinstance(user, discord.Member):
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        cat_id = _get_config(ctx, "ticket_category_id", guild_id=guild.id)
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "❌ Ticket category not configured.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await safe_resolve_accent(ctx, guild, log_label="jail")
        desc_text = self.description.value or "(no description)"
        ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
        name = f"ticket-{user.name[:16]}-{ts}"
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

        fm_guild_id = guild.id
        fm_source_url = self.source_message.jump_url

        def _create_fm_ticket():
            with ctx.open_db() as conn:
                tid = create_ticket(
                    conn,
                    guild_id=fm_guild_id,
                    user_id=user.id,
                    channel_id=channel.id,
                    description=desc_text,
                    source_message_url=fm_source_url,
                )
                write_audit(
                    conn,
                    guild_id=fm_guild_id,
                    action="ticket_open",
                    actor_id=user.id,
                    extra={
                        "ticket_id": tid,
                        "description": desc_text,
                        "source": fm_source_url,
                    },
                )
            return tid

        ticket_id = await asyncio.to_thread(_create_fm_ticket)

        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=desc_text,
            color=accent,
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
