"""Which gate tier runs the whole-repo type check.

pyright is unscopable — it needs the whole import graph, so a one-line change
costs the same minutes and ~1.5 GB as a refactor. Running it per commit meant N
parallel sessions ran N copies, which is enough memory pressure to wedge the
machine. These pin the tier matrix so it can't quietly come back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gate  # noqa: E402


@pytest.mark.parametrize(
    ("scoped", "quick", "forced", "expected"),
    [
        # The full gate — the pre-push / gate-main tier — always type-checks.
        pytest.param(False, False, False, True, id="full-gate-runs-it"),
        # The two per-commit tiers do not.
        pytest.param(True, False, False, False, id="scoped-skips-it"),
        pytest.param(False, True, False, False, id="quick-skips-it"),
        # --pyright / GATE_PYRIGHT=1 puts it back, in either fast tier.
        pytest.param(True, False, True, True, id="scoped-forced"),
        pytest.param(False, True, True, True, id="quick-forced"),
        # Forcing on the full gate changes nothing — it was already running.
        pytest.param(False, False, True, True, id="full-gate-forced"),
    ],
)
def test_pyright_tier_matrix(scoped, quick, forced, expected):
    assert gate.wants_pyright(scoped=scoped, quick=quick, forced=forced) is expected


def test_the_per_commit_hook_tier_does_not_type_check():
    """The pre-commit hook runs --scoped; that is the path that must stay cheap."""
    assert gate.wants_pyright(scoped=True, quick=False, forced=False) is False
