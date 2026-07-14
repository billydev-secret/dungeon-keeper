"""Economy guide panel — embed builder + /bank post-guide command."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.economy.guide import build_guide_embed, should_restick_guide
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
    embed = build_guide_embed(EconSettings(), colour=discord.Colour(0x123456))

    assert "Coins — how it works" in (embed.title or "")
    assert embed.colour == discord.Colour(0x123456)
    fields = {f.name: f.value or "" for f in embed.fields}
    earning = fields["Earning"]
    assert "🪙 5" in earning and "🪙 15" in earning  # login bases
    assert "+10" in earning  # streak cap
    assert "×1.5" in earning  # booster line
    assert "/bank quests" in earning
    spending = fields["Spending"]
    assert "50" in spending and "35" in spending  # colour / name prices
    assert "120" in spending and "75" in spending  # gradient / icon prices
    assert "/bank pay" in spending
    assert embed.footer.text and "grace" in embed.footer.text


def test_guide_embed_points_at_channels_and_roles_optin():
    fields = {
        f.name: f.value or ""
        for f in build_guide_embed(EconSettings()).fields
    }
    joining = fields["Joining"]
    assert "<id:customize>" in joining  # clickable "Channels & Roles" link
    assert "opt in" in joining.lower()


def test_guide_embed_uses_guild_branding():
    settings = EconSettings(
        currency_plural="Gems",
        currency_emoji="💎",
        currency_icon_url="https://cdn.example/gem.png",
        price_role_color=99,
    )
    embed = build_guide_embed(settings)

    assert "Gems" in (embed.title or "")
    assert "💎 99" in {f.name: f.value for f in embed.fields}["Spending"]
    assert embed.thumbnail.url == "https://cdn.example/gem.png"


def test_guide_embed_hides_pay_when_transfers_disabled():
    embed = build_guide_embed(EconSettings(transfers_enabled=False))
    spending = {f.name: f.value or "" for f in embed.fields}["Spending"]
    assert "/bank pay" not in spending


def test_guide_embed_hides_booster_line_without_bonus():
    embed = build_guide_embed(EconSettings(booster_multiplier=1.0))
    earning = {f.name: f.value or "" for f in embed.fields}["Earning"]
    assert "Boosters" not in earning


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


def test_restick_true_for_bot_notice_in_panel_channel():
    # Economy notices are bot-authored but still bury the panel — re-stick.
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
        new=AsyncMock(return_value=discord.Colour(0x123456)),
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
