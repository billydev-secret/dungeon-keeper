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
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.core.branding import resolve_accent_color
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
    parse_duration,
    release_jail,
    reopen_ticket,
    resolve_policy_vote,
    store_transcript,
    write_audit,
    PolicyTicketRow,
)
from bot_modules.jail.embeds import (
    build_policy_vote_update_embed,
    build_setup_complete_embed,
    build_setup_step_embed,
)
from bot_modules.jail.logic import (
    SETUP_FINAL_STEP,
    setup_button_label,
    setup_step_meta,
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


def _guild_has_any_ticket_panel(ctx: AppContext, guild_id: int) -> bool:
    with ctx.open_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM ticket_panels WHERE guild_id = ? LIMIT 1", (guild_id,)
        ).fetchone()
    return row is not None


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
            accent = await resolve_accent_color(ctx.db_path, channel.guild)
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
        self.ctx.set_config_value(self.config_key, ids, interaction.guild_id)
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
        self.ctx.set_config_value(self.config_key, str(ch.id), interaction.guild_id)
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
        self.ctx.set_config_value(self.config_key, str(cat.id), interaction.guild_id)
        await interaction.response.send_message(
            f"✅ Set **{self.config_key}** → {cat}", ephemeral=True
        )


# Maps the ``select_kind`` strings returned by ``setup_step_meta`` to the
# concrete Select classes here. Keeps the data (what each step asks for) in
# ``jail/logic.py`` while the Discord-side UI construction stays here where
# the Select classes live.
_SETUP_SELECTS: dict[str, type] = {
    "role": _SetupRoleSelect,
    "channel": _SetupChannelSelect,
    "category": _SetupCategorySelect,
}


def _setup_view(
    ctx: AppContext,
    step: int,
    *,
    colour: "discord.Colour | None" = None,
) -> tuple[discord.Embed, discord.ui.View]:
    """Return the embed + view for a given setup step.

    The per-step content (title, description, config key, select type,
    placeholder) lives in ``jail.logic.setup_step_meta`` so its wording can
    be tested without spinning up a View. This function is the glue:
    pick the right ``discord.ui.Select`` subclass, wire up the Next button,
    and return the rendered embed/view pair.

    ``colour`` is the resolved per-guild accent, threaded from the async
    ``/setup`` entry point (this function is sync, so it can't resolve it
    itself). Left ``None`` for tests, falling back to the builder default.
    """
    meta = setup_step_meta(step)
    if meta is None:
        return build_setup_complete_embed(), discord.ui.View()

    view = discord.ui.View(timeout=300)
    select_cls = _SETUP_SELECTS[meta["select_kind"]]
    view.add_item(select_cls(meta["config_key"], ctx, placeholder=meta["placeholder"]))

    async def next_step(interaction: discord.Interaction):
        e, v = _setup_view(ctx, step + 1, colour=colour)
        await interaction.response.edit_message(embed=e, view=v)

    btn: discord.ui.Button = discord.ui.Button(
        label=setup_button_label(step), style=discord.ButtonStyle.primary,
    )  # type: ignore[assignment]
    btn.callback = next_step  # type: ignore[method-assign]
    view.add_item(btn)
    return build_setup_step_embed(meta, colour=colour), view


# Kept for backwards compatibility with anything that imported the constant
# from ``jail_commands``. The canonical source is ``jail.logic.SETUP_FINAL_STEP``.
_SETUP_FINAL_STEP = SETUP_FINAL_STEP


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
        tid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        # Get ctx from bot
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
        member = interaction.user
        if not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message(
                "Only moderators can reopen tickets.", ephemeral=True
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
            accent = await resolve_accent_color(ctx.db_path, channel.guild)
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
                    embed=discord.Embed(
                        description=f"Your ticket in **{interaction.guild.name}** has been reopened.",  # type: ignore[union-attr]
                        color=accent,
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
        tid = int((item.custom_id or "").split(":")[-1])  # type: ignore[attr-defined]
        return cls(tid)

    async def callback(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
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
        accent = await resolve_accent_color(ctx.db_path, channel.guild)
        del_ch_id = channel.id
        del_guild_id = interaction.guild_id or 0
        del_member_id = member.id
        del_ticket_id = self.ticket_id

        def _fetch_del_ticket():
            with ctx.open_db() as conn:
                return get_ticket_by_channel(conn, del_ch_id)

        ticket = await asyncio.to_thread(_fetch_del_ticket)

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

        del_ticket_user_id = ticket["user_id"]

        def _delete_ticket():
            with ctx.open_db() as conn:
                delete_ticket(conn, del_ticket_id)
                write_audit(
                    conn,
                    guild_id=del_guild_id,
                    action="ticket_delete",
                    actor_id=del_member_id,
                    target_id=del_ticket_user_id,
                    extra={"ticket_id": del_ticket_id, "message_count": 0},
                )

        await asyncio.to_thread(_delete_ticket)

        audit_embed = discord.Embed(
            title="🗑️ Ticket Deleted",
            description=f"**Ticket #{self.ticket_id}** by <@{ticket['user_id']}> deleted by {member.mention}",
            color=accent,
        )
        await _post_audit(ctx, interaction.guild, audit_embed)  # type: ignore[arg-type]
        await channel.delete(reason=f"Ticket #{self.ticket_id} deleted by {member}")


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
        await interaction.response.send_message("Server-only.", ephemeral=True)
        return
    if not (_is_mod(member, ctx) or _is_admin(member, ctx)):
        await interaction.response.send_message(
            "Only mods and admins can vote.", ephemeral=True
        )
        return

    def _get_policy():
        with ctx.open_db() as conn:
            return get_policy_ticket(conn, policy_id)

    policy = await asyncio.to_thread(_get_policy)
    if not policy or policy["status"] != "voting":
        await interaction.response.send_message(
            "This vote is no longer active.", ephemeral=True
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
                "This only works in a server.", ephemeral=True
            )
            return

        cat_id = _get_config(ctx, "ticket_category_id", guild_id=guild.id)
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category is not configured. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await resolve_accent_color(ctx.db_path, guild)

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
            embed=discord.Embed(
                description=f"Your ticket has been created in **{guild.name}** → [Go to ticket]({channel.jump_url})",
                color=accent,
            ),
        )

        # Notify mods
        def _get_notify():
            with ctx.open_db() as conn:
                return get_config_value(conn, "ticket_notify_on_create", "1")

        notify = await asyncio.to_thread(_get_notify)
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

    def __init__(self, ticket_id: int):
        super().__init__()
        self.ticket_id = ticket_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        bot = interaction.client
        ctx: AppContext = cast("Bot", bot).ctx
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
        accent = await resolve_accent_color(ctx.db_path, guild)
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
                "This ticket is not open.", ephemeral=True
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
        await interaction.response.send_message("Server-only command.", ephemeral=True)
        return

    # Cheap precondition checks → initial response (no defer required).
    precheck = check_jail_preconditions(ctx, guild, target, mod)
    if precheck is not None:
        await interaction.response.send_message(
            precheck.error_message or "Cannot jail this user.", ephemeral=True
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
            result.error_message or "Failed to jail user.", ephemeral=True
        )
        return

    channel_mention = f"<#{result.channel_id}>" if result.channel_id else "(channel)"
    await interaction.followup.send(
        f"✅ {target} has been jailed → {channel_mention}", ephemeral=True
    )


async def _do_unjail(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    *,
    reason: str = "",
    actor: discord.Member | None = None,
) -> str:
    """Core unjail logic.  Returns a status message."""

    def _fetch_jail():
        with ctx.open_db() as conn:
            return get_active_jail(conn, guild.id, target.id)

    jail = await asyncio.to_thread(_fetch_jail)
    if not jail:
        return f"{target} is not currently jailed."

    # Restore roles — use remove/add rather than edit(roles=...) so that any
    # managed roles the member holds are left in place instead of causing 403.
    stored = json.loads(jail["stored_roles"])
    available_role_ids = {r.id for r in guild.roles}
    restorable_ids, missing = compute_roles_to_restore(stored, available_role_ids)
    roles_to_add: list[discord.Role] = [
        r for r in (guild.get_role(rid) for rid in restorable_ids) if r is not None
    ]

    jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=guild.id)
    jailed_role = guild.get_role(jailed_role_id)

    try:
        if jailed_role:
            await target.remove_roles(jailed_role, reason=f"Unjailed: {reason}")
        if roles_to_add:
            await target.add_roles(*roles_to_add, reason=f"Unjailed: {reason}")
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

    def _release():
        with ctx.open_db() as conn:
            release_jail(conn, jail_id_rel, reason=reason)
            write_audit(
                conn,
                guild_id=guild.id,
                action="jail_release",
                actor_id=actor_id,
                target_id=target.id,
                extra={"jail_id": jail_id_rel, "reason": reason},
            )

    await asyncio.to_thread(_release)

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

    def _fetch_rejoin_jail():
        with ctx.open_db() as conn:
            return get_active_jail(conn, member.guild.id, member.id)

    jail = await asyncio.to_thread(_fetch_rejoin_jail)
    if not jail:
        return False

    jailed_role_id = _get_config(ctx, "jailed_role_id", guild_id=member.guild.id)
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
                el_guild_id = guild.id

                def _get_expired():
                    with ctx.open_db() as conn:
                        return get_expired_jails(conn, el_guild_id)

                expired = await asyncio.to_thread(_get_expired)
                for jail in expired:
                    member = guild.get_member(jail["user_id"])
                    if member:
                        await _do_unjail(
                            ctx, guild, member, reason="Jail duration expired"
                        )
                    else:
                        # User left — just release the record
                        expired_jail_id = jail["id"]

                        def _release_left(jid: int = expired_jail_id) -> None:
                            with ctx.open_db() as conn:
                                release_jail(
                                    conn,
                                    jid,
                                    reason="Jail duration expired (user left)",
                                )

                        await asyncio.to_thread(_release_left)
        except Exception:
            log.exception("Error in jail expiry loop")
        await asyncio.sleep(60)


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


async def policy_vote_timeout_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Background task that resolves policy votes past their deadline."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(ctx.guild_id)
            if guild is not None:
                timeout_secs = _policy_vote_timeout_seconds(ctx, guild.id)
                pvt_guild_id = guild.id
                if timeout_secs > 0:
                    def _get_expired_votes():
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
            await interaction.response.send_message("Server only.", ephemeral=True)
            return

        cat_id = _get_config(ctx, "ticket_category_id", guild_id=guild.id)
        category = guild.get_channel(cat_id) if cat_id else None
        if not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Ticket category not configured.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        accent = await resolve_accent_color(ctx.db_path, guild)
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
