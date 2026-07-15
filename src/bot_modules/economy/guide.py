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

if TYPE_CHECKING:
    from bot_modules.services.economy_service import EconSettings


def build_guide_embed(
    settings: EconSettings, *, colour: discord.Colour | None = None
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
        ),
        colour=colour,
    )
    if settings.currency_icon_url:
        embed.set_thumbnail(url=settings.currency_icon_url)

    # `<id:customize>` renders as a clickable "Channels & Roles" link — the
    # server's onboarding screen where members grab the economy-game role that
    # unlocks these channels. Not a real text channel, so there is no id to
    # mention; this token is the only way to point at it.
    embed.add_field(
        name="Joining",
        value="Opt in any time from <id:customize> to join the game economy.",
        inline=False,
    )

    earn_lines = [
        (
            f"**Show up daily** — your first message of the day pays "
            f"{emoji} {settings.login_text_base}, or {emoji} "
            f"{settings.login_voice_base} if you hang out in voice for 5 "
            f"minutes first. Streaks add +1 per day (up to "
            f"+{settings.streak_bonus_cap}), with bonuses at day 7, 30 and 100."
        ),
        (
            f"**Chat & hang out** — everyday activity earns XP, and each "
            f"night your day's XP converts into {plural} automatically."
        ),
        (
            "**Quests** — `/bank quests` shows the current daily, weekly and "
            "community goals and lets you claim your rewards."
        ),
        (
            f"**Games & QOTD** — playing server games pays "
            f"{emoji} {settings.reward_game_participation}, winning "
            f"{emoji} {settings.reward_game_win}, and answering the question "
            f"of the day {emoji} {settings.reward_qotd}."
        ),
    ]
    if settings.booster_multiplier > 1:
        earn_lines.append(
            f"**Boosters** earn ×{settings.booster_multiplier:g} on everything."
        )
    embed.add_field(name="Earning", value="\n".join(earn_lines), inline=False)

    spend_lines = [
        (
            "`/bank shop` — rent perks for your own personal role (colour, "
            "name, gradient, icon), then style it from the shop's customise "
            "buttons. Prices and renewal terms are shown in the shop."
        ),
        "`/bank gift` — treat a friend to a custom role colour, on your tab.",
    ]
    if settings.transfers_enabled:
        spend_lines.append(
            f"`/bank pay` — send your {plural} straight to another member."
        )
    embed.add_field(name="Spending", value="\n".join(spend_lines), inline=False)

    embed.set_footer(
        text=(
            "Rentals renew weekly — if your balance can't cover a renewal you "
            "get a short grace period before the perk lapses."
        )
    )
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
