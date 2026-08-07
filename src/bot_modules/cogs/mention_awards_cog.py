"""Listener that pays Mention Awards: condition chips + a mention → currency.

Thin by design: chip matching lives in ``mention_awards/logic.py``, rule
storage in ``store.py``, and the payout in
``economy/game_rewards.pay_mention_award``. This cog reads the channel's
rules, hands the message to the matcher, and claims the one-time payout.

No commands — rules are built on the dashboard (Economy → Mention Awards).

**Message content is read, never stored.** ``message.content`` comes off the
gateway for the chip match and is discarded; nothing here writes it, and the
guild's content-storage setting is untouched.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from bot_modules.core.db_utils import open_db
from bot_modules.economy.game_rewards import pay_mention_award
from bot_modules.games_external import logic as external_logic
from bot_modules.mention_awards.logic import (
    PAYOUT_KIND,
    Rule,
    first_match,
    quest_occurrence,
)
from bot_modules.mention_awards.store import rules_for_channel

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# Rules are dashboard-written and read on every mention-bearing message, so
# they are cached with a short TTL rather than queried per message — the
# economy cog's trigger-config pattern ("a dashboard edit takes effect within
# 60s"). Most entries are empty lists: negative results are cached too, which
# is what keeps unwatched channels off the DB.
_CACHE_TTL = 60.0


class MentionAwardsCog(commands.Cog):
    """Watches configured channels for chip-matching award announcements."""

    def __init__(self, bot: "Bot"):
        self.bot = bot
        # (guild_id, channel_id) -> (monotonic deadline, rules)
        self._rules_cache: dict[tuple[int, int], tuple[float, list[Rule]]] = {}

    @property
    def db(self):
        return self.bot.games_db

    async def _channel_rules(self, guild_id: int, channel_id: int) -> list[Rule]:
        key = (guild_id, channel_id)
        hit = self._rules_cache.get(key)
        now = time.monotonic()
        if hit is not None and now < hit[0]:
            return hit[1]

        def _load() -> list[Rule]:
            with open_db(self.bot.ctx.db_path) as conn:
                return rules_for_channel(conn, guild_id, channel_id)

        rules = await asyncio.to_thread(_load)
        self._rules_cache[key] = (now + _CACHE_TTL, rules)
        return rules

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._maybe_pay(message)

    async def _maybe_pay(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        # Cheapest possible bail-out for the overwhelming majority of
        # messages: no user mention, no recipient, no cache hit needed.
        if not message.raw_mentions:
            return
        try:
            rules = await self._channel_rules(guild.id, message.channel.id)
            if not rules:
                return

            author_roles = [r.id for r in getattr(message.author, "roles", [])]
            found = first_match(
                rules,
                channel_id=message.channel.id,
                author_id=message.author.id,
                author_role_ids=author_roles,
                content=message.content or "",
                mentioned_user_ids=message.raw_mentions,
                mentioned_role_ids=message.raw_role_mentions,
            )
            if found is None:
                return

            # Claim before paying: the ledger row is the one-time guarantee,
            # and it also stops an edit re-firing the same announcement. The
            # ledger is one-payout-per-message across ALL kinds (its PK is
            # the message id alone — see claim_payout's contract).
            first = await external_logic.claim_payout(
                self.db, message.id, guild.id, PAYOUT_KIND
            )
            if not first:
                return

            await pay_mention_award(
                self.bot, guild.id, found.member_id,
                coins=found.amount, rule_id=found.rule_id,
                occurrence=quest_occurrence(message.id),
            )
            log.info(
                "Mention award: guild %s rule %s — %s named by %s (%d coins)",
                guild.id, found.rule_id, found.member_id,
                found.announcer_id, found.amount,
            )
        except Exception:
            log.exception("Mention award failed for message %s", message.id)


async def setup(bot: "Bot"):
    await bot.add_cog(MentionAwardsCog(bot))
