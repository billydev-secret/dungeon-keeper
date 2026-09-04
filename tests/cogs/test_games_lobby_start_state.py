"""Pressing a lobby game's start button must retire the 'joining' state.

The start-ping sweep polls `state='joining'`, so a game that starts but leaves
the row reading as an open lobby gets nudged "time to start" mid-game — a
clapback with `start_in:10` that the host starts at t+1min was still 'joining'
when its countdown expired at t+10min, four rounds in.

Only rushmore transitioned state before this; clapback/mlt/story created the
row 'joining' and never touched it again. mfk and compliment are exempt
because their close button ends the game outright, deleting the row.

These are wiring assertions, deliberately one per affected game: the guard
itself is unit-tested in tests/test_game_start_ping_service.py.
"""

from bot_modules.games.utils.game_manager import create_game
from bot_modules.services.games_db import GamesDb

CHAN = 4242
HOST = 5150


class _Resp:
    def __init__(self):
        self.edited = False

    async def edit_message(self, **kwargs):
        self.edited = True

    async def send_message(self, *a, **k):
        raise AssertionError(f"start was rejected by a guard: {a} {k}")


class _User:
    id = HOST
    display_name = "Host"


class _Chan:
    id = CHAN
    name = "games"

    async def send(self, *a, **k):
        return None


class _Interaction:
    """Minimal stand-in: guild is None so each game's start-ping block is skipped."""

    def __init__(self):
        self.user = _User()
        self.guild = None
        self.channel = _Chan()
        self.response = _Resp()


async def _state(db, game_id):
    row = await db.fetchone(
        "SELECT state FROM games_active_games WHERE game_id = ?", (game_id,)
    )
    return row["state"]


async def test_clapback_start_retires_the_joining_state(sync_db_path):
    import bot_modules.cogs.games_clapback_cog as cog_mod

    db = GamesDb(sync_db_path)
    players = [HOST, 2, 3]
    config = {"rounds": 1, "timer": 30, "vote_timer": 20}
    gid = await create_game(
        db, CHAN, HOST, "clapback", state="joining",
        payload={"config": config, "players": players, "host_id": HOST},
    )

    # The real cog, because Start now hands the lobby to its _begin_game /
    # _play pair (shared with the countdown auto-start); only the game loop
    # and the no-contact read are stubbed.
    from types import SimpleNamespace

    bot = SimpleNamespace(games_db=db, active_views={}, ctx=SimpleNamespace(db_path=sync_db_path))
    cog = cog_mod.ClapbackCog(bot)  # type: ignore[arg-type]

    async def _no_run(*a, **k):
        return None

    async def _no_pairs(guild, user_ids):
        return set()

    cog._run_game = _no_run  # type: ignore[method-assign]
    cog._forbidden_pairs = _no_pairs  # type: ignore[method-assign]
    view = cog_mod.ClapbackJoinView(gid, HOST, db, bot, cog, config)
    await view.start_game.callback(_Interaction())

    assert await _state(db, gid) != "joining"


async def test_mlt_start_retires_the_joining_state(sync_db_path):
    import bot_modules.cogs.games_mlt_cog as cog_mod

    db = GamesDb(sync_db_path)
    gid = await create_game(
        db, CHAN, HOST, "mlt", state="joining",
        payload={"players": [HOST, 2, 3], "rounds": {}, "crowns": {}},
    )

    class _Cog:
        async def _run_round(self, **k):
            return None

    view = cog_mod.MLTJoinView(gid, HOST, db, None, _Cog())
    await view.start_game.callback(_Interaction())

    assert await _state(db, gid) != "joining"


async def test_story_start_retires_the_joining_state(sync_db_path):
    import bot_modules.cogs.games_story_cog as cog_mod

    db = GamesDb(sync_db_path)
    gid = await create_game(
        db, CHAN, HOST, "story", state="joining",
        payload={"players": [HOST, 2], "sentences": [], "max_sentences": 10,
                 "visibility": "blind", "starter": ""},
    )

    class _Cog:
        async def _run_story(self, *a, **k):
            return None

    view = cog_mod.StoryJoinView(gid, HOST, db, None, _Cog())
    await view.start_story.callback(_Interaction())

    assert await _state(db, gid) != "joining"


async def test_ttl_start_guessing_records_the_guessing_state(sync_db_path):
    """Two Truths never wrote a phase, so boot recovery could not tell a lobby
    from a game in progress (vote-games-56); Start Guessing now says so."""
    import bot_modules.cogs.games_ttl_cog as cog_mod

    db = GamesDb(sync_db_path)
    subs = {"111": {"statements": ["a", "b", "c"], "lie": 2},
            "222": {"statements": ["d", "e", "f"], "lie": 0}}
    gid = await create_game(
        db, CHAN, HOST, "ttl", state="joining",
        payload={"submissions": subs, "submission_count": 2, "submitter_names": {},
                 "scores": {}, "prompt": None},
    )

    class _Cog:
        async def _run_guessing(self, **k):
            return None

    view = cog_mod.TTLSubmitView(gid, HOST, db, None, _Cog())
    await view.start_guessing.callback(_Interaction())  # type: ignore[arg-type]

    assert await _state(db, gid) == "guessing"
