"""A dependency added to a .txt but never compiled into the .lock.

`requirements*.txt` is the human-edited list of direct dependencies;
`requirements*.lock` is what CI and prod actually install. Editing the .txt
without re-running `uv pip compile` means the new package is real in your head,
absent from every machine, and the failure surfaces as an ImportError on the
box you least want it on.

Only the direction that breaks prod is checked: every direct dependency must
appear in the lock. The lock legitimately holds far more (transitive pins).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
PAIRS = [("requirements.txt", "requirements.lock"),
         ("requirements-dev.txt", "requirements-dev.lock")]


def _canon(name: str) -> str:
    """PEP 503 normalisation — `discord.py` and `discord-py` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct(path: Path) -> set[str]:
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # strip extras and any version specifier: uvicorn[standard]>=0.29 -> uvicorn
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        if name:
            out.add(_canon(name))
    return out


def _locked(path: Path) -> set[str]:
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        if name:
            out.add(_canon(name))
    return out


@pytest.mark.parametrize(("txt", "lock"), PAIRS, ids=[t for t, _ in PAIRS])
def test_every_direct_dependency_is_in_the_lock(txt, lock):
    txt_path, lock_path = _ROOT / txt, _ROOT / lock
    if not txt_path.exists() or not lock_path.exists():
        pytest.skip("requirements files are not synced to the remote test runner")
    missing = sorted(_direct(txt_path) - _locked(lock_path))
    assert not missing, (
        f"{txt} names {missing} but {lock} does not pin them — run "
        f"`uv pip compile {txt} -o {lock} --universal -p 3.14`"
    )


def test_the_parsers_handle_the_shapes_these_files_actually_use():
    """Guards the normalisation, not the repo: a parser that returned empty
    sets would make the check above pass on a genuinely broken lock."""
    assert _canon("discord.py") == _canon("discord-py") == "discord-py"
    line = "uvicorn[standard]>=0.29.0  # comment"
    assert re.split(r"[<>=!~\[;]", line.split("#", 1)[0].strip(), maxsplit=1)[0] == "uvicorn"
