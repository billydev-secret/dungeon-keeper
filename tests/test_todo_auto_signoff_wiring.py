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


class _StubCtx:
    def __init__(self, db_path):
        self.db_path = db_path

    def open_db(self):
        return open_db(self.db_path)


class _StubBot:
    """Just enough bot for the helper: it only ever reaches for ``ctx``."""

    def __init__(self, ctx=None):
        if ctx is not None:
            self.ctx = ctx


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
    ],
)
def test_the_human_launch_paths_sign_the_chore_off(module, function):
    assert "sign_off_game_chore" in _calls(_function(_module(module), function))


def test_the_scheduler_never_signs_the_chore_off():
    """The whole point of hooking the interactive seam.

    Two daily schedules already run in this server; if the scheduler's launch
    path ever reached the helper, the chore would tick itself green every
    morning and stop meaning anything.
    """
    source = (SRC / "bot_modules/services/scheduled_games_service.py").read_text(
        encoding="utf-8"
    )
    assert "sign_off_game_chore" not in source
    assert "auto_complete_chores" not in source


def test_the_qotd_registration_signs_its_chore_off():
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
