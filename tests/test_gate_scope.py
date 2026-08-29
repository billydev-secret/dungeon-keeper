"""gate.py's source → test mapping and mandatory-test rule (pure logic).

These two heuristics decide what the per-commit scoped tier actually runs, and
which new files it refuses to let through untested — so each branch gets a case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gate  # noqa: E402


# ── mandatory-test rule ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/bios/logic.py",
        "src/bot_modules/inactive/store.py",
        "src/bot_modules/voice_master/voice_logic.py",
        "src/bot_modules/services/economy_service.py",
    ],
)
def test_logic_layers_require_a_test(path):
    assert gate.requires_test(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/bios/cog.py",
        "src/bot_modules/bios/views.py",
        "src/bot_modules/bios/embeds.py",
        "src/web_server/routes/bios.py",
        "scripts/gate.py",
        "src/bot_modules/bios/logic_helpers.py",
    ],
)
def test_glue_layers_do_not_require_a_test(path):
    assert gate.requires_test(path) is False


# ── feature-token mapping ────────────────────────────────────────────────

def test_nested_feature_dir_resolves_to_the_feature_not_cogs():
    # src/bot_modules/cogs/<feature>/ — 'cogs' is generic, 'casino' is the feature.
    toks = gate._tokens_for("src/bot_modules/cogs/casino/blackjack.py")
    assert "casino" in toks
    assert "cogs" not in toks


def test_nested_generic_filename_still_maps_to_its_feature():
    toks = gate._tokens_for("src/bot_modules/cogs/quickdraw/logic.py")
    assert toks == {"quickdraw"}


def test_top_level_feature_dir_still_maps():
    assert gate._tokens_for("src/bot_modules/voice_master/logic.py") == {"voice_master"}


def test_service_file_maps_to_both_bare_and_suffixed_names():
    toks = gate._tokens_for("src/bot_modules/services/economy_service.py")
    assert {"economy", "economy_service"} <= toks


def test_generic_only_path_maps_to_nothing():
    assert gate._tokens_for("src/bot_modules/utils.py") == set()


# ── migrations: new vs modified ──────────────────────────────────────────

def test_editing_an_existing_migration_forces_a_full_run():
    """An edited migration can reshape tables under the whole db-backed suite."""
    assert gate.forces_full_run("src/migrations/134_todo_board.sql", is_new=False) is True


def test_adding_a_new_migration_does_not_force_a_full_run():
    """A new file can't alter an existing table; it was 8 of 13 recent fallbacks."""
    assert gate.forces_full_run("src/migrations/199_new_thing.sql", is_new=True) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/bot_modules/core/sticky.py",
        "src/bot_modules/models/thing.py",
        "pyproject.toml",
        "tests/conftest.py",
        "tests/web/conftest.py",
    ],
)
def test_these_fan_out_even_when_newly_added(path):
    """Newness only ever excuses a migration — everything else still fans out."""
    assert gate.forces_full_run(path, is_new=True) is True


def test_select_tests_honours_a_new_migration():
    targets, _, run_full = gate.select_tests(
        ["src/migrations/199_new_thing.sql"], new={"src/migrations/199_new_thing.sql"}
    )
    assert run_full is False


def test_select_tests_still_fans_out_on_an_edited_migration():
    _, _, run_full = gate.select_tests(["src/migrations/134_todo_board.sql"], new=set())
    assert run_full is True


# ── bootstrap: mapped, not fanned out ────────────────────────────────────


def test_bootstrap_edit_does_not_force_a_full_run():
    """Nearly every feature ship registers itself in __main__.py; that edit
    maps to the wiring tests instead of invalidating the whole suite."""
    assert gate.forces_full_run("src/dungeonkeeper/__main__.py", is_new=False) is False


def test_select_tests_maps_bootstrap_to_tests_that_name_it():
    targets, unmapped, run_full = gate.select_tests(
        ["src/dungeonkeeper/__main__.py"], new=set()
    )
    assert run_full is False
    assert "src/dungeonkeeper/__main__.py" not in unmapped
    # The wiring tests read __main__.py by path, so the content scan finds them.
    assert any("wiring" in t or "structure" in t for t in targets), targets


# ── cogs reach the app context one way ────────────────────────────────


def _cog_classes():
    """Every commands.Cog subclass in src/, with its __init__ and its body."""
    import ast

    root = Path(__file__).resolve().parents[1] / "src"
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not our problem here
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if not any("Cog" in ast.unparse(b) for b in cls.bases):
                continue
            init = next(
                (n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
                None,
            )
            yield path.relative_to(root.parent).as_posix(), cls.name, init, cls


def test_no_cog_takes_a_ctx_parameter():
    """CLAUDE.md: a cog takes ``(self, bot)`` and reaches the context through it.

    Pinned repo-wide rather than per-directory because the one cog this
    branch's sweep missed — RulesWatchMonitor — lives in rules_watch/, not
    cogs/. A convention with an unpinned exception is how the next one gets
    written, and a rebase onto a main that added a cog would reintroduce the
    split silently.
    """

    offenders = [
        f"{path}::{name}"
        for path, name, init, _cls in _cog_classes()
        if init is not None and "ctx" in [a.arg for a in init.args.args]
    ]
    assert not offenders, (
        "these cogs still take a ctx argument; take (self, bot) and read "
        "self.bot.ctx instead:\n  " + "\n  ".join(offenders)
    )


def test_no_cog_stores_self_ctx():
    """The field that made helpers duck-type what a caller happened to hold."""
    import ast

    offenders = []
    for path, name, init, _cls in _cog_classes():
        if init is None:
            continue
        for node in ast.walk(init):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "ctx"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    offenders.append(f"{path}::{name}")
    assert not offenders, (
        "these cogs assign self.ctx; every cog already has self.bot, so read "
        "self.bot.ctx and let shared helpers rely on the bot:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )


def test_no_cog_reads_self_ctx_anywhere():
    """Not just in __init__ — a cog that reads self.ctx in a method body is an
    AttributeError waiting for that path to run.

    This is the half that protects a rebase. main has moved on while this
    branch was in flight and its new code does use ``self.ctx`` in cog method
    bodies; without this the sweep would have to be re-applied by hand and by
    memory. The __init__-only checks above wouldn't have seen it.
    """
    import ast

    offenders = []
    for path, name, _init, cls in _cog_classes():
        for node in ast.walk(cls):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "ctx"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                offenders.append(f"{path}:{node.lineno}  {name}")
    assert not offenders, (
        "a cog has no self.ctx — read self.bot.ctx instead:\n  "
        + "\n  ".join(sorted(set(offenders)))
    )
