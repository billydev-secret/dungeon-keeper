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
