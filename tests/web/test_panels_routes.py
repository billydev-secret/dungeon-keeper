"""The channel-panel poster route.

Replaced six slash commands on 2026-07-28, so it is now the only way to place
any of these panels. What's asserted here is the plumbing those commands each
repeated and that the route now owns once: does it refuse an unknown panel, a
missing or wrong-typed channel, a channel the bot can't post in, and does it
turn a cog's "I couldn't" into something an admin can act on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest


def _bot(*, post_result="ok", cog_present=True):
    """A live-bot stand-in whose panel method returns ``post_result``."""
    bot = MagicMock()
    guild = MagicMock()
    guild.me = MagicMock()
    bot.get_guild.return_value = guild

    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "general"
    perms = MagicMock(view_channel=True, send_messages=True, embed_links=True)
    channel.permissions_for.return_value = perms
    guild.get_channel.return_value = channel

    if not cog_present:
        bot.get_cog.return_value = None
        return bot, guild, channel

    cog = MagicMock()
    message = MagicMock()
    message.jump_url = "https://discord.com/x/y/z"
    cog.post_guide_panel = AsyncMock(
        return_value=(message if post_result == "ok" else None)
    )
    cog.post_control_panel = AsyncMock(
        return_value=(message if post_result == "ok" else None)
    )
    bot.get_cog.return_value = cog
    return bot, guild, channel


@pytest.fixture
def client_with_bot(fake_ctx, authed_client):
    """authed_client, with a stand-in bot attached to the context."""
    def _attach(**kw):
        bot, guild, channel = _bot(**kw)
        fake_ctx.bot = bot
        return authed_client, bot, guild, channel

    return _attach


def test_list_panels_returns_the_registry(authed_client):
    r = authed_client.get("/api/panels")
    assert r.status_code == 200
    panels = r.json()["panels"]
    keys = {p["key"] for p in panels}
    assert "economy-guide" in keys and "ticket-panel" in keys
    assert all(p["label"] and p["description"] for p in panels)


def test_list_panels_works_without_a_live_bot(authed_client):
    """Static data, so the page can render and explain itself while the bot is
    down rather than showing a connection error."""
    r = authed_client.get("/api/panels")
    assert r.status_code == 200


def test_own_channel_panels_are_flagged(authed_client):
    """The UI hides the channel picker for these; the flag is how it knows."""
    panels = {p["key"]: p for p in authed_client.get("/api/panels").json()["panels"]}
    assert panels["voice-control"]["targets_own_channel"] is True
    assert panels["guess-prompt"]["targets_own_channel"] is True
    assert panels["economy-guide"]["targets_own_channel"] is False


def test_unknown_panel_is_a_404(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post("/api/panels/no-such-panel/post", json={"channel_id": "1"})
    assert r.status_code == 404


def test_posting_a_panel_calls_the_cog_and_returns_the_jump_url(client_with_bot):
    client, bot, guild, channel = client_with_bot()
    r = client.post("/api/panels/economy-guide/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert r.json()["message_url"] == "https://discord.com/x/y/z"
    bot.get_cog.return_value.post_guide_panel.assert_awaited_once()


def test_channel_is_required_for_a_channel_picking_panel(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post("/api/panels/economy-guide/post", json={})
    assert r.status_code == 400
    assert "channel" in r.json()["detail"].lower()


def test_non_text_channel_is_refused(client_with_bot):
    client, bot, guild, _ = client_with_bot()
    guild.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)
    r = client.post("/api/panels/economy-guide/post", json={"channel_id": "123"})
    assert r.status_code == 400
    assert "text channel" in r.json()["detail"].lower()


def test_missing_bot_permissions_name_what_is_missing(client_with_bot):
    """The old commands returned a bare "I can't post there"; naming the
    permission is the difference between a fixable error and a guess."""
    client, bot, guild, channel = client_with_bot()
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=False, embed_links=True
    )
    r = client.post("/api/panels/economy-guide/post", json={"channel_id": "123"})
    assert r.status_code == 400
    assert "Send Messages" in r.json()["detail"]


def test_own_channel_panel_ignores_a_supplied_channel(client_with_bot):
    """Voice Control posts into its configured control channel; honouring a
    picked one would strand the buttons where the cog never looks."""
    client, bot, *_ = client_with_bot()
    r = client.post("/api/panels/voice-control/post", json={"channel_id": "999"})
    assert r.status_code == 200, r.text
    args = bot.get_cog.return_value.post_control_panel.await_args.args
    assert args[1] is None


def test_own_channel_panel_with_no_configured_channel_explains_itself(client_with_bot):
    """The cog returns None when its own config is unset — the common case, so
    it gets its own wording rather than a generic Discord failure."""
    client, *_ = client_with_bot(post_result="none")
    r = client.post("/api/panels/voice-control/post", json={})
    assert r.status_code == 400
    assert "settings page" in r.json()["detail"]


def test_a_refused_post_is_reported_as_a_failure(client_with_bot):
    client, *_ = client_with_bot(post_result="none")
    r = client.post("/api/panels/economy-guide/post", json={"channel_id": "123"})
    assert r.status_code == 502


def test_missing_cog_is_a_503(client_with_bot):
    client, *_ = client_with_bot(cog_present=False)
    r = client.post("/api/panels/economy-guide/post", json={"channel_id": "123"})
    assert r.status_code == 503


def test_offline_bot_is_a_503(fake_ctx, authed_client):
    fake_ctx.bot = None
    r = authed_client.post("/api/panels/economy-guide/post", json={"channel_id": "1"})
    assert r.status_code == 503
