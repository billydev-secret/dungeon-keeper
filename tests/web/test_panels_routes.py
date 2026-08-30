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
    cog.post_bounty_panel = AsyncMock(
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
    # The Bounty Board joined them on 2026-08-29: it had always refused every
    # channel but the configured board, so the picker beside the Bounty Board
    # Channel setting had exactly one valid answer.
    assert panels["economy-bounty"]["targets_own_channel"] is True
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


# ── the sticky-collision guard ───────────────────────────────────────────


def _occupy(fake_ctx, channel_id: int, *, key: str) -> None:
    """Put one sticky panel's channel on record for the test guild."""
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        if key == "casino":
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                (fake_ctx.guild_id, "casino_panel_channel_id", str(channel_id)),
            )
        elif key == "pen-pals":
            conn.execute(
                "INSERT INTO pen_pals_config (guild_id, panel_channel_id)"
                " VALUES (?, ?)",
                (fake_ctx.guild_id, channel_id),
            )
        elif key == "voice-control":
            # Where the panel currently *is* — the registry's key for it.
            conn.execute(
                "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
                (
                    fake_ctx.guild_id,
                    "voice_master_panel_channel_id",
                    str(channel_id),
                ),
            )
        else:  # pragma: no cover - guards a typo in a test, not a code path
            raise AssertionError(f"no seeder for {key}")


def _set_control_channel(fake_ctx, channel_id: int) -> None:
    """Where Voice Control *posts* — a different key from where it is now."""
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (fake_ctx.guild_id, "voice_master_control_channel_id", str(channel_id)),
        )


def _set_bounty_channel(fake_ctx, channel_id: int) -> None:
    """The board channel — where the hub posts, and the only place it can."""
    from bot_modules.core.db_utils import open_db

    with open_db(fake_ctx.db_path) as conn:
        conn.execute(
            "INSERT INTO config (guild_id, key, value) VALUES (?, ?, ?)",
            (fake_ctx.guild_id, "econ_bounty_channel_id", str(channel_id)),
        )


def test_the_bounty_hub_takes_no_channel_from_the_caller(client_with_bot):
    """It reads ``bounty_channel_id`` itself, so a channel in the body — from a
    stale page, or a hand-rolled request — must not reach the cog."""
    client, bot, *_ = client_with_bot()
    r = client.post("/api/panels/economy-bounty/post", json={"channel_id": "999"})
    assert r.status_code == 200, r.text
    args = bot.get_cog.return_value.post_bounty_panel.await_args.args
    assert args[1] is None


def test_the_bounty_hub_checks_the_board_channels_residents(
    fake_ctx, client_with_bot
):
    """Its destination is the board channel, so that is whose bottom slot the
    collision guard has to look at.

    It warns rather than blocks, and can only ever warn: the sticky registry
    resolves the bounty panel's own channel from ``bounty_channel_id`` too, so
    setting the board channel already counts the hub as resident there — and a
    panel already in the target channel is never refused on account of who
    else is there, since refusing would not undo the collision, only lock the
    admin out of maintaining a panel that is sitting in it right now.
    """
    client, bot, guild, channel = client_with_bot()
    _set_bounty_channel(fake_ctx, 456)
    _occupy(fake_ctx, 456, key="casino")

    r = client.post("/api/panels/economy-bounty/post", json={})
    assert r.status_code == 200, r.text
    assert "casino hub panel" in r.json()["warning"]
    bot.get_cog.return_value.post_bounty_panel.assert_awaited_once()


def test_an_own_channel_panel_is_permission_checked_too(fake_ctx, client_with_bot):
    """Until 2026-08-29 only caller-picked channels were checked, so the
    own-channel panels reached the placement unguarded.

    That is the one guard that must not be skipped for a sticky panel: placing
    it deletes the message it replaces *before* sending the new one, so a
    channel the bot can't post in leaves the guild with no panel at all and a
    repost that fails the same way.
    """
    client, bot, guild, channel = client_with_bot()
    _set_bounty_channel(fake_ctx, 456)
    channel.permissions_for.return_value = MagicMock(
        view_channel=True, send_messages=True, embed_links=False
    )

    r = client.post("/api/panels/economy-bounty/post", json={})
    assert r.status_code == 400
    assert "Embed Links" in r.json()["detail"]
    bot.get_cog.return_value.post_bounty_panel.assert_not_awaited()


def test_an_unconfigured_own_channel_panel_is_left_to_the_cog(client_with_bot):
    """Nothing configured is the common case, and the cog's own message names
    the setting to go and fill in — a permission complaint would not."""
    client, bot, *_ = client_with_bot()

    r = client.post("/api/panels/economy-bounty/post", json={})
    assert r.status_code == 200, r.text
    bot.get_cog.return_value.post_bounty_panel.assert_awaited_once()


def test_posting_into_a_bot_chasing_panels_channel_is_refused(
    fake_ctx, client_with_bot
):
    """The half of the 2026-08-06 F1 fix that never landed.

    /bank auction start has refused this since 2026-07-28, but the dashboard's
    panel buttons posted straight into it — and the casino hub re-takes the
    bottom after every render, so whatever else goes in that channel is buried
    on every repaint with nothing the admin can do about it.
    """
    client, bot, guild, channel = client_with_bot()
    channel.id = 123
    _occupy(fake_ctx, 123, key="casino")

    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 400
    assert "casino hub panel" in r.json()["detail"]
    bot.get_cog.return_value.post_economy_panel.assert_not_awaited()


def test_posting_beside_a_human_only_panel_succeeds_with_a_warning(
    fake_ctx, client_with_bot
):
    """That collision is intermittent and visible, so it is the admin's call."""
    client, bot, guild, channel = client_with_bot()
    channel.id = 123
    _occupy(fake_ctx, 123, key="pen-pals")

    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert "pen pals panel" in r.json()["warning"]
    bot.get_cog.return_value.post_economy_panel.assert_awaited_once()


def test_an_empty_channel_posts_with_no_warning(fake_ctx, client_with_bot):
    client, *_ = client_with_bot()
    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None


def test_a_panel_is_not_refused_on_account_of_itself(fake_ctx, client_with_bot):
    """Voice Control posts into its own configured channel, so it is always
    already there — without the exclusion it would become unpostable."""
    client, bot, guild, channel = client_with_bot()
    channel.id = 456
    guild.get_channel.return_value = channel
    _set_control_channel(fake_ctx, 456)
    _occupy(fake_ctx, 456, key="voice-control")

    r = client.post("/api/panels/voice-control/post", json={})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None


def test_an_own_channel_panel_still_sees_the_other_residents(
    fake_ctx, client_with_bot
):
    """Its destination comes from its own config, so the guard has to look it
    up rather than take one from the caller.

    The panel is already there alongside the casino hub, so this warns rather
    than blocks — refusing would lock the admin out of refreshing it.
    """
    client, bot, guild, channel = client_with_bot()
    channel.id = 456
    _set_control_channel(fake_ctx, 456)
    _occupy(fake_ctx, 456, key="voice-control")
    _occupy(fake_ctx, 456, key="casino")

    r = client.post("/api/panels/voice-control/post", json={})
    assert r.status_code == 200, r.text
    assert "casino hub panel" in r.json()["warning"]
    bot.get_cog.return_value.post_control_panel.assert_awaited_once()


def test_an_own_channel_panels_first_post_is_still_checked(
    fake_ctx, client_with_bot
):
    """Nothing recorded yet is exactly when the check matters most.

    Reading the registry's key for this panel — where it *is* — answered 0 on a
    first post and skipped the guard entirely.
    """
    client, bot, *_ = client_with_bot()
    _set_control_channel(fake_ctx, 456)
    _occupy(fake_ctx, 456, key="casino")

    r = client.post("/api/panels/voice-control/post", json={})
    assert r.status_code == 400
    assert "casino hub panel" in r.json()["detail"]
    bot.get_cog.return_value.post_control_panel.assert_not_awaited()


def test_an_own_channel_panel_checks_where_it_is_going_not_where_it_was(
    fake_ctx, client_with_bot
):
    """After the Control Channel moves, the old channel's residents are no
    longer this panel's problem — and the new channel's are."""
    client, bot, *_ = client_with_bot()
    _occupy(fake_ctx, 456, key="voice-control")  # panel still sits in the old one
    _occupy(fake_ctx, 456, key="casino")         # which the casino hub holds
    _set_control_channel(fake_ctx, 789)          # admin repointed it here

    r = client.post("/api/panels/voice-control/post", json={})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None
    bot.get_cog.return_value.post_control_panel.assert_awaited_once()


@pytest.mark.parametrize(
    "key", [pytest.param("ticket-panel", id="ticket"),
            pytest.param("grant-audit", id="grant-audit")]
)
def test_a_panel_that_does_not_re_stick_is_never_refused(
    fake_ctx, client_with_bot, key
):
    """The ticket panel and the audit card are posted once and then scroll
    like any other message. They have no bottom-slot contest to lose, and
    refusing them would block a placement that always worked."""
    client, bot, guild, channel = client_with_bot()
    channel.id = 123
    _occupy(fake_ctx, 123, key="casino")
    cog = bot.get_cog.return_value
    cog.post_ticket_panel = AsyncMock(return_value=MagicMock(jump_url="u"))
    cog.post_audit_card = AsyncMock(return_value=MagicMock(jump_url="u"))

    r = client.post(f"/api/panels/{key}/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None


def test_a_panel_already_in_the_channel_is_warned_not_refused(
    fake_ctx, client_with_bot
):
    """The lockout the 2026-08-06 plan doc warned about.

    A guild whose economy panel already shares the casino hub's channel is a
    live, working setup. Refusing the re-post doesn't undo the collision — it
    just stops the admin refreshing a panel that is sitting there right now,
    with nothing they can do about it from Discord.
    """
    client, bot, guild, channel = client_with_bot()
    channel.id = 123
    _occupy(fake_ctx, 123, key="casino")
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(conn, fake_ctx.guild_id, {"guide_channel_id": 123})

    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 200, r.text
    assert "casino hub panel" in r.json()["warning"]
    bot.get_cog.return_value.post_economy_panel.assert_awaited_once()


def test_moving_a_panel_into_a_bot_chasing_channel_is_still_refused(
    fake_ctx, client_with_bot
):
    """The block is about *creating* a collision, and that still holds."""
    client, bot, guild, channel = client_with_bot()
    channel.id = 123
    _occupy(fake_ctx, 123, key="casino")
    from bot_modules.core.db_utils import open_db
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(fake_ctx.db_path) as conn:
        save_econ_settings(conn, fake_ctx.guild_id, {"guide_channel_id": 999})

    r = client.post("/api/panels/economy-panel/post", json={"channel_id": "123"})
    assert r.status_code == 400
    bot.get_cog.return_value.post_economy_panel.assert_not_awaited()
