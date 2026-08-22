"""Lint a Meadow Mahjong card file (spec §3.4) — the CLI face of the gate the
dashboard upload route runs server-side.

Usage:

    python scripts/validate_card.py path/to/card.json [more.json ...]

Prints every structural problem and every semantic finding, not just the
first; exits 1 if any file has errors (warnings alone exit 0). The check
itself lives in ``card_logic.lint_card_data`` so this script, the upload
route, and the tests can never disagree about what a valid card is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from bot_modules.games.mahjong.card_logic import lint_card_data  # noqa: E402


def lint_file(path: Path) -> bool:
    """Lint one card file; print findings; return True when error-free."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        print(f"{path}: cannot read — {e}")
        return False
    except json.JSONDecodeError as e:
        print(f"{path}: invalid JSON — {e}")
        return False

    report = lint_card_data(data)
    for err in report.errors:
        print(f"{path}: ERROR: {err}")
    for warn in report.warnings:
        print(f"{path}: warning: {warn}")
    if report.ok and not report.warnings:
        print(f"{path}: OK")
    elif report.ok:
        print(f"{path}: OK ({len(report.warnings)} warning(s))")
    return report.ok


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    ok = True
    for arg in argv:
        ok = lint_file(Path(arg)) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
