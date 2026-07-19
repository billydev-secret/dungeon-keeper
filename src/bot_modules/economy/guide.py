"""Economy guide panel — the "how it works" embed that sits in a channel.

One branded embed summarising how members earn and spend the guild's currency,
posted (and refreshed in place) by ``/bank post-guide``. The panel's channel and
message ids live in the ``econ_`` config (``guide_channel_id`` /
``guide_message_id``, same pattern as Voice Master's persistent panel) so a
repost replaces the old panel instead of stacking duplicates. Pure builder —
all Discord I/O stays in the cog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from bot_modules.economy.leaderboard import _pad

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings


def build_guide_embed(
    settings: EconSettings, *, color: discord.Color | None = None
) -> discord.Embed:
    """The member-facing how-to embed, templated on the guild's branding."""
    emoji = settings.currency_emoji
    plural = settings.currency_plural

    embed = discord.Embed(
        title=f"{emoji} {plural} — how it works",
        description=(
            f"Being active here earns you **{plural}**, which you can spend on "
            "role perks and gifts. Check your balance and recent activity any "
            "time with `/bank wallet` (only you see it)."
            "\n\u200b"
        ),
        color=color,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    # `<id:customize>` renders as a clickable "Channels & Roles" link — the
    # server's onboarding screen where members grab the economy-game role that
    # unlocks these channels. Not a real text channel, so there is no id to
    # mention; this token is the only way to point at it.
    embed.add_field(
        name="Joining",
        value=(
            "Opt in any time from <id:customize> to join the game economy."
            "\n\u200b"
        ),
        inline=False,
    )

    # What pays what, one aligned row each (same table treatment as the
    # leaderboard panel: fixed-width code cells, payment outside them). The
    # streak/booster fine print collapses into the footer.
    earn_rows = [
        ("First message of the day", f"{emoji} {settings.login_text_base}"),
        ("…after 5 min in voice", f"{emoji} {settings.login_voice_base}"),
        ("Play a server game", f"{emoji} {settings.reward_game_participation}"),
        ("Win it", f"{emoji} {settings.reward_game_win}"),
        ("Answer the QOTD", f"{emoji} {settings.reward_qotd}"),
    ]
    width = max(len(label) for label, _ in earn_rows)
    earn_lines = [
        f"`{_pad(label, width)}` {value}" for label, value in earn_rows
    ]
    earn_lines.append(
        f"Chatting earns XP all day — each night it converts into {plural} "
        "automatically. `/bank quests` adds daily and weekly goals on top."
    )
    embed.add_field(
        name="Earning",
        value="\n".join(earn_lines) + "\n\u200b",
        inline=False,
    )

    spend_rows = [
        ("/bank shop", "rent perks for your personal role — color, name, "
                       "gradient, icon (prices in the shop)"),
        ("/bank gift", "treat a friend to a role color, on your tab"),
    ]
    if settings.transfers_enabled:
        spend_rows.append(("/bank pay", f"send {plural} straight to a member"))
    width = max(len(cmd) for cmd, _ in spend_rows)
    spend_lines = [
        f"`{_pad(cmd, width)}` {text}" for cmd, text in spend_rows
    ]
    embed.add_field(name="Spending", value="\n".join(spend_lines), inline=False)

    footer_bits = [
        f"Streaks add +1/day (up to +{settings.streak_bonus_cap}), with "
        "bonuses at day 7, 30 and 100.",
    ]
    if settings.booster_multiplier > 1:
        footer_bits.append(
            f"Boosters earn ×{settings.booster_multiplier:g} on everything."
        )
    footer_bits.append(
        "Rentals renew weekly — a short grace period covers a missed renewal."
    )
    embed.set_footer(text=" ".join(footer_bits))
    return embed


def should_restick_guide(
    *,
    message_channel_id: int,
    message_id: int,
    panel_channel_id: int,
    panel_message_id: int,
) -> bool:
    """Whether a new message should push the guide panel back to the bottom.

    The panel is kept as the channel's last message by delete-and-repost
    (Discord has no reorder API), so a member message landing in its channel
    means it's no longer last. Bot messages are filtered out by the caller
    before we get here (re-sticking under our own repost self-loops), so this
    predicate only ever sees member activity; the message-id guard below stays
    as a belt-and-braces skip of the panel itself.
    """
    if not panel_channel_id or not panel_message_id:
        return False  # no panel posted yet
    if message_channel_id != panel_channel_id:
        return False  # activity in some other channel
    return message_id != panel_message_id  # skip our own panel
