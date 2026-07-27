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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import get_config_value
from bot_modules.inactive.logic import SweepCandidate, select_sweep_candidates
from bot_modules.inactive.store import active_inactive_user_ids, sweep_exempt_user_ids

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext

DEFAULT_THRESHOLD_DAYS = 30
DEFAULT_CAP = 25

# Effectively "no cap" for the dashboard preview, which lists every eligible
# member rather than one run's worth. Far above any real guild's member count,
# and still a positive int — select_sweep_candidates treats cap <= 0 as "select
# nobody", so 0 would silently empty the preview.
UNCAPPED = 1_000_000


# ── Config helpers ────────────────────────────────────────────────────


def read_int_config(ctx: AppContext, key: str, default: int, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, key, str(default), guild_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def read_threshold_days(ctx: AppContext, guild_id: int) -> int:
    return max(
        1, read_int_config(ctx, "inactive_threshold_days", DEFAULT_THRESHOLD_DAYS, guild_id)
    )


def read_sweep_cap(ctx: AppContext, guild_id: int) -> int:
    return max(1, read_int_config(ctx, "inactive_sweep_cap", DEFAULT_CAP, guild_id))


def auto_sweep_enabled(ctx: AppContext, guild_id: int) -> bool:
    return read_int_config(ctx, "inactive_auto_sweep", 0, guild_id) == 1


def read_inactive_channel_id(ctx: AppContext, guild_id: int) -> int:
    return read_int_config(ctx, "inactive_channel_id", 0, guild_id)


def read_inactive_role_id(ctx: AppContext, guild_id: int) -> int:
    """Return the configured ``@Inactive`` role id, or 0 if setup hasn't run.

    Deliberately a plain read rather than :func:`apply.ensure_inactive_role`:
    the preview must never create a role as a side effect of being looked at.
    """
    return read_int_config(ctx, "inactive_role_id", 0, guild_id)


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

    candidates: list[SweepCandidate]
    overflow: int  # eligible members the cap dropped
    threshold_days: int
    cap: int
    tracked_user_ids: set[int]  # who has any message history at all


async def compute_candidates(
    ctx: AppContext,
    guild: discord.Guild,
    *,
    threshold_days: int | None = None,
    cap: int | None = None,
) -> SweepSelection:
    """Select the members a sweep would move, for this guild's settings.

    Builds the per-member last-seen map (most recent of last-message / join so a
    fresh member who hasn't posted isn't treated as ancient) and the exclusion
    set (bots, owner, mods, admins, exempted members, already-inactive), then
    delegates the actual choice to the pure :func:`select_sweep_candidates`.

    ``threshold_days`` and ``cap`` default to the saved config. The dashboard
    preview passes its own so an unsaved threshold can be tried out, and
    ``cap=UNCAPPED`` so it can list every eligible member rather than one run's
    worth — the real sweeps pass neither.
    """
    guild_id = guild.id
    if threshold_days is None:
        threshold_days = read_threshold_days(ctx, guild_id)
    if cap is None:
        cap = read_sweep_cap(ctx, guild_id)

    def _fetch() -> tuple[dict[int, float], set[int], set[int]]:
        with ctx.open_db() as conn:
            return (
                gather_last_seen(conn, guild_id),
                active_inactive_user_ids(conn, guild_id),
                sweep_exempt_user_ids(conn, guild_id),
            )

    msg_last_seen, already_inactive, exempt = await asyncio.to_thread(_fetch)
    cfg = ctx.guild_config(guild_id)

    last_seen: dict[int, float] = {}
    exclude: set[int] = set(already_inactive) | exempt
    for m in guild.members:
        if (
            m.bot
            or m.id == guild.owner_id
            or m.guild_permissions.administrator
            or m.guild_permissions.manage_guild
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
        cap=cap,
        tracked_user_ids=set(msg_last_seen),
    )
