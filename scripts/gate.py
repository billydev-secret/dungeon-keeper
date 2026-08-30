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

# Optional: dispatch pytest to a faster machine (see scripts/remote_test.py and
# docs/dev_remote_testing.md). Inert unless REMOTE_TEST_HOST is set, and it
# falls back to local whenever that machine is unreachable — so a missing or
# broken remote can never block a commit.
try:
    import remote_test  # sys.path[0] is scripts/ when run as `python scripts/gate.py`
except ImportError:  # pragma: no cover — only if the file is absent
    remote_test = None  # type: ignore[assignment]

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
)

# src/dungeonkeeper/ (the bootstrap) is deliberately NOT a full-run prefix.
#
# Nearly every feature ship touches __main__.py to register its cog, loop, or
# persistent view — Member Info, Mahjong, Survivor, Music, Advisor and the
# no-contact sweep all did — so keeping it in FULL_RUN_PREFIXES made the full
# suite the *ordinary* price of shipping a feature. Its real failure modes are
# (a) an import/name error, which ruff+pyright catch on every commit, and
# (b) a wiring omission, which the tests that read __main__.py assert
# (see _bootstrap_targets); CI's full run stays the backstop for the rest,
# exactly as the scoping preamble above says.

# Migrations are the full-run trigger only when an *existing* one is edited.
#
# Tests build their schema by applying every migration, so editing one that has
# already run can change tables underneath the whole db-backed suite — that has
# to fan out. A brand-new migration file cannot alter an existing table's shape;
# the worst it does is fail to apply, and every db-backed test collapses at once,
# which the mapped tests catch just as loudly as a full run would.
#
# Measured before splitting these apart: migrations caused 8 of the 13 full-run
# fallbacks in the preceding 40 commits, and all 8 were new files.
FULL_RUN_IF_MODIFIED_PREFIXES = ("src/migrations/",)

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
# Most feature dirs name the layer by the *whole* basename (bios/logic.py,
# inactive/store.py), so a bare-suffix check alone would match none of them.
REQUIRE_TEST_SUFFIXES = ("_logic.py", "_service.py")
REQUIRE_TEST_NAMES = ("logic.py", "store.py", "service.py")


def requires_test(path: str) -> bool:
    """True if a *new* file at this path must ship with a mapped test."""
    name = path.rsplit("/", 1)[-1]
    return name in REQUIRE_TEST_NAMES or name.endswith(REQUIRE_TEST_SUFFIXES)


def _tokens_for(path: str) -> set[str]:
    """Feature tokens a source file maps onto, matched against test filenames."""
    parts = path.split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    toks: set[str] = {stem}
    if path.startswith("src/bot_modules/services/"):
        toks.add(stem[:-8] if stem.endswith("_service") else stem)
    elif path.startswith("src/bot_modules/"):
        # Every directory below bot_modules, so a feature nested under a generic
        # parent (cogs/casino/…) still resolves to 'casino' and not 'cogs'.
        toks.update(parts[2:-1])
    return {t for t in toks if t and t not in GENERIC_TOKENS}


def _test_files() -> list[Path]:
    return [
        p
        for p in TESTS.rglob("test_*.py")
        if "__pycache__" not in p.parts
    ]


def _bootstrap_targets(all_tests: list[Path]) -> set[str]:
    """Tests that read or import the bootstrap — the mapping for src/dungeonkeeper/.

    A wiring test proves its feature is registered by opening __main__.py (or
    importing dungeonkeeper), so a content scan finds every such assertion.
    Both markers are required: "dungeonkeeper" alone also matches logger names
    and the dungeonkeeper.db filename in ~11 unrelated files.
    utf-8 explicitly: the remote runner decodes as cp1252 otherwise, and any
    test file with an em-dash would crash the scan there.
    """
    hits: set[str] = set()
    for p in all_tests:
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "dungeonkeeper" in text and "__main__" in text:
            hits.add(str(p.relative_to(ROOT)))
    return hits


def _matches(test_basename: str, token: str) -> bool:
    # segment match: token must be a whole _-delimited run within the name
    return f"_{token}_" in f"_{test_basename}_"


def forces_full_run(path: str, is_new: bool) -> bool:
    """Whether one changed path invalidates the mapping and needs the whole suite.

    ``is_new`` distinguishes an added file from an edited one — see
    FULL_RUN_IF_MODIFIED_PREFIXES for why a new migration doesn't fan out but
    an edited one does.
    """
    if path in FULL_RUN_FILES or path.startswith(FULL_RUN_PREFIXES):
        return True
    if Path(path).name == "conftest.py":
        return True  # any dir's conftest fans out to its whole subtree
    return path.startswith(FULL_RUN_IF_MODIFIED_PREFIXES) and not is_new


def full_run_triggers(changed: list[str], added: set[str]) -> list[str]:
    """The changed paths that would, on their own, force the whole suite."""
    return [c for c in changed if forces_full_run(c, c in added)]


#: Lightweight tag marking the last commit a full local run went green on.
#: Local only and never pushed — it records what *this* machine has verified,
#: which is the question the nag asks. Moved by a full `gate.py`, read by
#: `dk_session.py teardown`.
FULL_GATE_TAG = "last-full-gate"


def mark_full_gate(cwd: Path | str | None = None) -> str | None:
    """Point FULL_GATE_TAG at HEAD after a full run went green.

    Only from the prod checkout on ``main``: a green branch says nothing about
    the tree the merges land on, and pointing the tag at a branch tip would let
    a work branch clear main's backlog without main ever being run.
    """
    root = Path(cwd) if cwd is not None else ROOT
    try:
        if in_linked_worktree(root):
            return None
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if branch != "main":
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "tag", "-f", FULL_GATE_TAG, head],
            cwd=root, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return None  # a missing tag is a missing nag, never a failed gate
    print(f"── marked {FULL_GATE_TAG} at {head[:8]} " + "─" * 20)
    return head


def ungated_merges(cwd: Path | str | None = None) -> int | None:
    """How many commits main has taken since its last green full run.

    ``None`` when the tag has never been set — "unknown", which reads
    differently from "zero" and should be reported differently.
    """
    root = Path(cwd) if cwd is not None else ROOT
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{FULL_GATE_TAG}^{{commit}}"],
            cwd=root, capture_output=True, text=True, check=True,
        )
        out = subprocess.run(
            ["git", "rev-list", "--count", f"{FULL_GATE_TAG}..main"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return int(out)
    except (subprocess.CalledProcessError, OSError, ValueError):
        return None


def in_linked_worktree(cwd: Path | str | None = None) -> bool:
    """True when *cwd* is a `git worktree`, i.e. a dk-session branch.

    A linked worktree's own git dir sits under the prod checkout's
    `.git/worktrees/<name>`, so `--git-dir` and `--git-common-dir` differ; in
    the prod checkout they are the same path. That is the distinction we want
    — not the branch name, which a session is free to change.

    Defaults to this checkout. The parameter exists so the behaviour can be
    tested from either side: a test that pinned the *ambient* checkout would
    assert one answer in prod and the opposite in a session worktree, and so
    could only ever pass in one of the two places this runs.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute",
             "--git-dir", "--git-common-dir"],
            cwd=cwd if cwd is not None else ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        return False  # not a repo, or git missing — behave like prod
    return len(out) == 2 and Path(out[0]).resolve() != Path(out[1]).resolve()


def select_tests(
    changed: list[str],
    new: set[str] | None = None,
    *,
    defer_full: bool = False,
) -> tuple[list[str], list[str], bool]:
    """Return (test targets, unmapped source files, run_full).

    ``defer_full`` drops the whole-suite fallback and maps the diff normally
    instead. Work branches pass it: a shared-file edit there paid ~10 minutes on
    every commit, which is the ordinary price of touching `core/` or a
    migration, and the run it bought is re-done on main after the merge anyway.
    The prod checkout never defers — a commit landing straight on main has no
    later gate to inherit.
    """
    added = new if new is not None else new_paths()
    if full_run_triggers(changed, added) and not defer_full:
        return [], [], True

    all_tests = _test_files()
    targets: set[str] = set()
    bootstrap_hits: set[str] | None = None  # computed once, on first bootstrap path
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
        if c.startswith("src/dungeonkeeper/"):
            # '__main__' tokens map to nothing; the content scan is the mapping —
            # and an empty scan must still land in unmapped, not vanish silently.
            if bootstrap_hits is None:
                bootstrap_hits = _bootstrap_targets(all_tests)
            if bootstrap_hits:
                targets.update(bootstrap_hits)
            else:
                unmapped.append(c)
            continue
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


# Everything under tests/web marked ``browser``, selected by marker rather than
# by name. The two panel *sweeps* (test_mobile_layout, test_panel_console) used
# to be listed here explicitly, which quietly meant the other five browser
# files — the targeted panel regressions — ran in no tier at all: the default
# suite excludes the marker, and nightly enumerated the same two names. A stale
# fixture in test_panel_js_fixes sat red on main for days because of it. Select
# the marker, and a new browser file is covered the moment it is written.
#
# Cost is proportionate: the five regression files are ~69 tests in ~60s, while
# the sweeps they join are minutes. PANEL_SCOPE/PANEL_VIEWPORTS below only
# affect the sweeps; the regression files ignore them.
_BROWSER_TESTS = (TESTS / "web",)


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
         *[str(t) for t in _BROWSER_TESTS]],
        cwd=ROOT, env=env,
    )
    if result.returncode != 0:
        print("GATE FAILED: browser panel checks", file=sys.stderr)
        sys.exit(result.returncode)


def run_pytest(py: str, *args: str) -> None:
    """Run pytest remotely when configured and reachable, else locally.

    ``remote_test.run`` returns None for every "can't or shouldn't dispatch"
    case, which is exactly the signal to fall through to the local path.
    """
    print("── pytest " + "─" * 54, flush=True)

    code = remote_test.run(args) if remote_test is not None else None
    if code is None:
        code = subprocess.run([py, "-m", "pytest", *args], cwd=ROOT).returncode

    if code != 0:
        print("GATE FAILED: pytest", file=sys.stderr)
        sys.exit(code)


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
        added = new_paths()
        triggers = full_run_triggers(changed, added)
        defer_full = bool(triggers) and in_linked_worktree()
        targets, unmapped, run_full = select_tests(
            changed, added, defer_full=defer_full
        )
        if unmapped:
            print("── scope: unmapped source (covered only by CI/nightly) " + "─" * 6)
            for f in unmapped:
                print(f"   ? {f}")
        # Hard-fail on NEW logic/service files with no mapped test: regression
        # coverage must land in the same commit as the feature. Escape hatch:
        # `git commit --no-verify` (for a genuine false positive — e.g. a new
        # module exercised only through an existing test under another name).
        missing = sorted(
            f for f in unmapped if f in added and requires_test(f)
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
        if defer_full:
            print("── scope: shared file changed → full suite DEFERRED to main " + "─" * 1)
            for t in triggers:
                print(f"   ~ {t}")
            print("   run `python scripts/gate.py` on main once your merge lands")
        if run_full:
            print("── scope: shared file changed → running FULL suite " + "─" * 10)
            run_pytest(py, *pytest_args)
        elif targets:
            print(f"── scope: {len(targets)} test file(s) for this diff " + "─" * 10)
            for t in targets:
                print(f"   • {t}")
            run_pytest(py, *targets, *pytest_args)
        else:
            print("── scope: no code/test changes mapped → skipping pytest " + "─" * 6)
        run_mobile(py, changed)
        print("GATE OK (scoped)")
        return

    run_pytest(py, *pytest_args)
    mark_full_gate()
    print("GATE OK")


if __name__ == "__main__":
    main()
