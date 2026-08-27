"""Announcing a sign-off outcome to the register channel.

The one place a denied or expired quest claim is made visible. The per-claim
card that used to carry that news in the bank channel is gone — pending claims
now live on the todo board and are resolved from it — and nothing DMs, so the
register is the whole of what a member is told.

Approvals are absent from this module on purpose: they credit ledger kind
``quest``, and the register drain in ``economy_loop.run_guild_register`` has
always posted those. Only the two outcomes that move no currency need posting
by hand.

The pure parts — the embed and the privacy rule — live in ``register.py`` next
to the feed they have to look like; this module is the Discord I/O around them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.economy.register import (
    build_signoff_notice_embed,
    suppress_signoff_notice,
)
from bot_modules.services.economy_service import load_econ_settings
from bot_modules.services.message_store import get_known_users_bulk

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger("dungeonkeeper.economy")


async def announce_signoff_outcome(
    bot: discord.Client,
    db_path: Path,
    guild_id: int,
    *,
    state: str,
    user_id: int,
    quest_title: str,
    trigger_kind: object = "",
    deny_reason: str | None = None,
) -> bool:
    """Best-effort: post a denied/expired claim to the guild's register.

    Returns True only on a real send. **Every** failure path is a quiet False,
    because all three callers — the board's Sign-Offs button, the dashboard's
    resolve endpoint and the expiry sweep — have already committed the
    resolution by the time they call this. A missing channel or a Forbidden
    must never undo a payout or 500 an API call.

    Posts nothing at all when:

    * the quest is on a privacy-suppressed trigger kind
      (:func:`register.suppress_signoff_notice`);
    * the guild has no register channel configured, or the economy is off.

    In those cases the outcome is deliberately silent — see the module
    docstring and ``docs/economy_spec.md``. The member still sees the claim
    reopen on their own ``/quests`` board.
    """
    if state not in ("denied", "expired"):
        return False
    if suppress_signoff_notice(trigger_kind):
        return False

    def _load() -> tuple[int, str]:
        # ``db_path`` rather than an AppContext because the expiry sweep holds
        # only the path — the same signature ``notify_member`` takes, for the
        # same reason.
        with open_db(db_path) as conn:
            settings = load_econ_settings(conn, guild_id)
            if not settings.enabled or not settings.register_channel_id:
                return 0, ""
            known = get_known_users_bulk(conn, guild_id, [user_id])
        return settings.register_channel_id, known.get(user_id, "")

    try:
        channel_id, known_name = await asyncio.to_thread(_load)
    except Exception:
        log.exception("econ sign-off: failed to load register settings")
        return False
    if not channel_id:
        return False

    guild = bot.get_guild(guild_id)
    if guild is None:
        return False
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        return False

    # Live nickname first, the stored one for a member who has since left, and
    # only then a placeholder — the register never prints a raw id.
    member = guild.get_member(user_id)
    embed = build_signoff_notice_embed(
        state,
        member_name=(
            member.display_name if member is not None else known_name or "someone"
        ),
        quest_title=quest_title,
        deny_reason=deny_reason,
        avatar_url=member.display_avatar.url if member is not None else None,
    )
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        log.warning(
            "econ sign-off: no permission to post to the register in guild %s.",
            guild_id,
        )
        return False
    except discord.HTTPException:
        log.warning("econ sign-off: register post failed in guild %s.", guild_id)
        return False
    except Exception:
        log.exception("econ sign-off: unexpected error posting to the register")
        return False
    return True
