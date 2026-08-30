"""The rotation's game lifecycle — ending and starting a room's game on the flip.

The decision of *which* rooms start and end is pure and lives in
``test_feature_rotation_logic.py``. What is asserted here is the part that
needs a bot: that a game is ended through its own completion site when one is
available (so the recap posts and the roster is paid) rather than through the
recap-less fallback, that a launch is refused rather than stomping a running
game, and above all that the flip runs **end → hide → show → start** — the
ordering is the whole reason a recap lands somewhere members can still read it.
"""

from __future__ import annotations

import discord

from bot_modules.core.db_utils import open_db
from bot_modules.feature_rotation.logic import GamePlan, Room
from bot_modules.services import feature_rotation_service as svc
from bot_modules.services.games_db import GamesDb

GUILD = 4242
OPEN_ROOM, CLOSING_ROOM = 101, 102


class _Chan(discord.abc.Messageable):
    """A stand-in the service's ``isinstance(..., Messageable)`` guard accepts.

    That guard is what stops the rotation trying to launch a game into a
    category or a voice channel, so the fake has to satisfy it rather than the
    guard being loosened for the test.
    """

    def __init__(self, cid):
        self.id = cid
        self.name = f"room-{cid}"
        self.sends = []

    async def _get_channel(self):
        return self

    async def send(self, *a, **k):
        self.sends.append(k.get("embed") or (a[0] if a else None))
        return None


class _Guild:
    """Isinstance-compatible with discord.Guild, the same trick the rotation's
    route tests use for their fake channel."""

    __class__ = discord.Guild  # type: ignore[assignment]
    id = GUILD

    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)


class _Ctx:
    def __init__(self, path):
        self._path = path

    def open_db(self):
        return open_db(self._path)


class _Bot:
    def __init__(self, path, channels, launchers=None):
        self.games_db = GamesDb(path)
        self.ctx = _Ctx(path)
        self.guild = _Guild(channels)
        self.active_views = {}
        self.game_launchers = launchers or {}

    def get_guild(self, gid):
        return self.guild


async def _active_game(db, channel_id, game_type="ama"):
    from bot_modules.games.utils.game_manager import create_game

    return await create_game(db, channel_id, 0, game_type, state="open", payload={})


# ── ending ───────────────────────────────────────────────────────────────────


async def test_a_view_that_can_close_itself_is_asked_to(sync_db_path):
    """The recap path. force_end_active_game pays but posts nothing."""
    chan = _Chan(CLOSING_ROOM)
    bot = _Bot(sync_db_path, {CLOSING_ROOM: chan})
    gid = await _active_game(bot.games_db, CLOSING_ROOM)

    closed = []

    class _View:
        async def close_now(self, channel):
            closed.append(channel.id)

    bot.active_views[gid] = _View()

    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM) is True
    assert closed == [CLOSING_ROOM]
    # close_now owns the archiving, so the row is still the view's to clear.
    assert await bot.games_db.fetchone(
        "SELECT 1 FROM games_active_games WHERE game_id = ?", (gid,)
    ) is not None


async def test_a_game_with_no_live_view_falls_back_to_force_end(sync_db_path):
    # After a restart the view is gone but the row is not; the game must still
    # be ended and paid, just without a recap.
    chan = _Chan(CLOSING_ROOM)
    bot = _Bot(sync_db_path, {CLOSING_ROOM: chan})
    gid = await _active_game(bot.games_db, CLOSING_ROOM)

    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM) is True
    assert await bot.games_db.fetchone(
        "SELECT 1 FROM games_active_games WHERE game_id = ?", (gid,)
    ) is None


async def test_ending_a_room_with_nothing_running_is_a_no_op(sync_db_path):
    bot = _Bot(sync_db_path, {CLOSING_ROOM: _Chan(CLOSING_ROOM)})
    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM) is False


async def test_a_closer_that_raises_does_not_stop_the_other_rooms(sync_db_path):
    chan_a, chan_b = _Chan(CLOSING_ROOM), _Chan(OPEN_ROOM)
    bot = _Bot(sync_db_path, {CLOSING_ROOM: chan_a, OPEN_ROOM: chan_b})
    bad = await _active_game(bot.games_db, CLOSING_ROOM)
    await _active_game(bot.games_db, OPEN_ROOM)

    class _Boom:
        async def close_now(self, channel):
            raise RuntimeError("kaboom")

    bot.active_views[bad] = _Boom()

    ended = await svc.apply_game_ends(
        bot, bot.guild, GamePlan(end=((CLOSING_ROOM, "ama"), (OPEN_ROOM, "ama")))
    )
    assert ended == 1  # the healthy room still closed


# ── starting ─────────────────────────────────────────────────────────────────


async def test_the_featured_rooms_game_launches_with_its_stored_options(sync_db_path):
    calls = []

    async def launcher(*, channel, host_id, host_name, guild_id, options):
        calls.append({"channel": channel.id, "host_id": host_id, "options": options})
        return "gid-1"

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"ama": launcher})
    rooms = [Room(OPEN_ROOM, launch_game="ama", launch_options='{"mode": "screened"}')]

    started = await svc.apply_game_starts(
        bot, bot.guild, GamePlan(start=((OPEN_ROOM, "ama"),)), rooms
    )

    assert started == 1
    assert calls[0]["options"] == {"mode": "screened"}


async def test_an_auto_launched_game_is_hosted_by_nobody(sync_db_path):
    """host_id 0 reaches pay_game_rewards as None, so no host bounty is paid.

    A real member there would collect a hosting bounty every featured day for
    hosting nothing.
    """
    calls = []

    async def launcher(*, channel, host_id, host_name, guild_id, options):
        calls.append(host_id)
        return "gid-1"

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"ama": launcher})
    await svc.start_room_game(bot, bot.guild, OPEN_ROOM, "ama", {})
    assert calls == [0]


async def test_a_room_that_already_has_a_game_is_not_stomped(sync_db_path):
    calls = []

    async def launcher(**kwargs):
        calls.append(kwargs)
        return "gid-1"

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"ama": launcher})
    await _active_game(bot.games_db, OPEN_ROOM)

    assert await svc.start_room_game(bot, bot.guild, OPEN_ROOM, "ama", {}) is None
    assert calls == []


async def test_a_busy_check_refusal_blocks_the_launch(sync_db_path):
    # Risky Rolls tracks rounds in memory rather than games_active_games, so
    # the table lookup alone would miss a live round.
    calls = []

    async def launcher(**kwargs):
        calls.append(kwargs)
        return "gid-1"

    async def busy(channel_id):
        return True

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"risky_roll": launcher})
    bot.game_busy_checks = {"risky_roll": busy}

    assert await svc.start_room_game(bot, bot.guild, OPEN_ROOM, "risky_roll", {}) is None
    assert calls == []


async def test_an_unregistered_game_key_fails_quietly(sync_db_path):
    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)})
    assert await svc.start_room_game(bot, bot.guild, OPEN_ROOM, "nope", {}) is None


async def test_a_launcher_that_raises_does_not_break_the_flip(sync_db_path):
    async def launcher(**kwargs):
        raise RuntimeError("kaboom")

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"ama": launcher})
    assert await svc.start_room_game(bot, bot.guild, OPEN_ROOM, "ama", {}) is None


async def test_a_missing_channel_is_not_launched_into(sync_db_path):
    async def launcher(**kwargs):
        raise AssertionError("should not be called")

    bot = _Bot(sync_db_path, {}, {"ama": launcher})
    assert await svc.start_room_game(bot, bot.guild, 999, "ama", {}) is None


# ── ordering ─────────────────────────────────────────────────────────────────


async def test_the_flip_ends_before_hiding_and_starts_after_showing(monkeypatch, sync_db_path):
    """end → hide → show → start, the ordering the whole design rests on.

    Ending after the hide would post the recap into a channel members can no
    longer open; starting before the show would post the game's first message
    into one that is still shut.
    """
    from bot_modules.feature_rotation.store import (
        RotationConfig,
        save_config,
        upsert_room,
    )

    order = []

    async def fake_hide(bot, guild, channel_id, reason):
        order.append(("hide", channel_id))
        return True

    async def fake_show(bot, guild, channel_id, reason):
        order.append(("show", channel_id))
        return True

    async def fake_end(bot, guild, channel_id, game_key=""):
        order.append(("end", channel_id))
        return True

    async def launcher(*, channel, host_id, host_name, guild_id, options):
        order.append(("start", channel.id))
        return "gid-1"

    monkeypatch.setattr(svc, "hide_room", fake_hide)
    monkeypatch.setattr(svc, "show_room", fake_show)
    monkeypatch.setattr(svc, "end_room_game", fake_end)

    bot = _Bot(
        sync_db_path,
        {OPEN_ROOM: _Chan(OPEN_ROOM), CLOSING_ROOM: _Chan(CLOSING_ROOM)},
        {"ama": launcher},
    )
    with open_db(sync_db_path) as c:
        save_config(c, RotationConfig(guild_id=GUILD, enabled=True, rooms_per_day=1))
        upsert_room(c, GUILD, Room(OPEN_ROOM, position=1, launch_game="ama"))
        upsert_room(c, GUILD, Room(CLOSING_ROOM, position=2, launch_game="ama"))

    # Sweep a couple of days so we land on the one that features OPEN_ROOM.
    for day in range(2):
        order.clear()
        with open_db(sync_db_path) as c:
            c.execute(
                "UPDATE feature_rotation_config SET last_flip_date = '' WHERE guild_id = ?",
                (GUILD,),
            )
        await svc._tick_guild(bot, bot.guild, 1_787_000_000.0 + day * 86400)
        if ("start", OPEN_ROOM) in order:
            break
    else:  # pragma: no cover - the two-room cycle always features each room
        raise AssertionError("never featured the open room")

    steps = [kind for kind, _ in order]
    assert steps.index("end") < steps.index("hide")
    assert steps.index("show") < steps.index("start")
    assert order[0][0] == "end" and order[-1][0] == "start"


async def test_a_second_pass_on_the_same_day_starts_nothing(monkeypatch, sync_db_path):
    """The day claim covers the new work too, or a restart double-launches."""
    from bot_modules.feature_rotation.store import (
        RotationConfig,
        save_config,
        upsert_room,
    )

    launches = []

    async def launcher(*, channel, host_id, host_name, guild_id, options):
        launches.append(channel.id)
        return "gid-1"

    async def noop_hide(bot, guild, channel_id, reason):
        return True

    monkeypatch.setattr(svc, "hide_room", noop_hide)
    monkeypatch.setattr(svc, "show_room", noop_hide)

    bot = _Bot(sync_db_path, {OPEN_ROOM: _Chan(OPEN_ROOM)}, {"ama": launcher})
    with open_db(sync_db_path) as c:
        save_config(c, RotationConfig(guild_id=GUILD, enabled=True, rooms_per_day=1))
        upsert_room(c, GUILD, Room(OPEN_ROOM, position=1, launch_game="ama"))

    now = 1_787_000_000.0
    await svc._tick_guild(bot, bot.guild, now)
    await svc._tick_guild(bot, bot.guild, now + 60)

    assert launches == [OPEN_ROOM]


# ── the contract with AMA ────────────────────────────────────────────────────


async def test_the_ama_view_exposes_the_closer_the_rotation_looks_for():
    """The one wiring assertion: end_room_game duck-types ``close_now``.

    If AMA ever loses this method the rotation silently degrades to
    ``force_end_active_game`` — still paid and archived, but with no recap
    posted — and nothing else in the suite would notice.
    """
    from bot_modules.cogs import games_ama_cog as ama_mod

    view = ama_mod.AMAView.__new__(ama_mod.AMAView)
    view._closed = False
    closed = []

    async def fake_do_close(channel):
        closed.append(channel)

    view._do_close = fake_do_close

    await view.close_now("channel")
    assert closed == ["channel"]

    # Idempotent: the flip may race the view's own close button.
    view._closed = True
    await view.close_now("channel")
    assert closed == ["channel"]


# ── games whose rounds the table cannot see ──────────────────────────────────
#
# Risky Rolls keeps rounds in rr_state.active_games, not games_active_games, so
# the rotation would end nothing at all for that room without the registered
# closer. Its cog registers one next to its existing busy-check.


async def test_a_registered_closer_ends_a_game_the_table_cannot_see(sync_db_path):
    seen = []

    async def closer(channel_id):
        seen.append(channel_id)
        return True

    bot = _Bot(sync_db_path, {CLOSING_ROOM: _Chan(CLOSING_ROOM)})
    bot.game_channel_closers = {"risky_roll": closer}

    # Nothing in games_active_games for this channel — the closer is the only
    # thing that can end it.
    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM, "risky_roll") is True
    assert seen == [CLOSING_ROOM]


async def test_a_closer_reporting_nothing_open_is_not_counted(sync_db_path):
    async def closer(channel_id):
        return False

    bot = _Bot(sync_db_path, {CLOSING_ROOM: _Chan(CLOSING_ROOM)})
    bot.game_channel_closers = {"risky_roll": closer}

    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM, "risky_roll") is False


async def test_a_raising_closer_still_lets_the_table_path_run(sync_db_path):
    async def closer(channel_id):
        raise RuntimeError("kaboom")

    bot = _Bot(sync_db_path, {CLOSING_ROOM: _Chan(CLOSING_ROOM)})
    bot.game_channel_closers = {"risky_roll": closer}
    gid = await _active_game(bot.games_db, CLOSING_ROOM)

    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM, "risky_roll") is True
    assert await bot.games_db.fetchone(
        "SELECT 1 FROM games_active_games WHERE game_id = ?", (gid,)
    ) is None


async def test_a_rooms_closer_is_not_used_for_a_different_game(sync_db_path):
    async def closer(channel_id):
        raise AssertionError("wrong game's closer")

    bot = _Bot(sync_db_path, {CLOSING_ROOM: _Chan(CLOSING_ROOM)})
    bot.game_channel_closers = {"risky_roll": closer}

    assert await svc.end_room_game(bot, bot.guild, CLOSING_ROOM, "ama") is False


async def test_the_risky_roll_cog_registers_both_halves_of_the_pair():
    """The wiring assertion. A busy-check without a closer would leave the
    rotation silently unable to end that room's game — the table lookup sees
    nothing for Risky Rolls, so the closer is the only path there is."""
    import bot_modules.cogs.risky_roll_cog as rr

    class _StubBot:
        def __init__(self):
            self.game_launchers = {}
            self.game_busy_checks = {}
            self.game_channel_closers = {}
            self.added = []

        async def add_cog(self, cog):
            self.added.append(cog)

    bot = _StubBot()
    await rr.setup(bot)

    assert "risky_roll" in bot.game_busy_checks
    assert "risky_roll" in bot.game_channel_closers
    cog = bot.added[0]
    assert bot.game_channel_closers["risky_roll"] == cog.close_channel_rounds
