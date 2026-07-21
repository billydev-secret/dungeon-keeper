"""Inactive-channel moderation cog.

Moves inactive members to a single shared inactive channel: their roles are
snapshotted and stripped, they get the ``@Inactive`` role (which can only see
the inactive channel), and a persistent panel there invites them to open a
ticket to be reactivated. Mirrors the jail cog's patterns but is deliberately
smaller — no per-user channels, transcripts, or policy machinery.

Two ways in:

* ``/inactive mark @user`` — manual, mirrors ``/jail``.
* Automatic sweep — a background loop (opt-in via the web dashboard's
  Inactive Sweep panel) that moves members idle past a configurable
  threshold. The sweep is a
  destructive mass role-strip, so it never touches bots/mods/admins/the owner,
  is hard-capped per run, and ``/inactive sweep`` defaults to a dry-run preview.

One way out: ``/inactive release @user`` restores roles and removes ``@Inactive``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from bot_modules.commands.jail_commands import (
    TicketPanelButton,
    _is_admin,
    _is_mod,
)
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import get_config_value, set_config_value
from bot_modules.inactive.apply import (
    apply_inactive,
    check_inactive_preconditions,
    ensure_inactive_role,
    reactivate_member,
)
from bot_modules.inactive.logic import select_sweep_candidates
from bot_modules.inactive.store import active_inactive_user_ids
from bot_modules.services.embeds import MOD_INFO

if TYPE_CHECKING:
    from bot_modules.core.app_context import AppContext, Bot

log = logging.getLogger("dungeonkeeper.inactive")

_DEFAULT_THRESHOLD_DAYS = 30
_DEFAULT_CAP = 25
_SWEEP_INTERVAL_SECONDS = 6 * 3600  # background loop cadence


# ── Config helpers ────────────────────────────────────────────────────


def _read_int(ctx: AppContext, key: str, default: int, guild_id: int) -> int:
    with ctx.open_db() as conn:
        raw = get_config_value(conn, key, str(default), guild_id)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _threshold_days(ctx: AppContext, guild_id: int) -> int:
    return max(1, _read_int(ctx, "inactive_threshold_days", _DEFAULT_THRESHOLD_DAYS, guild_id))


def _sweep_cap(ctx: AppContext, guild_id: int) -> int:
    return max(1, _read_int(ctx, "inactive_sweep_cap", _DEFAULT_CAP, guild_id))


def _auto_enabled(ctx: AppContext, guild_id: int) -> bool:
    return _read_int(ctx, "inactive_auto_sweep", 0, guild_id) == 1


def _inactive_channel_id(ctx: AppContext, guild_id: int) -> int:
    return _read_int(ctx, "inactive_channel_id", 0, guild_id)


# ── Sweep candidate gathering (Discord + DB, impure) ─────────────────


def _gather_last_seen(conn, guild_id: int) -> dict[int, float]:
    """Return ``user_id -> last message timestamp`` for a guild."""
    rows = conn.execute(
        "SELECT user_id, MAX(created_at) AS last FROM processed_messages "
        "WHERE guild_id = ? GROUP BY user_id",
        (guild_id,),
    ).fetchall()
    return {r["user_id"]: r["last"] for r in rows if r["last"] is not None}


async def _compute_candidates(ctx: AppContext, guild: discord.Guild):
    """Return ``(candidates, overflow, threshold_days)`` for the guild.

    Builds the per-member last-seen map (most recent of last-message / join so a
    fresh member who hasn't posted isn't treated as ancient) and the exclusion
    set (bots, owner, mods, admins, already-inactive), then delegates the actual
    selection to the pure :func:`select_sweep_candidates`.
    """
    guild_id = guild.id
    threshold_days = _threshold_days(ctx, guild_id)
    cap = _sweep_cap(ctx, guild_id)

    def _fetch() -> tuple[dict[int, float], set[int]]:
        with ctx.open_db() as conn:
            return _gather_last_seen(conn, guild_id), active_inactive_user_ids(conn, guild_id)

    msg_last_seen, already_inactive = await asyncio.to_thread(_fetch)
    cfg = ctx.guild_config(guild_id)

    last_seen: dict[int, float] = {}
    exclude: set[int] = set(already_inactive)
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
    return candidates, overflow, threshold_days


# ── Cog ───────────────────────────────────────────────────────────────


class InactiveCog(commands.Cog):
    inactive = app_commands.Group(
        name="inactive", description="Inactive-channel management."
    )

    def __init__(self, bot: Bot, ctx: AppContext) -> None:
        self.bot = bot
        self.ctx = ctx
        super().__init__()

    async def cog_load(self) -> None:
        # The panel reuses the ticket system's persistent "Open Ticket" button,
        # which JailCog already registers via add_dynamic_items — no need to
        # re-register it here. Start the auto-sweep background loop.
        self.bot.startup_task_factories.append(
            lambda: inactive_sweep_loop(self.bot, self.ctx)
        )

    # ── /inactive mark ────────────────────────────────────────────────

    @inactive.command(name="mark", description="Move a member to the inactive channel.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to move", reason="Optional note")
    async def inactive_mark(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("❌ Mod only.", ephemeral=True)
            return

        if not _inactive_channel_id(ctx, guild.id):
            await interaction.response.send_message(
                "❌ No inactive channel is set up yet. Run `/inactive panel` first so "
                "moved members have somewhere to land.",
                ephemeral=True,
            )
            return

        precheck = check_inactive_preconditions(ctx, guild, user, member)
        if precheck is not None:
            await interaction.response.send_message(
                precheck.error_message or "❌ Cannot move this user.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        result = await apply_inactive(
            ctx, guild, user, member, reason=reason or "", source="command"
        )
        if not result.ok:
            await interaction.followup.send(
                result.error_message or "❌ Failed to move user.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"💤 {user} moved to the inactive channel. Their roles are saved.",
            ephemeral=True,
        )

    # ── /reactivate (top-level for discoverability, like /unjail) ──────

    @inactive.command(name="release", description="Reactivate a member and restore their roles.")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(user="Member to reactivate", reason="Release reason")
    async def inactive_release(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_mod(member, ctx):
            await interaction.response.send_message("❌ Mod only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        result = await reactivate_member(ctx, guild, user, reason=reason or "", actor=member)
        await interaction.followup.send(result, ephemeral=True)

    # ── /inactive panel ───────────────────────────────────────────────

    @inactive.command(
        name="panel",
        description="Set up the inactive channel and post its info/ticket panel.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="The channel to use as the inactive channel")
    async def inactive_panel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_admin(member, ctx):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # Persist the channel choice, then ensure the @Inactive role exists and
        # can view this channel (create it now so the grant below lands on it).
        await asyncio.to_thread(
            _set_config, ctx, "inactive_channel_id", str(channel.id), guild.id
        )
        role = await ensure_inactive_role(ctx, guild)
        if role is None:
            await interaction.followup.send(
                "❌ Missing **Manage Roles** — can't create the Inactive role.",
                ephemeral=True,
            )
            return
        try:
            await channel.set_permissions(
                role, view_channel=True, send_messages=True, read_message_history=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"❌ Couldn't grant the Inactive role access to {channel.mention} — "
                "check my channel permissions.",
                ephemeral=True,
            )
            return

        accent = await resolve_accent_color(ctx.db_path, guild)
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
        await channel.send(embed=embed, view=view)
        await interaction.followup.send(
            f"✅ Inactive channel set to {channel.mention} and panel posted.",
            ephemeral=True,
        )

    # ── /inactive sweep ───────────────────────────────────────────────

    @inactive.command(
        name="sweep",
        description="Preview (or run) an inactivity sweep. Defaults to a dry run.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        apply="Set true to actually move the members. Omit for a dry-run preview."
    )
    async def inactive_sweep(
        self, interaction: discord.Interaction, apply: bool = False
    ) -> None:
        ctx = self.ctx
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not _is_admin(member, ctx):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return

        if not _inactive_channel_id(ctx, guild.id):
            await interaction.response.send_message(
                "❌ No inactive channel is set up yet. Run `/inactive panel` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        candidates, overflow, threshold_days = await _compute_candidates(ctx, guild)

        if not candidates:
            await interaction.followup.send(
                f"No members are inactive past **{threshold_days} days**.",
                ephemeral=True,
            )
            return

        lines = []
        for c in candidates[:20]:
            idle_days = int(c.idle_seconds // 86400)
            lines.append(f"• <@{c.user_id}> — idle {idle_days}d")
        listing = "\n".join(lines)
        if len(candidates) > 20:
            listing += f"\n…and {len(candidates) - 20} more"
        overflow_note = (
            f"\n\n⚠️ {overflow} more member(s) qualify but were held back by the "
            f"per-run cap." if overflow else ""
        )

        if not apply:
            await interaction.followup.send(
                f"**Dry run** — {len(candidates)} member(s) idle past "
                f"**{threshold_days} days** would be moved:\n{listing}{overflow_note}"
                f"\n\nRe-run with `apply: true` to move them.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        moved = 0
        for c in candidates:
            target = guild.get_member(c.user_id)
            if target is None:
                continue
            result = await apply_inactive(
                ctx, guild, target, member, reason="Inactivity sweep", source="command"
            )
            if result.ok:
                moved += 1
        await interaction.followup.send(
            f"✅ Moved **{moved}** member(s) to the inactive channel.{overflow_note}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

def _set_config(ctx: AppContext, key: str, value: str, guild_id: int) -> None:
    with ctx.open_db() as conn:
        set_config_value(conn, key, value, guild_id)


# ── Background auto-sweep loop ─────────────────────────────────────────


async def inactive_sweep_loop(bot: discord.Client, ctx: AppContext) -> None:
    """Move idle members to the inactive channel when auto-sweep is enabled.

    Runs every few hours but does nothing unless ``inactive_auto_sweep`` is on
    for the home guild. Respects the per-run cap and the same exclusions as the
    manual sweep (bots/mods/admins/owner/already-inactive).
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            guild = bot.get_guild(ctx.guild_id)
            if (
                guild is not None
                and guild.me is not None
                and _auto_enabled(ctx, guild.id)
                and _inactive_channel_id(ctx, guild.id)
            ):
                candidates, overflow, _ = await _compute_candidates(ctx, guild)
                moved = 0
                for c in candidates:
                    target = guild.get_member(c.user_id)
                    if target is None:
                        continue
                    result = await apply_inactive(
                        ctx, guild, target, guild.me, reason="Auto inactivity sweep",
                        source="auto",
                    )
                    if result.ok:
                        moved += 1
                if moved:
                    log.info(
                        "Auto-swept %d member(s) to inactive in guild %s (%d held by cap)",
                        moved, guild.id, overflow,
                    )
        except Exception:
            log.exception("Error in inactive sweep loop")
        await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)


async def setup(bot: Bot) -> None:
    await bot.add_cog(InactiveCog(bot, bot.ctx))
