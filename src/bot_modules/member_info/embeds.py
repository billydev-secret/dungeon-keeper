"""The ``/info`` embed. Plain values in, ``discord.Embed`` out.

Deliberately parallel to ``bot_modules.jail.embeds.build_modinfo_embed`` in
shape and field order, so the two read as the same card seen from two sides —
but it carries none of the mod-only fields, and the omissions are the point:

* **No watch-list count.** ``/modinfo`` shows how many mods are watching a
  member. Telling the member that is telling the target of an investigation
  they are under one.
* **No warnings, jail history or tickets.** Their own record is not shown
  here (an explicit product decision, 2026-08-22); if that ever changes it
  changes here, not by widening what the cog fetches.
"""

from __future__ import annotations

from collections.abc import Sequence

import discord

from bot_modules.member_info.logic import AccountFacts, OptInRow, displayable_roles
from bot_modules.services.embeds import (
    EMBED_FIELD_LIMIT,
    MOD_INFO,
    fit_lines,
    xp_breakdown_parts,
)


def _account_value(facts: AccountFacts) -> str:
    lines = [f"Joined: <t:{facts.joined_ts}:D>"] if facts.joined_ts else []
    lines.append(f"Account: <t:{facts.created_ts}:D> ({facts.account_age_days:,}d)")
    return "\n".join(lines)


def _level_value(facts: AccountFacts) -> str:
    if facts.level is None:
        return "No XP yet — chat a little and it starts counting."
    text = f"Level **{facts.level}** · {facts.total_xp:,.0f} XP"
    parts = xp_breakdown_parts(dict(facts.xp_by_source))
    if parts:
        text += "\n" + " · ".join(parts)
    return text


def _roles_value(facts: AccountFacts) -> str:
    """Roles as one separated line, capped by *length* as well as by count.

    Discord allows a 100-character role name, so twelve of them overrun the
    1024-byte field limit and Discord rejects the whole embed — which, past
    the command's defer(), surfaces as a card that never arrives. The count
    cap alone is not enough; names are dropped until the line fits, and the
    overflow counter absorbs whatever went.
    """
    names, overflow = displayable_roles(facts.role_names)
    if not names:
        return "None yet."

    def render(kept: list[str], dropped: int) -> str:
        text = " · ".join(kept)
        return f"{text} · *+{dropped} more*" if dropped else text

    while names and len(render(names, overflow)) > EMBED_FIELD_LIMIT:
        names.pop()
        overflow += 1
    return render(names, overflow) if names else f"*{overflow} roles*"


def _activity_value(facts: AccountFacts) -> str:
    last_seen = f"<t:{int(facts.last_seen_ts)}:R>" if facts.last_seen_ts else "—"
    lines = [f"Last seen: {last_seen}"]
    if facts.top_channels:
        lines += [
            f"<#{channel_id}> — {count:,} msgs" for channel_id, count in facts.top_channels
        ]
    else:
        lines.append("No messages recorded in the last 30 days.")
    return fit_lines(lines)


def build_member_info_embed(
    *,
    display_name: str,
    avatar_url: str | None,
    facts: AccountFacts,
    optin_rows: Sequence[OptInRow],
    wallet_line: str = "",
    wallet_extra: Sequence[str] = (),
    has_chart: bool = True,
    color: "discord.Color | None" = None,
) -> discord.Embed:
    """Assemble the ``/info`` card.

    ``wallet_line`` is empty when the guild's economy is disabled — the whole
    section then vanishes rather than rendering a zero balance for a currency
    that does not exist here.
    """
    if color is None:
        color = discord.Color(MOD_INFO)

    embed = discord.Embed(title=f"ℹ️ Your Info — {display_name}", color=color)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="👤 Account", value=_account_value(facts), inline=True)
    embed.add_field(name="⭐ Level", value=_level_value(facts), inline=True)
    embed.add_field(name="🎭 Roles", value=_roles_value(facts), inline=False)
    embed.add_field(
        name=f"💬 Activity — {facts.msgs_30d:,} msgs (30d)",
        value=_activity_value(facts),
        inline=False,
    )

    if wallet_line:
        embed.add_field(
            name="💰 Wallet",
            value=fit_lines([wallet_line, *wallet_extra]),
            inline=False,
        )

    if optin_rows:
        lines = [f"{row.emoji} **{row.label}** — {row.text}" for row in optin_rows]
        embed.add_field(name="🔔 Your Opt-ins", value=fit_lines(lines), inline=False)

    if has_chart:
        embed.set_image(url="attachment://info_activity.png")
    embed.set_footer(text="Only you can see this.")
    return embed
