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


# ── the launch pair ──────────────────────────────────────────────────────────


def test_the_panel_offers_the_launchable_games_with_their_option_fields(
    open_client, fake_ctx
):
    _wire(fake_ctx, None)
    body = open_client.get("/api/feature-rotation").json()
    games = {g["type"]: g for g in body["launchable_games"]}
    assert {"ama", "risky_roll"} <= set(games)
    assert {f["name"] for f in games["ama"]["fields"]} == {"mode", "format"}
    # Each game brings its own option fields, straight from the schedule schema.
    assert {f["name"] for f in games["risky_roll"]["fields"]} == {
        "auto_close_players", "auto_close_minutes",
    }


def test_saving_a_game_and_its_options_round_trips(open_client, fake_ctx):
    _wire(fake_ctx, None)
    res = open_client.put(
        f"/api/feature-rotation/rooms/{ROOM}",
        json={
            "position": 1,
            "label": "AMA",
            "blurb": "",
            "in_rotation": True,
            "hide_when_off": True,
            "announce": True,
            "quest_kinds": [],
            "blocked_kinds": [],
            "launch_game": "ama",
            "launch_options": {"mode": "screened", "format": "panel"},
        },
    )
    assert res.status_code == 200

    room = open_client.get("/api/feature-rotation").json()["rooms"][0]
    assert room["launch_game"] == "ama"
    assert room["launch_options"] == {"mode": "screened", "format": "panel"}


def test_risky_rolls_keeps_its_numeric_options(open_client, fake_ctx):
    _wire(fake_ctx, None)
    open_client.put(
        f"/api/feature-rotation/rooms/{ROOM}",
        json={
            "position": 1, "label": "", "blurb": "",
            "in_rotation": True, "hide_when_off": True, "announce": True,
            "quest_kinds": [], "blocked_kinds": [],
            "launch_game": "risky_roll",
            "launch_options": {"auto_close_minutes": 600, "auto_close_players": 30},
        },
    )
    room = open_client.get("/api/feature-rotation").json()["rooms"][0]
    assert room["launch_game"] == "risky_roll"
    assert room["launch_options"] == {
        "auto_close_minutes": 600, "auto_close_players": 30,
    }


def test_a_game_outside_the_allow_list_is_dropped_with_its_options(
    open_client, fake_ctx
):
    """Storing options for a game that will never run is how a dial ends up
    looking set while doing nothing — CLAUDE.md's unenforced-toggle rule."""
    _wire(fake_ctx, None)
    open_client.put(
        f"/api/feature-rotation/rooms/{ROOM}",
        json={
            "position": 1, "label": "", "blurb": "",
            "in_rotation": True, "hide_when_off": True, "announce": True,
            "quest_kinds": [], "blocked_kinds": [],
            "launch_game": "clapback",
            "launch_options": {"rounds": 3},
        },
    )
    room = open_client.get("/api/feature-rotation").json()["rooms"][0]
    assert room["launch_game"] == ""
    assert room["launch_options"] == {}


def test_an_option_value_outside_its_choice_list_is_dropped(open_client, fake_ctx):
    _wire(fake_ctx, None)
    open_client.put(
        f"/api/feature-rotation/rooms/{ROOM}",
        json={
            "position": 1, "label": "", "blurb": "",
            "in_rotation": True, "hide_when_off": True, "announce": True,
            "quest_kinds": [], "blocked_kinds": [],
            "launch_game": "ama",
            "launch_options": {"mode": "nonsense", "format": "panel", "bogus": 1},
        },
    )
    room = open_client.get("/api/feature-rotation").json()["rooms"][0]
    assert room["launch_game"] == "ama"
    assert room["launch_options"] == {"format": "panel"}
