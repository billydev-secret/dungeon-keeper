"""Economy guide panel — embed builder + /bank post-guide command."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.guide import (
    NOTIFY_CUSTOM_ID,
    NOTIFY_FAILED_MSG,
    NOTIFY_OFF_MSG,
    NOTIFY_ON_MSG,
    NOTIFY_UNCONFIGURED_MSG,
    GuideNotifyButton,
    GuideView,
    build_guide_embed,
    should_restick_guide,
)
from bot_modules.economy.logic import resolve_notify_toggle
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from migrations import apply_migrations_sync
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007
CHANNEL_ID = 111
OTHER_CHANNEL_ID = 222


# ── builder ─────────────────────────────────────────────────────────────────


def test_guide_embed_defaults_cover_earning_and_spending():
    embed = build_guide_embed(EconSettings(), color=discord.Color(0x123456))

    assert "Coins — how it works" in (embed.title or "")
    assert embed.color == discord.Color(0x123456)
    fields = {f.name: f.value or "" for f in embed.fields}
    earning = fields["Earning"]
    # what-pays-what rows are aligned: label in a code cell (padded to the
    # widest row), payment outside it — so match the label and pay separately
    # rather than pin the exact padding, which shifts as rows are added.
    assert "First message of the day" in earning
    assert "🪙 5" in earning  # text login base
    assert "🪙 15" in earning  # voice-first login base
    assert "/bank quests" in earning
    spending = fields["Spending"]
    assert "/bank shop" in spending
    assert "color, name, gradient, icon" in spending  # perks named, not priced
    assert "prices in the shop" in spending  # specifics deferred to the shop
    assert "/bank pay" in spending
    # fine print (streak cap, booster, rental grace) collapses to the footer
    footer = embed.footer.text or ""
    assert "+10" in footer and "×1.5" in footer and "grace" in footer


def test_guide_embed_conversion_line_gated_on_rate():
    # The XP→coin faucet ships off (rate 0): the guide must not promise a
    # nightly conversion that no longer happens.
    off = build_guide_embed(EconSettings())  # default xp_per_coin == 0.0
    off_earning = {f.name: f.value or "" for f in off.fields}["Earning"]
    assert "converts into" not in off_earning
    assert "/bank quests" in off_earning  # quests are still surfaced

    # Re-enabled (a positive rate): the conversion copy comes back.
    on = build_guide_embed(EconSettings(xp_per_coin=15.0))
    on_earning = {f.name: f.value or "" for f in on.fields}["Earning"]
    assert "converts into" in on_earning


def test_guide_embed_offers_notifications_not_channel_access():
    fields = {
        f.name: f.value or ""
        for f in build_guide_embed(EconSettings()).fields
    }
    notifications = fields["Notifications"]
    assert "Notifications" in notifications  # names the button to click
    assert "DM" in notifications
    # The role is a DM preference, so the panel must not promise access — and
    # must no longer point at the onboarding screen that used to gate it.
    assert "<id:customize>" not in notifications
    assert "never what you can see or earn" in notifications


def test_guide_embed_uses_guild_branding():
    settings = EconSettings(
        currency_plural="Gems",
        currency_emoji="💎",
        currency_icon_url="https://cdn.example/gem.png",
    )
    embed = build_guide_embed(settings)

    fields = {f.name: f.value or "" for f in embed.fields}
    assert "Gems" in (embed.title or "")
    assert "💎" in fields["Earning"]  # emoji flows into the earn lines
    assert "Gems" in fields["Spending"]  # plural flows into the /bank pay line
    assert embed.thumbnail.url == "https://cdn.example/gem.png"


def test_guide_embed_hides_pay_when_transfers_disabled():
    embed = build_guide_embed(EconSettings(transfers_enabled=False))
    spending = {f.name: f.value or "" for f in embed.fields}["Spending"]
    assert "/bank pay" not in spending


def test_guide_embed_hides_booster_line_without_bonus():
    embed = build_guide_embed(EconSettings(booster_multiplier=1.0))
    assert "Boosters" not in (embed.footer.text or "")


# ── sticky re-stick predicate ────────────────────────────────────────────────

PANEL_CH = 4242
PANEL_MSG = 9999


def test_restick_true_for_member_message_in_panel_channel():
    assert should_restick_guide(
        message_channel_id=PANEL_CH,
        message_id=123,
        panel_channel_id=PANEL_CH,
        panel_message_id=PANEL_MSG,
    )


def test_restick_predicate_is_author_agnostic():
    # The predicate only knows channel/message ids — bot-vs-member filtering
    # lives in the listener (see test_restick_listener_ignores_bot_messages),
    # so for a distinct id in the panel channel it always returns True.
    assert should_restick_guide(
        message_channel_id=PANEL_CH,
        message_id=555,
        panel_channel_id=PANEL_CH,
        panel_message_id=PANEL_MSG,
    )


def test_restick_false_for_the_panel_itself():
    # Our own repost must not trigger another repost (infinite loop).
    assert not should_restick_guide(
        message_channel_id=PANEL_CH,
        message_id=PANEL_MSG,
        panel_channel_id=PANEL_CH,
        panel_message_id=PANEL_MSG,
    )


def test_restick_false_for_other_channel():
    assert not should_restick_guide(
        message_channel_id=PANEL_CH + 1,
        message_id=123,
        panel_channel_id=PANEL_CH,
        panel_message_id=PANEL_MSG,
    )


def test_restick_false_when_no_panel_posted():
    assert not should_restick_guide(
        message_channel_id=PANEL_CH,
        message_id=123,
        panel_channel_id=0,
        panel_message_id=0,
    )


# ── settings round-trip ─────────────────────────────────────────────────────


def test_guide_ids_round_trip(tmp_path):
    db = tmp_path / "test.db"
    apply_migrations_sync(db)
    with open_db(db) as conn:
        save_econ_settings(
            conn, GUILD_ID, {"guide_channel_id": 123, "guide_message_id": 456}
        )
        settings = load_econ_settings(conn, GUILD_ID)
    assert settings.guide_channel_id == 123
    assert settings.guide_message_id == 456


# ── /bank post-guide ────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(db_path=db, open_db=lambda: open_db(db))


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.cogs.economy_cog.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    return EconomyCog(MagicMock(), ctx)


def _enable(db, **overrides) -> None:
    values: dict[str, object] = {"enabled": True}
    values.update(overrides)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, values)


def _member(*, admin: bool = False, role_ids: tuple[int, ...] = ()) -> MagicMock:
    m = MagicMock(spec=discord.Member)
    m.id = 500
    m.guild_permissions = MagicMock(administrator=admin)
    m.roles = [SimpleNamespace(id=rid) for rid in role_ids]
    return m


def _channel(channel_id: int) -> MagicMock:
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = channel_id
    ch.mention = f"<#{channel_id}>"
    ch.send = AsyncMock(return_value=MagicMock(id=8888))
    ch.fetch_message = AsyncMock()
    return ch


def _interaction(actor, channel, guild=None):
    inter = fake_interaction(guild=guild or FakeGuild(id=GUILD_ID))
    inter.user = actor
    inter.channel = channel
    return inter


async def _post_guide(cog, interaction, channel=None):
    await cog.bank_post_guide.callback(cog, interaction, channel)


def _stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.guide_channel_id, s.guide_message_id


# ── sticky repost ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_restick_now_reposts_panel_and_updates_ids(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    old = MagicMock()
    old.delete = AsyncMock()
    channel.fetch_message = AsyncMock(return_value=old)
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    cog.bot.get_guild = MagicMock(return_value=guild)

    await cog._restick_now(GUILD_ID)

    old.delete.assert_awaited_once()  # stale panel dropped
    channel.send.assert_awaited_once()  # fresh panel at the bottom
    assert _stored(db) == (CHANNEL_ID, 8888)  # new id persisted
    # In-memory cache updated so the listener skips our own repost.
    assert cog._guide_ref[GUILD_ID][1:] == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_restick_now_noop_without_existing_panel(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID)  # no guide_message_id → nothing posted
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    guild = FakeGuild(id=GUILD_ID, channels={CHANNEL_ID: channel})
    cog.bot.get_guild = MagicMock(return_value=guild)

    await cog._restick_now(GUILD_ID)

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_restick_cancels_pending_repost(ctx, db):
    cog = _make_cog(ctx)
    cog._schedule_guide_restick(GUILD_ID)
    first = cog._restick_tasks[GUILD_ID]
    cog._schedule_guide_restick(GUILD_ID)
    second = cog._restick_tasks[GUILD_ID]

    assert first is not second  # re-armed, not stacked
    assert cog._restick_tasks[GUILD_ID] is second
    # Let the cancellation of `first` settle, then clean both up.
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    assert first.cancelled()


def _listener_msg(*, author_bot: bool, channel_id: int, message_id: int) -> MagicMock:
    m = MagicMock(spec=discord.Message)
    m.guild = FakeGuild(id=GUILD_ID)
    m.author = MagicMock(bot=author_bot)
    m.channel = SimpleNamespace(id=channel_id)
    m.id = message_id
    return m


@pytest.mark.asyncio
async def test_restick_listener_ignores_bot_messages(ctx, db):
    # Panel posted and cached, so _guide_panel_ref is a pure cache hit.
    cog = _make_cog(ctx)
    cog._guide_ref[GUILD_ID] = (time.monotonic() + 300, CHANNEL_ID, PANEL_MSG)
    cog._schedule_guide_restick = MagicMock()

    # Our own repost / economy notices must not arm another repost — this is
    # the self-loop the id-cache alone can't be relied on to catch.
    await cog._restick_guide_panel(
        _listener_msg(author_bot=True, channel_id=CHANNEL_ID, message_id=777)
    )
    cog._schedule_guide_restick.assert_not_called()

    # A member message in the panel channel still re-sticks.
    await cog._restick_guide_panel(
        _listener_msg(author_bot=False, channel_id=CHANNEL_ID, message_id=777)
    )
    cog._schedule_guide_restick.assert_called_once_with(GUILD_ID)


@pytest.mark.asyncio
async def test_post_guide_disabled_gate(ctx, db):
    cog = _make_cog(ctx)
    interaction = _interaction(_member(admin=True), _channel(CHANNEL_ID))

    await _post_guide(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "isn't enabled" in msg


@pytest.mark.asyncio
async def test_post_guide_plain_member_refused(ctx, db):
    _enable(db, manager_role_id=MANAGER_ROLE_ID)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(), channel)

    await _post_guide(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "permission" in msg
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_guide_rejects_non_text_channel(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    interaction = _interaction(_member(admin=True), MagicMock())  # not a TextChannel

    await _post_guide(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "text channel" in msg


@pytest.mark.asyncio
async def test_post_guide_posts_and_saves_ids(ctx, db):
    _enable(
        db,
        currency_plural="Gems",
        currency_emoji="💎",
        manager_role_id=MANAGER_ROLE_ID,
    )
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(role_ids=(MANAGER_ROLE_ID,)), channel)

    await _post_guide(cog, interaction)

    embed = channel.send.await_args.kwargs["embed"]
    assert "Gems" in embed.title
    assert _stored(db) == (CHANNEL_ID, 8888)
    msg = interaction.response.send_message.await_args.args[0]
    assert "Posted" in msg and interaction.response.send_message.await_args.kwargs[
        "ephemeral"
    ]


@pytest.mark.asyncio
async def test_post_guide_explicit_channel_overrides_current(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    here, there = _channel(CHANNEL_ID), _channel(OTHER_CHANNEL_ID)
    interaction = _interaction(_member(admin=True), here)

    await _post_guide(cog, interaction, there)

    there.send.assert_awaited_once()
    here.send.assert_not_awaited()
    assert _stored(db) == (OTHER_CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_refreshes_in_place(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    old = MagicMock()
    old.edit = AsyncMock()
    channel.fetch_message.return_value = old
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    channel.fetch_message.assert_awaited_once_with(4444)
    old.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert _stored(db) == (CHANNEL_ID, 4444)  # ids unchanged
    msg = interaction.response.send_message.await_args.args[0]
    assert "Refreshed" in msg


@pytest.mark.asyncio
async def test_post_guide_reposts_when_old_message_gone(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    channel.fetch_message.side_effect = discord.NotFound(
        MagicMock(status=404), "gone"
    )
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    channel.send.assert_awaited_once()
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_move_deletes_old_panel(ctx, db):
    _enable(db, guide_channel_id=OTHER_CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    old_channel = _channel(OTHER_CHANNEL_ID)
    old = MagicMock()
    old.delete = AsyncMock()
    old_channel.fetch_message.return_value = old
    guild = FakeGuild(id=GUILD_ID, channels={OTHER_CHANNEL_ID: old_channel})
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(admin=True), channel, guild=guild)

    await _post_guide(cog, interaction)

    old.delete.assert_awaited_once()
    channel.send.assert_awaited_once()
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_forbidden_target(ctx, db):
    _enable(db)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    msg = interaction.response.send_message.await_args.args[0]
    assert "permission to post" in msg
    assert _stored(db) == (0, 0)  # nothing saved


# ── notifications toggle ─────────────────────────────────────────────────────


NOTIFY_ROLE_ID = 6060


def test_resolve_notify_toggle_grants_when_member_lacks_the_role():
    assert resolve_notify_toggle(role_id=NOTIFY_ROLE_ID, member_role_ids=set()) == "grant"
    assert (
        resolve_notify_toggle(role_id=NOTIFY_ROLE_ID, member_role_ids={999})
        == "grant"
    )


def test_resolve_notify_toggle_removes_when_member_holds_the_role():
    assert (
        resolve_notify_toggle(
            role_id=NOTIFY_ROLE_ID, member_role_ids={999, NOTIFY_ROLE_ID}
        )
        == "remove"
    )


def test_resolve_notify_toggle_unconfigured_without_a_role():
    # An unset role must not read as "grant" — there is nothing to grant.
    assert resolve_notify_toggle(role_id=0, member_role_ids={999}) == "unconfigured"


def _notify_interaction(db, *, member, guild):
    inter = fake_interaction(guild=guild)
    inter.user = member
    inter.client = MagicMock()
    inter.client.ctx = SimpleNamespace(db_path=db, open_db=lambda: open_db(db))
    return inter


def _notify_member_mock(*, role_ids: tuple[int, ...]) -> MagicMock:
    m = _member(role_ids=role_ids)
    m.add_roles = AsyncMock()
    m.remove_roles = AsyncMock()
    return m


def _guild_with_role(role_id: int | None):
    guild = FakeGuild(id=GUILD_ID)
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    guild.get_role = MagicMock(return_value=None if role_id is None else role)
    return guild, role


@pytest.mark.asyncio
async def test_notify_button_grants_role_and_confirms(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, role = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args[0] is role
    member.remove_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_ON_MSG


@pytest.mark.asyncio
async def test_notify_button_removes_role_when_already_opted_in(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, role = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=(NOTIFY_ROLE_ID,))
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    member.remove_roles.assert_awaited_once()
    assert member.remove_roles.await_args.args[0] is role
    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_OFF_MSG


@pytest.mark.asyncio
async def test_notify_button_inert_when_no_role_configured(db):
    _enable(db)  # game_role_id stays 0
    guild, _ = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_handles_deleted_role(db):
    # Configured, but the role has since been deleted in Discord.
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, _ = _guild_with_role(None)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    member.add_roles.assert_not_awaited()
    assert inter.response.send_message.await_args.args[0] == NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_reports_a_failed_role_edit(db):
    # Bot's own role sits below the notification role → Discord refuses.
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild, _ = _guild_with_role(NOTIFY_ROLE_ID)
    member = _notify_member_mock(role_ids=())
    member.add_roles.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    assert inter.response.send_message.await_args.args[0] == NOTIFY_FAILED_MSG


@pytest.mark.asyncio
async def test_notify_button_rejects_a_dm_click(db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    inter = fake_interaction(guild=None)
    inter.user = MagicMock(spec=discord.User)  # not a Member
    inter.client = MagicMock()

    await GuideNotifyButton().callback(inter)

    assert "only works in a server" in inter.response.send_message.await_args.args[0]


def test_guide_view_carries_the_persistent_toggle():
    view = GuideView()
    assert view.timeout is None  # persistent across restarts
    assert [item.custom_id for item in view.children] == [NOTIFY_CUSTOM_ID]


@pytest.mark.asyncio
async def test_post_guide_attaches_the_notify_button(ctx, db):
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    view = channel.send.await_args.kwargs["view"]
    assert [item.custom_id for item in view.children] == [NOTIFY_CUSTOM_ID]
