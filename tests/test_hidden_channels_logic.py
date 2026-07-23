"""Store + ordering tests for hidden-channel holds.

The ``hidden_channels`` row is the *only* record of a channel's original
overwrites once ``/hidden hide`` has stripped them, so these pin two things:
the store helpers themselves (insert / lookup / list / restore / delete), and
the ordering guarantee in the cog — the row must exist **before** the
irreversible ``channel.edit()`` runs, and must be deleted again if that edit
fails, so a DB failure can never lose the snapshot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot_modules.cogs.hidden_channels_cog import HIDDEN_CATEGORY_NAME, HiddenChannelsCog
from bot_modules.core.db_utils import open_db
from bot_modules.hidden_channels.store import (
    create_hidden,
    delete_hidden,
    get_active_hidden,
    list_active_hidden,
    mark_restored,
)

GUILD = 4242
CHANNEL = 777
ADMIN = 99

STORED = [{"id": 1, "type": "role", "allow": 0, "deny": 1024}]


def _insert(conn: sqlite3.Connection, *, channel_id: int = CHANNEL) -> int:
    return create_hidden(
        conn,
        guild_id=GUILD,
        channel_id=channel_id,
        original_parent_id=55,
        original_position=3,
        stored_overwrites=STORED,  # type: ignore[arg-type]
        hidden_by=ADMIN,
    )


# --------------------------------------------------------------------------
# store helpers
# --------------------------------------------------------------------------


def test_create_and_get_active_hidden_roundtrip(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        hidden_id = _insert(conn)
        assert hidden_id > 0

    with open_db(sync_db_path) as conn:
        row = get_active_hidden(conn, GUILD, CHANNEL)

    assert row is not None
    assert row["id"] == hidden_id
    assert row["original_parent_id"] == 55
    assert row["original_position"] == 3
    assert row["hidden_by"] == ADMIN
    assert row["status"] == "active"
    assert '"deny": 1024' in row["stored_overwrites"]


def test_get_active_hidden_is_scoped_to_guild_and_channel(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        _insert(conn)

    with open_db(sync_db_path) as conn:
        assert get_active_hidden(conn, GUILD, CHANNEL + 1) is None
        assert get_active_hidden(conn, GUILD + 1, CHANNEL) is None


def test_duplicate_active_hold_is_rejected(sync_db_path: Path):
    """The partial unique index is what makes "already hidden" enforceable."""
    with open_db(sync_db_path) as conn:
        _insert(conn)

    with pytest.raises(sqlite3.IntegrityError), open_db(sync_db_path) as conn:
        _insert(conn)


def test_restore_flow_frees_the_channel_to_be_hidden_again(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        hidden_id = _insert(conn)

    with open_db(sync_db_path) as conn:
        assert mark_restored(conn, hidden_id) is True

    with open_db(sync_db_path) as conn:
        assert get_active_hidden(conn, GUILD, CHANNEL) is None
        # Restored rows no longer occupy the unique index.
        _insert(conn)
        assert get_active_hidden(conn, GUILD, CHANNEL) is not None


def test_mark_restored_is_idempotent(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        hidden_id = _insert(conn)

    with open_db(sync_db_path) as conn:
        assert mark_restored(conn, hidden_id) is True
    with open_db(sync_db_path) as conn:
        assert mark_restored(conn, hidden_id) is False
        assert mark_restored(conn, 999999) is False


def test_list_active_hidden_is_guild_scoped_and_oldest_first(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        first = _insert(conn, channel_id=1)
        second = _insert(conn, channel_id=2)
        create_hidden(
            conn,
            guild_id=GUILD + 1,
            channel_id=3,
            original_parent_id=None,
            original_position=0,
            stored_overwrites=[],
            hidden_by=ADMIN,
        )
        restored = _insert(conn, channel_id=4)
        mark_restored(conn, restored)

    with open_db(sync_db_path) as conn:
        rows = list_active_hidden(conn, GUILD)

    assert [r["id"] for r in rows] == [first, second]


def test_delete_hidden_removes_only_the_named_row(sync_db_path: Path):
    with open_db(sync_db_path) as conn:
        first = _insert(conn, channel_id=1)
        _insert(conn, channel_id=2)

    with open_db(sync_db_path) as conn:
        assert delete_hidden(conn, first) is True
    with open_db(sync_db_path) as conn:
        assert delete_hidden(conn, first) is False
        assert [r["channel_id"] for r in list_active_hidden(conn, GUILD)] == [2]


# --------------------------------------------------------------------------
# /hidden hide ordering: row first, edit second
# --------------------------------------------------------------------------


def _cog(sync_db_path: Path) -> HiddenChannelsCog:
    ctx = MagicMock()
    ctx.is_admin.return_value = True
    ctx.open_db = lambda: open_db(sync_db_path)
    return HiddenChannelsCog(MagicMock(), ctx)


def _guild() -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    guild.id = GUILD
    guild.me = MagicMock(spec=discord.Member)
    guild.me.guild_permissions = discord.Permissions(
        manage_channels=True, manage_roles=True
    )
    guild.default_role = MagicMock(spec=discord.Role)
    category = MagicMock(spec=discord.CategoryChannel)
    category.name = HIDDEN_CATEGORY_NAME
    guild.categories = [category]
    return guild


def _channel() -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = CHANNEL
    channel.name = "secret"
    channel.position = 3
    channel.category = None
    channel.overwrites = {}
    channel.edit = AsyncMock()
    return channel


def _interaction(guild: MagicMock) -> MagicMock:
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = MagicMock()
    interaction.user.id = ADMIN
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_hide_writes_the_restore_row_before_editing_the_channel(
    sync_db_path: Path,
):
    guild, channel = _guild(), _channel()
    seen: dict[str, object] = {}

    async def _edit(**_kwargs):
        with open_db(sync_db_path) as conn:
            seen["row"] = get_active_hidden(conn, GUILD, CHANNEL)

    channel.edit = AsyncMock(side_effect=_edit)
    interaction = _interaction(guild)

    cog = _cog(sync_db_path)
    await cog.hide.callback(cog, interaction, channel)  # type: ignore[union-attr]

    # The snapshot was already durable when the irreversible edit ran.
    assert seen["row"] is not None
    assert seen["row"]["original_position"] == 3  # type: ignore[index]


@pytest.mark.asyncio
async def test_hide_rolls_the_row_back_when_the_channel_edit_fails(
    sync_db_path: Path,
):
    guild, channel = _guild(), _channel()
    channel.edit = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no perms")
    )
    interaction = _interaction(guild)

    cog = _cog(sync_db_path)
    await cog.hide.callback(cog, interaction, channel)  # type: ignore[union-attr]

    with open_db(sync_db_path) as conn:
        assert get_active_hidden(conn, GUILD, CHANNEL) is None
        assert list_active_hidden(conn, GUILD) == []
    assert "not allowed" in interaction.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_hide_leaves_the_channel_untouched_when_the_db_write_fails(
    sync_db_path: Path,
):
    guild, channel = _guild(), _channel()
    interaction = _interaction(guild)

    cog = _cog(sync_db_path)
    calls = {"n": 0}

    def _open():
        calls["n"] += 1
        if calls["n"] > 1:  # the pre-check succeeds; the insert doesn't
            raise sqlite3.OperationalError("database is locked")
        return open_db(sync_db_path)

    cog.ctx.open_db = _open
    await cog.hide.callback(cog, interaction, channel)  # type: ignore[union-attr]

    channel.edit.assert_not_awaited()
    assert "couldn't save" in interaction.followup.send.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_hide_happy_path_leaves_an_active_row(sync_db_path: Path):
    guild, channel = _guild(), _channel()
    interaction = _interaction(guild)

    cog = _cog(sync_db_path)
    await cog.hide.callback(cog, interaction, channel)  # type: ignore[union-attr]

    channel.edit.assert_awaited_once()
    with open_db(sync_db_path) as conn:
        row = get_active_hidden(conn, GUILD, CHANNEL)
    assert row is not None and row["hidden_by"] == ADMIN
