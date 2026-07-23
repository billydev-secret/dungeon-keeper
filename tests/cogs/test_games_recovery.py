"""Crash-recovery seam: after a restart, in-flight games re-register their views.

Each party cog registers a ``recover_game`` in ``bot.game_recoverers``; the
startup sweep (``recover_active_games``) walks ``games_active_games`` and hands
each row to its recoverer, which rebuilds the view and re-binds it to the stored
message via ``bot.add_view(view, message_id=...)``. These tests launch a game,
simulate a restart by clearing the in-memory view map, run the sweep, and assert
the view is back and bound to the right message.
"""

import asyncio

import pytest

from bot_modules.cogs.games_clapback_cog import ClapbackCog
from bot_modules.cogs.games_fantasies_cog import FantasiesCog, FantasiesMainView
from bot_modules.cogs.games_ffa_cog import FFACog
from bot_modules.cogs.games_hottakes_cog import HotTakeVoteView, HotTakesCog
from bot_modules.cogs.games_legitlibs import LegitLibsCog
from bot_modules.cogs.games_price_cog import PriceCog
from bot_modules.cogs.games_rushmore_cog import RushmoreCog, RushmoreJoinView
from bot_modules.cogs.games_mfk_cog import MFKCog
from bot_modules.cogs.games_mlt_cog import MLTCog, MLTJoinView
from bot_modules.cogs.games_nhie_cog import NHIECog
from bot_modules.cogs.games_story_cog import StoryCog, StoryJoinView
from bot_modules.cogs.games_traditional_cog import TraditionalCog
from bot_modules.cogs.games_ttl_cog import TTLCog, TTLGuessView
from bot_modules.cogs.games_wyr_cog import WYRCog
from bot_modules.games.utils.game_manager import (
    create_game,
    get_game_payload,
    update_game_message,
    update_game_payload,
)
from bot_modules.games.utils.recovery import recover_active_games
from bot_modules.services.games_db import GamesDb


class _FakeMessage:
    def __init__(self, mid: int):
        self.id = mid
        self.embeds: list = []
        self.edited = False

    async def edit(self, **kwargs):
        self.edited = True
        return None


class _FakeChannel:
    guild = None

    def __init__(self, cid: int):
        self.id = cid
        self.name = "games"
        self.sends: list = []
        self._messages: dict[int, _FakeMessage] = {}
        self._next = 1000

    async def send(self, *args, **kwargs):
        self._next += 1
        msg = _FakeMessage(self._next)
        self._messages[msg.id] = msg
        self.sends.append((args, kwargs))
        return msg

    async def fetch_message(self, mid: int):
        msg = self._messages.get(int(mid))
        if msg is None:
            raise KeyError(mid)  # mimics discord.NotFound for a deleted message
        return msg


class _FakeBot:
    def __init__(self, db: GamesDb):
        self.games_db = db
        self.active_views: dict = {}
        self.game_launchers: dict = {}
        self.game_recoverers: dict = {}
        self.added_views: list = []
        self._channels: dict[int, _FakeChannel] = {}

    def get_channel(self, cid: int):
        return self._channels.get(int(cid))

    def add_view(self, view, message_id=None):
        self.added_views.append((view, message_id))


async def _launch_and_restart(cog_cls, game_type, launch_options):
    """Launch a game, then simulate a restart (clear the in-memory view map)."""
    db = GamesDb(_launch_and_restart.db_path)
    bot = _FakeBot(db)
    cog = cog_cls(bot)  # type: ignore[arg-type]
    bot.game_recoverers[game_type] = cog.recover_game

    channel = _FakeChannel(4242)
    bot._channels[channel.id] = channel

    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester",
        guild_id=9001, options=launch_options,
    )
    assert game_id is not None

    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (game_id,))
    assert row["message_id"] is not None
    launched_msg_id = row["message_id"]

    # Simulate the restart: the persistent-view registry and active_views are bare.
    bot.active_views.clear()

    await recover_active_games(bot)
    return bot, game_id, launched_msg_id


@pytest.fixture(autouse=True)
def _bind_db(sync_db_path):
    _launch_and_restart.db_path = sync_db_path
    yield


async def test_traditional_recovers_view_bound_to_message():
    bot, game_id, msg_id = await _launch_and_restart(TraditionalCog, "traditional", {})

    assert game_id in bot.active_views, "view not re-registered in active_views"
    assert len(bot.added_views) == 1, "expected exactly one add_view call"
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id, "view must be bound to the game's message_id"
    assert bot.active_views[game_id] is view


async def test_ffa_recovers_with_prompt_from_payload():
    # Embed mode (/games play ffa) is the stateful, recoverable variant; a custom
    # prompt skips the DB prompt bank so the test needs no seeded questions.
    bot, game_id, msg_id = await _launch_and_restart(
        FFACog, "ffa", {"prompt": "best pizza topping?", "kind": "truth"}
    )

    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id
    assert view.text == "best pizza topping?", "prompt not restored from payload"


async def test_mfk_recovers_view():
    bot, game_id, msg_id = await _launch_and_restart(MFKCog, "mfk", {})

    assert game_id in bot.active_views
    _, bound_id = bot.added_views[0]
    assert bound_id == msg_id


async def test_unknown_game_type_is_skipped_not_crashed(sync_db_path):
    """A game whose type has no registered recoverer is skipped cleanly."""
    from bot_modules.games.utils.game_manager import create_game, update_game_message

    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)  # no recoverers registered
    channel = _FakeChannel(777)
    bot._channels[channel.id] = channel
    msg = await channel.send()

    game_id = await create_game(db, channel.id, 1, "no_such_game", state="open", payload={})
    await update_game_message(db, game_id, msg.id)

    await recover_active_games(bot)  # must not raise

    assert bot.added_views == []
    assert game_id not in bot.active_views


async def test_wyr_recovers_round_view():
    bot, game_id, msg_id = await _launch_and_restart(WYRCog, "wyr", {"question": "fly | be invisible"})
    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id
    assert view.option_a == "fly" and view.option_b == "be invisible"


async def test_nhie_recovers_round_view():
    bot, game_id, msg_id = await _launch_and_restart(NHIECog, "nhie", {"question": "ghosted someone"})
    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert bound_id == msg_id
    assert view.statement == "ghosted someone"


async def test_mlt_recovers_join_lobby():
    bot, game_id, msg_id = await _launch_and_restart(MLTCog, "mlt", {})
    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert isinstance(view, MLTJoinView)
    assert bound_id == msg_id


async def test_story_recovers_join_lobby():
    bot, game_id, msg_id = await _launch_and_restart(StoryCog, "story", {})
    assert game_id in bot.active_views
    view, bound_id = bot.added_views[0]
    assert isinstance(view, StoryJoinView)
    assert bound_id == msg_id


async def test_story_in_play_ends_gracefully(sync_db_path):
    """A story past the join phase can't resume, so it's ended cleanly."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = StoryCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["story"] = cog.recover_game
    channel = _FakeChannel(999)
    bot._channels[channel.id] = channel
    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )
    payload = await get_game_payload(db, game_id)
    payload["sentences"] = [{"author": 2001, "text": "Once upon a time"}]
    await update_game_payload(db, game_id, payload)

    bot.active_views.clear()
    await recover_active_games(bot)

    assert bot.added_views == []  # no view re-registered
    assert game_id not in bot.active_views
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (game_id,))
    assert row is None  # game ended


async def test_clapback_recover_respawns_game_loop(sync_db_path):
    """Recovery retires the stale phase message and re-spawns the game loop."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = ClapbackCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["clapback"] = cog.recover_game
    calls = {}

    async def fake_run_game(game_id, channel, payload):
        calls["run_game"] = len(payload.get("round_history", [])) + 1

    cog._run_game = fake_run_game  # type: ignore[method-assign]

    channel = _FakeChannel(1100)
    bot._channels[channel.id] = channel
    game_id = await create_game(
        db, channel.id, 1, "clapback", state="playing",
        payload={"config": {"rounds": 3}, "players": [1, 2], "host_id": 1, "scores": {},
                 "round_history": [{"round": 1, "prompt": "p", "matchups": []}]},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    _baseline = set(asyncio.all_tasks())
    await recover_active_games(bot)
    for _ in range(10):
        await asyncio.sleep(0.01)
        if "run_game" in calls:
            break
    try:
        assert stale.edited
        assert calls.get("run_game") == 2  # resumes after the one completed round
    finally:
        _cancel_pending(_baseline)


async def test_price_recover_resumes_mid_round_without_score_drift(sync_db_path):
    """Round 2 was submitted (so it's in payload["rounds"]) but interrupted
    mid-scoring. Resume must use completed_rounds (1) -> round 2, NOT
    len(rounds) (2) -> round 3, and roll scores back to the checkpoint so the
    partial round-2 point is neither double-counted nor lost."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = PriceCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["price"] = cog.recover_game
    calls = {}

    async def fake_run_round(*, round_num, **kw):
        calls["round_num"] = round_num

    cog._run_round = fake_run_round  # type: ignore[method-assign]

    channel = _FakeChannel(1200)
    bot._channels[channel.id] = channel
    game_id = await create_game(
        db, channel.id, 7, "price", state="playing",
        payload={"settings": {"rounds": 3, "timer": 60, "vote_timer": 30, "source": "bank"},
                 "total_rounds": 3,
                 "rounds": {"1": {}, "2": {}},  # round 2 entry exists but wasn't scored
                 "completed_rounds": 1,
                 "scores": {"reasonable_wins": {"7": 2}, "unhinged_wins": {}},      # partial round-2 point
                 "scores_checkpoint": {"reasonable_wins": {"7": 1}, "unhinged_wins": {}}},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    _baseline = set(asyncio.all_tasks())
    await recover_active_games(bot)
    for _ in range(10):
        await asyncio.sleep(0.01)
        if "round_num" in calls:
            break
    try:
        assert stale.edited
        assert calls.get("round_num") == 2  # completed_rounds+1, not len(rounds)+1
        payload = await get_game_payload(db, game_id)
        assert payload["scores"] == {"reasonable_wins": {"7": 1}, "unhinged_wins": {}}
    finally:
        _cancel_pending(_baseline)


async def test_rushmore_recover_join_lobby(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = RushmoreCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["rushmore"] = cog.recover_game
    channel = _FakeChannel(1300)
    bot._channels[channel.id] = channel
    game_id = await create_game(
        db, channel.id, 9, "rushmore", state="joining",
        payload={"settings": {"timer": 60, "source": "host", "vote_timer": 30},
                 "players": [9, 10], "topic": "movies"},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    await recover_active_games(bot)

    view, bound_id = bot.added_views[0]
    assert isinstance(view, RushmoreJoinView)
    assert bound_id == stale.id
    assert view.players == [9, 10]
    assert view.topic == "movies"


async def test_rushmore_recover_underway_ends_gracefully(sync_db_path):
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = RushmoreCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["rushmore"] = cog.recover_game
    channel = _FakeChannel(1301)
    bot._channels[channel.id] = channel
    game_id = await create_game(
        db, channel.id, 9, "rushmore", state="playing",
        payload={"settings": {"timer": 60, "source": "host", "vote_timer": 30},
                 "players": [9, 10], "topic": "movies", "boards": {}},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    await recover_active_games(bot)

    assert bot.added_views == []
    row = await db.fetchone("SELECT * FROM games_active_games WHERE game_id = ?", (game_id,))
    assert row is None  # underway game ended gracefully


async def test_fantasies_recovers_control_panel(sync_db_path):
    """Host-driven: the main control panel re-registers, round counter restored."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = FantasiesCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["fantasies"] = cog.recover_game
    channel = _FakeChannel(321)
    bot._channels[channel.id] = channel
    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )
    row = await db.fetchone("SELECT message_id FROM games_active_games WHERE game_id = ?", (game_id,))
    msg_id = row["message_id"]
    payload = await get_game_payload(db, game_id)
    payload["rounds"] = {"1": [{"text": "x", "user_id": 1, "category": "F"}],
                         "2": [{"text": "y", "user_id": 2, "category": "F"}]}
    await update_game_payload(db, game_id, payload)

    bot.active_views.clear()
    await recover_active_games(bot)

    view, bound_id = bot.added_views[0]
    assert isinstance(view, FantasiesMainView)
    assert bound_id == msg_id
    assert view.round_num == 2  # numbering continues from the last collected round


async def _poll_for(bot, game_id, view_type, tries=50):
    """Wait for a re-driven background loop to post and register its view."""
    for _ in range(tries):
        if isinstance(bot.active_views.get(game_id), view_type):
            return bot.active_views[game_id]
        await asyncio.sleep(0.02)
    return bot.active_views.get(game_id)


def _cancel_pending(baseline):
    """Cancel only tasks spawned since ``baseline`` (re-driven game loops).

    Scoped to avoid touching the test runner's own tasks, which matters under
    pytest-xdist / asyncio_mode=auto.
    """
    for task in asyncio.all_tasks() - baseline:
        if task is not asyncio.current_task():
            task.cancel()


async def test_ttl_redrives_guessing_skipping_played_subjects(sync_db_path):
    """Re-drive restarts the interrupted subject's round; scored subjects skipped."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = TTLCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["ttl"] = cog.recover_game
    channel = _FakeChannel(654)
    bot._channels[channel.id] = channel
    subs = {"111": {"statements": ["a", "b", "c"], "lie": 2},
            "222": {"statements": ["d", "e", "f"], "lie": 0}}
    game_id = await create_game(
        db, channel.id, 2001, "ttl", state="guessing",
        payload={"submissions": subs, "submission_count": 2, "submitter_names": {},
                 "scores": {"111": {"correct": 1, "fooled": 0, "points": 1}}, "prompt": None},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    _baseline = set(asyncio.all_tasks())
    await recover_active_games(bot)
    try:
        view = await _poll_for(bot, game_id, TTLGuessView)
        assert isinstance(view, TTLGuessView), f"got {type(view).__name__}"
        assert view.subject_id == 222  # 111 already scored -> skipped
        # The loop registers the view *before* posting the fresh message and
        # persisting its id (games_ttl_cog: active_views[..] = view → send →
        # update_game_message), so poll rather than read the row once.
        row = None
        for _ in range(50):
            row = await db.fetchone(
                "SELECT message_id FROM games_active_games WHERE game_id = ?", (game_id,)
            )
            if row is not None and int(row["message_id"]) != stale.id:
                break
            await asyncio.sleep(0.02)
        assert row is not None and int(row["message_id"]) != stale.id
    finally:
        _cancel_pending(_baseline)


async def test_hottakes_redrives_voting_skipping_done_takes(sync_db_path):
    """Re-drive resumes voting at the first take without a persisted result."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = HotTakesCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["hottakes"] = cog.recover_game
    channel = _FakeChannel(655)
    bot._channels[channel.id] = channel
    takes = [{"text": "take A", "user_id": 111}, {"text": "take B", "user_id": 222}]
    results = [{"text": "take A", "counts": {}, "avg": 0, "std": 0, "voters": [111], "author": 111}]
    game_id = await create_game(
        db, channel.id, 2001, "hottakes", state="voting",
        payload={"takes": takes, "results": results},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    _baseline = set(asyncio.all_tasks())
    await recover_active_games(bot)
    try:
        view = await _poll_for(bot, game_id, HotTakeVoteView)
        assert isinstance(view, HotTakeVoteView), f"got {type(view).__name__}"
        assert view.take_num == 2  # take A done -> resume at take B
        assert view.take_text == "take B"
    finally:
        _cancel_pending(_baseline)


async def _legitlibs_interrupted(sync_db_path, mode):
    """Seed an in-flight LegitLibs round, restart, and run the sweep."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = LegitLibsCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["legitlibs"] = cog.recover_game
    channel = _FakeChannel(1500)
    bot._channels[channel.id] = channel
    game_id = await create_game(
        db, channel.id, 5, "legitlibs", state="joining",
        payload={"mode": mode, "players": [5], "host_id": 5},
    )
    stale = await channel.send()
    await update_game_message(db, game_id, stale.id)

    bot.active_views.clear()
    await recover_active_games(bot)
    return bot, channel, game_id


async def test_legitlibs_interrupted_ends_and_unblocks(sync_db_path):
    """A mid-flight LegitLibs round can't resume, so recovery ends it (freeing
    the channel) and posts a restart notice — no view is re-registered."""
    bot, channel, game_id = await _legitlibs_interrupted(sync_db_path, "classic")

    assert bot.added_views == []  # nothing to rebind — it's a blocking loop
    assert game_id not in bot.active_views
    row = await bot.games_db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    assert row is None  # channel unblocked immediately
    notices = [a for (a, _kw) in channel.sends if a and "interrupted by a bot restart" in str(a[0])]
    assert notices, "no restart notice posted"
    assert "LegitLibs" in str(notices[-1][0])


async def test_legitlibs_quiplash_interrupted_labels_quiplash(sync_db_path):
    """Quiplash shares the legitlibs launcher/type; the notice names the mode."""
    bot, channel, game_id = await _legitlibs_interrupted(sync_db_path, "quiplash")

    row = await bot.games_db.fetchone(
        "SELECT * FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    assert row is None
    notices = [a for (a, _kw) in channel.sends if a and "interrupted by a bot restart" in str(a[0])]
    assert notices and "Quiplash" in str(notices[-1][0])


async def test_legitlibs_setup_registers_recoverer(monkeypatch):
    """setup() wires legitlibs into bot.game_recoverers (the registry the sweep
    walks); without it a restart mid-round bricks every button."""
    from discord import app_commands

    from bot_modules.cogs import games_legitlibs

    # Throwaway command group so setup() doesn't mutate the shared `play` group.
    monkeypatch.setattr(
        games_legitlibs, "play", app_commands.Group(name="play", description="x")
    )

    class _Bot:
        def __init__(self):
            self.game_launchers: dict = {}
            self.game_recoverers: dict = {}
            self.tree = self

        def remove_command(self, _name):
            return None

        async def add_cog(self, _cog):
            return None

    bot = _Bot()
    await games_legitlibs.setup(bot)
    assert "legitlibs" in bot.game_recoverers


async def test_deleted_anchor_message_is_skipped(sync_db_path):
    """When the anchor message is gone, recovery skips rather than crashing."""
    db = GamesDb(sync_db_path)
    bot = _FakeBot(db)
    cog = TraditionalCog(bot)  # type: ignore[arg-type]
    bot.game_recoverers["traditional"] = cog.recover_game

    channel = _FakeChannel(888)
    bot._channels[channel.id] = channel
    game_id = await cog.launch(
        channel=channel, host_id=2001, host_name="Tester", guild_id=9001, options={},
    )
    bot.active_views.clear()

    # Drop the message so fetch_message raises (simulates a deleted message).
    channel._messages.clear()

    await recover_active_games(bot)  # must not raise

    assert bot.added_views == []
    assert game_id not in bot.active_views
