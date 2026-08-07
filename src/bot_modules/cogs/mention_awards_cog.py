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
    effective_channel_id,
    first_match,
    quest_occurrence,
)
from bot_modules.mention_awards.store import rules_for_channel

if TYPE_CHECKING:
    from bot_modules.core.app_context import Bot

log = logging.getLogger(__name__)

# Rules are dashboard-written and read on every mention-bearing message, so
# they are cached with a short TTL rather than queried per message — the
# economy cog's trigger-config pattern. Most entries are empty lists:
# negative results are cached too, which is what keeps unwatched channels off
# the DB. The dashboard routes invalidate through reset_rules_cache on every
# write (the games_external refresh pattern), so the TTL is a backstop for
# out-of-band DB edits, not the propagation path — without the write-through,
# a rule created just after a message cached [] would silently drop the next
# announcement (2026-08 review finding).
_CACHE_TTL = 60.0
# Every thread ever posted in leaves a (tiny) negative entry; a hard cap
# bounds the pathological case. Clearing wholesale is fine — entries rebuild
# on the next message at one query each.
_CACHE_MAX_ENTRIES = 4096


class MentionAwardsCog(commands.Cog):
    """Watches configured channels for chip-matching award announcements."""

    def __init__(self, bot: "Bot"):
        self.bot = bot
        # (guild_id, channel_id) -> (monotonic deadline, rules)
        self._rules_cache: dict[tuple[int, int], tuple[float, list[Rule]]] = {}

    @property
    def db(self):
        return self.bot.games_db

    def reset_rules_cache(self, guild_id: int | None = None) -> None:
        """Drop cached rules so a dashboard write is live immediately.

        Called by the web routes after every create/update/delete (the
        games_external ``refresh_watch_cache`` pattern — same process).
        """
        if guild_id is None:
            self._rules_cache.clear()
            return
        for key in [k for k in self._rules_cache if k[0] == guild_id]:
            del self._rules_cache[key]

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
        if len(self._rules_cache) >= _CACHE_MAX_ENTRIES:
            self._rules_cache.clear()
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
            # A thread's message counts toward its parent channel — the
            # sibling convention (photo challenge, trigger quests).
            channel_key = effective_channel_id(
                message.channel.id, getattr(message.channel, "parent_id", None)
            )
            rules = await self._channel_rules(guild.id, channel_key)
            if not rules:
                return

            author_roles = [r.id for r in getattr(message.author, "roles", [])]
            found = first_match(
                rules,
                channel_id=channel_key,
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

            paid = await pay_mention_award(
                self.bot, guild.id, found.member_id,
                coins=found.amount, rule_id=found.rule_id,
                occurrence=quest_occurrence(message.id),
            )
            if not paid:
                # The credit didn't land (economy off, member unresolvable…).
                # Release the claim so the announcement isn't burned forever —
                # an edit of the message, or the backfill, can retry it.
                await external_logic.release_payout(
                    self.db, message.id, PAYOUT_KIND
                )
                log.warning(
                    "Mention award: payout for message %s did not credit — "
                    "claim released for retry",
                    message.id,
                )
                return
            log.info(
                "Mention award: guild %s rule %s — %s named by %s (%d coins)",
                guild.id, found.rule_id, found.member_id,
                found.announcer_id, found.amount,
            )
        except Exception:
            log.exception("Mention award failed for message %s", message.id)


async def setup(bot: "Bot"):
    await bot.add_cog(MentionAwardsCog(bot))
