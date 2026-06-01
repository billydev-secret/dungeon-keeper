"""Embed builders for the jail / ticket / policy / warnings subsystem.

These functions take plain Python values (ints, strings, lists of dicts)
and return ``discord.Embed`` objects. They make no network calls and run
no queries — every field is computed from the arguments, so the cog can
gather data once and these can render it without touching state.

Why factor it out:
  - The cog used to build embeds inline from DB rows; tests couldn't reach
    the formatting without a fake interaction. Now each builder takes plain
    data and the cog's job shrinks to "fetch rows → call builder → send".
  - The same policy-vote embed shape is rebuilt three times across the cog
    and ``commands/jail_commands.py``. Centralizing here means one place to
    fix when (e.g.) the "+N more" overflow rule changes.
  - ``discord.Embed.timestamp`` requires a real ``datetime``; pass one in
    and the builder uses ``datetime.now(timezone.utc)`` only as a default
    so tests can pin the value for snapshot comparisons.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import discord

from bot_modules.jail.logic import cap_mentions
from bot_modules.services.embeds import (
    MOD_INFO,
    MOD_JAIL,
    MOD_POLICY,
    MOD_SUCCESS,
    MOD_TICKET,
    MOD_WARNING,
)


# Defaults that mirror the cog's existing knobs. Kept here so tests can
# assert behavior changes when (e.g.) the page size moves without finding
# the constant baked into the cog.
DEFAULT_MAX_ELIGIBLE_MENTIONS = 25
DEFAULT_POLICIES_PAGE_SIZE = 25
DEFAULT_WARNINGS_PAGE_SIZE = 20
DEFAULT_POLICY_DESC_PREVIEW = 100

# ``MOD_JAIL`` is ``0xE74C3C`` — the same bright red used in the original
# ``commands/jail_commands.py`` for rejected-vote and timeout audit embeds.
# Reusing the named constant keeps every "rejected" indicator in sync.
_REJECTED_COLOR_INT = MOD_JAIL


# ── Mention helpers ────────────────────────────────────────────────────


def _format_mentions(ids: Sequence[int]) -> str:
    """Render an ID list as a comma-separated mention list, or ``"—"``."""
    return ", ".join(f"<@{uid}>" for uid in ids) or "—"


def _format_capped_mentions(
    ids: Sequence[int],
    *,
    max_count: int = DEFAULT_MAX_ELIGIBLE_MENTIONS,
) -> str:
    """Render mentions with an ``"+N more"`` suffix when the cap is exceeded."""
    shown, overflow = cap_mentions(list(ids), max_count=max_count)
    base = _format_mentions(shown)
    if overflow:
        return f"{base} *+{overflow} more*"
    return base


# ── Policy vote ────────────────────────────────────────────────────────


def build_policy_vote_initial_embed(
    *,
    channel_name: str,
    vote_text: str,
    eligible_ids: Sequence[int],
    max_mentions: int = DEFAULT_MAX_ELIGIBLE_MENTIONS,
) -> discord.Embed:
    """Build the very first embed posted when a policy vote opens.

    All counts are zero / "—"; only the eligible-voter "awaiting" list is
    populated. Renders the ``"+N more"`` overflow when the roster is wider
    than ``max_mentions`` so the field stays under Discord's 1024-char cap.
    """
    embed = discord.Embed(title=f"Policy Vote: {channel_name}", color=MOD_POLICY)
    embed.add_field(name="📜 Policy Text", value=vote_text, inline=False)
    embed.add_field(
        name="Votes Cast", value=f"0/{len(list(eligible_ids))}", inline=True
    )
    embed.add_field(name="Status", value="🗳️ Voting", inline=True)
    embed.add_field(name="✅ Yes", value="—", inline=False)
    embed.add_field(name="❌ No", value="—", inline=False)
    embed.add_field(name="➖ Abstain", value="—", inline=False)
    embed.add_field(
        name="⏳ Awaiting",
        value=_format_capped_mentions(eligible_ids, max_count=max_mentions),
        inline=False,
    )
    return embed


def build_policy_vote_update_embed(
    *,
    policy_title: str,
    vote_text: str,
    yes_ids: Sequence[int],
    no_ids: Sequence[int],
    abstain_ids: Sequence[int],
    awaiting_ids: Sequence[int],
    outcome: str | None = None,
) -> discord.Embed:
    """Build the running-tally embed shown after each vote is cast.

    ``outcome`` is one of:
      - ``None``     — voting still open, "🗳️ Voting" status.
      - ``"adopted"`` — green check, "✅ Adopted" status.
      - ``"rejected"`` — red x, "❌ Rejected" status.

    Mirrors the field order used in ``_handle_policy_vote`` so embed
    updates land cleanly via ``set_field_at``.
    """
    eligible_count = (
        len(list(yes_ids)) + len(list(no_ids))
        + len(list(abstain_ids)) + len(list(awaiting_ids))
    )
    voted_count = eligible_count - len(list(awaiting_ids))

    if outcome == "adopted":
        color = MOD_SUCCESS
        status = "✅ Adopted"
    elif outcome == "rejected":
        color = _REJECTED_COLOR_INT
        status = "❌ Rejected"
    else:
        color = MOD_POLICY
        status = "🗳️ Voting"

    embed = discord.Embed(title=f"Policy Vote: {policy_title}", color=color)
    embed.add_field(name="📜 Policy Text", value=vote_text or "(no text)", inline=False)
    embed.add_field(
        name="Votes Cast", value=f"{voted_count}/{eligible_count}", inline=True
    )
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="✅ Yes", value=_format_mentions(yes_ids), inline=False)
    embed.add_field(name="❌ No", value=_format_mentions(no_ids), inline=False)
    embed.add_field(name="➖ Abstain", value=_format_mentions(abstain_ids), inline=False)
    embed.add_field(name="⏳ Awaiting", value=_format_mentions(awaiting_ids), inline=False)
    return embed


# ── Policy list ────────────────────────────────────────────────────────


def build_policy_list_embed(
    policies: Sequence[Mapping[str, Any]],
    *,
    page_size: int = DEFAULT_POLICIES_PAGE_SIZE,
    desc_preview: int = DEFAULT_POLICY_DESC_PREVIEW,
) -> discord.Embed:
    """Build the ``/policy list`` embed.

    Each policy row is rendered as one embed field showing its ID, title,
    a description preview (with an ellipsis if truncated), and the
    ``<t:…:d>`` adoption timestamp. When the list is longer than
    ``page_size`` rows, a footer reports the truncation count.

    Pass ``page_size=0`` to disable pagination (used by tests that want to
    snapshot the full list).
    """
    embed = discord.Embed(title="📋 Passed Policies", color=MOD_POLICY)
    effective_size = page_size if page_size > 0 else len(policies)
    for p in policies[:effective_size]:
        passed_ts = f"<t:{int(p['passed_at'])}:d>"
        description = p.get("description") or ""
        preview = description[:desc_preview]
        ellipsis = "…" if len(description) > desc_preview else ""
        embed.add_field(
            name=f"#{p['id']} — {p['title']}",
            value=f"{preview}{ellipsis}\nPassed: {passed_ts}",
            inline=False,
        )
    if page_size > 0 and len(policies) > page_size:
        embed.set_footer(
            text=f"Showing {page_size} of {len(policies)} policies."
        )
    return embed


# ── Warnings list ──────────────────────────────────────────────────────


def build_warnings_list_embed(
    user_label: str,
    warns: Sequence[Mapping[str, Any]],
    *,
    page_size: int = DEFAULT_WARNINGS_PAGE_SIZE,
    ts_formatter=None,
) -> discord.Embed:
    """Build the ``/warnings`` list embed.

    ``warns`` is a sequence of rows in newest-first order with at least
    ``id``, ``created_at``, ``moderator_id``, ``reason``, ``revoked``,
    ``revoke_reason``.

    ``ts_formatter`` is an optional ``Callable[[float | None], str]`` — if
    not given, falls back to Discord's ``<t:…:f>`` form. Pass an identity
    formatter in tests for stable snapshots.

    The cog's count footer ("N active / M total") is always rendered.
    Truncates after ``page_size`` rows so the embed stays under Discord's
    4096-char description cap.
    """
    formatter = ts_formatter or (lambda ts: f"<t:{int(ts)}:f>" if ts else "N/A")
    lines: list[str] = []
    for w in warns:
        status = "~~Revoked~~" if w["revoked"] else "**Active**"
        dt = formatter(w["created_at"])
        line = f"#{w['id']} — {status} — {dt} — by <@{w['moderator_id']}>"
        if w["reason"]:
            line += f"\n  Reason: {w['reason']}"
        if w["revoked"] and w.get("revoke_reason"):
            line += f"\n  Revoke reason: {w['revoke_reason']}"
        lines.append(line)

    shown_lines = lines[:page_size]
    description = "\n\n".join(shown_lines)
    if len(lines) > page_size:
        description += (
            f"\n\n*…and {len(lines) - page_size} more (older). "
            "Inspect via the dashboard for the full list.*"
        )
    embed = discord.Embed(
        title=f"Warnings for {user_label}", description=description, color=MOD_WARNING,
    )
    active = sum(1 for w in warns if not w["revoked"])
    embed.set_footer(text=f"{active} active / {len(warns)} total")
    return embed


# ── Ticket panel ───────────────────────────────────────────────────────


def build_ticket_panel_embed() -> discord.Embed:
    """The static "📩 Support Tickets" embed for ``/ticket panel``."""
    return discord.Embed(
        title="📩 Support Tickets",
        description=(
            "Need help from the mod team? Click the button below to open a "
            "private ticket.\n\n"
            "A moderator will respond as soon as possible."
        ),
        color=MOD_TICKET,
    )


def build_ticket_open_embed(
    *,
    ticket_id: int,
    description: str,
    opener_mention: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the welcome embed posted inside a freshly-opened ticket."""
    embed = discord.Embed(
        title=f"Ticket #{ticket_id}",
        description=description,
        color=MOD_TICKET,
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(name="Opened by", value=opener_mention, inline=True)
    embed.add_field(name="Status", value="🟢 Open", inline=True)
    # Up-front consent notice — users should know their words will be archived.
    embed.set_footer(
        text=(
            "When this ticket is closed, the conversation is archived to "
            "the moderator transcript channel."
        )
    )
    return embed


# ── Setup wizard completion ────────────────────────────────────────────


def build_setup_step_embed(step_meta: Mapping[str, str]) -> discord.Embed:
    """Build the per-step embed for the ``/setup`` wizard.

    Takes the dict returned by ``jail.logic.setup_step_meta`` so the data
    and the View stay decoupled.
    """
    return discord.Embed(
        title=step_meta["title"],
        description=step_meta["description"],
        color=MOD_TICKET,
    )


def build_setup_complete_embed() -> discord.Embed:
    """Final "Setup Complete" embed shown after the wizard ends."""
    return discord.Embed(
        title="Setup Complete",
        description="All settings saved. Use `/config` to adjust later.",
        color=MOD_SUCCESS,
    )


# ── Mod info ──────────────────────────────────────────────────────────


def build_modinfo_embed(
    *,
    user_label: str,
    user_avatar_url: str | None,
    account_created: datetime,
    account_age_days: int,
    joined_at: datetime | None,
    xp_row: Mapping[str, Any] | None,
    watcher_count: int,
    active_jail: Mapping[str, Any] | None,
    jail_history: Sequence[Mapping[str, Any]],
    warns: Sequence[Mapping[str, Any]],
    tickets: Sequence[Mapping[str, Any]],
    last_seen_ts: float | None,
    top_channels: Sequence[Mapping[str, Any]],
    msgs_30d_total: int,
    ts_formatter=None,
) -> discord.Embed:
    """Assemble the ``/modinfo`` summary embed.

    All inputs are plain values — the cog does the DB lookups and Discord
    member resolution, then passes results in. This is the largest single
    win for cog testability: the field-by-field formatting used to live
    inline in the slash-command handler.

    ``ts_formatter`` mirrors ``build_warnings_list_embed``.
    """
    formatter = ts_formatter or (lambda ts: f"<t:{int(ts)}:f>" if ts else "N/A")

    embed = discord.Embed(title=f"Mod Info — {user_label}", color=MOD_INFO)
    if user_avatar_url:
        embed.set_thumbnail(url=user_avatar_url)

    # ── Account ──────────────────────────────────────────────────────
    created_ts = int(account_created.timestamp())
    acct_lines = f"Created: <t:{created_ts}:D> ({account_age_days}d ago)"
    if joined_at is not None:
        acct_lines += f"\nJoined: <t:{int(joined_at.timestamp())}:D>"
    embed.add_field(name="👤 Account", value=acct_lines, inline=True)

    # ── XP / Level ───────────────────────────────────────────────────
    if xp_row is not None:
        xp_text = f"Level **{xp_row['level']}** · {xp_row['total_xp']:,.0f} XP"
    else:
        xp_text = "No XP recorded"
    embed.add_field(name="⭐ Level", value=xp_text, inline=True)

    # ── Watch list ───────────────────────────────────────────────────
    if watcher_count:
        suffix = "s" if watcher_count != 1 else ""
        watch_text = f"👁 **{watcher_count} mod{suffix} watching**"
    else:
        watch_text = "Not watched"
    embed.add_field(name="🔍 Watch List", value=watch_text, inline=True)

    # ── Jail ─────────────────────────────────────────────────────────
    if active_jail is not None:
        jail_text = f"**Currently jailed** since {formatter(active_jail['created_at'])}"
        if active_jail.get("expires_at"):
            jail_text += f"\nExpires: {formatter(active_jail['expires_at'])}"
        if active_jail.get("reason"):
            jail_text += f"\nReason: {active_jail['reason']}"
    else:
        jail_text = "Not currently jailed"
    if len(jail_history) > 1 or (len(jail_history) == 1 and active_jail is None):
        past = [j for j in jail_history if j["status"] != "active"]
        jail_text += f"\n**Past jails:** {len(past)}"
        if past:
            recent = past[0]
            jail_text += (
                f"\n  Most recent: {formatter(recent['created_at'])} — "
                f"{recent.get('release_reason', '')}"
            )
    embed.add_field(name="🔒 Jail", value=jail_text, inline=False)

    # ── Warnings ─────────────────────────────────────────────────────
    active_warns = [w for w in warns if not w["revoked"]]
    warn_text = f"**Active:** {len(active_warns)} / **Total:** {len(warns)}"
    for w in active_warns[:3]:
        warn_text += (
            f"\n  #{w['id']} — {formatter(w['created_at'])} — "
            f"{w['reason'] or 'no reason'}"
        )
    embed.add_field(name="⚠️ Warnings", value=warn_text, inline=False)

    # ── Tickets ──────────────────────────────────────────────────────
    open_t = sum(1 for t in tickets if t["status"] == "open")
    closed_t = sum(1 for t in tickets if t["status"] in ("closed", "deleted"))
    ticket_text = f"**Open:** {open_t} / **Closed:** {closed_t}"
    if tickets:
        recent_ticket = tickets[0]
        ticket_text += (
            f"\n  Most recent: #{recent_ticket['id']} — "
            f"{recent_ticket['status']} — {formatter(recent_ticket['created_at'])}"
        )
    embed.add_field(name="📩 Tickets", value=ticket_text, inline=False)

    # ── Activity ─────────────────────────────────────────────────────
    last_seen = formatter(last_seen_ts) if last_seen_ts else "Never"
    ch_lines = "\n".join(
        f"<#{row['channel_id']}> — {row['cnt']} msgs" for row in top_channels
    ) or "No activity"
    embed.add_field(
        name=f"💬 Activity — {msgs_30d_total} msgs (30d)",
        value=f"Last seen: {last_seen}\n{ch_lines}",
        inline=False,
    )

    embed.set_image(url="attachment://modinfo_activity.png")
    return embed


# ── Jail and warning audit ────────────────────────────────────────────


def build_jail_audit_embed(
    *,
    target_mention: str,
    moderator_mention: str,
    duration_text: str,
    reason: str = "",
) -> discord.Embed:
    """Audit-log embed posted after a ``/jail``."""
    description = (
        f"{target_mention} jailed by {moderator_mention}\n"
        f"**Duration:** {duration_text}"
    )
    if reason:
        description += f"\n**Reason:** {reason}"
    return discord.Embed(
        title="🔒 Member Jailed", description=description, color=MOD_JAIL,
    )


def build_warning_audit_embed(
    *,
    target_mention: str,
    moderator_mention: str,
    active_count: int,
    reason: str = "",
    notes: str = "",
    source_jump_url: str | None = None,
) -> discord.Embed:
    """Build the "⚠️ Warning Issued" audit embed.

    ``source_jump_url`` is supplied by the message-context-menu path so the
    audit row can link back to the offending message; the slash-command
    path passes ``None``.
    """
    parts = [f"{target_mention} warned by {moderator_mention}"]
    if reason:
        parts.append(f"**Reason:** {reason}")
    if notes:
        parts.append(f"**Notes:** {notes}")
    parts.append(f"**Active warnings:** {active_count}")
    if source_jump_url:
        parts.append(f"[Jump to source message]({source_jump_url})")
    return discord.Embed(
        title="⚠️ Warning Issued",
        description="\n".join(parts),
        color=MOD_WARNING,
    )


def build_warning_threshold_embed(
    *,
    target_mention: str,
    active_count: int,
    admin_role_ids: Sequence[int],
) -> discord.Embed:
    """Posted when a warning pushes a member to or past the threshold."""
    pings = " ".join(f"<@&{rid}>" for rid in admin_role_ids) if admin_role_ids else ""
    return discord.Embed(
        title="🚨 Warning Threshold Reached",
        description=(
            f"{target_mention} has reached **{active_count}** active warnings.\n{pings}"
        ),
        color=MOD_JAIL,
    )


def build_warning_revoke_audit_embed(
    *,
    warning_id: int,
    target_mention: str,
    moderator_mention: str,
    active_count: int,
    reason: str = "",
) -> discord.Embed:
    """Audit-log embed posted after a ``/revokewarn``."""
    parts = [f"#{warning_id} for {target_mention} revoked by {moderator_mention}"]
    if reason:
        parts.append(f"**Reason:** {reason}")
    parts.append(f"**Active warnings:** {active_count}")
    return discord.Embed(
        title="✅ Warning Revoked",
        description="\n".join(parts),
        color=MOD_SUCCESS,
    )


# ── Policy proposal ───────────────────────────────────────────────────


def build_policy_proposal_embed(
    *,
    policy_id: int,
    title: str,
    description: str,
    proposer_mention: str,
    now: datetime | None = None,
) -> discord.Embed:
    """Build the policy-proposal embed posted when ``/policy open`` runs."""
    embed = discord.Embed(
        title=f"📋 Policy Proposal #{policy_id}: {title}",
        description=description,
        color=MOD_POLICY,
        timestamp=now or datetime.now(timezone.utc),
    )
    embed.add_field(name="Proposed by", value=proposer_mention, inline=True)
    embed.add_field(name="Status", value="💬 Open for Discussion", inline=True)
    embed.set_footer(text="Use /policy vote to start the formal vote when ready.")
    return embed


def build_policy_close_embed(
    *,
    title: str,
    moderator_mention: str,
    reason: str = "",
) -> discord.Embed:
    """Embed posted when an admin closes a policy proposal without voting."""
    embed = discord.Embed(
        title="📋 Policy Proposal Closed",
        description=f"**{title}** was closed by {moderator_mention}.",
        color=MOD_INFO,
    )
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    return embed


def build_adopted_policies_embed(
    adopted: Sequence[Mapping[str, Any]],
) -> discord.Embed:
    """Embed listing all policies that were adopted from a parent proposal."""
    embed = discord.Embed(title="Adopted Policies from This Proposal", color=MOD_SUCCESS)
    for p in adopted:
        embed.add_field(
            name=p["title"], value=p["description"][:1024], inline=False,
        )
    return embed
