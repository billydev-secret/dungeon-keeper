"""Coin Drops loop — posts random drops; a persistent Claim button settles them.

Registered as a startup task factory (see ``__main__.py``). Each ~1-minute
tick expires overdue drops, then walks the guilds: a guild whose faucet is
live (``drops_configured``) gets a jittered next-drop time, and when it comes
due the pouch is posted — unless the guild already holds an open pouch, the
channel is mid-game, or nobody has spoken since our own last message (a drop
should land in conversation, not echo into the void; the due time simply
stands until someone talks).

Claims are a :class:`DropClaimButton` press — a ``DynamicItem`` whose
``custom_id`` carries the drop id, so it needs no state and survives
restarts (registered in ``__main__.py``). The DB's conditional UPDATE
(`try_claim_drop`) arbitrates who actually wins; losers get an ephemeral
"too slow".
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import TYPE_CHECKING, cast

import discord

from bot_modules.chat_revive.actions import channel_is_busy
from bot_modules.core.branding import resolve_accent_color
from bot_modules.core.db_utils import open_db
from bot_modules.services.economy_drops_service import (
    create_drop,
    discard_drop,
    drops_configured,
    expire_due_drops,
    has_open_drop,
    next_drop_delay,
    roll_amount,
    set_drop_message,
    try_claim_drop,
)
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bot_modules.core.app_context import Bot

log = logging.getLogger("dungeonkeeper.economy")

TICK_SECONDS = 60.0

# guild_id → wall-clock ts of the next scheduled drop. Deliberately
# in-memory: a restart just re-jitters each guild's next drop.
_next_due: dict[int, float] = {}
_rng = random.Random()


def _unit(settings: EconSettings, amount: int) -> str:
    return settings.currency_name if abs(amount) == 1 else settings.currency_plural


def drop_embed(
    settings: EconSettings, amount: int, expires_at: float, accent: discord.Colour
) -> discord.Embed:
    return discord.Embed(
        title="🪂 Coin drop!",
        description=(
            f"A pouch of {settings.currency_emoji} **{amount:,} "
            f"{_unit(settings, amount)}** just landed.\n"
            f"First to press **Claim** grabs it — "
            f"it vanishes <t:{int(expires_at)}:R>."
        ),
        color=accent,
    )


def claimed_embed(
    settings: EconSettings,
    credited: int,
    claimant: discord.Member,
    accent: discord.Colour,
) -> discord.Embed:
    return discord.Embed(
        title="🪂 Drop claimed!",
        description=(
            f"**{claimant.display_name}** grabbed {settings.currency_emoji} "
            f"**{credited:,} {_unit(settings, credited)}**."
        ),
        color=accent,
    )


def expired_embed(accent: discord.Colour) -> discord.Embed:
    return discord.Embed(
        title="🪂 Drop vanished",
        description="Nobody grabbed it in time.",
        color=accent,
    )


# ── the claim button ──────────────────────────────────────────────────


class DropClaimButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"econ_drop:claim:(?P<drop_id>\d+)",
):
    """Persistent first-wins Claim button; ``custom_id`` carries the drop id.

    Stateless by design — everything needed at click time is the id plus a
    fresh settings read, so a restart between post and claim costs nothing.
    The conditional UPDATE in ``try_claim_drop`` is the race arbiter; the
    winning click also edits the drop message, which removes the button.
    """

    def __init__(self, drop_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Claim",
                emoji="🪙",
                style=discord.ButtonStyle.success,
                custom_id=f"econ_drop:claim:{drop_id}",
            )
        )
        self.drop_id = drop_id

    @classmethod
    async def from_custom_id(  # type: ignore[override]
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> DropClaimButton:
        return cls(int(match["drop_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This only works in a server.", ephemeral=True
            )
            return
        bot = cast("Bot", interaction.client)
        db_path = bot.ctx.db_path
        booster = member.premium_since is not None

        def _claim() -> tuple[EconSettings, int | None]:
            with open_db(db_path) as conn:
                settings = load_econ_settings(conn, guild.id)
                if not settings.enabled:
                    return settings, None
                credited = try_claim_drop(
                    conn,
                    settings,
                    self.drop_id,
                    guild.id,
                    member.id,
                    now_ts=time.time(),
                    booster=booster,
                )
                return settings, credited

        settings, credited = await asyncio.to_thread(_claim)
        if credited is None:
            await interaction.response.send_message(
                "Too slow — this pouch is already gone!", ephemeral=True
            )
            return
        accent = await resolve_accent_color(db_path, guild)
        await interaction.response.edit_message(
            embed=claimed_embed(settings, credited, member, accent), view=None
        )


def _drop_view(drop_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(DropClaimButton(drop_id))
    return view


# ── sync wrappers (run via asyncio.to_thread) ─────────────────────────


def _expire_sync(db_path: Path, now_ts: float) -> list[dict]:
    with open_db(db_path) as conn:
        return [dict(row) for row in expire_due_drops(conn, now_ts)]


def _guild_state_sync(db_path: Path, guild_id: int) -> tuple[EconSettings, bool]:
    with open_db(db_path) as conn:
        return load_econ_settings(conn, guild_id), has_open_drop(conn, guild_id)


def _create_sync(
    db_path: Path,
    guild_id: int,
    channel_id: int,
    amount: int,
    *,
    now_ts: float,
    expire_minutes: int,
) -> int:
    with open_db(db_path) as conn:
        return create_drop(
            conn,
            guild_id,
            channel_id,
            amount,
            now_ts=now_ts,
            expire_minutes=expire_minutes,
        )


def _set_message_sync(db_path: Path, drop_id: int, message_id: int) -> None:
    with open_db(db_path) as conn:
        set_drop_message(conn, drop_id, message_id)


def _discard_sync(db_path: Path, drop_id: int) -> None:
    with open_db(db_path) as conn:
        discard_drop(conn, drop_id)


# ── the drop scheduler ────────────────────────────────────────────────


async def _someone_spoke_since_our_last(channel: discord.TextChannel, bot: Bot) -> bool:
    """A drop should land in conversation — if the newest message is our own
    (a previous drop, a leaderboard refresh…), hold until a human talks."""
    try:
        newest = [m async for m in channel.history(limit=1)]
    except discord.HTTPException:
        log.exception("drop history check failed in #%s", channel.name)
        return False
    if not newest:
        return True
    bot_user = getattr(bot, "user", None)
    return bot_user is None or newest[0].author.id != bot_user.id


async def _consider_guild(
    bot: Bot, db_path: Path, guild: discord.Guild, now_ts: float
) -> None:
    settings, pouch_out = await asyncio.to_thread(
        _guild_state_sync, db_path, guild.id
    )
    if not drops_configured(settings):
        _next_due.pop(guild.id, None)
        return
    due = _next_due.get(guild.id)
    if due is None:
        _next_due[guild.id] = now_ts + next_drop_delay(settings, _rng)
        return
    if now_ts < due:
        return
    channel = guild.get_channel(settings.drops_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return  # misconfigured; the due time stands until fixed
    # Never stack pouches: an open drop blocks the next one (its claim or
    # expiry frees the slot).
    if pouch_out:
        return
    if await channel_is_busy(bot, channel.id):
        return  # mid-game; retry next tick
    if not await _someone_spoke_since_our_last(channel, bot):
        return
    amount = roll_amount(settings, _rng)
    expires_at = now_ts + max(1, settings.drops_expire_minutes) * 60.0
    # The row comes first — the Claim button's custom_id needs the id.
    drop_id = await asyncio.to_thread(
        _create_sync,
        db_path,
        guild.id,
        channel.id,
        amount,
        now_ts=now_ts,
        expire_minutes=settings.drops_expire_minutes,
    )
    accent = await resolve_accent_color(db_path, guild)
    try:
        msg = await channel.send(
            embed=drop_embed(settings, amount, expires_at, accent),
            view=_drop_view(drop_id),
        )
    except discord.HTTPException:
        log.exception("drop send failed in #%s (guild %s)", channel.name, guild.id)
        await asyncio.to_thread(_discard_sync, db_path, drop_id)
        return
    await asyncio.to_thread(_set_message_sync, db_path, drop_id, msg.id)
    _next_due[guild.id] = now_ts + next_drop_delay(settings, _rng)
    log.info(
        "dropped %s coins in #%s (guild %s, drop %s)",
        amount,
        channel.name,
        guild.id,
        drop_id,
    )


async def _sweep_expired(bot: Bot, db_path: Path, now_ts: float) -> None:
    rows = await asyncio.to_thread(_expire_sync, db_path, now_ts)
    for row in rows:
        message_id = int(row["message_id"])
        if message_id == 0:
            continue  # send never completed; nothing to edit
        guild = bot.get_guild(int(row["guild_id"]))
        if guild is None:
            continue
        channel = guild.get_channel(int(row["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            continue
        try:
            msg = await channel.fetch_message(message_id)
            accent = await resolve_accent_color(db_path, guild)
            await msg.edit(embed=expired_embed(accent), view=None)
        except discord.HTTPException:
            pass  # drop message deleted or unreachable — the row is settled


async def run_tick(bot: Bot, db_path: Path, now_ts: float) -> None:
    """One tick: expire overdue drops, then give each guild its chance."""
    try:
        await _sweep_expired(bot, db_path, now_ts)
    except Exception:
        log.exception("drop expiry sweep failed")
    for guild in bot.guilds:
        try:
            await _consider_guild(bot, db_path, guild, now_ts)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("drop tick failed for guild %s", guild.id)


async def economy_drops_loop(bot: Bot, db_path: Path) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await run_tick(bot, db_path, time.time())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("coin drop tick crashed")
        await asyncio.sleep(TICK_SECONDS)
