#!/usr/bin/env python3
"""Canonical pre-commit gate — cross-platform (Linux + Windows).

Usage:
    python scripts/gate.py            # ruff + pyright + FULL pytest
    python scripts/gate.py --scoped   # ruff + pyright + tests for changed files
    python scripts/gate.py --quick    # ruff + pyright only (pre-commit hook)
    python scripts/gate.py -k foo     # extra args forwarded to pytest

Runs everything with the repo venv's interpreter, located automatically,
so it works no matter which python launched this script.

``--scoped`` is the fast per-commit tier: it diffs the working tree against
HEAD (plus untracked files), maps each changed file to the tests that cover
it, and runs only those. The full suite still runs in CI on every push/PR and
nightly (``.github/workflows/nightly.yml``), so anything the heuristic misses
— e.g. a test that imports a changed module indirectly — is caught there.
"""

from __future__ import annotations

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


def main() -> None:
    argv = sys.argv[1:]
    quick = "--quick" in argv
    scoped = "--scoped" in argv
    pytest_args = [a for a in argv if a not in ("--quick", "--scoped")]

    py = venv_python()
    run(py, "ruff", "-m", "ruff", "check", ".")
    run(py, "pyright", "-m", "pyright")

    if quick:
        print("GATE OK (quick)")
        return

    if scoped:
        changed = changed_paths()
        targets, unmapped, run_full = select_tests(changed)
        if unmapped:
            print("── scope: unmapped source (covered only by CI/nightly) " + "─" * 6)
            for f in unmapped:
                print(f"   ? {f}")
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
        print("GATE OK (scoped)")
        return

    run(py, "pytest", "-m", "pytest", *pytest_args)
    print("GATE OK")


if __name__ == "__main__":
    main()
