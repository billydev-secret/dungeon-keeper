"""Wellness Guardian message-time enforcement.

This module owns the decide_action() decision tree (spec §4.4) and the
wellness_on_message() hook called from handlers/events.py.

Decision tree:
    if user is paused / opted-out / not a participant → ALLOW
    if user is in cooldown → ALLOW (cooldown applies to bot interactions, not messages)
    if user is in an active blackout → enforce_by_level(user.enforcement_level)
    cap_hits = caps that would be exceeded by this message
    if no cap hits → ALLOW (and increment per-cap counters)
    pick worst escalation across hits:
        overage 1 → NUDGE
        overage 2 → COOLDOWN
        overage 3+ → FRICTION

Per spec §4.3, friction means: bot deletes the message, DMs the user the
content + countdown to next allowed post. Slow mode is per-user GLOBAL
(not per-channel) — moving channels does not defeat friction.

Friction ratchet protection: when an overage triggers friction, the message
is dropped and does NOT count toward the cap. Otherwise users in friction
would never catch up.

DM failure protection: if the user has DMs disabled, we do NOT silently
delete the message. Instead we fall back to allowing the message and posting
an ephemeral note (or just allowing if even that fails) — silent message
destruction would be a trust killer.
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass

import discord

from services.wellness_service import (
    COOLDOWN_DURATION_SECONDS,
    NUDGE_SUPPRESSION_SECONDS,
    WellnessBlackout,
    WellnessCap,
    WellnessUser,
    apply_streak_violation,
    arm_slow_mode,
    can_send_away,
    get_cap_counter,
    get_slow_mode,
    get_wellness_user,
    increment_cap_counter,
    increment_cap_overage,
    is_channel_exempt,
    list_blackouts,
    list_caps,
    record_away_sent,
    set_cooldown,
    update_slow_mode_last_message,
    user_now,
    window_start_for,
    window_start_epoch,
)

log = logging.getLogger("dungeonkeeper.wellness.enforcement")


class Action(enum.IntEnum):
    """Enforcement action ordered by severity. Higher number = more strict."""
    ALLOW = 0
    NUDGE = 1
    COOLDOWN = 2
    FRICTION = 3


@dataclass
class EnforcementDecision:
    action: Action
    cap_hits: list[WellnessCap]
    blackout: WellnessBlackout | None = None
    reason: str = ""


def _enforcement_to_action(level: str) -> Action:
    """Map enforcement_level → max action allowed by user's preference."""
    if level == "gentle":
        return Action.NUDGE
    if level == "cooldown":
        return Action.COOLDOWN
    if level == "slow_mode":
        return Action.FRICTION
    # gradual: highest available; the per-window escalation handles ramp-up
    return Action.FRICTION


def _category_id_for_channel(channel) -> int:
    """Return a channel's category id, 0 if none. Threads inherit parent category."""
    parent = getattr(channel, "category", None)
    if parent is not None:
        return parent.id
    # Threads have channel.parent which has the category
    parent_chan = getattr(channel, "parent", None)
    if parent_chan is not None:
        cat = getattr(parent_chan, "category", None)
        if cat:
            return cat.id
    return 0


def _cap_applies_to_channel(cap: WellnessCap, channel) -> bool:
    if cap.scope == "global":
        return True
    if cap.scope == "channel":
        # Match either the channel itself or its parent thread
        if channel.id == cap.scope_target_id:
            return True
        parent = getattr(channel, "parent", None)
        if parent is not None and parent.id == cap.scope_target_id:
            return True
        return False
    if cap.scope == "category":
        return _category_id_for_channel(channel) == cap.scope_target_id
    return False  # voice / unknown


def _select_worst_action(level_max: Action, overage_actions: list[Action]) -> Action:
    """Pick the most-severe action allowed by the user's enforcement level."""
    worst = max(overage_actions) if overage_actions else Action.ALLOW
    return Action(min(int(worst), int(level_max)))


def decide_action(
    conn,
    user: WellnessUser,
    message: discord.Message,
    now: float,
) -> EnforcementDecision:
    """Compute the enforcement action for a single message. See module docstring."""
    if not user.is_active or user.is_paused:
        return EnforcementDecision(Action.ALLOW, [], reason="inactive")

    guild_id = message.guild.id if message.guild else 0
    channel = message.channel

    # Active blackout? Use the user's enforcement level directly.
    now_local = user_now(user.timezone)
    blackouts = list_blackouts(conn, guild_id, user.user_id)
    active_blackout: WellnessBlackout | None = None
    for b in blackouts:
        if b.is_active_at(now_local):
            active_blackout = b
            break

    if active_blackout is not None:
        action = _enforcement_to_action(user.enforcement_level)
        return EnforcementDecision(action, [], blackout=active_blackout, reason="blackout")

    # Cap evaluation
    caps = list_caps(conn, guild_id, user.user_id)
    if not caps:
        return EnforcementDecision(Action.ALLOW, [], reason="no_caps")

    exempt = is_channel_exempt(conn, guild_id, channel.id)

    # Find caps the message applies to
    applicable_caps: list[WellnessCap] = []
    for c in caps:
        if not _cap_applies_to_channel(c, channel):
            continue
        if exempt and c.exclude_exempt:
            continue
        applicable_caps.append(c)
    if not applicable_caps:
        return EnforcementDecision(Action.ALLOW, [], reason="not_applicable")

    # Increment counters and look for overages
    cap_hits: list[WellnessCap] = []
    overage_actions: list[Action] = []
    for cap in applicable_caps:
        ws = window_start_epoch(cap.window, now_local, user.daily_reset_hour)
        prev_count = get_cap_counter(conn, cap.id, ws)
        if prev_count >= cap.cap_limit:
            overage = increment_cap_overage(conn, cap.id, ws)
            cap_hits.append(cap)
            if overage == 1:
                overage_actions.append(Action.NUDGE)
            elif overage == 2:
                overage_actions.append(Action.COOLDOWN)
            else:
                overage_actions.append(Action.FRICTION)
        else:
            # Under the cap — increment normally and continue
            increment_cap_counter(conn, cap.id, ws)

    if not cap_hits:
        return EnforcementDecision(Action.ALLOW, [], reason="under_cap")

    level_max = _enforcement_to_action(user.enforcement_level)
    action = _select_worst_action(level_max, overage_actions)
    return EnforcementDecision(action, cap_hits, reason="cap_overage")


# ---------------------------------------------------------------------------
# Per-user friction tracking
# ---------------------------------------------------------------------------

def _friction_blocks_message(conn, guild_id: int, user_id: int, slow_mode_rate: int, now: float) -> tuple[bool, float]:
    """Check if active slow mode blocks this message. Returns (blocked, seconds_until_allowed)."""
    state = get_slow_mode(conn, guild_id, user_id)
    if state is None or state.active_until_ts <= now:
        return (False, 0.0)
    elapsed = now - state.last_message_ts
    if elapsed >= slow_mode_rate:
        return (False, 0.0)
    return (True, slow_mode_rate - elapsed)


def _arm_friction_for_caps(
    conn, guild_id: int, user_id: int, cap_hits: list[WellnessCap], now_local, daily_reset_hour: int,
) -> None:
    """Arm slow-mode active_until_ts to the latest window end across all hit caps."""
    latest_end = 0.0
    triggered_cap_id = 0
    triggered_window_start = 0
    for cap in cap_hits:
        start = window_start_for(cap.window, now_local, daily_reset_hour)
        if cap.window == "hourly":
            end = start.timestamp() + 3600
        elif cap.window == "daily":
            end = start.timestamp() + 86400
        else:
            end = start.timestamp() + 7 * 86400
        if end > latest_end:
            latest_end = end
            triggered_cap_id = cap.id
            triggered_window_start = int(start.timestamp())
    if latest_end > 0:
        arm_slow_mode(
            conn,
            guild_id,
            user_id,
            triggered_by_cap_id=triggered_cap_id,
            triggered_window_start=triggered_window_start,
            active_until_ts=latest_end,
        )


# ---------------------------------------------------------------------------
# DM helpers
# ---------------------------------------------------------------------------

async def _try_dm(user: discord.User | discord.Member, *, content: str | None = None,
                  embed: discord.Embed | None = None) -> bool:
    try:
        kwargs: dict = {}
        if content:
            kwargs["content"] = content
        if embed:
            kwargs["embed"] = embed
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


def _format_seconds(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}:{sec:02d}"
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}"


# ---------------------------------------------------------------------------
# Top-level message hook
# ---------------------------------------------------------------------------

async def wellness_on_message(ctx, message: discord.Message) -> bool:
    """Hook called from handlers/events.py:127. Returns True if the message
    was consumed (deleted, away-replied, etc.) and the host handler should stop.
    """
    if message.author.bot or message.guild is None:
        return False

    guild = message.guild
    author = message.author

    # 0. Away-mention interception — fires regardless of whether the AUTHOR is opted-in.
    # The away replies are non-destructive (they just post a notice in-channel) so
    # they never consume the message.
    try:
        await _handle_away_mentions(ctx, message)
    except Exception:
        log.exception("wellness: away-mention handler failed")

    try:
        with ctx.open_db() as conn:
            user = get_wellness_user(conn, guild.id, author.id)
            if user is None or not user.is_active or user.is_paused:
                return False

            now = time.time()

            # 1. Slow mode pre-check (friction already armed by previous overage)
            blocked, wait_seconds = _friction_blocks_message(
                conn, guild.id, author.id, user.slow_mode_rate_seconds, now,
            )
            if blocked:
                # Try to delete + DM. If DM fails, do NOT delete.
                if not _bot_can_manage_messages(message):
                    return False
                dm_ok = await _try_dm(
                    author,
                    embed=discord.Embed(
                        title="🐢 Slow mode is active",
                        description=(
                            f"Your message was held. You can post again in **{_format_seconds(wait_seconds)}**.\n\n"
                            f"Your message: *{_truncate(message.content, 1500)}*\n\n"
                            "*Adjust your settings anytime with `/wellness settings`.*"
                        ),
                        color=discord.Color.from_str("#7BC97B"),
                    ),
                )
                if not dm_ok:
                    log.info("wellness: friction skipped (DM closed) for user %s", author.id)
                    return False
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound):
                    return False
                return True
            else:
                # Friction is dormant — record this message timestamp so the next post starts the rate limit
                state = get_slow_mode(conn, guild.id, author.id)
                if state is not None and state.active_until_ts > now:
                    update_slow_mode_last_message(conn, guild.id, author.id, now)

            # 2. Cap evaluation + escalation
            decision = decide_action(conn, user, message, now)

            if decision.action == Action.ALLOW:
                return False

            now_local = user_now(user.timezone)

            # Streak violation: any cap-overage or blackout-triggered enforcement counts as a slip
            today_iso = now_local.date().isoformat()
            try:
                apply_streak_violation(conn, guild.id, author.id, today_iso)
            except Exception:
                log.exception("wellness: streak violation update failed")

            if decision.action == Action.NUDGE:
                await _send_nudge(ctx, conn, message, user, decision)
                return False

            if decision.action == Action.COOLDOWN:
                set_cooldown(conn, guild.id, author.id, now + COOLDOWN_DURATION_SECONDS)
                await _send_cooldown(ctx, message, user, decision)
                return False

            if decision.action == Action.FRICTION:
                if not _bot_can_manage_messages(message):
                    # Degrade to nudge if we can't actually delete messages
                    await _send_nudge(ctx, conn, message, user, decision)
                    return False
                # Arm slow mode for the duration of the cap window(s)
                _arm_friction_for_caps(
                    conn, guild.id, author.id, decision.cap_hits, now_local, user.daily_reset_hour,
                )
                # Try to DM first; only delete if DM succeeded
                dm_ok = await _try_dm(
                    author,
                    embed=discord.Embed(
                        title="🐢 Slow mode is now active",
                        description=(
                            f"You've gone over your cap a few times — slow mode is on so you can keep posting at a calmer pace.\n\n"
                            f"You can post again in **{_format_seconds(user.slow_mode_rate_seconds)}**.\n\n"
                            f"Your message: *{_truncate(message.content, 1500)}*\n\n"
                            "*Adjust your settings anytime with `/wellness settings`.*"
                        ),
                        color=discord.Color.from_str("#7BC97B"),
                    ),
                )
                if not dm_ok:
                    log.info("wellness: friction skipped (DM closed) for user %s", author.id)
                    return False
                try:
                    await message.delete()
                except (discord.Forbidden, discord.NotFound):
                    return False
                # Record this as the last message ts for the rate limit
                update_slow_mode_last_message(conn, guild.id, author.id, now)
                return True
    except Exception:
        log.exception("wellness_on_message: unexpected error; allowing message")
        return False

    return False


def _bot_can_manage_messages(message: discord.Message) -> bool:
    if message.guild is None or message.guild.me is None:
        return False
    perms = message.channel.permissions_for(message.guild.me)
    return perms.manage_messages


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return "(no text)"
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _format_cap_summary(cap: WellnessCap, count: int) -> str:
    return f"{count}/{cap.cap_limit} {cap.window}"


async def _send_nudge(ctx, conn, message: discord.Message, user: WellnessUser, decision: EnforcementDecision) -> None:
    """Send a 'heads up' message via the user's notifications_pref. Suppress if recently nudged."""
    now = time.time()
    if user.last_nudge_at and (now - user.last_nudge_at) < NUDGE_SUPPRESSION_SECONDS:
        return
    conn.execute(
        "UPDATE wellness_users SET last_nudge_at = ? WHERE guild_id = ? AND user_id = ?",
        (now, message.guild.id if message.guild else 0, message.author.id),
    )

    cap = decision.cap_hits[0] if decision.cap_hits else None
    if cap is None:
        return
    desc = (
        f"💛 Heads up — you've hit your cap of **{cap.cap_limit}** {cap.window} messages. "
        "No worries, just keeping you in the loop. You're doing great!"
    )
    await _deliver_user_notice(message, user, desc)


async def _send_cooldown(ctx, message: discord.Message, user: WellnessUser, decision: EnforcementDecision) -> None:
    desc = (
        "☕ Time for a 5-minute breather. "
        "Stretch, hydrate, look out a window. You can keep posting — this is just a gentle pause."
    )
    await _deliver_user_notice(message, user, desc)


async def _deliver_user_notice(message: discord.Message, user: WellnessUser, text: str) -> None:
    """Deliver per the user's notifications_pref (ephemeral / dm / both)."""
    pref = user.notifications_pref
    sent_ephemeral = False
    if pref in ("ephemeral", "both"):
        # We can't send ephemeral replies from on_message — fallback to a non-ephemeral channel reply
        try:
            sent = await message.channel.send(
                f"{message.author.mention} {text}",
                allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
                delete_after=30,
            )
            sent_ephemeral = sent is not None
        except (discord.Forbidden, discord.HTTPException):
            sent_ephemeral = False
    if pref in ("dm", "both") or not sent_ephemeral:
        await _try_dm(message.author, content=text)


# ---------------------------------------------------------------------------
# Away-mention auto-reply (spec §4.6)
# ---------------------------------------------------------------------------

AWAY_DEFAULT_TEXT = (
    "I'm taking a wellness break right now and may not see this for a while. "
    "I'll get back to you when I'm back. 💚"
)


async def _handle_away_mentions(ctx, message: discord.Message) -> None:
    """If the message @-mentions any wellness users with away mode on, post
    an auto-reply in-channel (rate-limited per channel)."""
    if not message.mentions:
        return
    if message.guild is None:
        return
    # Skip if the channel doesn't allow us to send messages
    me = message.guild.me
    if me is None:
        return
    perms = message.channel.permissions_for(me)
    if not perms.send_messages:
        return

    guild_id = message.guild.id
    now = time.time()
    seen: set[int] = set()
    away_targets: list[tuple[discord.User | discord.Member, WellnessUser]] = []
    with ctx.open_db() as conn:
        for mentioned in message.mentions:
            if mentioned.bot or mentioned.id == message.author.id:
                continue
            if mentioned.id in seen:
                continue
            seen.add(mentioned.id)
            other = get_wellness_user(conn, guild_id, mentioned.id)
            if other is None or not other.is_active:
                continue
            if not other.away_enabled:
                continue
            if not can_send_away(conn, guild_id, mentioned.id, message.channel.id, now):
                continue
            away_targets.append((mentioned, other))
        # Reserve rate-limit slots immediately so a flood of mentions can't burst past it
        for mentioned, _other in away_targets:
            record_away_sent(conn, guild_id, mentioned.id, message.channel.id, now)

    if not away_targets:
        return

    for mentioned, other in away_targets:
        text = (other.away_message or AWAY_DEFAULT_TEXT).strip()
        embed = discord.Embed(
            title=f"💚 {mentioned.display_name} is away",
            description=text,
            color=discord.Color.from_str("#7BC97B"),
        )
        embed.set_footer(text="Wellness Guardian — auto-reply")
        try:
            await message.channel.send(
                content=message.author.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author], roles=False, everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.debug("wellness: failed to post away reply for %s in #%s",
                      mentioned.id, getattr(message.channel, 'name', '?'))
