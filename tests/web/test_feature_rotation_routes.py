"""Tests for /api/feature-rotation/* — the two guards that touch Discord.

Both are here rather than at the logic layer because both are decisions the
*route* makes about a Discord call that already happened: whether Apply Now
still means something with the rotation switched off, and whether a room may
leave the pool while it is still hidden. Everything else about the rotation is
covered in tests/test_feature_rotation_{logic,store}.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from bot_modules.feature_rotation.logic import Room
from bot_modules.feature_rotation.store import list_pool, list_pool_state, upsert_room

GID = 123  # FakeCtx default guild
ROOM = 555


class _Channel:
    """Just enough discord.TextChannel for show_room; isinstance-compatible."""

    __class__ = discord.TextChannel  # type: ignore[assignment]

    def __init__(self, cid: int) -> None:
        self.id = cid
        self.name = "confessions"
        self.overwrites: dict = {}
        self.edit = AsyncMock()


def _wire(fake_ctx, channel: _Channel | None) -> None:
    guild = SimpleNamespace(
        id=fake_ctx.guild_id,
        get_channel=lambda c: channel if channel and c == channel.id else None,
    )
    fake_ctx.bot = SimpleNamespace(
        ctx=fake_ctx,  # show_room reads bot.ctx to reach the saved overwrites
        get_guild=lambda gid: guild if gid == fake_ctx.guild_id else None,
    )


def _pool_a_hidden_room(fake_ctx) -> None:
    """One room in the pool, currently hidden, rotation off."""
    with fake_ctx.open_db() as conn:
        upsert_room(conn, GID, Room(ROOM, label="Confessions"))
        conn.execute(
            "UPDATE feature_rotation_pool SET hidden_at = 1, stored_overwrites = '[]' "
            "WHERE guild_id = ? AND channel_id = ?",
            (GID, ROOM),
        )
        conn.commit()


def test_apply_now_reopens_everything_when_the_rotation_is_off(open_client, fake_ctx):
    """Switching the feature off has to be reversible from the dashboard.

    There is no derived day while the rotation is off — that is what "off"
    means to every other reader — so this used to 400, stranding every room
    that happened to be hidden at the time with no way back.
    """
    _pool_a_hidden_room(fake_ctx)
    channel = _Channel(ROOM)
    _wire(fake_ctx, channel)

    res = open_client.post("/api/feature-rotation/apply", json={})
    assert res.status_code == 200, res.text
    assert res.json()["shown"] == 1
    assert channel.edit.await_count == 1
    with fake_ctx.open_db() as conn:
        assert list_pool_state(conn, GID).get(ROOM) is not True


def test_apply_now_still_refuses_an_empty_pool(open_client, fake_ctx):
    _wire(fake_ctx, None)
    res = open_client.post("/api/feature-rotation/apply", json={})
    assert res.status_code == 400


def test_removing_a_hidden_room_is_refused_when_it_cannot_be_reopened(
    open_client, fake_ctx
):
    """The one unrecoverable mistake this feature can make.

    Deleting the row discards the saved overwrites, so doing it while the
    channel is still hidden leaves an invisible channel and no record of what
    its permissions were. A bot that is offline, or that lost Manage Channels,
    must not be able to turn "remove from the pool" into that.
    """
    _pool_a_hidden_room(fake_ctx)
    _wire(fake_ctx, None)  # the bot can't see the channel, so the restore fails

    res = open_client.delete(f"/api/feature-rotation/rooms/{ROOM}")
    assert res.status_code == 409
    with fake_ctx.open_db() as conn:
        assert [r.channel_id for r in list_pool(conn, GID)] == [ROOM]


def test_removing_a_visible_room_still_works(open_client, fake_ctx):
    """The guard is about hidden rooms only — an ordinary removal is unaffected."""
    with fake_ctx.open_db() as conn:
        upsert_room(conn, GID, Room(ROOM, label="Confessions"))
        conn.commit()
    _wire(fake_ctx, None)

    assert open_client.delete(f"/api/feature-rotation/rooms/{ROOM}").status_code == 200
    with fake_ctx.open_db() as conn:
        assert list_pool(conn, GID) == []
