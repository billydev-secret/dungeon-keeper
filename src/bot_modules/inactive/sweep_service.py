"""Sweep candidate gathering — the shared step between Discord/DB and selection.

The pure "who qualifies" decision lives in :mod:`bot_modules.inactive.logic`,
but the work of *building its inputs* — the per-member last-seen map and the
exclusion set — carries just as much policy: it is where bots, the owner,
admins, mods, exempted members and existing holds are kept out of a destructive
mass role-strip. That gathering lived in the cog while the sweep was the only
consumer. The dashboard's dry-run preview is a second consumer, and a preview
that rebuilt these rules would drift from the sweep it claims to predict — a
preview disagreeing with reality is worse than no preview — so both import this
module and neither owns a copy.

Everything here is impure (config reads, a SQLite query, guild member state).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_config_value
from bot_modules.inactive.logic import SweepCandidate, select_sweep_candidates
from bot_modules.inactive.store import active_inactive_user_ids, sweep_exempt_user_ids

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

log = logging.getLogger("dungeonkeeper.inactive")

DEFAULT_THRESHOLD_DAYS = 30
DEFAULT_CAP = 25

# Default for compute_candidates' ``cap``, distinguishing "the caller said
# nothing, use the guild's setting" from ``cap=None``, which means "no cap at
# all" — what the dashboard preview passes to list every eligible member.
USE_SAVED_CAP = -1


# ── Config helpers ────────────────────────────────────────────────────


def _int_from(conn, key: str, default: int, guild_id: int) -> int:
    """Read an int config value from an already-open connection."""
    try:
        return int(get_config_value(conn, key, str(default), guild_id))
    except (TypeError, ValueError):
        return default


def _read_int_config(ctx: AppContext, key: str, default: int, guild_id: int) -> int:
    with ctx.open_db() as conn:
        return _int_from(conn, key, default, guild_id)


def auto_sweep_enabled(ctx: AppContext, guild_id: int) -> bool:
    return _read_int_config(ctx, "inactive_auto_sweep", 0, guild_id) == 1


def read_inactive_channel_id(ctx: AppContext, guild_id: int) -> int:
    return _read_int_config(ctx, "inactive_channel_id", 0, guild_id)


# ── Candidate gathering (Discord + DB, impure) ───────────────────────


def gather_last_seen(conn, guild_id: int) -> dict[int, float]:
    """Return ``user_id -> last message timestamp`` for a guild."""
    rows = conn.execute(
        "SELECT user_id, MAX(created_at) AS last FROM processed_messages "
        "WHERE guild_id = ? GROUP BY user_id",
        (guild_id,),
    ).fetchall()
    return {r["user_id"]: r["last"] for r in rows if r["last"] is not None}


@dataclass(frozen=True)
class SweepSelection:
    """The outcome of one selection pass."""

    candidates: list[SweepCandidate]  # most-idle first
    overflow: int  # eligible members the cap dropped
    threshold_days: int
    saved_cap: int  # the guild's configured per-run cap, whatever cap was applied
    tracked_user_ids: set[int]  # who has any message history at all


async def compute_candidates(
    ctx: AppContext,
    guild: discord.Guild,
    *,
    threshold_days: int | None = None,
    cap: int | None = USE_SAVED_CAP,
) -> SweepSelection:
    """Select the members a sweep would move, for this guild's settings.

    Builds the per-member last-seen map (most recent of last-message / join so a
    fresh member who hasn't posted isn't treated as ancient) and the exclusion
    set (bots, owner, mods, admins, exempted members, already-inactive), then
    delegates the actual choice to the pure :func:`select_sweep_candidates`.

    ``threshold_days`` and ``cap`` default to the saved config; the dashboard
    preview passes its own threshold so an unsaved value can be tried out, and
    ``cap=None`` so it lists every eligible member rather than one run's worth.
    The saved cap comes back on the result either way, so a caller that lifted it
    can still say what a single run would reach.
    """
    guild_id = guild.id

    # One trip to SQLite off the event loop for everything, settings included —
    # the dashboard preview calls this from a request handler.
    def _fetch() -> tuple[dict[int, float], set[int], set[int], int, int]:
        with ctx.open_db() as conn:
            return (
                gather_last_seen(conn, guild_id),
                active_inactive_user_ids(conn, guild_id),
                sweep_exempt_user_ids(conn, guild_id),
                _int_from(conn, "inactive_threshold_days", DEFAULT_THRESHOLD_DAYS, guild_id),
                _int_from(conn, "inactive_sweep_cap", DEFAULT_CAP, guild_id),
            )

    (
        msg_last_seen,
        already_inactive,
        exempt,
        saved_threshold,
        saved_cap,
    ) = await asyncio.to_thread(_fetch)
    saved_cap = max(1, saved_cap)
    if threshold_days is None:
        threshold_days = max(1, saved_threshold)
    if cap == USE_SAVED_CAP:
        cap = saved_cap
    cfg = ctx.guild_config(guild_id)

    last_seen: dict[int, float] = {}
    exclude: set[int] = set(already_inactive) | exempt
    for m in guild.members:
        # guild_permissions isn't cached — each read rebuilds and sorts the
        # member's role list, so take it once per member rather than twice.
        perms = m.guild_permissions
        if (
            m.bot
            or m.id == guild.owner_id
            or perms.administrator
            or perms.manage_guild
            or cfg.member_is_mod(m)
            or cfg.member_is_admin(m)
        ):
            exclude.add(m.id)
            continue
        if m.joined_at is None:
            # No cached join time — don't risk sweeping a member we can't age.
            continue
        joined_ts = m.joined_at.timestamp()
        last_seen[m.id] = max(msg_last_seen.get(m.id, 0.0), joined_ts)

    now = discord.utils.utcnow().timestamp()
    candidates, overflow = select_sweep_candidates(
        last_seen=last_seen,
        now=now,
        threshold_seconds=threshold_days * 86400,
        exclude_ids=exclude,
        cap=cap,
    )
    return SweepSelection(
        candidates=candidates,
        overflow=overflow,
        threshold_days=threshold_days,
        saved_cap=saved_cap,
        tracked_user_ids=set(msg_last_seen),
    )


async def setup_inactive_channel(ctx, guild, channel) -> tuple[bool, str]:
    """Point the inactive system at ``channel`` and publish its info panel.

    Returns ``(ok, note)`` — ``note`` carries a warning worth showing even on
    success (a stale channel whose overwrite couldn't be cleared), or the reason
    on failure.

    Four things have to happen together, which is why this isn't just a config
    write: persist the choice, make sure the ``@Inactive`` role exists, give it
    access to the new channel, and *revoke* it from the old one on a re-point —
    otherwise the previous channel stays visible to inactive members forever.
    The info panel with its ticket button goes in last.

    Lifted out of ``/inactive panel`` on 2026-07-28. The dashboard's own
    Inactive Sweep page used to tell admins to go run that command; now it does
    this itself.
    """
    import asyncio

    import discord

    from bot_modules.core.branding import safe_resolve_accent
    from bot_modules.core.db_utils import get_config_value, set_config_value
    from bot_modules.commands.jail_commands import TicketPanelButton
    from bot_modules.inactive.apply import ensure_inactive_role
    from bot_modules.inactive.logic import stale_inactive_channel_id
    from bot_modules.services.embeds import MOD_INFO

    if not isinstance(channel, discord.TextChannel):
        return False, "Pick a regular text channel."

    guild_id = guild.id

    def _read_previous() -> str:
        with ctx.open_db() as conn:
            return get_config_value(conn, "inactive_channel_id", "0", guild_id) or "0"

    previous_raw = await asyncio.to_thread(_read_previous)
    stale_channel_id = stale_inactive_channel_id(previous_raw, channel.id)

    # Order matters. Everything that can fail runs before anything destructive:
    # a failure below leaves the guild exactly as it was, rather than half
    # re-pointed with the old channel's access already revoked and no working
    # replacement. So the config write and the revoke come last.
    role = await ensure_inactive_role(ctx, guild)
    if role is None:
        return False, "Missing **Manage Roles** — can't create the Inactive role."

    try:
        await channel.set_permissions(
            role, view_channel=True, send_messages=True, read_message_history=True
        )
    except discord.Forbidden:
        return False, (
            f"Couldn't grant the Inactive role access to #{channel.name} — "
            "check my channel permissions."
        )

    accent = await safe_resolve_accent(ctx, guild, log_label="sweep")
    embed = discord.Embed(
        title="💤 You're in the Inactive Channel",
        description=(
            "You've been moved here because you've been inactive for a while.\n\n"
            "**Your roles are safe** — nothing has been deleted. When you're "
            "ready to come back, just open a ticket below and a moderator will "
            "restore your access.\n\nWelcome back whenever you like!"
        ),
        color=accent or MOD_INFO,
    )
    view = discord.ui.View(timeout=None)
    view.add_item(TicketPanelButton())
    try:
        await channel.send(embed=embed, view=view)
    except discord.HTTPException:
        return False, f"Couldn't post the info panel in #{channel.name}."

    def _persist() -> None:
        with ctx.open_db() as conn:
            set_config_value(conn, "inactive_channel_id", str(channel.id), guild_id)

    await asyncio.to_thread(_persist)

    # Last, and only once the new channel is known good: take the old channel's
    # access away so it doesn't stay visible to inactive members forever. A
    # failure here is a warning, not a failure — the new channel already works.
    note = ""
    if stale_channel_id:
        old_channel = guild.get_channel(stale_channel_id)
        if old_channel is not None:
            try:
                await old_channel.set_permissions(role, overwrite=None)
            except discord.HTTPException:
                log.warning(
                    "Could not revoke @Inactive from old inactive channel %s",
                    stale_channel_id, exc_info=True,
                )
                note = (
                    f"Couldn't remove the Inactive role's access to "
                    f"<#{stale_channel_id}> — clear it by hand."
                )
    return True, note


async def run_inactive_sweep(ctx, guild, actor) -> tuple[int, int, int]:
    """Move every eligible member to the inactive channel.

    Returns ``(moved, considered, overflow)``. Selection runs through the same
    ``compute_candidates`` the dry-run preview and the auto-sweep use, so what
    this moves can't drift from what the preview showed.
    """
    from bot_modules.inactive.apply import apply_inactive

    selection = await compute_candidates(ctx, guild)
    moved = 0
    for c in selection.candidates:
        target = guild.get_member(c.user_id)
        if target is None:
            continue
        result = await apply_inactive(
            ctx, guild, target, actor, reason="Inactivity sweep", source="dashboard"
        )
        if result.ok:
            moved += 1
    return moved, len(selection.candidates), selection.overflow
