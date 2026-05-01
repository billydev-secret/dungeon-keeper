"""Tests for beta_tools.webhook_fleet."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


WEBHOOK_NAME = "dk-tools-ghost"


def _fake_text_channel(channel_id: int = 4001, existing_webhooks: list | None = None):
    """Build a fake TextChannel with the relevant async API surface."""
    ch = MagicMock()
    ch.id = channel_id
    ch.webhooks = AsyncMock(return_value=existing_webhooks or [])
    ch.create_webhook = AsyncMock()
    return ch


def _fake_webhook(name: str = WEBHOOK_NAME, webhook_id: int = 5001):
    wh = MagicMock()
    wh.name = name
    wh.id = webhook_id
    wh.send = AsyncMock()
    return wh


async def test_ensure_returns_existing_webhook_if_present():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    wh = await fleet.ensure(channel)
    assert wh is existing
    channel.create_webhook.assert_not_called()


async def test_ensure_creates_webhook_when_missing():
    from beta_tools.webhook_fleet import WebhookFleet

    new_wh = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[])
    channel.create_webhook.return_value = new_wh
    fleet = WebhookFleet()

    wh = await fleet.ensure(channel)
    assert wh is new_wh
    channel.create_webhook.assert_awaited_once_with(name=WEBHOOK_NAME, reason="dk_tools beta sim")


async def test_ensure_caches_per_channel_id():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    await fleet.ensure(channel)
    await fleet.ensure(channel)  # second call should hit cache, not re-list
    channel.webhooks.assert_awaited_once()


async def test_send_uses_username_and_avatar():
    from beta_tools.webhook_fleet import WebhookFleet

    existing = _fake_webhook()
    channel = _fake_text_channel(existing_webhooks=[existing])
    fleet = WebhookFleet()

    await fleet.send(channel, content="hello", username="GhostName", avatar_url="https://x/y.png")

    existing.send.assert_awaited_once_with(
        content="hello", username="GhostName", avatar_url="https://x/y.png", wait=False,
    )
