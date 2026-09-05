"""The env scrub in ``tests/conftest.py`` has to keep up with the code.

``bot_modules.core.config`` calls ``load_dotenv(override=True)`` at module
scope, so every test runs with the developer's real ``.env`` merged into
``os.environ`` — and the production checkout has one. ``_hermetic_env`` scrubs
that back out, but only for the names it knows about, and a comment asking
future authors to add new ones by hand is an honour system.

This file is the gate. A new ``os.getenv`` that is in neither
``SCRUBBED_ENV_VARS`` nor ``ENV_NOT_SCRUBBED`` fails here, so the choice —
"is this an application setting or is it machine plumbing?" — is made
deliberately, once, rather than discovered later as a test that passes on CI
and behaves differently on the machine that serves the dashboard.

Scope is ``src/web_server`` and ``src/bot_modules``: the two trees the suite
actually exercises. ``src/beta_tools``, ``src/dk_mcp`` and the
``src/dungeonkeeper`` entry point are separate processes with their own
bootstrap, and nothing here builds them.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import ENV_NOT_SCRUBBED, SCRUBBED_ENV_VARS

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (_ROOT / "src" / "web_server", _ROOT / "src" / "bot_modules")

#: ``os.getenv("X")`` / ``os.environ["X"]`` / ``os.environ.get("X")``. An
#: f-string name (``DISCORD_TOKEN_{suffix}``) is deliberately not matched —
#: those are bootstrap credentials resolved per environment, and a test that
#: calls ``load_config`` supplies them itself.
_ENV_READ = re.compile(
    r"os\.(?:getenv\(|environ\.get\(|environ\[)\s*[\"']([A-Z][A-Z0-9_]*)[\"']"
)


def _env_reads() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for tree in _TREES:
        for path in tree.rglob("*.py"):
            for name in _ENV_READ.findall(path.read_text(encoding="utf-8")):
                found.setdefault(name, set()).add(str(path.relative_to(_ROOT)))
    return found


def test_every_env_read_is_classified():
    reads = _env_reads()
    known = set(SCRUBBED_ENV_VARS) | set(ENV_NOT_SCRUBBED)
    missing = {n: sorted(paths) for n, paths in reads.items() if n not in known}
    assert not missing, (
        "These environment variables are read by the app but classified "
        "nowhere, so a developer's .env leaks into every test that touches "
        "them. Add each to SCRUBBED_ENV_VARS in tests/conftest.py, or to "
        "ENV_NOT_SCRUBBED with the reason it must survive:\n"
        + "\n".join(f"  {n} — {', '.join(p)}" for n, p in sorted(missing.items()))
    )


def test_the_scrub_list_has_no_dead_entries():
    """A name nothing reads is stale — drop it rather than let the list rot."""
    stale = sorted(set(SCRUBBED_ENV_VARS) - set(_env_reads()))
    assert not stale, (
        "SCRUBBED_ENV_VARS names the app no longer reads: " + ", ".join(stale)
    )


def test_the_two_lists_do_not_overlap():
    """A name in both is ambiguous — the scrub would win and the reason lie."""
    both = sorted(set(SCRUBBED_ENV_VARS) & set(ENV_NOT_SCRUBBED))
    assert not both, "classified as both scrubbed and preserved: " + ", ".join(both)


def test_the_scrub_actually_applies_here():
    """End-to-end: the autouse fixture reaches a test outside ``tests/web/``.

    The per-directory version of this fixture could not, which is what made
    ``tests/test_web_routes.py`` — a dashboard suite living outside
    ``tests/web/`` — hand-roll its own partial scrub.
    """
    import os

    assert os.getenv("DASHBOARD_BASE_URL") is None
    assert os.getenv("SUPPORT_USER_ID") is None
