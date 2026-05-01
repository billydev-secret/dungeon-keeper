"""WebhookFleet — per-channel webhooks for ghost message dispatch.

Creates and reuses a single webhook named 'dk-tools-ghost' per channel.
Idempotent: ensure() finds an existing webhook before creating a new one.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("beta_tools.webhook_fleet")

WEBHOOK_NAME = "dk-tools-ghost"


class WebhookFleet:
    def __init__(self) -> None:
        # Cache mapping channel_id → Webhook so we don't re-list webhooks every send.
        self._cache: dict[int, discord.Webhook] = {}

    async def ensure(self, channel: discord.TextChannel) -> discord.Webhook:
        """Return the dk-tools-ghost webhook for the channel, creating if missing. Cached."""
        if channel.id in self._cache:
            return self._cache[channel.id]

        existing = await channel.webhooks()
        for wh in existing:
            if wh.name == WEBHOOK_NAME:
                log.info("reusing existing webhook in channel %d (%s)", channel.id, getattr(channel, "name", "?"))
                self._cache[channel.id] = wh
                return wh

        log.info("creating webhook in channel %d (%s)", channel.id, getattr(channel, "name", "?"))
        wh = await channel.create_webhook(name=WEBHOOK_NAME, reason="dk_tools beta sim")
        self._cache[channel.id] = wh
        return wh

    async def send(
        self,
        channel: discord.TextChannel,
        *,
        content: str,
        username: str,
        avatar_url: str,
    ) -> None:
        """Send a message via the channel's ghost webhook with custom name + avatar."""
        wh = await self.ensure(channel)
        await wh.send(content=content, username=username, avatar_url=avatar_url, wait=False)

    def invalidate(self, channel_id: int) -> None:
        """Drop a cached webhook (e.g. after a manual delete in Discord)."""
        self._cache.pop(channel_id, None)
