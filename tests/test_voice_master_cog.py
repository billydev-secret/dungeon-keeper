"""Cog/interaction tests for Voice Master.

Covers the apply-helpers and the resolution / edit-budget gating, with the
Discord side mocked. Pure-function logic is covered by
``tests/test_voice_master_service.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from migrations import apply_migrations_sync
from bot_modules.services.voice_master_service import (
    LOCKED_STATUS_TEXT,
    OPEN_STATUS_TEXT,
    add_name_blocklist,
    insert_active_channel,
    load_profile,
    record_edit_in_db,
)
from tests.fakes import fake_interaction

GUILD = 9001
OWNER = 1001
NEW_OWNER = 1002
CH = 5001


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    """A minimal AppContext stand-in exposing only what the helpers touch."""
    c = SimpleNamespace(
        db_path=db,
        guild_id=GUILD,
        open_db=lambda: open_db(db),
    )
    return c


@pytest.fixture
def voice_channel():
    """A MagicMock that quacks like a discord.VoiceChannel."""
    g = MagicMock()
    g.id = GUILD
    g.name = "Test Guild"
    g.default_role = MagicMock()
    g.default_role.id = 0

    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = CH
    ch.name = "Owner's Room"
    ch.guild = g
    ch.members = []
    ch.overwrites = {}
    ch.set_permissions = AsyncMock()
    ch.edit = AsyncMock()
    ch.delete = AsyncMock()
    ch.overwrites_for = MagicMock(return_value=discord.PermissionOverwrite())
    return ch


@pytest.fixture
def owner_member():
    m = MagicMock(spec=discord.Member)
    m.id = OWNER
    m.bot = False
    m.display_name = "Owner"
    m.name = "owner_user"
    m.mention = f"<@{OWNER}>"
    m.voice = None
    return m


def _wire_interaction(ctx, *, user_id: int = OWNER) -> MagicMock:
    """A fake interaction wired with our test ctx and a guild that resolves the test channel."""
    inter = fake_interaction()
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.mention = f"<@{user_id}>"
    inter.guild = MagicMock()
    inter.guild.id = GUILD
    inter.client = MagicMock()
    setattr(inter.client, "ctx", ctx)
    return inter


# ── Multi-guild safety: unconfigured guild must not create/delete channels ──


@pytest.mark.asyncio
async def test_voice_state_update_noop_for_unconfigured_guild(ctx):
    """A voice-state update in a guild with no Voice Master config must never
    create a channel. Guards the ``cfg.hub_channel_id == 0`` early-return that
    lets the cog run safely across all guilds after the home-gate removal.
    """
    from bot_modules.cogs.voice_master_cog import VoiceMasterCog

    cog = VoiceMasterCog(MagicMock(), ctx)

    guild = MagicMock()
    guild.id = 7777  # not GUILD — and the DB is empty, so unconfigured
    guild.create_voice_channel = AsyncMock()

    member = MagicMock(spec=discord.Member)
    member.bot = False
    member.id = OWNER
    member.guild = guild

    hub = MagicMock()
    hub.id = 12345
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=hub)

    await cog.on_voice_state_update(member, before, after)

    guild.create_voice_channel.assert_not_called()


# ── _resolve_owned_channel ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_owned_channel_no_channel_replies_friendly(ctx):
    from bot_modules.commands.voice_master_commands import _resolve_owned_channel

    inter = _wire_interaction(ctx)
    result = await _resolve_owned_channel(inter)
    assert result is None
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "join the Hub" in msg


@pytest.mark.asyncio
async def test_resolve_owned_channel_returns_channel_and_row(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _resolve_owned_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    result = await _resolve_owned_channel(inter)
    assert result is not None
    ch, row = result
    assert ch is voice_channel
    assert row.owner_id == OWNER


@pytest.mark.asyncio
async def test_resolve_owned_channel_handles_missing_discord_channel(ctx):
    from bot_modules.commands.voice_master_commands import _resolve_owned_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    result = await _resolve_owned_channel(inter)
    assert result is None
    inter.response.send_message.assert_awaited_once()


# ── _gate_and_record_edit (edit budget) ────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_and_record_edit_allows_first_edit(ctx):
    from bot_modules.commands.voice_master_commands import _gate_and_record_edit
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None
    ok = await _gate_and_record_edit(inter, row)
    assert ok is True
    # DB row should reflect the new edit.
    with open_db(ctx.db_path) as conn:
        updated = get_active_channel(conn, CH)
    assert updated is not None
    assert max(updated.last_edit_at_1, updated.last_edit_at_2) > 1.0


@pytest.mark.asyncio
async def test_gate_and_record_edit_blocks_when_budget_exhausted(ctx):
    """Two recent edits → third is rejected with the friendly retry message."""
    from bot_modules.commands.voice_master_commands import _gate_and_record_edit
    from bot_modules.services.voice_master_service import get_active_channel

    import time as time_module
    now = time_module.time()
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=now
        )
        # Manually mark two recent edits within the 600s window.
        record_edit_in_db(conn, CH, now=now - 60)
        record_edit_in_db(conn, CH, now=now - 30)
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None
    ok = await _gate_and_record_edit(inter, row)
    assert ok is False
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "try again" in msg.lower()


# ── _apply_access_state ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_access_locked_sets_overwrite_and_saves_profile(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_access_state(inter, voice_channel, row, state="locked")

    # No present members, so only the @everyone overwrite is written.
    voice_channel.set_permissions.assert_awaited_once()
    args, kwargs = voice_channel.set_permissions.await_args
    overwrite = kwargs["overwrite"]
    # Locked = age-gated + hidden: both View and Connect denied to @everyone.
    assert overwrite.connect is False
    assert overwrite.view_channel is False
    # Profile saved with the full locked flag set (locked ⇒ hidden ⇒ age_gated).
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.locked is True
    assert p.hidden is True
    assert p.age_gated is True
    assert p.spectator is False
    # Access state is advertised via the status line and the age gate flipped on;
    # the channel is never renamed to signal state.
    status_calls = [
        c.kwargs["status"]
        for c in voice_channel.edit.await_args_list
        if "status" in c.kwargs
    ]
    assert status_calls == [LOCKED_STATUS_TEXT]
    nsfw_calls = [
        c.kwargs["nsfw"]
        for c in voice_channel.edit.await_args_list
        if "nsfw" in c.kwargs
    ]
    assert nsfw_calls == [True]
    assert all("name" not in c.kwargs for c in voice_channel.edit.await_args_list)


@pytest.mark.asyncio
async def test_apply_access_nsfw_gates_without_locking(ctx, voice_channel):
    """The NSFW-open state age-gates but leaves @everyone able to see and join."""
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_access_state(inter, voice_channel, row, state="nsfw")

    args, kwargs = voice_channel.set_permissions.await_args
    overwrite = kwargs["overwrite"]
    # Nothing is denied to @everyone, so the overwrite is empty → cleared (None);
    # if present it must leave both connect and view inheriting.
    assert overwrite is None or (
        overwrite.connect is None and overwrite.view_channel is None
    )
    nsfw_calls = [
        c.kwargs["nsfw"]
        for c in voice_channel.edit.await_args_list
        if "nsfw" in c.kwargs
    ]
    assert nsfw_calls == [True]  # age gate on
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert (p.locked, p.hidden, p.spectator, p.age_gated) == (
        False, False, False, True
    )


@pytest.mark.asyncio
async def test_apply_access_open_clears_gate(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_access_state(inter, voice_channel, row, state="open")

    status_calls = [
        c.kwargs["status"]
        for c in voice_channel.edit.await_args_list
        if "status" in c.kwargs
    ]
    assert status_calls == [OPEN_STATUS_TEXT]
    nsfw_calls = [
        c.kwargs["nsfw"]
        for c in voice_channel.edit.await_args_list
        if "nsfw" in c.kwargs
    ]
    assert nsfw_calls == [False]  # age gate cleared


@pytest.mark.asyncio
async def test_apply_access_defers_before_slow_call(ctx, voice_channel):
    """If the response wasn't already done, _apply_access_state must defer first."""
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_access_state(inter, voice_channel, row, state="open")
    inter.response.defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_access_locked_grants_both_to_in_channel_members(ctx, voice_channel):
    """Entering the locked state hands each in-channel member both an explicit
    Connect and View overwrite so the channel's text chat keeps working — Discord
    ties text-chat access to both, which the @everyone denials would otherwise
    strip. The owner (persistent overwrite) and the bot are skipped."""
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    assert row is not None

    guild = voice_channel.guild
    guild.me = MagicMock()
    guild.me.id = 999  # bot — must be skipped
    owner_m = MagicMock(spec=discord.Member)
    owner_m.id = OWNER
    guest = MagicMock(spec=discord.Member)
    guest.id = 4242
    voice_channel.members = [owner_m, guest]
    guild.get_member = MagicMock(
        side_effect=lambda uid: {OWNER: owner_m, 4242: guest}.get(uid)
    )
    # Fresh overwrite per target so the @everyone and guest edits don't alias.
    voice_channel.overwrites_for = MagicMock(
        side_effect=lambda target: discord.PermissionOverwrite()
    )

    inter = _wire_interaction(ctx)
    await _apply_access_state(inter, voice_channel, row, state="locked")

    targets = [c.args[0] for c in voice_channel.set_permissions.await_args_list]
    assert guild.default_role in targets  # @everyone lock+hide applied
    assert guest in targets               # in-channel member granted
    assert owner_m not in targets         # owner skipped (persistent overwrite)
    guest_calls = [
        c for c in voice_channel.set_permissions.await_args_list if c.args[0] is guest
    ]
    # The guest is rescued with both a View grant (hide) and a Connect grant (lock).
    assert any(c.kwargs["overwrite"].view_channel is True for c in guest_calls)
    assert any(c.kwargs["overwrite"].connect is True for c in guest_calls)


@pytest.mark.asyncio
async def test_apply_access_open_clears_only_transient_lock_grants(ctx, voice_channel):
    """Leaving the locked state for open drops the transient text-chat grants
    (unhide then unlock). Owner, trusted and blocked entries survive. A one-off
    invited guest's now-redundant explicit overwrite is also cleared — harmless,
    since an open room grants access to everyone anyway."""
    from bot_modules.commands.voice_master_commands import _apply_access_state
    from bot_modules.services.voice_master_service import (
        add_blocked,
        add_trusted,
        get_active_channel,
    )

    TRUSTED, BLOCKED, GUEST, INVITED = 100, 200, 300, 400
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        add_trusted(conn, GUILD, OWNER, TRUSTED)
        add_blocked(conn, GUILD, OWNER, BLOCKED)
        row = get_active_channel(conn, CH)
    assert row is not None

    def mk(i):
        m = MagicMock(spec=discord.Member)
        m.id = i
        return m

    owner_m, trusted_m, blocked_m, guest_m, invited_m = (
        mk(OWNER), mk(TRUSTED), mk(BLOCKED), mk(GUEST), mk(INVITED)
    )
    member_overwrites = {
        owner_m: discord.PermissionOverwrite(connect=True, view_channel=True),
        trusted_m: discord.PermissionOverwrite(connect=True, view_channel=True),
        blocked_m: discord.PermissionOverwrite(connect=False),
        # Transient lock grant — connect only, view left to inherit.
        guest_m: discord.PermissionOverwrite(connect=True),
        # One-off invite/knock guest — not trusted, but carries view=True.
        invited_m: discord.PermissionOverwrite(connect=True, view_channel=True),
    }
    voice_channel.overwrites = member_overwrites
    # @everyone currently denies Connect — so the channel classifies as locked and
    # the leave-locked cleanup runs. overwrites_for resolves members from the dict.
    everyone_ow = discord.PermissionOverwrite(connect=False, view_channel=False)
    voice_channel.overwrites_for = MagicMock(
        side_effect=lambda t: (
            everyone_ow if t is voice_channel.guild.default_role
            else member_overwrites.get(t, discord.PermissionOverwrite())
        )
    )
    voice_channel.guild.get_member = MagicMock(
        side_effect=lambda uid: {
            OWNER: owner_m, TRUSTED: trusted_m, BLOCKED: blocked_m,
            GUEST: guest_m, INVITED: invited_m,
        }.get(uid)
    )

    inter = _wire_interaction(ctx)
    await _apply_access_state(inter, voice_channel, row, state="open")

    removed = [
        c.args[0]
        for c in voice_channel.set_permissions.await_args_list
        if c.kwargs.get("overwrite") is None
    ]
    # The transient grants are dropped; the privileged entries survive.
    assert guest_m in removed
    assert owner_m not in removed
    assert trusted_m not in removed
    assert blocked_m not in removed


# ── _apply_rename + name blocklist ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_rename_rejects_blocklisted_name(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        add_name_blocklist(conn, GUILD, "badword", added_by=OWNER)
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_rename(inter, voice_channel, row, new_name="My BADWORD Room")

    voice_channel.edit.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "filter" in msg.lower()


@pytest.mark.asyncio
async def test_apply_rename_succeeds_and_saves_name(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_rename(inter, voice_channel, row, new_name="Game Night")

    voice_channel.edit.assert_awaited_once()
    args, kwargs = voice_channel.edit.await_args
    # Lock state now lives on the status line, so the channel name is written
    # bare — no icon to strip from the saved profile name.
    assert kwargs["name"] == "Game Night"
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.saved_name == "Game Night"


# ── _apply_invite ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_invite_rejects_bot_target(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_invite
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    target = MagicMock(spec=discord.Member)
    target.bot = True
    target.id = 9999
    target.mention = "<@9999>"
    assert row is not None

    await _apply_invite(inter, voice_channel, row, target=target, remember=False)

    voice_channel.set_permissions.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "bot" in msg.lower()


@pytest.mark.asyncio
async def test_post_inline_panel_sends_panel_to_voice_chat(voice_channel, owner_member):
    """Posts an embed + view via channel.send."""
    from bot_modules.commands.voice_master_commands import post_inline_panel
    from unittest.mock import AsyncMock as _AM

    voice_channel.send = _AM(return_value=MagicMock())
    msg = await post_inline_panel(voice_channel, owner_member)
    assert msg is not None
    voice_channel.send.assert_awaited_once()
    assert voice_channel.send.await_args is not None
    kwargs = voice_channel.send.await_args.kwargs
    assert "embed" in kwargs
    assert "view" in kwargs
    # The embed should mention the owner.
    assert owner_member.mention in kwargs["embed"].description


@pytest.mark.asyncio
async def test_post_inline_panel_swallows_forbidden(voice_channel, owner_member):
    """A locked-down voice chat shouldn't crash the Hub-join flow."""
    from bot_modules.commands.voice_master_commands import post_inline_panel
    from unittest.mock import AsyncMock as _AM

    voice_channel.send = _AM(side_effect=discord.Forbidden(MagicMock(), "no perms"))
    msg = await post_inline_panel(voice_channel, owner_member)
    assert msg is None


@pytest.mark.asyncio
async def test_apply_invite_with_remember_writes_to_trust_list(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_invite
    from bot_modules.services.voice_master_service import get_active_channel, list_trusted

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    target = MagicMock(spec=discord.Member)
    target.id = NEW_OWNER
    target.bot = False
    target.mention = f"<@{NEW_OWNER}>"
    target.send = AsyncMock()  # for try_dm
    assert row is not None

    await _apply_invite(inter, voice_channel, row, target=target, remember=True)

    voice_channel.set_permissions.assert_awaited_once()
    with open_db(ctx.db_path) as conn:
        trusted = list_trusted(conn, GUILD, OWNER)
    assert NEW_OWNER in trusted


# ── voice-style lease gate on rename/limit (sinks round 3, stage 3) ─────────


def _arm_style_paywall(ctx, *, price: int = 30, enabled: bool = True) -> None:
    from bot_modules.services.economy_service import save_econ_settings

    with open_db(ctx.db_path) as conn:
        save_econ_settings(
            conn, GUILD, {"enabled": enabled, "price_voice_style": price}
        )


def _lease_voice_style(ctx, *, beneficiary: int = OWNER, payer: int | None = None) -> None:
    with open_db(ctx.db_path) as conn:
        conn.execute(
            """
            INSERT INTO econ_rentals
                (guild_id, user_id, perk, state, price, started_at, next_bill_at,
                 cancel_at_period_end, suspended, beneficiary_id, created_at)
            VALUES (?, ?, 'voice_style', 'active', 30, 1, 999999, 0, 0, ?, 1)
            """,
            (GUILD, payer if payer is not None else beneficiary, beneficiary),
        )


@pytest.mark.asyncio
async def test_apply_rename_blocked_by_armed_paywall(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename
    from bot_modules.services.voice_master_service import get_active_channel

    _arm_style_paywall(ctx)
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_rename(inter, voice_channel, row, new_name="Game Night")

    voice_channel.edit.assert_not_called()
    msg = inter.response.send_message.await_args.args[0]
    assert "leased" in msg.lower()


@pytest.mark.asyncio
async def test_apply_rename_passes_with_lease(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename
    from bot_modules.services.voice_master_service import get_active_channel

    _arm_style_paywall(ctx)
    _lease_voice_style(ctx)
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_rename(inter, voice_channel, row, new_name="Game Night")

    voice_channel.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_rename_free_while_dark_or_economy_off(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename
    from bot_modules.services.voice_master_service import get_active_channel

    # Case 1: economy on but price 0 (the shipped-dark default).
    _arm_style_paywall(ctx, price=0)
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None
    await _apply_rename(inter, voice_channel, row, new_name="Free Rename")
    voice_channel.edit.assert_awaited_once()

    # Case 2: priced but the economy itself is disabled.
    voice_channel.edit.reset_mock()
    _arm_style_paywall(ctx, price=30, enabled=False)
    inter = _wire_interaction(ctx)
    await _apply_rename(inter, voice_channel, row, new_name="Still Free")
    voice_channel.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_limit_blocked_and_passes_with_gifted_lease(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_limit
    from bot_modules.services.voice_master_service import get_active_channel

    _arm_style_paywall(ctx)
    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_limit(inter, voice_channel, row, new_limit=5)
    voice_channel.edit.assert_not_called()

    # A gifted lease (someone else pays, OWNER is beneficiary) unblocks.
    _lease_voice_style(ctx, beneficiary=OWNER, payer=999)
    inter = _wire_interaction(ctx)
    await _apply_limit(inter, voice_channel, row, new_limit=5)
    voice_channel.edit.assert_awaited_once()
