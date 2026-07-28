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
        "src/dungeonkeeper/bot.py",
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
