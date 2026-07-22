"""Text file I/O must name its encoding explicitly.

Python uses the *locale* encoding when `encoding=` is omitted. On the Fedora
prod box that is UTF-8, so omissions are invisible; on Windows it is cp1252,
which cannot decode the em-dashes and box-drawing characters this repo is full
of. Two tests failed exactly this way the first time the suite ran on the
Windows test host (`UnicodeDecodeError: 'charmap' codec can't decode byte
0x9d`), and the same latent bug sat in five other call sites that happened not
to hit a non-ASCII byte.

Since the suite now also runs on Windows (see docs/dev_remote_testing.md), this
guard keeps the tree portable rather than waiting for the next silent
locale-dependent read.

Detection is AST-based, not textual. Two earlier regex attempts failed in ways
worth remembering: ``\\([^)]*\\)`` stops at the first ``)`` and so mis-flags
``write_text(json.dumps(x), encoding="utf-8")``; and any text scan matches
English prose inside docstrings, e.g. "…is open (LAN-only)". Parsing sees only
real calls.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("src", "tests", "scripts")

# A mode string containing 'b' — bytes have no encoding, so those are exempt.
_BINARY_MODE = re.compile(r"[rwax+]*b[rwax+]*")


def _python_files() -> list[Path]:
    """Every scanned .py file except this one.

    This module necessarily contains literal examples of the bad patterns (see
    the self-tests below), so scanning itself would always fail.
    """
    here = Path(__file__).resolve()
    return [
        path
        for directory in SCANNED_DIRS
        for path in (REPO / directory).rglob("*.py")
        if "__pycache__" not in path.parts and path.resolve() != here
    ]


def _has_binary_mode(node: ast.Call) -> bool:
    return any(
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and _BINARY_MODE.fullmatch(arg.value)
        for arg in node.args
    )


def _walk(tree: ast.AST, call: str, *, builtin: bool = False) -> list[tuple[int, str]]:
    """Locate `call(...)` invocations that pass no `encoding=` keyword.

    Returns (line number, unparsed call). Binary-mode calls are exempt.

    ``builtin=True`` matches only an unqualified name, so scanning for ``open``
    finds the builtin but not ``Image.open(io.BytesIO(...))`` — PIL takes a byte
    stream and has nothing to do with text encoding. The default matches
    attribute calls such as ``p.read_text()``.
    """
    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if builtin:
            if not (isinstance(func, ast.Name) and func.id == call):
                continue
        elif not (isinstance(func, ast.Attribute) and func.attr == call):
            continue

        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        if _has_binary_mode(node):
            continue

        hits.append((node.lineno, ast.unparse(node)))

    return hits


def find_calls_missing_encoding(
    source: str, call: str, *, builtin: bool = False
) -> list[tuple[int, str]]:
    """String front-end to :func:`_walk`, used by the self-tests below."""
    return _walk(ast.parse(source), call, builtin=builtin)


def test_no_python_file_has_a_utf8_bom():
    """A BOM survives import (Python sniffs utf-8-sig) but breaks plain readers.

    Three tracked files carried one until this guard was added; ast.parse()
    rejects the decoded text outright with 'invalid non-printable character
    U+FEFF'.
    """
    offenders = [
        str(path.relative_to(REPO))
        for path in _python_files()
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    assert not offenders, (
        "Python sources must not start with a UTF-8 BOM:\n  " + "\n  ".join(offenders)
    )


# Checks to run, as (label, builtin). Parsed in one pass — walking the tree
# once per check made this the slowest test in the scoped tier.
_CHECKS: tuple[tuple[str, bool], ...] = (
    ("read_text", False),
    ("write_text", False),
    ("open", True),
)


@cache
def _scan() -> dict[str, list[str]]:
    """Offenders per check, from a single parse of each file."""
    found: dict[str, list[str]] = {call: [] for call, _ in _CHECKS}

    for path in _python_files():
        # utf-8-sig so a stray BOM degrades to a clear failure in the BOM test
        # above rather than a confusing SyntaxError here.
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        rel = path.relative_to(REPO)

        for call, builtin in _CHECKS:
            for line_number, snippet in _walk(tree, call, builtin=builtin):
                found[call].append(f"{rel}:{line_number}: {snippet}")

    return found


def _offenders(call: str) -> list[str]:
    return _scan()[call]


def test_no_read_text_without_encoding():
    offenders = _offenders("read_text")
    assert not offenders, (
        "read_text() without encoding= is locale-dependent and fails on Windows "
        '(cp1252). Use read_text(encoding="utf-8"):\n  ' + "\n  ".join(offenders)
    )


def test_no_write_text_without_encoding():
    offenders = _offenders("write_text")
    assert not offenders, (
        "write_text() without encoding= is locale-dependent. "
        'Use write_text(..., encoding="utf-8"):\n  ' + "\n  ".join(offenders)
    )


def test_no_text_mode_open_without_encoding():
    offenders = _offenders("open")
    assert not offenders, (
        "open() in text mode without encoding= is locale-dependent. Pass "
        'encoding="utf-8", or use a binary mode if the payload is bytes:\n  '
        + "\n  ".join(offenders)
    )


# ── guard the guard ────────────────────────────────────────────────────────────
# A detector that silently matched nothing would let every offender through.


def test_detects_a_bare_call():
    assert find_calls_missing_encoding("x = p.read_text()", "read_text")


def test_accepts_an_explicit_encoding():
    assert not find_calls_missing_encoding(
        'x = p.read_text(encoding="utf-8")', "read_text"
    )


def test_nested_call_before_encoding_is_not_a_false_positive():
    """Broke the first regex attempt: the inner ')' ended the match early."""
    assert not find_calls_missing_encoding(
        'p.write_text(json.dumps(data), encoding="utf-8")', "write_text"
    )


def test_nested_call_without_encoding_is_still_caught():
    assert find_calls_missing_encoding("p.write_text(json.dumps(data))", "write_text")


def test_prose_in_a_docstring_is_not_a_call():
    """Broke the second regex attempt: 'is open (LAN-only)' looked like open()."""
    source = '"""The dashboard is open (LAN-only) by default."""\nx = 1\n'
    assert not find_calls_missing_encoding(source, "open", builtin=True)


def test_qualified_open_is_not_the_builtin():
    """PIL's Image.open takes bytes — flagging it would be pure noise."""
    assert not find_calls_missing_encoding(
        "Image.open(io.BytesIO(data))", "open", builtin=True
    )


def test_binary_mode_is_exempt():
    assert not find_calls_missing_encoding('open(path, "rb")', "open", builtin=True)


def test_text_mode_is_not_exempt():
    assert find_calls_missing_encoding('open(path, "r")', "open", builtin=True)
    assert find_calls_missing_encoding("open(path)", "open", builtin=True)


def test_comments_are_ignored():
    assert not find_calls_missing_encoding("# p.read_text()", "read_text")


def test_multiline_call_is_handled():
    assert not find_calls_missing_encoding(
        'p.write_text(\n    json.dumps(data),\n    encoding="utf-8",\n)', "write_text"
    )


def test_reports_the_right_line_number():
    source = "a = 1\nb = 2\nx = p.read_text()\n"
    assert find_calls_missing_encoding(source, "read_text")[0][0] == 3
