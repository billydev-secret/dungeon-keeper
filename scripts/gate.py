#!/usr/bin/env python3
"""Canonical pre-commit gate — cross-platform (Linux + Windows).

Usage:
    python scripts/gate.py            # ruff + pyright + full pytest
    python scripts/gate.py --quick    # ruff + pyright only (pre-commit hook)
    python scripts/gate.py -k foo     # extra args forwarded to pytest

Runs everything with the repo venv's interpreter, located automatically,
so it works no matter which python launched this script.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    pytest_args = [a for a in argv if a != "--quick"]

    py = venv_python()
    run(py, "ruff", "-m", "ruff", "check", ".")
    run(py, "pyright", "-m", "pyright")
    if not quick:
        run(py, "pytest", "-m", "pytest", *pytest_args)
    print("GATE OK" + (" (quick)" if quick else ""))


if __name__ == "__main__":
    main()
