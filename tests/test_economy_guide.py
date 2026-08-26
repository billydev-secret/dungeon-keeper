"""Economy guide — the embed builder, the ❓ button, and the 🔔 toggle.

Also the economy panel's **id wiring**, which lives here rather than with the
board it renders because the ids are the guide's: when the two panels merged on
2026-08-18 the surviving message was the guide's, so ``guide_channel_id`` /
``guide_message_id`` are what the one panel reads and writes. The placement
paths (edit in place, move, a refused post, the disabled gate) come along with
them; the panel's *content* and refresh loops are in
tests/test_economy_leaderboard.py.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot_modules.core.db_utils import open_db, set_config_value
from bot_modules.economy.guide import (
    HOW_IT_WORKS_CUSTOM_ID,
    NOTIFY_CUSTOM_ID,
    NOTIFY_FAILED_MSG,
    NOTIFY_OFF_MSG,
    NOTIFY_ON_MSG,
    NOTIFY_UNCONFIGURED_MSG,
    GuideNotifyButton,
    HowItWorksButton,
    build_guide_embed,
)
from bot_modules.economy.logic import resolve_notify_toggle
from bot_modules.services.economy_service import (
    EconSettings,
    load_econ_settings,
    save_econ_settings,
)
from tests.db_template import migrated_db
from tests.fakes import FakeGuild, fake_interaction

GUILD_ID = 9001
MANAGER_ROLE_ID = 7007
CHANNEL_ID = 111
OTHER_CHANNEL_ID = 222


# ── builder ─────────────────────────────────────────────────────────────────


def test_guide_embed_defaults_cover_earning_and_spending():
    embed = build_guide_embed(EconSettings(), color=discord.Color(0x123456))

    assert "Coins — How It Works" in (embed.title or "")
    assert embed.color == discord.Color(0x123456)
    fields = {f.name: f.value or "" for f in embed.fields}
    earning = fields["💰 Earning"]
    # what-pays-what rows are aligned: label in a code cell (padded to the
    # widest row), payment outside it — so match the label and pay separately
    # rather than pin the exact padding, which shifts as rows are added.
    assert "First message of the day" in earning
    assert "🪙 5" in earning  # text login base
    assert "🪙 15" in earning  # voice-first login base
    assert "/bank quests" in earning
    spending = fields["🛍️ Spending"]
    assert "/bank shop" in spending
    assert "color, name, gradient, holographic, icon" in spending  # perks named, not priced
    assert "prices in the shop" in spending  # specifics deferred to the shop
    assert "/bank pay" in spending
    # fine print (streak cap, booster, rental grace) collapses to the footer
    footer = embed.footer.text or ""
    assert "+10" in footer and "×1.5" in footer and "grace" in footer


def test_guide_embed_conversion_line_gated_on_rate():
    # The XP→coin faucet ships off (rate 0): the guide must not promise a
    # nightly conversion that no longer happens.
    off = build_guide_embed(EconSettings())  # default xp_per_coin == 0.0
    off_earning = {f.name: f.value or "" for f in off.fields}["💰 Earning"]
    assert "converts into" not in off_earning
    assert "/bank quests" in off_earning  # quests are still surfaced

    # Re-enabled (a positive rate): the conversion copy comes back.
    on = build_guide_embed(EconSettings(xp_per_coin=15.0))
    on_earning = {f.name: f.value or "" for f in on.fields}["💰 Earning"]
    assert "converts into" in on_earning
    assert "a day)" not in on_earning  # uncapped: no ceiling promised

    # A ceiling is named, so a heavy day that stops paying isn't a mystery.
    capped = build_guide_embed(
        EconSettings(xp_per_coin=15.0, conversion_daily_cap=1200)
    )
    capped_earning = {f.name: f.value or "" for f in capped.fields}["💰 Earning"]
    assert "up to 1,200 a day" in capped_earning


def test_guide_embed_offers_notifications_not_channel_access():
    fields = {
        f.name: f.value or ""
        for f in build_guide_embed(EconSettings()).fields
    }
    notifications = fields["🔔 Notifications"]
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
    assert "💎" in fields["💰 Earning"]  # emoji flows into the earn lines
    assert "Gems" in fields["🛍️ Spending"]  # plural flows into the /bank pay line
    assert embed.thumbnail.url == "https://cdn.example/gem.png"


def test_guide_embed_hides_pay_when_transfers_disabled():
    embed = build_guide_embed(EconSettings(transfers_enabled=False))
    spending = {f.name: f.value or "" for f in embed.fields}["🛍️ Spending"]
    assert "/bank pay" not in spending


def test_guide_embed_hides_booster_line_without_bonus():
    embed = build_guide_embed(EconSettings(booster_multiplier=1.0))
    assert "Boosters" not in (embed.footer.text or "")


# ── sticky re-stick predicate ────────────────────────────────────────────────

PANEL_CH = 4242
PANEL_MSG = 9999







# ── settings round-trip ─────────────────────────────────────────────────────


def test_guide_ids_round_trip(tmp_path):
    db = tmp_path / "test.db"
    migrated_db(db)
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
    migrated_db(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    return SimpleNamespace(
        db_path=db, open_db=lambda: open_db(db), set_config_value=_set
    )


@pytest.fixture(autouse=True)
def _patch_accent():
    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        new=AsyncMock(return_value=discord.Color(0x123456)),
    ):
        yield


def _make_cog(ctx):
    from bot_modules.cogs.economy_cog import EconomyCog

    _bot = MagicMock()
    _bot.ctx = ctx
    return EconomyCog(_bot)


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
    # core.sticky edits/deletes through a partial message (one REST call).
    ch.get_partial_message = MagicMock(return_value=MagicMock(
        edit=AsyncMock(), delete=AsyncMock()
    ))
    return ch


def _interaction(actor, channel, guild=None):
    inter = fake_interaction(guild=guild or FakeGuild(id=GUILD_ID))
    inter.user = actor
    inter.channel = channel
    return inter


async def _post_guide(cog, interaction, channel=None):
    """Post the economy panel.

    /bank post-guide was replaced by a dashboard post control on 2026-07-28,
    and the panel it placed absorbed the leaderboard on 2026-08-18, so this
    drives the one cog method the route calls. ``interaction`` is kept in the
    signature to leave the call sites alone — only its guild is used now, since
    permission is the route's job.
    """
    return await cog.post_economy_panel(
        interaction.guild, channel or interaction.channel
    )


def _stored(db) -> tuple[int, int]:
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    return s.guide_channel_id, s.guide_message_id


# ── sticky repost ────────────────────────────────────────────────────────────


# The sticky machinery itself (debounce, cancel-and-rearm, bot-message skip,
# repost-never-creates, id caching) lives in core.sticky and is covered by
# tests/test_core_sticky.py. What stays here is the economy-specific wiring:
# which ids the panel reads, and the disabled-economy gate.


@pytest.mark.asyncio
async def test_panel_ids_are_zero_when_the_economy_is_disabled(ctx, db):
    """A disabled economy must read as "no panel", so nothing re-sticks."""
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    with open_db(db) as conn:
        save_econ_settings(conn, GUILD_ID, {"enabled": "0"})
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "panel") == (0, 0)


@pytest.mark.asyncio
async def test_panel_ids_read_the_guide_fields(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    assert cog._panel_ids(GUILD_ID, "panel") == (CHANNEL_ID, 4444)


def test_save_panel_ids_writes_only_the_guide_fields(ctx, db):
    _enable(db, shop_channel_id=777, shop_message_id=888)
    cog = _make_cog(ctx)
    cog._save_panel_ids(GUILD_ID, "panel", CHANNEL_ID, 4444)
    with open_db(db) as conn:
        s = load_econ_settings(conn, GUILD_ID)
    assert (s.guide_channel_id, s.guide_message_id) == (CHANNEL_ID, 4444)
    assert (s.shop_channel_id, s.shop_message_id) == (777, 888)  # untouched


@pytest.mark.asyncio
async def test_post_guide_disabled_gate(ctx, db):
    cog = _make_cog(ctx)
    interaction = _interaction(_member(admin=True), _channel(CHANNEL_ID))

    # Domain rule, kept on the cog: no currency, no currency guide, however you
    # got here. It raises rather than returning None so the route can name the
    # actual fix — a bare None surfaces as "Discord rejected the post", which
    # sends the admin to check bot permissions instead of the economy toggle.
    with pytest.raises(ValueError, match="disabled"):
        await _post_guide(cog, interaction)


# "Plain member refused" and "rejects a non-text channel" moved with the
# command: post_economy_panel is unguarded by design, and its only caller is
# POST /api/panels/{key}/post, which is admin-gated and does the channel-type
# check (tests/web/test_panels_routes.py, plus the authz sweep).


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
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock(), id=4444)
    channel.get_partial_message = MagicMock(return_value=old)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    # Same channel → edited in place, so the panel doesn't hop to the bottom.
    old.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert _stored(db) == (CHANNEL_ID, 4444)  # ids unchanged


@pytest.mark.asyncio
async def test_post_guide_reposts_when_old_message_gone(ctx, db):
    _enable(db, guide_channel_id=CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    gone = MagicMock(delete=AsyncMock())
    gone.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    channel.get_partial_message = MagicMock(return_value=gone)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    channel.send.assert_awaited_once()
    assert _stored(db) == (CHANNEL_ID, 8888)


@pytest.mark.asyncio
async def test_post_guide_move_deletes_old_panel(ctx, db):
    _enable(db, guide_channel_id=OTHER_CHANNEL_ID, guide_message_id=4444)
    cog = _make_cog(ctx)
    old_channel = _channel(OTHER_CHANNEL_ID)
    old = MagicMock(edit=AsyncMock(), delete=AsyncMock())
    old_channel.get_partial_message = MagicMock(return_value=old)
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

    result = await _post_guide(cog, interaction)

    # The cog reports failure by returning None; turning that into a
    # user-facing "missing permission" message is the route's job now
    # (tests/web/test_panels_routes.py).
    assert result is None
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
    def _set(key, value, guild_id=GUILD_ID):
        # core.role_provision persists a provisioned role id through this.
        with open_db(db) as conn:
            set_config_value(conn, key, value, guild_id)
            conn.commit()
        return value

    inter.client.ctx = SimpleNamespace(
        db_path=db, open_db=lambda: open_db(db), set_config_value=_set
    )
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
async def test_notify_button_provisions_the_role_when_never_configured(db):
    """The dead end this stage exists to remove.

    Pressing 🔔 on a guild that never set an opt-in role used to tell the
    member to go find an admin. Now the role gets made and granted — this is
    the one place in the feature where "first use" is a member asking for it.
    """
    _enable(db)  # no row at all — never configured
    guild = FakeGuild(id=GUILD_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    made = list(guild.roles)
    assert [r.name for r in made] == ["Economy Notifications"]
    member.add_roles.assert_awaited_once()
    assert member.add_roles.await_args.args[0] is made[0]
    assert inter.response.send_message.await_args.args[0] != NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_provisions_even_over_a_stored_zero(db):
    """A stored 0 on THIS dial is not a decision (Billy, 2026-08-22).

    Economy Settings saves as one form and writes ``game_role_id: "0"`` for an
    untouched picker on every save, so changing a payout leaves a 0 here — and
    prod has exactly that in two guilds. There is also no coherent "off" for an
    opt-in role: with no role, nobody can opt in at all. So unlike every other
    ping dial, a 0 here still provisions.
    """
    _enable(db, game_role_id=0)
    guild = FakeGuild(id=GUILD_ID)
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    made = list(guild.roles)
    assert [r.name for r in made] == ["Economy Notifications"]
    member.add_roles.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] != NOTIFY_UNCONFIGURED_MSG


@pytest.mark.asyncio
async def test_notify_button_remakes_a_deleted_role(db):
    """Configured, but the role was deleted in Discord since.

    Used to be the same dead end as unconfigured. Now it is remade and granted
    — and because an id *was* stored, this counts as a recreate, so the mod
    channel hears about it (the old role's holders are gone).
    """
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    guild = FakeGuild(id=GUILD_ID)  # role 6060 is not in it
    member = _notify_member_mock(role_ids=())
    inter = _notify_interaction(db, member=member, guild=guild)

    await GuideNotifyButton().callback(inter)

    made = list(guild.roles)
    assert [r.name for r in made] == ["Economy Notifications"]
    assert made[0].id != NOTIFY_ROLE_ID
    member.add_roles.assert_awaited_once()


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


# The button row's own contract (order, ids, no timeout) lives with the view
# in tests/test_economy_quest_views.py. What is asserted here is the wiring —
# that the panel the cog sends carries the guide's two buttons at all.


@pytest.mark.asyncio
async def test_posting_the_panel_attaches_the_guide_buttons(ctx, db):
    """The wiring assertion: the panel the cog actually sends carries them."""
    _enable(db, game_role_id=NOTIFY_ROLE_ID)
    cog = _make_cog(ctx)
    channel = _channel(CHANNEL_ID)
    interaction = _interaction(_member(admin=True), channel)

    await _post_guide(cog, interaction)

    view = channel.send.await_args.kwargs["view"]
    ids = [item.custom_id for item in view.children]
    assert HOW_IT_WORKS_CUSTOM_ID in ids
    assert NOTIFY_CUSTOM_ID in ids


@pytest.mark.asyncio
async def test_how_it_works_serves_the_guide_ephemerally(ctx, db):
    """The ❓ button is the only route to the guide now, so it renders the same
    builder the panel used to be — privately, and off live settings."""
    _enable(db, currency_plural="Gems", currency_emoji="💎")
    guild = FakeGuild(id=GUILD_ID)
    inter = fake_interaction(guild=guild)
    inter.client = SimpleNamespace(ctx=ctx)
    inter.response.send_message = AsyncMock()

    with patch(
        "bot_modules.core.branding.resolve_accent_color",
        AsyncMock(return_value=discord.Color(0x123456)),
    ):
        await HowItWorksButton().callback(inter)

    kwargs = inter.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    embed = kwargs["embed"]
    assert "Gems" in embed.title and "How It Works" in embed.title
    assert embed.color == discord.Color(0x123456)


@pytest.mark.asyncio
async def test_how_it_works_outside_a_guild_says_so(ctx, db):
    inter = fake_interaction()
    inter.guild = None  # fake_interaction substitutes a guild for None
    inter.client = SimpleNamespace(ctx=ctx)

    await HowItWorksButton().callback(inter)

    assert "only works in a server" in inter.response.send_message.await_args.args[0]
