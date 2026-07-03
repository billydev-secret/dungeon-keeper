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


# ── _apply_lock ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_lock_sets_overwrite_and_saves_profile(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_lock
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_lock(inter, voice_channel, row, locked=True)

    voice_channel.set_permissions.assert_awaited_once()
    # Verify the @everyone overwrite has connect=False
    args, kwargs = voice_channel.set_permissions.await_args
    overwrite = kwargs["overwrite"]
    assert overwrite.connect is False
    # Profile should be saved with locked=True.
    with open_db(ctx.db_path) as conn:
        p = load_profile(conn, GUILD, OWNER)
    assert p is not None
    assert p.locked is True
    # Lock state is advertised via the channel status line (separate endpoint
    # from the name edit), never by renaming the channel.
    status_calls = [
        c.kwargs["status"]
        for c in voice_channel.edit.await_args_list
        if "status" in c.kwargs
    ]
    assert status_calls == [LOCKED_STATUS_TEXT]
    assert all("name" not in c.kwargs for c in voice_channel.edit.await_args_list)


@pytest.mark.asyncio
async def test_apply_unlock_sets_open_status(ctx, voice_channel):
    from bot_modules.commands.voice_master_commands import _apply_lock
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_lock(inter, voice_channel, row, locked=False)

    status_calls = [
        c.kwargs["status"]
        for c in voice_channel.edit.await_args_list
        if "status" in c.kwargs
    ]
    assert status_calls == [OPEN_STATUS_TEXT]


@pytest.mark.asyncio
async def test_apply_lock_defers_before_slow_call(ctx, voice_channel):
    """If the response wasn't already done, _apply_lock must defer first."""
    from bot_modules.commands.voice_master_commands import _apply_lock
    from bot_modules.services.voice_master_service import get_active_channel

    with open_db(ctx.db_path) as conn:
        insert_active_channel(
            conn, channel_id=CH, guild_id=GUILD, owner_id=OWNER, now=1.0
        )
        row = get_active_channel(conn, CH)
    inter = _wire_interaction(ctx)
    assert row is not None

    await _apply_lock(inter, voice_channel, row, locked=False)
    inter.response.defer.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_lock_grants_connect_to_in_channel_members(ctx, voice_channel):
    """Locking hands each in-channel member an explicit Connect overwrite so the
    voice channel's text chat keeps working — Discord ties text-chat access to
    Connect, which the @everyone denial would otherwise strip. The owner (who
    has a persistent overwrite) and the bot are skipped."""
    from bot_modules.commands.voice_master_commands import _apply_lock
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
    await _apply_lock(inter, voice_channel, row, locked=True)

    targets = [c.args[0] for c in voice_channel.set_permissions.await_args_list]
    assert guild.default_role in targets  # @everyone lock applied
    assert guest in targets               # in-channel member granted
    assert owner_m not in targets         # owner skipped (persistent overwrite)
    guest_call = next(
        c for c in voice_channel.set_permissions.await_args_list if c.args[0] is guest
    )
    assert guest_call.kwargs["overwrite"].connect is True


@pytest.mark.asyncio
async def test_apply_unlock_clears_only_lock_shaped_grants(ctx, voice_channel):
    """Unlock drops only the lock-grant shape (connect=True, view inherited).
    The owner, trusted/blocked entries, and one-off invite/knock guests (who
    carry connect=True *and* view_channel=True) must survive."""
    from bot_modules.commands.voice_master_commands import _apply_lock
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
    voice_channel.overwrites = {
        owner_m: discord.PermissionOverwrite(connect=True, view_channel=True),
        trusted_m: discord.PermissionOverwrite(connect=True, view_channel=True),
        blocked_m: discord.PermissionOverwrite(connect=False),
        # Transient lock grant — connect only, view left to inherit.
        guest_m: discord.PermissionOverwrite(connect=True),
        # One-off invite/knock guest — not trusted, but carries view=True.
        invited_m: discord.PermissionOverwrite(connect=True, view_channel=True),
    }
    voice_channel.guild.get_member = MagicMock(
        side_effect=lambda uid: {
            OWNER: owner_m, TRUSTED: trusted_m, BLOCKED: blocked_m,
            GUEST: guest_m, INVITED: invited_m,
        }.get(uid)
    )

    inter = _wire_interaction(ctx)
    await _apply_lock(inter, voice_channel, row, locked=False)

    removed = [
        c.args[0]
        for c in voice_channel.set_permissions.await_args_list
        if c.kwargs.get("overwrite") is None
    ]
    assert removed == [guest_m]  # invited guest preserved despite no trust entry


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
