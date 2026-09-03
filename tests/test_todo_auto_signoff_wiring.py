"""The glue that lets a recurring chore sign itself off.

``auto_complete_chores`` itself is covered in
``tests/test_todo_recurring_service.py``; this file is about the *call sites*,
because for this feature the call sites carry a rule that the service can't
enforce and a reviewer can't see from either end alone:

    **A scheduled game is not you running a game.**

That rule is implemented by geography, not by a flag. Every party game reaches
its board through two doors — a member's ``/games play``, and the scheduler
calling the same ``launch()`` on a timer — and only the interactive door passes
through ``finish_launch_response``. Nothing in the scheduler opts out, so
nothing in the scheduler can forget to. The tripwires below are what stop that
from being quietly undone by a later refactor that moves the call one frame.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from bot_modules.core.db_utils import open_db
from bot_modules.games.utils.game_manager import sign_off_game_chore
from bot_modules.services.todo_recurring_service import (
    VALID_AUTO_COMPLETE,
    create_recurring,
    has_open_instance,
)
from tests.db_template import migrated_db

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"

GUILD = 555
HOST = 7007
#: 2026-09-02 09:30 UTC — past the 09:00 slot below, so creating the chore
#: materialises today's instance and there is something to tick.
NOW = 1_788_341_400.0


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    migrated_db(db_path)
    return db_path


class _StubMember:
    def __init__(self, user_id, is_mod):
        self.id = user_id
        self.is_mod = is_mod


class _StubGuild:
    def __init__(self, members):
        self._members = {m.id: m for m in members}

    def get_member(self, user_id):
        return self._members.get(user_id)


class _StubCtx:
    def __init__(self, db_path):
        self.db_path = db_path
        self.config_loads = 0

    def open_db(self):
        return open_db(self.db_path)

    def guild_config(self, guild_id):
        """Warmed off-loop before the mod check, so the Member stays on it."""
        self.config_loads += 1
        return object()

    def member_is_mod(self, member):
        return member.is_mod


class _StubCog:
    def __init__(self, bot):
        self._bot = bot

    async def refresh_board(self, guild_id):
        self._bot.repainted.append(guild_id)
        return True


class _StubBot:
    """Just enough bot: ``ctx``, the guild it resolves the actor in, and the cog."""

    def __init__(self, ctx=None, *, members=(_StubMember(HOST, True),)):
        if ctx is not None:
            self.ctx = ctx
        self.repainted: list[int] = []
        self._guild = _StubGuild(members)

    def get_guild(self, guild_id):
        return self._guild if guild_id == GUILD else None

    def get_cog(self, name):
        return _StubCog(self) if name == "TodoCog" else None


def _game_chore(conn, *, guild=GUILD):
    rid = create_recurring(
        conn, guild, task="Run any game somewhere", recurrence="daily",
        time_of_day=540, auto_complete="game", created_by=HOST, now_ts=NOW,
    )
    # Without this the "it ticked nothing" assertions below would pass on a
    # chore that never had an instance to tick — which is exactly how the
    # first draft of this file went green while testing nothing.
    assert has_open_instance(conn, rid)
    return rid


# ── the helper the game seams call ────────────────────────────────────


async def test_sign_off_game_chore_ticks_the_chore(db):
    with open_db(db) as conn:
        rid = _game_chore(conn)

    await sign_off_game_chore(_StubBot(_StubCtx(db)), GUILD, HOST)

    with open_db(db) as conn:
        assert not has_open_instance(conn, rid)


@pytest.mark.parametrize(
    ("guild_id", "user_id"),
    [(None, HOST), (0, HOST), (GUILD, None), (GUILD, 0)],
)
async def test_sign_off_game_chore_ignores_an_unresolved_actor(db, guild_id, user_id):
    """A DM, or a game whose guild couldn't be resolved, credits nobody."""
    with open_db(db) as conn:
        rid = _game_chore(conn)

    await sign_off_game_chore(_StubBot(_StubCtx(db)), guild_id, user_id)

    with open_db(db) as conn:
        assert has_open_instance(conn, rid)


async def test_sign_off_game_chore_never_breaks_the_launch(db):
    """It hangs off a game starting. A broken chore must not eat the game."""
    with open_db(db) as conn:
        _game_chore(conn)

    # No ctx at all, and a ctx whose db is gone: both are swallowed.
    await sign_off_game_chore(_StubBot(), GUILD, HOST)
    await sign_off_game_chore(_StubBot(_StubCtx(db.parent / "no-such.db")), GUILD, HOST)


async def test_sign_off_game_chore_repaints_the_board(db):
    """The row is only half the job — the board is where a mod reads it.

    ``todo_board_loop`` repaints only what it spawned itself plus failed
    retries, so without this the chore stays visibly outstanding until the next
    daily spawn even though the database says otherwise.
    """
    with open_db(db) as conn:
        _game_chore(conn)

    bot = _StubBot(_StubCtx(db))
    bot.repainted = []
    await sign_off_game_chore(bot, GUILD, HOST)

    assert bot.repainted == [GUILD]


async def test_a_game_with_no_chore_behind_it_costs_no_board_edit(db):
    """Every game start would otherwise repaint a board it never changed."""
    bot = _StubBot(_StubCtx(db))
    bot.repainted = []
    await sign_off_game_chore(bot, GUILD, HOST)

    assert bot.repainted == []


def test_auto_complete_chores_never_raises(db, monkeypatch):
    """Its docstring promises this, and a third caller will believe it.

    One unhappy definition must also not swallow the chores behind it.
    """
    import bot_modules.services.todo_recurring_service as svc

    with open_db(db) as conn:
        _game_chore(conn)
        second = create_recurring(
            conn, GUILD, task="Run a second game", recurrence="daily",
            time_of_day=540, auto_complete="game", now_ts=NOW,
        )

        real = svc.open_instance_id
        calls = {"n": 0}

        def _explode_once(conn_, recurring_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db went away")
            return real(conn_, recurring_id)

        monkeypatch.setattr(svc, "open_instance_id", _explode_once)
        done = svc.auto_complete_chores(
            conn, GUILD, "game", completed_by=HOST, now_ts=NOW
        )

        # The first blew up; the second still got its tick.
        assert len(done) == 1
        assert not has_open_instance(conn, second)


async def test_an_ordinary_member_does_not_tick_the_mod_chore(db):
    """Two members duelling is a game being run — not a mod doing their chore.

    Without this the chore went green on any active day with no moderator
    involved, which makes it a report on how busy the server was.
    """
    with open_db(db) as conn:
        rid = _game_chore(conn)

    bot = _StubBot(_StubCtx(db), members=(_StubMember(HOST, False),))
    await sign_off_game_chore(bot, GUILD, HOST)

    with open_db(db) as conn:
        assert has_open_instance(conn, rid)
    assert bot.repainted == []


async def test_an_unresolvable_member_is_not_treated_as_a_mod(db):
    """A chore left open is recoverable; a tick that shouldn't have happened isn't."""
    with open_db(db) as conn:
        rid = _game_chore(conn)

    await sign_off_game_chore(_StubBot(_StubCtx(db), members=()), GUILD, HOST)

    with open_db(db) as conn:
        assert has_open_instance(conn, rid)


# ── where it is (and isn't) called ────────────────────────────────────


def _module(relative: str) -> ast.Module:
    return ast.parse((SRC / relative).read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — did it get renamed?")


def _calls(node: ast.AST) -> set[str]:
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                out.add(func.id)
            elif isinstance(func, ast.Attribute):
                out.add(func.attr)
    return out


@pytest.mark.parametrize(
    ("module", "function"),
    [
        # A party game started by hand from /games play.
        ("bot_modules/games/utils/game_manager.py", "finish_launch_response"),
        # A 1v1 duel the challenged player accepted.
        ("bot_modules/duels/base_duel.py", "_handle_accept"),
        # An N-player lobby game that reached its start.
        ("bot_modules/duels/base_game.py", "_handle_lobby_start"),
        # Risky Rolls opened by hand — it has its own command and does not
        # share the party games' finish_launch_response.
        ("bot_modules/cogs/risky_roll_cog.py", "_start_game"),
        # Recap relaunch buttons go straight to the launcher, missing the seam.
        ("bot_modules/cogs/games_price_cog.py", "run_again"),
        ("bot_modules/cogs/games_rushmore_cog.py", "run_again"),
        ("bot_modules/cogs/games_clapback_cog.py", "play_again"),
        ("bot_modules/cogs/games_clapback_cog.py", "play_again_shuffled"),
    ],
)
def test_the_human_launch_paths_sign_the_chore_off(module, function):
    assert "sign_off_game_chore" in _calls(_function(_module(module), function))


#: Relaunching a game from its recap — the shape that keeps being missed.
#: These call a launcher directly rather than going through the shared
#: ``finish_launch_response``, so each one has to carry the sign-off itself.
#:
#: Matched on the *method* rather than on ``self.cog.launch(``: a view that
#: holds its cog as ``self._cog``, or reaches it through ``bot.get_cog``, is
#: the same defect wearing different spelling, and the narrower pattern would
#: let it through while the existing matches kept the scan looking healthy.
_RELAUNCHERS = (".launch(", "._start_new_game(")


def test_no_recap_relaunch_button_is_left_without_the_seam():
    """Found by hand three times, once per game. Found by the suite from now on.

    The list above is a list, so it can be short by one and look complete —
    which is exactly what happened to Clapback. This asks the tree instead: any
    button handler that relaunches a game by calling a launcher directly must
    sign the chore off, or a mod restarting a round gets no credit for it.
    """
    missing = []
    found = []
    for path in (SRC / "bot_modules/cogs").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            # Button handlers only. A slash-command callback also calls its
            # launcher, but reaches the seam through finish_launch_response.
            decorators = " ".join(
                ast.get_source_segment(source, d) or "" for d in node.decorator_list
            )
            if "ui.button" not in decorators:
                continue
            body = ast.get_source_segment(source, node) or ""
            if not any(call in body for call in _RELAUNCHERS):
                continue
            found.append(f"{path.name}:{node.name}")
            if "sign_off_game_chore" not in body:
                missing.append(f"{path.name}:{node.name}")

    # A scan that matches nothing passes trivially, which would make this test
    # a decoration rather than a guard.
    assert len(found) >= 4, f"the relaunch scan stopped matching: {found}"
    assert not missing, f"relaunch without a chore sign-off: {missing}"


@pytest.mark.parametrize(
    "module",
    [
        "bot_modules/services/scheduled_games_service.py",
        # The second automatic launcher: feature rotation drives the same
        # bot.game_launchers registry, so listing only the scheduler would let
        # a refactor break the invariant with this tripwire still green.
        "bot_modules/services/feature_rotation_service.py",
    ],
)
def test_no_automatic_launcher_signs_the_chore_off(module):
    """The whole point of hooking the interactive seam.

    Two daily schedules already run in this server; if an automatic launch path
    ever reached the helper, the chore would tick itself green every morning
    and stop meaning anything.
    """
    source = (SRC / module).read_text(encoding="utf-8")
    assert "sign_off_game_chore" not in source
    assert "auto_complete_chores" not in source


def test_every_qotd_registration_signs_its_chore_off():
    """Both ways a question gets registered, held together.

    The marker path in events_cog and `/qotd post` in economy_cog are separate
    create_qotd call sites, and wiring only the first left the chore marked
    missed for the command a mod is most likely to use — the one that renders
    the card and drains the sponsored queue. A third call site would be just as
    easy to add and just as silent, so the rule is enforced here rather than
    remembered.
    """
    registrars = [
        path
        for path in SRC.rglob("*.py")
        if "create_qotd(" in path.read_text(encoding="utf-8")
        and path.name != "economy_service.py"  # where create_qotd is defined
    ]
    assert len(registrars) >= 2, "expected the marker path and /qotd post"
    for path in registrars:
        source = path.read_text(encoding="utf-8")
        assert "auto_complete_chores" in source, (
            f"{path.name} registers a QOTD but never signs the chore off"
        )


def test_the_marker_registration_signs_its_chore_off():
    """Inside the create_qotd branch, so it inherits its once-per-message gate."""
    handler = _module("bot_modules/cogs/events_cog.py")
    calls = _calls(handler)
    assert "auto_complete_chores" in calls

    source = (SRC / "bot_modules/cogs/events_cog.py").read_text(encoding="utf-8")
    registration = source.split("create_qotd(", 1)[1]
    assert 'auto_complete_chores' in registration[:1200]
    assert '"qotd"' in registration[:1200]


# ── the picker and the service agree ──────────────────────────────────


def test_the_dashboard_offers_exactly_the_triggers_the_service_knows():
    """A picker value the service rejects is a chore that never signs off.

    The two lists are written in different languages and can't import each
    other, so this is the only thing holding them together.
    """
    panel = (
        SRC / "web_server/static/js/panels/todo.js"
    ).read_text(encoding="utf-8")
    block = panel.split("AUTO_COMPLETE_OPTIONS = [", 1)[1].split("];", 1)[0]
    offered = [v for v in re.findall(r'\["([^"]*)",', block) if v]
    assert offered == list(VALID_AUTO_COMPLETE)
