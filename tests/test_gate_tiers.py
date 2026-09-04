"""Which gate tier runs the two heavy checks (pyright, browser sweep).

Neither can be scoped to a diff — pyright needs the whole import graph, the
panel sweep needs a browser — so both cost the same minutes and hundreds of
megabytes on a one-line change as on a refactor. Running them automatically
meant N parallel sessions ran N copies, which is enough to wedge the machine.
CI owns them now; locally they are opt-in. These pin that matrix so it cannot
quietly come back, and so the CI job that replaced them cannot quietly go away.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gate  # noqa: E402

_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


@pytest.mark.parametrize(
    ("quick", "forced", "expected"),
    [
        # The automatic tiers — the pre-commit hook and the full gate — skip.
        pytest.param(False, False, False, id="automatic-tier-skips"),
        # --quick is the deliberate "check the heavy things before I push" tier.
        pytest.param(True, False, True, id="quick-runs-it"),
        # --pyright / --browser / GATE_* force one into any run.
        pytest.param(False, True, True, id="forced-runs-it"),
        pytest.param(True, True, True, id="quick-and-forced"),
    ],
)
def test_heavy_check_tier_matrix(quick, forced, expected):
    assert gate.wants_heavy(quick=quick, forced=forced) is expected


def test_the_pre_commit_hook_tier_runs_neither_heavy_check():
    """The hook runs --scoped, which is neither quick nor forced."""
    assert gate.wants_heavy(quick=False, forced=False) is False


def test_the_full_local_gate_runs_neither_heavy_check():
    """A full run is pytest, not a type check — CI owns pyright now."""
    assert gate.wants_heavy(quick=False, forced=False) is False


def test_a_forced_full_run_sweeps_every_panel_not_a_diff():
    """/dk-regress forces the sweep on a tree with no diff to scope to.

    `run_mobile` scopes to changed panels by default, which on a clean main
    selects nothing — so the pre-release tier must be able to ask for all of
    them explicitly, or `--browser` is a silent no-op there.
    """
    import inspect

    sig = inspect.signature(gate.run_mobile)
    assert "all_panels" in sig.parameters
    assert sig.parameters["all_panels"].default is False


def _workflow(name: str) -> dict:
    return yaml.safe_load(
        (_WORKFLOWS / name).read_text(encoding="utf-8")  # cp1252 runners exist
    )


@pytest.mark.skipif(
    not (_WORKFLOWS / "test.yml").exists(),
    reason="workflows are not synced to the remote test runner",
)
def test_push_ci_still_type_checks_and_runs_the_browser_sweep():
    """The local gate stopped running these; CI must be the one that does."""
    jobs = _workflow("test.yml")["jobs"]
    steps = [s.get("run", "") for s in jobs["test"]["steps"]]
    assert any("pyright" in s for s in steps), "push CI lost its pyright step"

    assert "browser" in jobs, "push CI lost its browser job"
    browser_steps = " ".join(s.get("run", "") for s in jobs["browser"]["steps"])
    assert "playwright install" in browser_steps
    # By marker, never by filename — naming files once left five of the seven
    # browser test files running in no tier at all.
    assert "-m browser" in browser_steps
