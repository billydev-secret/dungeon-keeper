"""Static analysis — ruff, pyright, eslint, and stylelint as pytest items."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_STATIC = _ROOT / "src" / "web_server" / "static"


def _node_bin(name: str) -> Path | None:
    """Return the path to a node_modules/.bin executable, or None if not installed."""
    stem = name + (".cmd" if sys.platform == "win32" else "")
    path = _ROOT / "node_modules" / ".bin" / stem
    return path if path.exists() else None


def _require_node_tool(name: str) -> Path:
    node_modules = _ROOT / "node_modules"
    if not node_modules.exists():
        pytest.skip("node_modules not found — run: npm install")
    bin_path = _node_bin(name)
    if bin_path is None:
        pytest.skip(f"{name} not found in node_modules — run: npm install")
    return bin_path


def test_ruff() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(_ROOT)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pyright() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "pyright"],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_eslint() -> None:
    eslint = _require_node_tool("eslint")
    js_dir = _STATIC / "js"
    result = subprocess.run(
        [str(eslint), str(js_dir)],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stylelint() -> None:
    stylelint = _require_node_tool("stylelint")
    result = subprocess.run(
        [str(stylelint), str(_STATIC / "**" / "*.css")],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
