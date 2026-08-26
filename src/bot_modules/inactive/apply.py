"""Canonical inactive-hold flow: move a member to the inactive channel and back.

Mirrors :mod:`bot_modules.jail.apply` but for the softer "inactivity" case:

* There is **one shared** inactive channel (not a per-user channel), granted to
  the ``@Inactive`` role rather than to each member individually.
* Holds are indefinite — a member leaves the inactive channel only when a
  moderator reactivates them (after they file a ticket).

Both the manual ``/inactive mark`` command and the automatic sweep route
through :func:`apply_inactive`, and ``/reactivate`` routes through
:func:`reactivate_member`, so permission checks, the role snapshot, and audit
logging live in one place. Like the jail flow, these return structured results
rather than raising so callers can format their own user-facing messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

import discord

from bot_modules.core.app_context import AppContext
from bot_modules.core.db_utils import get_config_value
from bot_modules.core.role_provision import (
    RoleSpec,
    ensure_feature_role,
    mod_log_announcer,
)
from bot_modules.inactive.store import (
    create_inactive,
    get_active_inactive,
    is_sweep_exempt,
    reactivate_inactive,
)
from bot_modules.services.embeds import MOD_INFO, MOD_SUCCESS
from bot_modules.inactive.logic import roles_to_strip
from bot_modules.services.moderation import (
    compute_roles_to_restore,
    write_audit,
)

log = logging.getLogger("dungeonkeeper.inactive")

InactiveErrorKind = Literal[
    "bot_target",         # target is a bot
    "self_target",        # actor tried to mark themselves
    "admin_target",       # target holds admin
    "mod_target",         # target is mod, actor is not admin
    "already_inactive",   # active inactive row exists for target
    "exempt_target",      # target is exempt from inactivity holds
    "no_role_perms",      # missing Manage Roles when creating @Inactive
    "no_member_perms",    # missing perms to edit target's roles
]


@dataclass(frozen=True)
class InactiveOutcome:
    """What happened when :func:`apply_inactive` ran."""

    ok: bool
    inactive_id: int | None = None
    error_kind: InactiveErrorKind | None = None
    error_message: str | None = None


# ── Precondition checks (no Discord calls, no DM) ────────────────────


def check_inactive_preconditions(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    moderator: discord.Member,
) -> InactiveOutcome | None:
    """Return a non-OK outcome if the hold must be refused, else ``None``.

    Same moderation policy as jailing: never a bot, never yourself, never an
    admin, and only an admin may move a fellow moderator. Also refuses if the
    member is already held inactive (idempotency) or has been exempted from
    inactivity holds on the dashboard.

    The exemption check here is what makes an exemption absolute: every entry
    path — ``/inactive mark``, ``/inactive sweep``, the background loop — reaches
    it, because :func:`apply_inactive` runs these preconditions itself. The
    sweeps *also* drop exempt members from their exclusion set upstream, so an
    exemption keeps them out of the selection entirely rather than selecting
    them and refusing one by one; this is the backstop that holds when nothing
    filtered first. A moderator with a real reason lifts the exemption first.
    """
    if target.bot:
        return InactiveOutcome(
            ok=False, error_kind="bot_target", error_message="❌ Cannot move a bot."
        )
    if target.id == moderator.id:
        return InactiveOutcome(
            ok=False,
            error_kind="self_target",
            error_message="❌ Cannot move yourself to the inactive channel.",
        )

    cfg = ctx.guild_config(guild.id)
    if target.guild_permissions.administrator or cfg.member_is_admin(target):
        return InactiveOutcome(
            ok=False, error_kind="admin_target", error_message="❌ Cannot move an admin."
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
        return InactiveOutcome(
            ok=False,
            error_kind="mod_target",
            error_message="❌ Only admins can move a moderator.",
        )

    with ctx.open_db() as conn:
        if get_active_inactive(conn, guild.id, target.id):
            return InactiveOutcome(
                ok=False,
                error_kind="already_inactive",
                error_message=f"❌ {target} is already in the inactive channel.",
            )
        if is_sweep_exempt(conn, guild.id, target.id):
            return InactiveOutcome(
                ok=False,
                error_kind="exempt_target",
                error_message=(
                    f"❌ {target} is exempt from inactivity holds. Lift the exemption "
                    f"on the dashboard's **Inactive Sweep** panel first."
                ),
            )

    return None


# ── @Inactive role helper ────────────────────────────────────────────


async def ensure_inactive_role(
    ctx: AppContext, guild: discord.Guild
) -> discord.Role | None:
    """Return the guild's ``@Inactive`` role, creating it if missing.

    On first creation the role is denied view+send on every channel (so an
    inactive member can't see the server), then granted view on the configured
    inactive channel if one is set. A role *adopted* by name skips that sweep —
    it is one the guild already set up, and re-denying every channel on it would
    be a destructive surprise. Returns ``None`` if the role has to be created
    but the bot lacks **Manage Roles**.
    """
    guild_id = guild.id

    def _get_ids() -> tuple[int, int]:
        with ctx.open_db() as conn:
            role_raw = get_config_value(conn, "inactive_role_id", "0", guild_id)
            chan_raw = get_config_value(conn, "inactive_channel_id", "0", guild_id)
        try:
            role_id = int(role_raw or "0")
        except ValueError:
            role_id = 0
        try:
            chan_id = int(chan_raw or "0")
        except ValueError:
            chan_id = 0
        return role_id, chan_id

    role_id, chan_id = await asyncio.to_thread(_get_ids)

    async def _lock_down(role: discord.Role) -> None:
        """Deny view everywhere, then grant the inactive channel back.

        Per channel and explicit, never a category sync — category grants do
        not cascade to the channels inside them.
        """
        inactive_channel = guild.get_channel(chan_id) if chan_id else None
        for channel in guild.channels:
            try:
                if inactive_channel is not None and channel.id == inactive_channel.id:
                    await channel.set_permissions(
                        role, view_channel=True, send_messages=True,
                        read_message_history=True,
                    )
                else:
                    await channel.set_permissions(
                        role, view_channel=False, send_messages=False
                    )
            except discord.HTTPException:
                # Best-effort per channel: a denied or transiently-failing
                # overwrite skips that channel rather than aborting the setup.
                pass

    return await ensure_feature_role(
        guild,
        RoleSpec(
            name="Inactive",
            reason="Dungeon Keeper inactive-channel setup",
        ),
        load=lambda: role_id,
        store=lambda rid: ctx.set_config_value("inactive_role_id", str(rid), guild.id),
        on_create=_lock_down,
        announce=mod_log_announcer(ctx, guild),
        feature="the inactive sweep",
    )


# ── Main entrypoint ──────────────────────────────────────────────────


async def apply_inactive(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    moderator: discord.Member,
    *,
    reason: str = "",
    source: str = "command",
) -> InactiveOutcome:
    """Snapshot + strip ``target``'s roles and give them the ``@Inactive`` role.

    ``moderator`` is the acting mod for a manual mark; for the automatic sweep
    pass ``guild.me`` and ``source="auto"``. ``source`` is recorded in the audit
    log and the DB row; it does not change behavior.
    """
    precheck = check_inactive_preconditions(ctx, guild, target, moderator)
    if precheck is not None:
        return precheck

    role = await ensure_inactive_role(ctx, guild)
    if role is None:
        return InactiveOutcome(
            ok=False,
            error_kind="no_role_perms",
            error_message="❌ Missing **Manage Roles** — can't create the Inactive role.",
        )

    # Snapshot everything except @everyone and the Inactive role itself. Exclude
    # managed roles (integrations control them and the bot can't restore them).
    stored_roles = roles_to_strip(
        [r.id for r in target.roles if not r.managed],
        default_role_id=guild.default_role.id,
        inactive_role_id=role.id,
    )

    removable = [
        r for r in target.roles
        if not r.managed and r.id != guild.default_role.id
    ]
    try:
        await target.remove_roles(*removable, reason=f"Moved to inactive by {moderator}")
        await target.add_roles(role, reason=f"Moved to inactive by {moderator}")
    except discord.Forbidden:
        return InactiveOutcome(
            ok=False,
            error_kind="no_member_perms",
            error_message=(
                "❌ Missing permission to manage this user's roles — check that my "
                "top role is above theirs and above the Inactive role."
            ),
        )

    guild_id = guild.id
    target_id = target.id
    moderator_id = moderator.id

    def _persist() -> int:
        with ctx.open_db() as conn:
            iid = create_inactive(
                conn,
                guild_id=guild_id,
                user_id=target_id,
                moderator_id=moderator_id,
                reason=reason,
                stored_roles=stored_roles,
                source=source,
            )
            write_audit(
                conn,
                guild_id=guild_id,
                action="inactive_apply",
                actor_id=moderator_id,
                target_id=target_id,
                extra={"inactive_id": iid, "reason": reason, "source": source},
            )
        return iid

    inactive_id = await asyncio.to_thread(_persist)

    # Feed the promotion-review watch set so this sleeper is spotted the moment
    # they post in the sleeper channel.
    from bot_modules.services.promotion_review_service import note_inactive

    await asyncio.to_thread(note_inactive, ctx.db_path, guild_id, target_id)

    # DM the member so they know where they went and how to get back.
    chan_id = await asyncio.to_thread(_read_channel_id, ctx, guild_id)
    channel_line = f"\nHead to <#{chan_id}> and open a ticket to restore your access." if chan_id else ""
    dm_embed = discord.Embed(
        title="You've Been Moved to the Inactive Channel",
        description=(
            f"You've been moved to the inactive area of **{guild.name}** due to "
            f"inactivity. **Your roles are saved** and will be restored when you're "
            f"reactivated." + channel_line
            + (f"\n\n**Note:** {reason}" if reason else "")
        ),
        color=MOD_INFO,
    )
    try:
        await target.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

    await _post_inactive_audit(
        ctx,
        guild,
        discord.Embed(
            title="💤 Member Moved to Inactive",
            description=(
                f"{target.mention} moved by {moderator.mention}"
                + (" (auto-sweep)" if source == "auto" else "")
                + (f"\n**Reason:** {reason}" if reason else "")
            ),
            color=MOD_INFO,
        ),
    )

    return InactiveOutcome(ok=True, inactive_id=inactive_id)


# ── Reactivation ─────────────────────────────────────────────────────


async def reactivate_member(
    ctx: AppContext,
    guild: discord.Guild,
    target: discord.Member,
    *,
    reason: str = "",
    actor: discord.Member | None = None,
) -> str:
    """Restore a member's snapshotted roles and remove ``@Inactive``.

    Returns a human-readable status message. The ticket the member opened (if
    any) is intentionally left for a moderator to close.
    """
    guild_id = guild.id

    def _fetch() -> dict | None:
        with ctx.open_db() as conn:
            return get_active_inactive(conn, guild_id, target.id)

    row = await asyncio.to_thread(_fetch)
    if not row:
        return f"❌ {target} is not currently in the inactive channel."

    stored = json.loads(row["stored_roles"])
    available_role_ids = {r.id for r in guild.roles}
    restorable_ids, missing = compute_roles_to_restore(stored, available_role_ids)
    roles_to_add = [
        r for r in (guild.get_role(rid) for rid in restorable_ids) if r is not None
    ]

    inactive_role_id = await asyncio.to_thread(_read_role_id, ctx, guild_id)
    inactive_role = guild.get_role(inactive_role_id) if inactive_role_id else None

    # Restore the real roles *before* removing @Inactive so a partial failure
    # never strands the member with neither their roles nor the Inactive role
    # (which would leave them seeing nothing but @everyone).
    try:
        if roles_to_add:
            await target.add_roles(*roles_to_add, reason=f"Reactivated: {reason}")
        if inactive_role and inactive_role in target.roles:
            await target.remove_roles(inactive_role, reason=f"Reactivated: {reason}")
    except discord.Forbidden:
        return "❌ Could not fully restore roles — missing permissions. Try again."

    inactive_id = row["id"]

    def _release() -> None:
        with ctx.open_db() as conn:
            reactivate_inactive(conn, inactive_id, reason=reason)
            write_audit(
                conn,
                guild_id=guild_id,
                action="inactive_reactivate",
                actor_id=actor.id if actor else 0,
                target_id=target.id,
                extra={"inactive_id": inactive_id, "reason": reason},
            )

    await asyncio.to_thread(_release)

    dm_embed = discord.Embed(
        title="You've Been Reactivated",
        description=(
            f"Your access to **{guild.name}** has been restored and your roles "
            f"are back." + (f"\n**Note:** {reason}" if reason else "")
        ),
        color=MOD_SUCCESS,
    )
    try:
        await target.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        pass

    await _post_inactive_audit(
        ctx,
        guild,
        discord.Embed(
            title="✅ Member Reactivated",
            description=(
                f"{target.mention} reactivated"
                + (f" by {actor.mention}" if actor else "")
                + (f"\n**Reason:** {reason}" if reason else "")
            ),
            color=MOD_SUCCESS,
        ),
    )

    note = f"\n⚠️ Could not restore {len(missing)} deleted role(s)." if missing else ""
    return f"✅ {target} has been reactivated.{note}"


# ── Small config / audit helpers ─────────────────────────────────────


def _read_channel_id(ctx: AppContext, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, "inactive_channel_id", "0", guild_id)
    try:
        return int(raw or "0")
    except ValueError:
        return 0


def _read_role_id(ctx: AppContext, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, "inactive_role_id", "0", guild_id)
    try:
        return int(raw or "0")
    except ValueError:
        return 0


async def _post_inactive_audit(
    ctx: AppContext, guild: discord.Guild, embed: discord.Embed
) -> None:
    log_ch_id = await asyncio.to_thread(_read_log_channel_id, ctx, guild.id)
    if not log_ch_id:
        return
    ch = guild.get_channel(log_ch_id)
    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass


def _read_log_channel_id(ctx: AppContext, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, "log_channel_id", "0", guild_id)
    try:
        return int(raw or "0")
    except ValueError:
        return 0
