"""The ship flow's review stages must stay wired to the things they name.

`/dk-ship` calls two reviewers by name: the built-in `code-review` skill and
this repo's own `standards-review` agent. Both are prose references in a
markdown command file, so nothing but this test stops a rename or a deleted
agent from turning a ship stage into a silent no-op — and a review stage that
quietly stops running is worse than not having one, because the ship still
reports success.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SHIP = _ROOT / ".claude" / "commands" / "dk-ship.md"
_REGRESS = _ROOT / ".claude" / "commands" / "dk-regress.md"
_AGENT = _ROOT / ".claude" / "agents" / "standards-review.md"

pytestmark = pytest.mark.skipif(
    not _SHIP.exists(),
    reason=".claude/ is not synced to the remote test runner",
)


def _ship() -> str:
    return _SHIP.read_text(encoding="utf-8")


def test_the_standards_agent_the_ship_flow_names_exists():
    assert _AGENT.exists(), "dk-ship calls standards-review; the agent file is gone"


def test_the_agent_declares_the_name_the_ship_flow_calls():
    front = _AGENT.read_text(encoding="utf-8").split("---")[1]
    assert "name: standards-review" in front


def test_the_ship_flow_still_invokes_both_reviewers():
    s = _ship()
    assert "code-review" in s, "the code review stage vanished from dk-ship"
    assert "standards-review" in s, "the standards stage vanished from dk-ship"


def test_both_stages_are_default_on_with_an_escape_hatch():
    s = _ship()
    for flag in ("--no-review", "--no-standards"):
        assert flag in s, f"{flag} is undocumented — the stage has no opt-out"
    # "skip only if" is the phrasing that makes a stage default-on; if a stage
    # ever flips to opt-in, this is the line that should have changed with it.
    assert s.count("skip only if") >= 2


def test_the_standards_agent_is_read_only():
    """It reports; it must never edit. Edits belong to /code-review --fix."""
    front = _AGENT.read_text(encoding="utf-8").split("---")[1]
    tools = next(
        (ln.split(":", 1)[1] for ln in front.splitlines() if ln.startswith("tools:")),
        "",
    )
    for writer in ("Edit", "Write", "NotebookEdit"):
        assert writer not in tools, f"standards-review must not hold {writer}"


# ── the pre-release command ───────────────────────────────────────────


def test_dk_regress_exists_and_calls_the_standards_agent():
    assert _REGRESS.exists(), "the pre-release command is gone"
    s = _REGRESS.read_text(encoding="utf-8")
    assert "standards-review" in s


def test_dk_regress_forces_both_heavy_checks_back_on():
    """The per-commit tiers dropped pyright and the sweep; this tier is where
    they are paid before code reaches members, so it must ask for them."""
    s = _REGRESS.read_text(encoding="utf-8")
    assert "--pyright" in s and "--browser" in s


def test_dk_regress_never_restarts_the_service():
    """Restarting prod is the user's button, always."""
    s = _REGRESS.read_text(encoding="utf-8").lower()
    assert "systemctl restart" not in s
    assert "never restarts" in s
