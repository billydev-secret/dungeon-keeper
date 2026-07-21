#!/usr/bin/env python3
"""Canonical pre-commit gate — cross-platform (Linux + Windows).

Usage:
    python scripts/gate.py            # ruff + pyright + FULL pytest
    python scripts/gate.py --scoped   # ruff + pyright + tests for changed files
    python scripts/gate.py --quick    # ruff + pyright + scoped browser panel checks (no pytest)
    python scripts/gate.py -k foo     # extra args forwarded to pytest

The pre-commit hook runs ``--scoped`` (not ``--quick``).

Runs everything with the repo venv's interpreter, located automatically,
so it works no matter which python launched this script.

``--scoped`` is the fast per-commit tier: it diffs the working tree against
HEAD (plus untracked files), maps each changed file to the tests that cover
it, and runs only those. The full suite still runs in CI on every push/PR and
nightly (``.github/workflows/nightly.yml``), so anything the heuristic misses
— e.g. a test that imports a changed module indirectly — is caught there.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# ── scoping heuristic ────────────────────────────────────────────────────
#
# The mapping is deliberately best-effort: bounded runtime beats perfect
# selection because CI runs the full suite as the real backstop. Two escape
# hatches keep it honest:
#   * FULL_RUN_* below force the whole suite when a broadly-shared file moves;
#   * changed source with no matching test is reported, not silently dropped.

# A changed path matching any of these invalidates ~everything → run it all.
FULL_RUN_FILES = {
    "pyproject.toml",
    "scripts/gate.py",
    "tests/conftest.py",
    "tests/fakes.py",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements.lock",
    "requirements-dev.lock",
}
FULL_RUN_PREFIXES = (
    "src/bot_modules/core/",  # app_context, db_utils, xp_system — imported everywhere
    "src/bot_modules/models/",
    "src/migrations/",  # schema change touches every db-backed test
    "src/dungeonkeeper/",  # bot entrypoint / bootstrap
)

# Tokens too generic to identify a feature on their own.
GENERIC_TOKENS = {
    "init",
    "utils",
    "service",
    "services",
    "cog",
    "cogs",
    "logic",
    "views",
    "view",
    "db",
    "models",
    "model",
    "config",
    "embeds",
    "commands",
    "helpers",
    "base",
    "main",
}


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    )
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def changed_paths() -> list[str]:
    """Uncommitted work about to be committed: tracked diff vs HEAD + untracked."""
    tracked = _git("diff", "--name-only", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return sorted(set(tracked) | set(untracked))


def new_paths() -> set[str]:
    """Files that did not exist at HEAD: added-in-diff + untracked."""
    added = _git("diff", "--name-only", "--diff-filter=A", "HEAD")
    untracked = _git("ls-files", "--others", "--exclude-standard")
    return set(added) | set(untracked)


# New source at these layers must ship with a mapped test — an unmapped *new*
# file here means untested logic entering the tree, which the scoped gate
# blocks (existing-file drift is left to CI/nightly). Cogs/views/embeds are
# intentionally excluded: they're glue, tested through the logic layer.
REQUIRE_TEST_SUFFIXES = ("_logic.py", "_service.py")


def _tokens_for(path: str) -> set[str]:
    """Feature tokens a source file maps onto, matched against test filenames."""
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    toks: set[str] = {stem}
    if path.startswith("src/bot_modules/services/"):
        toks.add(stem[:-8] if stem.endswith("_service") else stem)
    elif path.startswith("src/bot_modules/") and len(parts) > 3:
        toks.add(parts[2])  # feature directory name, e.g. voice_master, games_ama
    return {t for t in toks if t and t not in GENERIC_TOKENS}


def _test_files() -> list[Path]:
    return [
        p
        for p in TESTS.rglob("test_*.py")
        if "__pycache__" not in p.parts
    ]


def _matches(test_basename: str, token: str) -> bool:
    # segment match: token must be a whole _-delimited run within the name
    return f"_{token}_" in f"_{test_basename}_"


def select_tests(changed: list[str]) -> tuple[list[str], list[str], bool]:
    """Return (test targets, unmapped source files, run_full)."""
    if any(
        c in FULL_RUN_FILES
        or c.startswith(FULL_RUN_PREFIXES)
        or Path(c).name == "conftest.py"  # any dir's conftest fans out to its whole subtree
        for c in changed
    ):
        return [], [], True

    all_tests = _test_files()
    targets: set[str] = set()
    unmapped: list[str] = []

    for c in changed:
        if c.startswith("tests/") and Path(c).name.startswith("test_"):
            if (ROOT / c).exists():
                targets.add(c)
            continue
        if not c.startswith("src/"):
            continue  # docs, workflows, etc. — no test impact
        toks = _tokens_for(c)
        if c.startswith("src/web_server/"):
            targets.update(str(p.relative_to(ROOT)) for p in (TESTS / "web").glob("test_*.py"))
        hits = {
            str(p.relative_to(ROOT))
            for p in all_tests
            for t in toks
            if _matches(p.stem, t)
        }
        if hits:
            targets.update(hits)
        elif not c.startswith("src/web_server/"):
            unmapped.append(c)

    return sorted(targets), unmapped, False


def venv_python() -> str:
    if sys.prefix != sys.base_prefix:
        # Already inside a virtualenv (activated shell or CI) — use it.
        return sys.executable
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        cand = ROOT / rel
        if cand.exists():
            return str(cand)
    return sys.executable


def run(py: str, label: str, *args: str) -> None:
    print(f"── {label} " + "─" * max(0, 60 - len(label)), flush=True)
    result = subprocess.run([py, *args], cwd=ROOT)
    if result.returncode != 0:
        print(f"GATE FAILED: {label}", file=sys.stderr)
        sys.exit(result.returncode)


# ── mobile-layout gate (scoped) ──────────────────────────────────────────
#
# Static assets → the browser-driven responsive check (tests/web/test_mobile_layout.py).
# Scope narrows to affected panels so a one-panel edit doesn't sweep all 173:
#   * a change under static/js/panels/<x>.js (not help.js) → just the panel(s)
#     whose module is <x>.js;
#   * any CSS, or shared JS (static/js/ outside panels/, or panels/help.js which
#     every help page shares) → all panels, since one rule restyles everything.
# HTML-only changes are skipped here (content, not layout; the one wide-table
# risk, help-overview, is already in the test's KNOWN_OVERFLOW baseline).
# Non-fatal when Playwright/Chromium isn't installed — the test itself skips, and
# a machine without a browser (plain CI) must not be blocked from committing.

STATIC_ROOT = "src/web_server/static/"
_PANEL_MODULE_RE = re.compile(r'id:\s*"([^"]+)".*?module:\s*"\./panels/([^"?]+)"')


def _panel_id_to_module() -> dict[str, str]:
    """Map every panel id → its module basename, parsed from app.js's registry."""
    app_js = ROOT / STATIC_ROOT / "js" / "app.js"
    out: dict[str, str] = {}
    if app_js.exists():
        for m in _PANEL_MODULE_RE.finditer(app_js.read_text(encoding="utf-8")):
            out[m.group(1)] = m.group(2)
    return out


def mobile_scope(changed: list[str]) -> tuple[bool, set[str] | None]:
    """(run?, panel ids or None-for-all) for the changed static assets."""
    static = [c for c in changed if c.startswith(STATIC_ROOT)]
    if not static:
        return False, None
    panels_prefix = STATIC_ROOT + "js/panels/"
    id_to_mod = _panel_id_to_module()
    scoped: set[str] = set()
    for c in static:
        if c.endswith(".css"):
            return True, None  # any CSS rule can restyle every panel
        if c.startswith(panels_prefix) and not c.endswith("/help.js"):
            base = c[len(panels_prefix):]
            hits = {pid for pid, mod in id_to_mod.items() if mod == base}
            if hits:
                scoped |= hits
            else:
                return True, None  # unknown module (helper?) — be safe, sweep all
        elif c.endswith(".js"):
            return True, None  # shared JS (root of js/, or help.js) → all panels
        # .html and everything else: no layout scope
    if not scoped:
        return False, None
    return True, scoped


_BROWSER_PROBE = (
    "import sys; from pathlib import Path; from playwright.sync_api import sync_playwright\n"
    "try:\n"
    "    with sync_playwright() as pw:\n"
    "        sys.exit(0 if Path(pw.chromium.executable_path).exists() else 3)\n"
    "except Exception:\n"
    "    sys.exit(3)\n"
)


def _browser_available(py: str) -> bool:
    """True if Playwright imports *and* a Chromium build is actually installed."""
    return subprocess.run([py, "-c", _BROWSER_PROBE], cwd=ROOT,
                          capture_output=True).returncode == 0


_BROWSER_TESTS = ("test_mobile_layout.py", "test_panel_console.py")


def run_mobile(py: str, changed: list[str]) -> None:
    """Run the scoped browser checks (layout + console) if dashboard assets
    changed and a browser is available; else print why it skipped (non-fatal)."""
    should, panels = mobile_scope(changed)
    if not should:
        return
    label = "all panels" if panels is None else ", ".join(sorted(panels))
    if not _browser_available(py):
        print("── browser: Playwright/Chromium not installed → skipping panel checks " + "─" * 3)
        print("   (install: pip install playwright && python -m playwright install chromium)")
        return
    print(f"── browser: panel checks — layout + console ({label}) " + "─" * 6, flush=True)
    env = dict(os.environ)
    if panels is not None:
        # A handful of panels — check all three widths, it's cheap.
        env["PANEL_SCOPE"] = ",".join(sorted(panels))
    else:
        # A CSS / shared-JS change sweeps every panel; at ~1s each a three-width
        # layout sweep is minutes, too slow for a pre-commit tier. Phone is where
        # nearly every overflow shows; nightly's full sweep covers the rest.
        # (The console sweep ignores viewport, so this only trims the layout one.)
        env.setdefault("PANEL_VIEWPORTS", "phone")
    result = subprocess.run(
        [py, "-m", "pytest", "-m", "browser", "-n", "0",
         *[str(TESTS / "web" / t) for t in _BROWSER_TESTS]],
        cwd=ROOT, env=env,
    )
    if result.returncode != 0:
        print("GATE FAILED: browser panel checks", file=sys.stderr)
        sys.exit(result.returncode)


def main() -> None:
    argv = sys.argv[1:]
    quick = "--quick" in argv
    scoped = "--scoped" in argv
    pytest_args = [a for a in argv if a not in ("--quick", "--scoped")]

    py = venv_python()
    run(py, "ruff", "-m", "ruff", "check", ".")
    run(py, "pyright", "-m", "pyright")

    if quick:
        # Scoped mobile-layout check for any changed dashboard assets. Non-fatal
        # without a browser, so a plain machine still commits.
        run_mobile(py, changed_paths())
        print("GATE OK (quick)")
        return

    if scoped:
        changed = changed_paths()
        targets, unmapped, run_full = select_tests(changed)
        if unmapped:
            print("── scope: unmapped source (covered only by CI/nightly) " + "─" * 6)
            for f in unmapped:
                print(f"   ? {f}")
        # Hard-fail on NEW logic/service files with no mapped test: regression
        # coverage must land in the same commit as the feature. Escape hatch:
        # `git commit --no-verify` (for a genuine false positive — e.g. a new
        # module exercised only through an existing test under another name).
        new = new_paths()
        missing = sorted(
            f for f in unmapped if f in new and f.endswith(REQUIRE_TEST_SUFFIXES)
        )
        if missing:
            print("── scope: NEW logic/service file(s) with no test " + "─" * 12, file=sys.stderr)
            for f in missing:
                print(f"   ✗ {f}", file=sys.stderr)
            print(
                "GATE FAILED: add a mapped test (e.g. tests/test_<feature>_logic.py) "
                "covering the happy path and each guard, or bypass with "
                "`git commit --no-verify` if covered elsewhere.",
                file=sys.stderr,
            )
            sys.exit(1)
        if run_full:
            print("── scope: shared file changed → running FULL suite " + "─" * 10)
            run(py, "pytest", "-m", "pytest", *pytest_args)
        elif targets:
            print(f"── scope: {len(targets)} test file(s) for this diff " + "─" * 10)
            for t in targets:
                print(f"   • {t}")
            run(py, "pytest", "-m", "pytest", *targets, *pytest_args)
        else:
            print("── scope: no code/test changes mapped → skipping pytest " + "─" * 6)
        run_mobile(py, changed)
        print("GATE OK (scoped)")
        return

    run(py, "pytest", "-m", "pytest", *pytest_args)
    print("GATE OK")


if __name__ == "__main__":
    main()
