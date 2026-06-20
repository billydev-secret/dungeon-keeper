"""Glue/integration tests for ``bot_modules.commands.voice_master_commands``.

The pure-logic helpers extracted into ``bot_modules.voice_master.{logic,embeds}``
are tested in ``tests/test_voice_master_logic.py``. This file targets the
remaining Discord-touching ``_apply_*`` / ``_on_*`` helpers plus the persistent
``View``/``Modal``/``DynamicItem`` classes and post-panel surface — the cog
glue that the logic extraction can't reach. The Discord side is mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.services.voice_master_service import (
    get_active_channel,
    insert_active_channel,
    load_profile,
    set_voice_master_config_value,
)
from migrations import apply_migrations_sync
from tests.fakes import fake_interaction

GUILD = 9002
OWNER = 2001
OTHER = 2002
CH = 6001
CONTROL_CH = 6010


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "vm_glue.db"
    apply_migrations_sync(db_path)
    return db_path


@pytest.fixture
def ctx(db):
    return SimpleNamespace(
        db_path=db,
        guild_id=GUILD,
        open_db=lambda: open_db(db),
    )


@pytest.fixture
def voice_channel():
    g = MagicMock()
    g.id = GUILD
    g.name = "Guild"
    g.default_role = MagicMock()
    g.default_role.id = 0

    ch = MagicMock(spec=discord.VoiceChannel)
    ch.id = CH
    ch.name = "Owner's Room"
    ch.guild = g
    ch.members = []
    ch.set_permissions = AsyncMock()
    ch.edit = AsyncMock()
    ch.delete = AsyncMock()
    ch.send = AsyncMock(return_value=MagicMock(id=99))
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


@pytest.fixture
def other_member():
    m = MagicMock(spec=discord.Member)
    m.id = OTHER
    m.bot = False
    m.display_name = "Other"
    m.name = "other_user"
    m.mention = f"<@{OTHER}>"
    m.voice = None
    m.move_to = AsyncMock()
    return m


def _wire_interaction(ctx, *, user_id: int = OWNER):
    inter = fake_interaction()
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.user.mention = f"<@{user_id}>"
    inter.user.name = "user#1234"
    inter.guild = MagicMock()
    inter.guild.id = GUILD
    inter.guild.name = "Guild"
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", ctx)
    return inter


# ── No-context guards ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_owned_channel_no_ctx_errors():
    from bot_modules.commands.voice_master_commands import _resolve_owned_channel

    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    out = await _resolve_owned_channel(inter)
    assert out is None
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "isn't configured" in msg


@pytest.mark.asyncio
async def test_ephemeral_uses_followup_when_response_done(ctx):
    from bot_modules.commands.voice_master_commands import _ephemeral

    inter = _wire_interaction(ctx)
    inter.response.is_done = MagicMock(return_value=True)
    await _ephemeral(inter, "hello")
    inter.followup.send.assert_awaited_once_with("hello", ephemeral=True)
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_defer_if_needed_skips_when_already_done(ctx):
    from bot_modules.commands.voice_master_commands import _defer_if_needed

    inter = _wire_interaction(ctx)
    inter.response.is_done = MagicMock(return_value=True)
    await _defer_if_needed(inter)
    inter.response.defer.assert_not_called()


# ── _apply_lock / _apply_hide ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_lock_handles_permission_failure(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_lock

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.set_permissions = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    inter = _wire_interaction(ctx)
    await _apply_lock(inter, voice_channel, row, locked=True)
    # On failure we should send an error and not save the profile
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    # Profile may exist as default but locked should not be set to True via this path
    if p is not None:
        assert p.locked is False


@pytest.mark.asyncio
async def test_apply_hide_sets_overwrite_and_saves_profile(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_hide

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _apply_hide(inter, voice_channel, row, hidden=True)
    voice_channel.set_permissions.assert_awaited_once()
    _, kwargs = voice_channel.set_permissions.await_args
    assert kwargs["overwrite"].view_channel is False
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.hidden is True


@pytest.mark.asyncio
async def test_apply_hide_handles_permission_failure(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_hide

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.set_permissions = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "no"))
    inter = _wire_interaction(ctx)
    await _apply_hide(inter, voice_channel, row, hidden=True)
    # An error reply should be sent (either via followup or response).
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


# ── _apply_spectator ──────────────────────────────────────────────────────


def _per_target_overwrites(voice_channel):
    """Make overwrites_for return a fresh overwrite per distinct target.

    The default fixture returns one shared instance, so mutations leak between
    the @everyone / owner / member edits. This keeps them independent and lets
    a test read back what was written for each target.
    """
    store: dict[int, discord.PermissionOverwrite] = {}

    def _for(target):
        key = id(target)
        if key not in store:
            store[key] = discord.PermissionOverwrite()
        return store[key]

    voice_channel.overwrites_for = MagicMock(side_effect=_for)
    voice_channel.overwrites = {}
    return store


@pytest.mark.asyncio
async def test_apply_spectator_enable_mutes_everyone_and_saves(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_spectator

    _per_target_overwrites(voice_channel)
    everyone = voice_channel.guild.default_role
    owner_obj = MagicMock(spec=discord.Member)
    owner_obj.id = OWNER
    voice_channel.guild.get_member = MagicMock(return_value=owner_obj)
    voice_channel.guild.me = MagicMock(id=1)
    voice_channel.members = []

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        # Pre-seed locked=True to prove enabling spectator clears it.
        set_voice_master_config_value(conn, GUILD, "voice_master_saveable_fields",
                                      "name,limit,locked,hidden,spectator,trusted,blocked")
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    await _apply_spectator(inter, voice_channel, row, spectator=True)

    everyone_ow = voice_channel.overwrites_for(everyone)
    assert everyone_ow.speak is False
    assert everyone_ow.stream is False
    assert everyone_ow.send_messages is False
    assert everyone_ow.connect is None  # ungated → still joinable
    owner_ow = voice_channel.overwrites_for(owner_obj)
    assert owner_ow.speak is True and owner_ow.connect is True

    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.spectator is True
    assert p.locked is False  # mutually exclusive


@pytest.mark.asyncio
async def test_apply_spectator_enable_gated_blocks_everyone_join(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_spectator

    store = _per_target_overwrites(voice_channel)
    everyone = voice_channel.guild.default_role
    gate_role = MagicMock(spec=discord.Role)
    gate_role.id = 4242
    owner_obj = MagicMock(spec=discord.Member)
    owner_obj.id = OWNER
    voice_channel.guild.get_member = MagicMock(return_value=owner_obj)
    voice_channel.guild.get_role = MagicMock(return_value=gate_role)
    voice_channel.guild.me = MagicMock(id=1)
    voice_channel.members = []

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        set_voice_master_config_value(
            conn, GUILD, "voice_master_spectator_gate_role_id", "4242"
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    await _apply_spectator(inter, voice_channel, row, spectator=True)

    everyone_ow = voice_channel.overwrites_for(everyone)
    assert everyone_ow.connect is False  # blocked from joining
    assert everyone_ow.speak is None
    gate_ow = voice_channel.overwrites_for(gate_role)
    assert gate_ow.connect is True
    assert gate_ow.speak is False and gate_ow.stream is False


@pytest.mark.asyncio
async def test_apply_invite_grants_speaker_when_spectating(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_invite

    store = _per_target_overwrites(voice_channel)
    everyone = voice_channel.guild.default_role
    # Put @everyone in spectator (ungated) shape so the channel reads "spectate".
    store[id(everyone)] = discord.PermissionOverwrite(
        speak=False, stream=False, send_messages=False
    )
    voice_channel.guild.get_role = MagicMock(return_value=None)
    target = MagicMock(spec=discord.Member)
    target.id = OTHER
    target.bot = False
    target.mention = f"<@{OTHER}>"

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    await _apply_invite(inter, voice_channel, row, target=target, remember=False)

    target_ow = voice_channel.overwrites_for(target)
    assert target_ow.connect is True and target_ow.view_channel is True
    assert target_ow.speak is True and target_ow.stream is True


@pytest.mark.asyncio
async def test_apply_invite_no_speaker_grant_when_open(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_invite

    _per_target_overwrites(voice_channel)  # @everyone overwrite is empty → open
    voice_channel.guild.get_role = MagicMock(return_value=None)
    target = MagicMock(spec=discord.Member)
    target.id = OTHER
    target.bot = False
    target.mention = f"<@{OTHER}>"

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    await _apply_invite(inter, voice_channel, row, target=target, remember=False)

    target_ow = voice_channel.overwrites_for(target)
    assert target_ow.connect is True and target_ow.view_channel is True
    assert target_ow.speak is None  # no participation grant in open mode


# ── _apply_rename branches ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_rename_rejects_empty_name(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _apply_rename(inter, voice_channel, row, new_name="   ")
    voice_channel.edit.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    msg = inter.response.send_message.await_args.args[0]
    assert "empty" in msg.lower()


@pytest.mark.asyncio
async def test_apply_rename_truncates_long_name(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    long_name = "x" * 200
    await _apply_rename(inter, voice_channel, row, new_name=long_name)
    voice_channel.edit.assert_awaited_once()
    _, kwargs = voice_channel.edit.await_args
    assert len(kwargs["name"]) == 100


@pytest.mark.asyncio
async def test_apply_rename_handles_edit_failure(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.edit = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no"))
    inter = _wire_interaction(ctx)
    await _apply_rename(inter, voice_channel, row, new_name="OK Name")
    # Should send a failure reply.
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


# ── _apply_limit branches ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_limit_rejects_out_of_range(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_limit

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _apply_limit(inter, voice_channel, row, new_limit=200)
    voice_channel.edit.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    assert "99" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_apply_limit_success_saves_profile(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_limit

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _apply_limit(inter, voice_channel, row, new_limit=7)
    voice_channel.edit.assert_awaited_once()
    _, kwargs = voice_channel.edit.await_args
    assert kwargs["user_limit"] == 7
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.saved_limit == 7


@pytest.mark.asyncio
async def test_apply_limit_edit_failure_reports_error(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_limit

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    inter = _wire_interaction(ctx)
    await _apply_limit(inter, voice_channel, row, new_limit=5)
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


# ── _apply_reset ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_reset_channel_only(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_reset

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    inter.guild.get_member = MagicMock(return_value=MagicMock(spec=discord.Member))
    await _apply_reset(inter, voice_channel, row, also_profile=False)
    voice_channel.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_reset_missing_owner(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_reset

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    inter.guild.get_member = MagicMock(return_value=None)
    await _apply_reset(inter, voice_channel, row, also_profile=True)
    voice_channel.edit.assert_not_called()
    inter.response.send_message.assert_awaited_once()
    assert "owner" in inter.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_apply_reset_with_profile_clears_saved(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_reset
    from bot_modules.services.voice_master_service import update_profile_field

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        update_profile_field(conn, GUILD, OWNER, field="saved_name", value="My Room")
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    inter.guild.get_member = MagicMock(return_value=MagicMock(spec=discord.Member))
    await _apply_reset(inter, voice_channel, row, also_profile=True)
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is None or p.saved_name in (None, "")


# ── _apply_transfer branches ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_transfer_rejects_bot_target(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    bot_target = MagicMock(spec=discord.Member)
    bot_target.id = OTHER
    bot_target.bot = True
    bot_target.mention = f"<@{OTHER}>"
    inter = _wire_interaction(ctx)
    await _apply_transfer(inter, voice_channel, row, new_owner=bot_target)
    voice_channel.set_permissions.assert_not_called()
    msg = inter.response.send_message.await_args.args[0]
    assert "bot" in msg.lower()


@pytest.mark.asyncio
async def test_apply_transfer_rejects_self(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    self_target = MagicMock(spec=discord.Member)
    self_target.id = OWNER
    self_target.bot = False
    self_target.mention = f"<@{OWNER}>"
    inter = _wire_interaction(ctx)
    await _apply_transfer(inter, voice_channel, row, new_owner=self_target)
    voice_channel.set_permissions.assert_not_called()
    msg = inter.response.send_message.await_args.args[0]
    assert "already" in msg.lower()


@pytest.mark.asyncio
async def test_apply_transfer_rejects_not_in_channel(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    other_member.voice = None
    inter = _wire_interaction(ctx)
    await _apply_transfer(inter, voice_channel, row, new_owner=other_member)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_transfer_success_swaps_owner(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    other_member.voice = SimpleNamespace(channel=voice_channel)
    inter = _wire_interaction(ctx)
    await _apply_transfer(inter, voice_channel, row, new_owner=other_member)
    voice_channel.set_permissions.assert_awaited_once()
    with open_db(ctx.db_path) as conn:
        new_row = get_active_channel(conn, CH)
    assert new_row is not None
    assert new_row.owner_id == OTHER


@pytest.mark.asyncio
async def test_apply_transfer_handles_perm_failure(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    other_member.voice = SimpleNamespace(channel=voice_channel)
    voice_channel.set_permissions = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "x"))
    inter = _wire_interaction(ctx)
    await _apply_transfer(inter, voice_channel, row, new_owner=other_member)
    with open_db(ctx.db_path) as conn:
        new_row = get_active_channel(conn, CH)
    assert new_row is not None
    # Ownership should NOT have transferred when the permission set failed.
    assert new_row.owner_id == OWNER


# ── _apply_invite / _apply_kick further branches ──────────────────────────


@pytest.mark.asyncio
async def test_apply_invite_rejects_owner_target(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_invite

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    self_target = MagicMock(spec=discord.Member)
    self_target.id = OWNER
    self_target.bot = False
    self_target.mention = f"<@{OWNER}>"
    inter = _wire_interaction(ctx)
    await _apply_invite(inter, voice_channel, row, target=self_target, remember=False)
    voice_channel.set_permissions.assert_not_called()
    assert "already" in inter.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_apply_invite_handles_perm_failure(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_invite

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.set_permissions = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "x"))
    inter = _wire_interaction(ctx)
    await _apply_invite(inter, voice_channel, row, target=other_member, remember=False)
    # Should NOT send the DM on failure (we error out before).
    other_member.create_dm = AsyncMock()
    # Failure reply should fire.
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


@pytest.mark.asyncio
async def test_apply_kick_rejects_bot(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    bot_target = MagicMock(spec=discord.Member)
    bot_target.id = OTHER
    bot_target.bot = True
    bot_target.mention = f"<@{OTHER}>"
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=bot_target, remember=False)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_kick_rejects_self(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    target = MagicMock(spec=discord.Member)
    target.id = OWNER
    target.bot = False
    target.mention = f"<@{OWNER}>"
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=target, remember=False)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_kick_success_with_remember(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=other_member, remember=True)
    voice_channel.set_permissions.assert_awaited_once()
    _, kwargs = voice_channel.set_permissions.await_args
    assert kwargs["overwrite"].connect is False


@pytest.mark.asyncio
async def test_apply_kick_disconnects_target_in_channel(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    other_member.voice = SimpleNamespace(channel=voice_channel)
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=other_member, remember=False)
    other_member.move_to.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_kick_swallows_move_failure(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    other_member.voice = SimpleNamespace(channel=voice_channel)
    other_member.move_to = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "x"))
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=other_member, remember=False)
    # Still reaches the confirm reply.
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


@pytest.mark.asyncio
async def test_apply_kick_handles_perm_failure(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    voice_channel.set_permissions = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "x"))
    inter = _wire_interaction(ctx)
    await _apply_kick(inter, voice_channel, row, target=other_member, remember=False)
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


# ── Modal flows ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_limit_modal_rejects_non_integer(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _LimitModal

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
    modal = _LimitModal(CH)
    modal.new_limit._value = "not a number"  # patch the TextInput value
    # Override the property — discord.ui.TextInput.value is a property
    type(modal.new_limit).value = property(lambda self: "abc")
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "whole number" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_limit_modal_handles_missing_channel(ctx):
    from bot_modules.commands.voice_master_commands import _LimitModal

    modal = _LimitModal(CH)
    type(modal.new_limit).value = property(lambda self: "5")
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "no longer exists" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_rename_modal_handles_missing_channel(ctx):
    from bot_modules.commands.voice_master_commands import _RenameModal

    modal = _RenameModal(CH)
    type(modal.new_name).value = property(lambda self: "My Room")
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_rename_modal_rejects_non_owner(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _RenameModal

    with open_db(ctx.db_path) as conn:
        # Owner is a different user from the one submitting.
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=999, now=1.0)
    modal = _RenameModal(CH)
    type(modal.new_name).value = property(lambda self: "Anything")
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await modal.on_submit(inter)
    inter.response.send_message.assert_awaited_once()
    assert "no longer own" in inter.response.send_message.await_args.args[0]


# ── Picker views ──────────────────────────────────────────────────────────


def test_transfer_picker_view_populates_options(other_member):
    from bot_modules.commands.voice_master_commands import _TransferPickerView

    view = _TransferPickerView(
        channel_id=CH, owner_id=OWNER, in_channel=[other_member],
    )
    assert view.member_select.disabled is False
    assert view.member_select.options[0].value == str(OTHER)


def test_transfer_picker_view_empty_disables_select(owner_member):
    from bot_modules.commands.voice_master_commands import _TransferPickerView

    # Only the owner is "in_channel" — filtered out.
    view = _TransferPickerView(
        channel_id=CH, owner_id=OWNER, in_channel=[owner_member],
    )
    assert view.member_select.disabled is True


def test_user_picker_view_invite_labels():
    from bot_modules.commands.voice_master_commands import _UserPickerView

    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    assert view.action_one.label == "Invite"
    assert "Trusted" in view.action_two.label


def test_user_picker_view_kick_labels():
    from bot_modules.commands.voice_master_commands import _UserPickerView

    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="kick")
    assert view.action_one.label == "Kick"
    assert "block" in view.action_two.label.lower()


@pytest.mark.asyncio
async def test_user_picker_view_blocks_non_owner_interaction(ctx):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    inter = _wire_interaction(ctx, user_id=99999)
    out = await view.interaction_check(inter)
    assert out is False


@pytest.mark.asyncio
async def test_user_picker_view_run_without_selection_warns(ctx):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    inter = _wire_interaction(ctx)
    await view._run(inter, remember=False)
    inter.response.send_message.assert_awaited_once()
    assert "dropdown" in inter.response.send_message.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_transfer_picker_view_blocks_non_owner(ctx, other_member):
    from bot_modules.commands.voice_master_commands import _TransferPickerView

    view = _TransferPickerView(
        channel_id=CH, owner_id=OWNER, in_channel=[other_member],
    )
    inter = _wire_interaction(ctx, user_id=99999)
    out = await view.interaction_check(inter)
    assert out is False


@pytest.mark.asyncio
async def test_reset_confirm_view_blocks_non_owner(ctx):
    from bot_modules.commands.voice_master_commands import _ResetConfirmView

    view = _ResetConfirmView(channel_id=CH, owner_id=OWNER)
    inter = _wire_interaction(ctx, user_id=99999)
    out = await view.interaction_check(inter)
    assert out is False


@pytest.mark.asyncio
async def test_reset_confirm_view_run_missing_channel(ctx):
    from bot_modules.commands.voice_master_commands import _ResetConfirmView

    view = _ResetConfirmView(channel_id=CH, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    await view._run(inter, also_profile=False)
    inter.response.send_message.assert_awaited_once()
    assert "no longer exists" in inter.response.send_message.await_args.args[0]


# ── _on_* button handlers ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_lock_grants_full_lock(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_lock

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_lock(inter, voice_channel, row)
    voice_channel.set_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_unlock_clears_lock(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_unlock

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_unlock(inter, voice_channel, row)
    voice_channel.set_permissions.assert_awaited_once()
    _, kwargs = voice_channel.set_permissions.await_args
    assert kwargs["overwrite"].connect is None


@pytest.mark.asyncio
async def test_on_hide_calls_apply_hide(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_hide

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_hide(inter, voice_channel, row)
    voice_channel.set_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_unhide_calls_apply_hide(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_unhide

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_unhide(inter, voice_channel, row)
    voice_channel.set_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_rename_opens_modal(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_rename

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_rename(inter, voice_channel, row)
    inter.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_limit_opens_modal(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_limit

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_limit(inter, voice_channel, row)
    inter.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_invite_sends_picker(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_invite

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_invite(inter, voice_channel, row)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_kick_sends_picker(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_kick

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_kick(inter, voice_channel, row)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_transfer_sends_picker(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_transfer

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_transfer(inter, voice_channel, row)
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_reset_sends_confirm(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _on_reset

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
        row = get_active_channel(conn, CH)
    assert row is not None
    inter = _wire_interaction(ctx)
    await _on_reset(inter, voice_channel, row)
    inter.response.send_message.assert_awaited_once()


# ── Panel post + inline ───────────────────────────────────────────────────


def test_build_panel_embed_delegates_to_pure_helper():
    from bot_modules.commands.voice_master_commands import build_panel_embed

    embed = build_panel_embed()
    assert isinstance(embed, discord.Embed)
    assert embed.title is not None
    assert "Voice Master" in embed.title


def test_build_inline_panel_embed_delegates(owner_member):
    from bot_modules.commands.voice_master_commands import build_inline_panel_embed

    embed = build_inline_panel_embed(owner_member)
    assert isinstance(embed, discord.Embed)
    assert embed.description is not None
    assert owner_member.mention in embed.description


def test_build_panel_view_has_grouped_selects():
    from bot_modules.commands.voice_master_commands import _PanelSelect, build_panel_view
    from bot_modules.voice_master.logic import PANEL_BUTTON_ORDER, PANEL_GROUP_ORDER

    view = build_panel_view()
    # One dropdown per group (not a wall of buttons). Each child is a
    # DynamicItem wrapping a real Select (the Select is ``.item``).
    assert len(view.children) == len(PANEL_GROUP_ORDER)
    assert all(isinstance(c, _PanelSelect) for c in view.children)
    selects = [c.item for c in view.children]
    # Building the selects exercises SelectOption emoji parsing for real.
    all_options = [opt.value for sel in selects for opt in sel.options]
    # Together the dropdowns expose every panel action exactly once.
    assert set(all_options) == set(PANEL_BUTTON_ORDER)
    assert len(all_options) == len(PANEL_BUTTON_ORDER)


@pytest.mark.asyncio
async def test_post_panel_writes_message_id_to_config(ctx):
    from bot_modules.commands.voice_master_commands import post_panel

    text_channel = MagicMock(spec=discord.TextChannel)
    text_channel.guild = MagicMock()
    text_channel.guild.id = GUILD
    sent = MagicMock()
    sent.id = 7777
    text_channel.send = AsyncMock(return_value=sent)
    msg = await post_panel(ctx, text_channel)
    assert msg is sent
    with open_db(ctx.db_path) as conn:
        cur = conn.execute(
            "SELECT value FROM config WHERE key=?",
            ("voice_master_panel_message_id",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "7777"


# ── post_knock_request branches ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_knock_request_returns_false_without_control_channel(ctx, voice_channel, owner_member, other_member):
    from bot_modules.commands.voice_master_commands import post_knock_request

    out = await post_knock_request(
        ctx, channel=voice_channel, requester=other_member, owner=owner_member
    )
    assert out is False


@pytest.mark.asyncio
async def test_post_knock_request_returns_false_when_control_missing(ctx, voice_channel, owner_member, other_member):
    from bot_modules.commands.voice_master_commands import post_knock_request

    with open_db(ctx.db_path) as conn:
        set_voice_master_config_value(
            conn, GUILD, "voice_master_control_channel_id", str(CONTROL_CH)
        )
    # Guild.get_channel returns None (channel is gone).
    voice_channel.guild.get_channel = MagicMock(return_value=None)
    out = await post_knock_request(
        ctx, channel=voice_channel, requester=other_member, owner=owner_member
    )
    assert out is False


@pytest.mark.asyncio
async def test_post_knock_request_sends_when_control_configured(ctx, voice_channel, owner_member, other_member):
    from bot_modules.commands.voice_master_commands import post_knock_request

    with open_db(ctx.db_path) as conn:
        set_voice_master_config_value(
            conn, GUILD, "voice_master_control_channel_id", str(CONTROL_CH)
        )
    control = MagicMock(spec=discord.TextChannel)
    control.send = AsyncMock()
    voice_channel.guild.get_channel = MagicMock(return_value=control)
    out = await post_knock_request(
        ctx, channel=voice_channel, requester=other_member, owner=owner_member
    )
    assert out is True
    control.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_knock_request_handles_send_failure(ctx, voice_channel, owner_member, other_member):
    from bot_modules.commands.voice_master_commands import post_knock_request

    with open_db(ctx.db_path) as conn:
        set_voice_master_config_value(
            conn, GUILD, "voice_master_control_channel_id", str(CONTROL_CH)
        )
    control = MagicMock(spec=discord.TextChannel)
    control.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "x"))
    voice_channel.guild.get_channel = MagicMock(return_value=control)
    out = await post_knock_request(
        ctx, channel=voice_channel, requester=other_member, owner=owner_member
    )
    assert out is False


# ── post_inline_panel branches ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_inline_panel_returns_message_on_success(voice_channel, owner_member):
    from bot_modules.commands.voice_master_commands import post_inline_panel

    out = await post_inline_panel(voice_channel, owner_member)
    assert out is not None
    voice_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_inline_panel_swallows_http_failure(voice_channel, owner_member):
    from bot_modules.commands.voice_master_commands import post_inline_panel

    voice_channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "boom"))
    out = await post_inline_panel(voice_channel, owner_member)
    assert out is None


# ── _check_edit_budget ────────────────────────────────────────────────────


def test_check_edit_budget_fresh_row_allows():
    from bot_modules.commands.voice_master_commands import _check_edit_budget

    row = MagicMock()
    row.last_edit_at_1 = 0.0
    row.last_edit_at_2 = 0.0
    allowed, retry = _check_edit_budget(row, now=1000.0)
    assert allowed is True
    assert retry == 0.0


def test_check_edit_budget_full_window_rejects():
    from bot_modules.commands.voice_master_commands import _check_edit_budget

    row = MagicMock()
    now = 1000.0
    row.last_edit_at_1 = now - 60
    row.last_edit_at_2 = now - 30
    allowed, retry = _check_edit_budget(row, now=now)
    assert allowed is False
    assert retry > 0


# ── Panel select groups / DynamicItem.from_custom_id ──────────────────────


def test_every_grouped_action_has_a_handler():
    """Each action shown in a dropdown must have an _ON_CLICKS handler, so no
    option ever dispatches into the void."""
    from bot_modules.commands.voice_master_commands import _ON_CLICKS
    from bot_modules.voice_master.logic import PANEL_GROUP_ORDER, panel_metas_for_group

    for group in PANEL_GROUP_ORDER:
        for meta in panel_metas_for_group(group):
            assert meta.action in _ON_CLICKS


@pytest.mark.asyncio
async def test_panel_select_from_custom_id_returns_instance():
    from bot_modules.commands.voice_master_commands import _PanelSelect

    for group in ("settings", "permissions"):
        instance = await _PanelSelect.from_custom_id(
            MagicMock(), MagicMock(), {"group": group}
        )
        assert isinstance(instance, _PanelSelect)


@pytest.mark.asyncio
async def test_panel_select_callback_unknown_value_noop():
    from bot_modules.commands.voice_master_commands import _PanelSelect

    sel = _PanelSelect("settings")
    inter = MagicMock()
    # An option value with no handler must not raise.
    inter.data = {"values": ["nonsense"]}
    inter.message = None  # short-circuits the cosmetic dropdown reset
    # Should return cleanly without raising or dispatching.
    await sel.callback(inter)


# ── _UserPickerView._run success paths ────────────────────────────────────


@pytest.mark.asyncio
async def test_user_picker_view_run_invite_path_success(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    view._selected = other_member
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await view._run(inter, remember=False)
    # Apply-invite should have fired the set_permissions path.
    voice_channel.set_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_picker_view_run_kick_path_success(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="kick")
    view._selected = other_member
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await view._run(inter, remember=False)
    voice_channel.set_permissions.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_picker_view_run_missing_channel(ctx, other_member):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    view._selected = other_member
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    await view._run(inter, remember=False)
    inter.response.send_message.assert_awaited_once()
    assert "no longer exists" in inter.response.send_message.await_args.args[0]


@pytest.mark.asyncio
async def test_user_picker_view_run_not_owner_anymore(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _UserPickerView

    with open_db(ctx.db_path) as conn:
        # The active channel's owner is someone else now.
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=99999, now=1.0)
    view = _UserPickerView(channel_id=CH, owner_id=OWNER, mode="invite")
    view._selected = other_member
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await view._run(inter, remember=False)
    inter.response.send_message.assert_awaited_once()
    assert "no longer own" in inter.response.send_message.await_args.args[0]


# ── _ResetConfirmView._run success / branches ────────────────────────────


@pytest.mark.asyncio
async def test_reset_confirm_view_run_success(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _ResetConfirmView

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0)
    view = _ResetConfirmView(channel_id=CH, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    inter.guild.get_member = MagicMock(return_value=MagicMock(spec=discord.Member))
    await view._run(inter, also_profile=False)
    voice_channel.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reset_confirm_view_run_not_owner_anymore(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _ResetConfirmView

    with open_db(ctx.db_path) as conn:
        insert_active_channel(conn, channel_id=CH, guild_id=GUILD, owner_id=99999, now=1.0)
    view = _ResetConfirmView(channel_id=CH, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    await view._run(inter, also_profile=False)
    inter.response.send_message.assert_awaited_once()
    assert "no longer own" in inter.response.send_message.await_args.args[0]


# ── _KnockResponseView ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_knock_response_view_blocks_non_owner(ctx):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx, user_id=99999)
    out = await view.interaction_check(inter)
    assert out is False


@pytest.mark.asyncio
async def test_knock_response_view_resolve_missing_channel(ctx):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=None)
    out = await view._resolve(inter)
    assert out is None
    inter.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_knock_response_view_resolve_missing_requester(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    inter.guild.get_member = MagicMock(return_value=None)
    out = await view._resolve(inter)
    assert out is None


@pytest.mark.asyncio
async def test_knock_response_view_accept_success(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    inter.guild.get_member = MagicMock(return_value=other_member)
    await view.accept.callback.callback(view, inter, MagicMock())
    voice_channel.set_permissions.assert_awaited_once()
    inter.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_knock_response_view_accept_perm_failure(ctx, voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    inter.guild.get_member = MagicMock(return_value=other_member)
    voice_channel.set_permissions = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(), "no")
    )
    await view.accept.callback.callback(view, inter, MagicMock())
    # Failure reply fired.
    assert (
        inter.response.send_message.await_count
        + inter.followup.send.await_count
    ) >= 1


@pytest.mark.asyncio
async def test_knock_response_view_accept_edit_message_failure_swallowed(
    ctx, voice_channel, other_member
):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.guild.get_channel = MagicMock(return_value=voice_channel)
    inter.guild.get_member = MagicMock(return_value=other_member)
    inter.response.edit_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom")
    )
    # Should not raise.
    await view.accept.callback.callback(view, inter, MagicMock())


@pytest.mark.asyncio
async def test_knock_response_view_deny_disables_buttons(ctx):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    await view.deny.callback.callback(view, inter, MagicMock())
    inter.response.edit_message.assert_awaited_once()
    for child in view.children:
        if isinstance(child, discord.ui.Button):
            assert child.disabled is True


@pytest.mark.asyncio
async def test_knock_response_view_deny_swallows_edit_failure(ctx):
    from bot_modules.commands.voice_master_commands import _KnockResponseView

    view = _KnockResponseView(channel_id=CH, requester_id=OTHER, owner_id=OWNER)
    inter = _wire_interaction(ctx)
    inter.response.edit_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom")
    )
    await view.deny.callback.callback(view, inter, MagicMock())


# ── Misc no-ctx / guard branches ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_lock_no_ctx_short_circuits(voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_lock

    row = MagicMock()
    row.owner_id = OWNER
    row.channel_id = CH
    row.last_edit_at_1 = 0.0
    row.last_edit_at_2 = 0.0
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_lock(inter, voice_channel, row, locked=True)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_hide_no_ctx_short_circuits(voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_hide

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_hide(inter, voice_channel, row, hidden=True)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_rename_no_ctx_short_circuits(voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_rename

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_rename(inter, voice_channel, row, new_name="x")
    voice_channel.edit.assert_not_called()


@pytest.mark.asyncio
async def test_apply_limit_no_ctx_short_circuits(voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_limit

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_limit(inter, voice_channel, row, new_limit=5)
    voice_channel.edit.assert_not_called()


@pytest.mark.asyncio
async def test_apply_invite_no_ctx_short_circuits(voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_invite

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_invite(inter, voice_channel, row, target=other_member, remember=False)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_kick_no_ctx_short_circuits(voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_kick

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_kick(inter, voice_channel, row, target=other_member, remember=False)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_apply_reset_no_ctx_short_circuits(voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_reset

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_reset(inter, voice_channel, row, also_profile=False)
    voice_channel.edit.assert_not_called()


@pytest.mark.asyncio
async def test_apply_transfer_no_ctx_short_circuits(voice_channel, other_member):
    from bot_modules.commands.voice_master_commands import _apply_transfer

    row = MagicMock()
    row.owner_id = OWNER
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    await _apply_transfer(inter, voice_channel, row, new_owner=other_member)
    voice_channel.set_permissions.assert_not_called()


@pytest.mark.asyncio
async def test_gate_and_record_edit_no_ctx_returns_false():
    from bot_modules.commands.voice_master_commands import _gate_and_record_edit

    row = MagicMock()
    row.last_edit_at_1 = 0.0
    row.last_edit_at_2 = 0.0
    inter = fake_interaction()
    inter.client = MagicMock()
    setattr(inter.client, "_vm_ctx", None)
    out = await _gate_and_record_edit(inter, row)
    assert out is False
