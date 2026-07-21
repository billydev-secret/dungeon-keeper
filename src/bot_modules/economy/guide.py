"""Economy guide panel — the "how it works" embed that sits in a channel.

One branded embed summarising how members earn and spend the guild's currency,
posted (and refreshed in place) by ``/bank post-guide``. The panel's channel and
message ids live in the ``econ_`` config (``guide_channel_id`` /
``guide_message_id``, same pattern as Voice Master's persistent panel) so a
repost replaces the old panel instead of stacking duplicates.

The panel also carries the economy's one member-facing self-service control: a
**persistent** 🔔 Notifications button that toggles the opt-in role
(``game_role_id``) on the clicker. Its ``custom_id`` is static — there is no
per-message state to carry — so the cog re-registers a bare ``GuideView`` with
``bot.add_view`` at load and clicks on the existing panel still route after a
restart. That role is a DM preference only: it gates no channel and no payout,
so opting out costs a member nothing but their DMs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.economy.leaderboard import _pad
from bot_modules.economy.logic import resolve_notify_toggle
from bot_modules.services.economy_service import load_econ_settings

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot
    from bot_modules.services.economy_service import EconSettings

log = logging.getLogger("dungeonkeeper.economy")

NOTIFY_CUSTOM_ID = "econ_guide_notify"

NOTIFY_ON_MSG = (
    "🔔 Notifications **on** — your daily streak digest and raffle wins will "
    "come to your DMs. Click again to turn them off."
)
NOTIFY_OFF_MSG = (
    "🔕 Notifications **off** — you'll still earn and spend exactly as before, "
    "you just won't get the DMs. Click again to turn them back on."
)
NOTIFY_UNCONFIGURED_MSG = (
    "Notifications aren't set up in this server yet — ask a mod to pick the "
    "economy notification role on the dashboard."
)
NOTIFY_FAILED_MSG = (
    "I couldn't update your roles just now — my role may sit below the "
    "notification role. Ask a mod to check, then try again."
)


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

    # The opt-in role is a DM preference, nothing more — it gates no channel
    # and no payout — so this field promises notifications, not access. (It
    # used to point at <id:customize>, back when that role doubled as the
    # onboarding gate hiding these channels.)
    embed.add_field(
        name="🔔 Notifications",
        value=(
            "Hit **🔔 Notifications** below to have your daily streak digest "
            "and raffle wins DMed to you. Toggle it off any time — it only "
            "changes your DMs, never what you can see or earn."
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
        ("Post in the Photo Challenge", f"{emoji} {settings.reward_photo_post}"),
    ]
    width = max(len(label) for label, _ in earn_rows)
    earn_lines = [
        f"`{_pad(label, width)}` {value}" for label, value in earn_rows
    ]
    # The XP→coin conversion line only holds when the faucet is on (rate > 0);
    # it ships off, so promise it only when a guild has re-enabled it.
    if settings.xp_per_coin > 0:
        earn_lines.append(
            f"Chatting earns XP all day — each night it converts into {plural} "
            "automatically. `/bank quests` adds daily and weekly goals on top."
        )
    else:
        earn_lines.append(
            "`/bank quests` adds daily and weekly goals — the surest way to earn."
        )
    embed.add_field(
        name="💰 Earning",
        value="\n".join(earn_lines) + "\n\u200b",
        inline=False,
    )

    spend_rows = [
        ("/bank shop", "rent perks for your personal role — color, name, "
                       "gradient, icon (prices in the shop)"),
        ("/bank gift", "treat a friend to any perk, on your tab"),
    ]
    if settings.transfers_enabled:
        spend_rows.append(("/bank pay", f"send {plural} straight to a member"))
    width = max(len(cmd) for cmd, _ in spend_rows)
    spend_lines = [
        f"`{_pad(cmd, width)}` {text}" for cmd, text in spend_rows
    ]
    embed.add_field(name="🛍️ Spending", value="\n".join(spend_lines), inline=False)

    footer_bits = [
        f"Streaks add +1/day (up to +{settings.streak_bonus_cap}), with "
        "bonuses at day 7, 30 and 100.",
    ]
    if settings.price_streak_shield > 0:
        footer_bits.append(
            "One missed day is forgiven each week; a 🛡️ shield from the shop "
            "covers one more."
        )
    if settings.booster_multiplier > 1:
        footer_bits.append(
            f"Boosters earn ×{settings.booster_multiplier:g} on everything."
        )
    if settings.demurrage_rate_pct > 0:
        footer_bits.append(
            f"A weekly 🐉 hoard tax collects {settings.demurrage_rate_pct}% of "
            f"anything above {settings.demurrage_threshold:,} {plural} — "
            "spending keeps a balance safe."
        )
    footer_bits.append(
        "Rentals renew weekly — a short grace period covers a missed renewal."
    )
    embed.set_footer(text=" ".join(footer_bits))
    return embed


async def _safe_ephemeral(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral note whether or not the interaction was answered."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.debug("econ guide: failed to send ephemeral note", exc_info=True)


class GuideNotifyButton(discord.ui.Button):
    """Persistent 🔔 toggle for the economy's opt-in DM role.

    One button serves everyone, so its label can't reflect per-member state;
    the click answers ephemerally with whichever way it just flipped.
    """

    def __init__(self) -> None:
        super().__init__(
            label="Notifications",
            emoji="🔔",
            style=discord.ButtonStyle.secondary,
            custom_id=NOTIFY_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await _safe_ephemeral(interaction, "This only works in a server.")
            return

        bot = cast("Bot", interaction.client)

        def _read() -> EconSettings:
            with bot.ctx.open_db() as conn:
                return load_econ_settings(conn, guild.id)

        settings = await asyncio.to_thread(_read)
        action = resolve_notify_toggle(
            role_id=settings.game_role_id,
            member_role_ids={r.id for r in member.roles},
        )
        if action == "unconfigured":
            await _safe_ephemeral(interaction, NOTIFY_UNCONFIGURED_MSG)
            return

        role = guild.get_role(settings.game_role_id)
        if role is None:
            # Configured but deleted since — same dead end as unconfigured
            # from the member's side, so it reads the same.
            await _safe_ephemeral(interaction, NOTIFY_UNCONFIGURED_MSG)
            return

        try:
            if action == "grant":
                await member.add_roles(role, reason="Economy notifications opt-in")
            else:
                await member.remove_roles(role, reason="Economy notifications opt-out")
        except discord.HTTPException:
            log.debug("econ guide: notify toggle failed", exc_info=True)
            await _safe_ephemeral(interaction, NOTIFY_FAILED_MSG)
            return

        await _safe_ephemeral(
            interaction, NOTIFY_ON_MSG if action == "grant" else NOTIFY_OFF_MSG
        )


class GuideView(discord.ui.View):
    """The persistent view attached to the guide panel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(GuideNotifyButton())


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
