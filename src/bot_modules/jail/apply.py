"""Canonical jail-application flow.

This is the single source of truth for "place a member in a moderation hold."
Both the bot-side slash command (``/jail``) and the dashboard ticket-jail
endpoint route through here so they share permission checks, role snapshot,
channel creation, audit logging, and DM notification.

The function returns a structured :class:`JailOutcome` rather than raising,
so callers can format their own user-facing messages (an interaction
followup, a JSON 200/400 response, etc.).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import discord

from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import get_config_value
from bot_modules.services.moderation import (
    compute_roles_to_snapshot,
    create_jail,
    fmt_duration,
    get_active_jail,
    set_jail_channel,
    write_audit,
)
from bot_modules.services.embeds import MOD_JAIL as _CLR_JAIL


# ── Result type ──────────────────────────────────────────────────────


JailErrorKind = Literal[
    "bot_target",        # target is a bot
    "self_target",       # moderator tried to jail themselves
    "admin_target",      # target holds admin
    "mod_target",        # target is mod, actor is not admin
    "already_jailed",    # active jail row exists for target
    "no_role_perms",     # missing Manage Roles when creating Jailed role
    "no_member_perms",   # missing perms to edit target's roles
    "no_channel_perms",  # missing Manage Channels when creating jail channel
]


@dataclass(frozen=True)
class JailOutcome:
    """What happened when ``apply_jail`` ran.

    ``ok`` distinguishes success from any failure. On success ``jail_id`` and
    ``channel_id`` are populated. On failure, ``error_kind`` is one of the
    documented strings above and ``error_message`` is a human-readable string
    suitable for surfacing to the moderator.
    """

    ok: bool
    jail_id: int | None = None
    channel_id: int | None = None
    error_kind: JailErrorKind | None = None
    error_message: str | None = None


# ── Precondition checks (no Discord calls, no DM) ────────────────────


def check_jail_preconditions(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    moderator: discord.Member,
) -> JailOutcome | None:
    """Return a non-OK ``JailOutcome`` if the jail must be refused, else ``None``.

    Validates the moderation policy (no bots, no self-jail, no jailing admins,
    no jailing mods unless the actor is admin) and confirms the target isn't
    already in an active jail. Separated from ``apply_jail`` so slash commands
    can send the rejection as an initial interaction response (before
    deferring) while the dashboard route can use a single call site.
    """
    if target.bot:
        return JailOutcome(
            ok=False, error_kind="bot_target", error_message="Cannot jail a bot."
        )
    if target.id == moderator.id:
        return JailOutcome(
            ok=False, error_kind="self_target", error_message="Cannot jail yourself."
        )

    cfg = ctx.guild_config(guild.id)
    target_is_admin = (
        target.guild_permissions.administrator or cfg.member_is_admin(target)
    )
    if target_is_admin:
        return JailOutcome(
            ok=False, error_kind="admin_target", error_message="Cannot jail an admin."
        )

    target_is_mod = (
        target.guild_permissions.manage_guild
        or target.guild_permissions.administrator
        or cfg.member_is_mod(target)
    )
    actor_is_admin = (
        moderator.guild_permissions.administrator or cfg.member_is_admin(moderator)
    )
    if target_is_mod and not actor_is_admin:
        return JailOutcome(
            ok=False,
            error_kind="mod_target",
            error_message="Only admins can jail a moderator.",
        )

    with ctx.open_db() as conn:
        if get_active_jail(conn, guild.id, target.id):
            return JailOutcome(
                ok=False,
                error_kind="already_jailed",
                error_message=f"{target} is already jailed.",
            )

    return None


# ── Main entrypoint ──────────────────────────────────────────────────


async def apply_jail(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    moderator: discord.Member,
    *,
    reason: str = "",
    duration_seconds: int | None = None,
    source: str = "command",
    source_extra: dict | None = None,
) -> JailOutcome:
    """Place ``target`` in a moderation hold and return what happened.

    Performs the full canonical flow:

    1. Run :func:`check_jail_preconditions` (cheap, synchronous validation).
    2. Ensure the ``@Jailed`` role exists, creating it if missing and
       denying view+send on every channel.
    3. Snapshot the target's current roles for restoration on release.
    4. Strip those roles and assign only ``@Jailed``.
    5. Insert a ``jails`` row (with the role snapshot; ``channel_id`` 0) and a
       ``jail_create`` audit log entry **immediately**, so a failure before the
       channel exists can never strand the member role-less with no restoration
       record. The audit ``extra`` dict carries ``source`` so dashboard-driven
       jails can be filtered out of pure-Discord ones later.
    6. Create a private jail channel with overwrites for the target, the
       bot, and any configured mod roles, then record its id on the row.
    7. Post a welcome embed in the jail channel and DM the target.
    8. Post the audit embed in the configured log channel.

    If channel creation fails (missing **Manage Channels**), steps 6-8 are
    skipped but the ``jails`` row from step 5 remains, so restoration and the
    auto-release loop still work — the moderator just grants the permission.

    ``source`` is recorded in the audit log only; it does not change behavior.
    Pass ``"dashboard"`` from the web route, ``"command"`` from the slash
    command, ``"auto"`` from any future automation.

    ``source_extra`` is merged into the audit ``extra`` dict so callers can
    attach contextual IDs (e.g. ``{"ticket_id": 42}``) to the single canonical
    audit row without writing a duplicate cross-link entry.
    """
    precheck = check_jail_preconditions(ctx, guild, target, moderator)
    if precheck is not None:
        return precheck

    # ── Step 1: ensure @Jailed role ──────────────────────────────────
    guild_id = guild.id

    def _get_jailed_role_id():
        with ctx.open_db() as conn:
            return get_config_value(conn, "jailed_role_id", "0", guild_id)

    jailed_role_id_raw = await asyncio.to_thread(_get_jailed_role_id)
    try:
        jailed_role_id = int(jailed_role_id_raw or "0")
    except ValueError:
        jailed_role_id = 0

    jailed_role = guild.get_role(jailed_role_id) if jailed_role_id else None
    if jailed_role is None:
        try:
            jailed_role = await guild.create_role(
                name="Jailed",
                reason="Dungeon Keeper jail system setup",
                permissions=discord.Permissions.none(),
            )
        except discord.Forbidden:
            return JailOutcome(
                ok=False,
                error_kind="no_role_perms",
                error_message=(
                    "Missing **Manage Roles** — can't create the Jailed role."
                ),
            )
        ctx.set_config_value("jailed_role_id", str(jailed_role.id), guild.id)
        # Deny view + send on all channels so jailed members can't see the
        # server. Best-effort; channels that refuse the overwrite (because
        # the bot lacks permission on them specifically) are skipped.
        for channel in guild.channels:
            try:
                await channel.set_permissions(
                    jailed_role, view_channel=False, send_messages=False
                )
            except discord.Forbidden:
                pass

    # ── Step 2: snapshot + strip roles ──────────────────────────────
    # Exclude managed roles from the snapshot — they're controlled by
    # integrations and can't be restored by the bot anyway.
    stored_roles = compute_roles_to_snapshot(
        [r.id for r in target.roles if not r.managed],
        default_role_id=guild.default_role.id,
        jailed_role_id=jailed_role.id,
    )

    # Use remove_roles + add_roles instead of edit(roles=[...]) so that any
    # integration-managed roles on the member (e.g. Nitro Booster, bot roles)
    # are left in place rather than causing a blanket 403 Forbidden.
    removable = [
        r for r in target.roles
        if not r.managed and r.id != guild.default_role.id
    ]
    try:
        await target.remove_roles(*removable, reason=f"Jailed by {moderator}")
        await target.add_roles(jailed_role, reason=f"Jailed by {moderator}")
    except discord.Forbidden:
        bot_top = guild.me.top_role
        target_top = target.top_role
        if guild.owner_id == target.id:
            detail = f"{target} is the server owner — Discord prevents role edits on the owner."
        else:
            detail = (
                f"Bot top role: **{bot_top.name}** (pos {bot_top.position}) | "
                f"Jailed role: **{jailed_role.name}** (pos {jailed_role.position}) | "
                f"{target.display_name} top role: **{target_top.name}** (pos {target_top.position})"
            )
        return JailOutcome(
            ok=False,
            error_kind="no_member_perms",
            error_message=f"Missing permission to manage this user's roles.\n{detail}",
        )

    # ── Step 3: persist the jail row + audit BEFORE the channel ─────
    # Write the restoration snapshot the instant the roles are stripped, so a
    # failure or crash before the channel exists can never strand the member
    # role-less with no jails row. Restoration reads ``stored_roles`` off this
    # row and the auto-release loop / ``/jail release`` both key off it; if the
    # row were only written after the channel (as before), a Manage-Channels
    # failure here left the member at @everyone+@Jailed forever with no record.
    # ``channel_id`` starts at 0 and is filled in once the channel is created.
    audit_extra: dict = {
        "jail_id": None,  # filled in after create_jail returns
        "reason": reason,
        "duration": (
            fmt_duration(duration_seconds) if duration_seconds else "indefinite"
        ),
        "source": source,
    }
    if source_extra:
        # Caller-supplied keys (e.g. ticket_id) merge in last but cannot
        # overwrite the canonical fields.
        for key, value in source_extra.items():
            if key not in audit_extra:
                audit_extra[key] = value

    target_id = target.id
    moderator_id = moderator.id

    def _persist():
        with ctx.open_db() as conn:
            jid = create_jail(
                conn,
                guild_id=guild_id,
                user_id=target_id,
                moderator_id=moderator_id,
                reason=reason,
                stored_roles=stored_roles,
                channel_id=0,  # filled in once the channel is created
                duration_seconds=duration_seconds,
            )
            audit_extra["jail_id"] = jid
            write_audit(
                conn,
                guild_id=guild_id,
                action="jail_create",
                actor_id=moderator_id,
                target_id=target_id,
                extra=audit_extra,
            )
        return jid

    jail_id = await asyncio.to_thread(_persist)

    # ── Step 4: create private jail channel ─────────────────────────
    def _get_cat_id():
        with ctx.open_db() as conn:
            return get_config_value(conn, "jail_category_id", "0", guild_id)

    cat_id_raw = await asyncio.to_thread(_get_cat_id)
    try:
        cat_id = int(cat_id_raw or "0")
    except ValueError:
        cat_id = 0
    category = guild.get_channel(cat_id) if cat_id else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    ts = datetime.now(timezone.utc).strftime("%m%d-%H%M")
    ch_name = f"jail-{target.name[:16]}-{ts}"

    mod_role_ids = ctx.guild_config(guild.id).mod_role_ids

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

    try:
        jail_channel = await guild.create_text_channel(
            ch_name, category=category, overwrites=overwrites,  # type: ignore[arg-type]
        )
    except discord.Forbidden:
        # The role swap already happened AND the jail row is persisted above
        # (with channel_id 0), so restoration and the auto-release loop still
        # work — the member is not stranded. We don't roll anything back:
        # better to leave the user jailed and have a moderator grant **Manage
        # Channels** than to silently un-strip their roles and abandon them.
        return JailOutcome(
            ok=False,
            jail_id=jail_id,
            error_kind="no_channel_perms",
            error_message=(
                "Roles applied, but I couldn't create the jail channel — "
                "grant **Manage Channels** to finish setup."
            ),
        )

    # ── Step 5: record the channel id on the jail row ───────────────
    jail_channel_id = jail_channel.id

    def _set_channel():
        with ctx.open_db() as conn:
            set_jail_channel(conn, jail_id, jail_channel_id)

    await asyncio.to_thread(_set_channel)

    # ── Step 6: jail-channel welcome embed ───────────────────────────
    duration_text = (
        fmt_duration(duration_seconds) if duration_seconds else "Indefinite"
    )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    expiry_ts = now_ts + duration_seconds if duration_seconds else None
    countdown_line = (
        f"**Releases:** <t:{expiry_ts}:R> (<t:{expiry_ts}:f>)\n"
        if expiry_ts
        else ""
    )
    embed = discord.Embed(
        title="🔒 Moderation Hold",
        description=(
            f"{target.mention}, you have been placed in a moderation hold.\n\n"
            f"**Moderator:** {moderator.mention}\n"
            f"**Duration:** {duration_text}\n"
            + countdown_line
            + (f"**Reason:** {reason}\n" if reason else "")
            + "\nA moderator will review your case here."
        ),
        color=_CLR_JAIL,
        timestamp=datetime.now(timezone.utc),
    )
    try:
        await jail_channel.send(embed=embed)
    except discord.HTTPException:
        # Channel exists but send failed — non-fatal; mods can still post.
        pass

    # ── Step 7: DM the user ──────────────────────────────────────────
    dm_embed = discord.Embed(
        title="You've been placed in a moderation hold",
        description=(
            f"**Server:** {guild.name}\n"
            f"**Moderator:** {moderator}\n"
            f"**Duration:** {duration_text}\n"
            + (f"**Reason:** {reason}\n" if reason else "")
            + "\nPlease check the jail channel — a moderator will review your situation."
        ),
        color=_CLR_JAIL,
    )
    try:
        await target.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        # DMs closed — fallback note in the jail channel.
        try:
            await jail_channel.send(
                f"⚠️ Could not DM {target.mention} — they may have DMs disabled.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

    # ── Step 8: post audit-log embed ─────────────────────────────────
    def _get_log_ch():
        with ctx.open_db() as conn:
            return get_config_value(conn, "log_channel_id", "0", guild_id)

    log_ch_id_raw = await asyncio.to_thread(_get_log_ch)
    try:
        log_ch_id = int(log_ch_id_raw or "0")
    except ValueError:
        log_ch_id = 0
    if log_ch_id:
        log_ch = guild.get_channel(log_ch_id)
        if isinstance(log_ch, discord.TextChannel):
            audit_embed = discord.Embed(
                title="🔒 Member Jailed",
                description=(
                    f"{target.mention} jailed by {moderator.mention}\n"
                    f"**Duration:** {duration_text}"
                    + (f"\n**Reason:** {reason}" if reason else "")
                ),
                color=_CLR_JAIL,
            )
            try:
                await log_ch.send(
                    embed=audit_embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    return JailOutcome(ok=True, jail_id=jail_id, channel_id=jail_channel.id)
