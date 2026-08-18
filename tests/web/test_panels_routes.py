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
    cog.post_economy_panel = AsyncMock(
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
    assert "economy-panel" in keys and "ticket-panel" in keys
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
    assert panels["economy-panel"]["targets_own_channel"] is False


def test_unknown_panel_is_a_404(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post("/api/panels/no-such-panel/post", json={"channel_id": "1"})
    assert r.status_code == 404


def test_posting_a_panel_calls_the_cog_and_returns_the_jump_url(client_with_bot):
    client, bot, guild, channel = client_with_bot()
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert r.json()["message_url"] == "https://discord.com/x/y/z"
    bot.get_cog.return_value.post_economy_panel.assert_awaited_once()


def test_channel_is_required_for_a_channel_picking_panel(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post("/api/panels/economy-panel/post", json={})
    assert r.status_code == 400
    assert "channel" in r.json()["detail"].lower()


@pytest.mark.parametrize(
    "unset",
    [pytest.param("0", id="sentinel"), pytest.param(" 0 ", id="padded"),
     pytest.param("", id="empty")],
)
def test_unpicked_channel_says_pick_one_not_wrong_channel_type(
    client_with_bot, unset
):
    """"0" is the picker's unset sentinel and arrives as a *truthy* string, so
    it used to fall through to get_channel(0) and answer "Channel must be a
    text channel in this guild" — sending the admin hunting for a channel
    problem when they simply never tapped a row in the filter dropdown."""
    client, *_ = client_with_bot()
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": unset})
    assert r.status_code == 400
    assert r.json()["detail"] == "Pick a channel for this panel"


def test_non_text_channel_is_refused(client_with_bot):
    client, bot, guild, _ = client_with_bot()
    guild.get_channel.return_value = MagicMock(spec=discord.VoiceChannel)
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 400
    assert "text channel" in r.json()["detail"].lower()


def test_missing_bot_permissions_name_what_is_missing(client_with_bot):
    """The old commands returned a bare "I can't post there"; naming the
    permission is the difference between a fixable error and a guess."""
    client, bot, guild, channel = client_with_bot()
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=False, embed_links=True
    )
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
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
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 502


def test_missing_cog_is_a_503(client_with_bot):
    client, *_ = client_with_bot(cog_present=False)
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 503


def test_offline_bot_is_a_503(fake_ctx, authed_client):
    fake_ctx.bot = None
    r = authed_client.post("/api/panels/economy-panel/post", json={"channel_id": "1"})
    assert r.status_code == 503


# ── declared options (the grant-audit card) ──────────────────────────


def test_option_specs_are_described_for_the_dashboard(authed_client):
    """The page renders the controls from this, so a missing kind or default
    would ship an input the admin can't use."""
    panels = {p["key"]: p for p in authed_client.get("/api/panels").json()["panels"]}
    opts = {o["name"]: o for o in panels["grant-audit"]["options"]}
    assert opts["role_key"]["kind"] == "grant_role"
    assert "choices" in opts["role_key"]
    assert opts["min_level"]["kind"] == "int"
    assert opts["min_level"]["minimum"] == 1
    # Panels without options say so plainly rather than omitting the key.
    assert panels["economy-panel"]["options"] == []


def test_options_reach_the_panel_method(client_with_bot):
    client, bot, *_ = client_with_bot()
    cog = bot.get_cog.return_value
    cog.post_audit_card = AsyncMock(return_value=MagicMock(jump_url="u"))

    r = client.post(
        "/api/panels/grant-audit/post",
        json={"channel_id": "123", "options": {"role_key": "vip", "min_level": "9"}},
    )
    assert r.status_code == 200, r.text
    kwargs = cog.post_audit_card.await_args.kwargs
    assert kwargs == {"role_key": "vip", "min_level": 9}


def test_missing_options_fall_back_to_declared_defaults(client_with_bot):
    client, bot, *_ = client_with_bot()
    cog = bot.get_cog.return_value
    cog.post_audit_card = AsyncMock(return_value=MagicMock(jump_url="u"))

    client.post("/api/panels/grant-audit/post", json={"channel_id": "123"})

    assert cog.post_audit_card.await_args.kwargs == {"role_key": "nsfw", "min_level": 5}


def test_undeclared_options_are_dropped_not_forwarded(client_with_bot):
    """A crafted body must not reach a keyword the panel method never meant to
    expose."""
    client, bot, *_ = client_with_bot()
    cog = bot.get_cog.return_value
    cog.post_audit_card = AsyncMock(return_value=MagicMock(jump_url="u"))

    client.post(
        "/api/panels/grant-audit/post",
        json={"channel_id": "123", "options": {"role_key": "vip", "ctx": "pwned"}},
    )

    assert "ctx" not in cog.post_audit_card.await_args.kwargs


def test_a_non_numeric_int_option_is_refused(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post(
        "/api/panels/grant-audit/post",
        json={"channel_id": "123", "options": {"min_level": "lots"}},
    )
    assert r.status_code == 400
    assert "whole number" in r.json()["detail"]


def test_an_int_option_below_its_minimum_is_refused(client_with_bot):
    client, *_ = client_with_bot()
    r = client.post(
        "/api/panels/grant-audit/post",
        json={"channel_id": "123", "options": {"min_level": "0"}},
    )
    assert r.status_code == 400
    assert "at least 1" in r.json()["detail"]


def test_a_panel_refusal_reason_reaches_the_admin(client_with_bot):
    """post_audit_card raises ValueError naming what to fix (an unconfigured
    grant role, say) — that should surface, not become a generic 502."""
    client, bot, *_ = client_with_bot()
    cog = bot.get_cog.return_value
    cog.post_audit_card = AsyncMock(
        side_effect=ValueError("The grant role 'nsfw' is not configured.")
    )

    r = client.post("/api/panels/grant-audit/post", json={"channel_id": "123"})
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"]
