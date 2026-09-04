"""Slash command names and descriptions Discord will actually accept.

The command tree is synced at startup. A name Discord rejects fails the whole
sync, not just its own command — so every command in the bot disappears
because one was renamed with a capital letter. Like the context-menu ceiling,
this is only visible across the *whole* tree, so it is a source scan: building
a live tree needs a real Bot plus every cog's dependencies.

Not covered here: duplicate names. A subcommand may legally repeat a name in
another group, and group membership is not reliably visible statically.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Discord's rule for a slash command / group / parameter name.
NAME_RE = re.compile(r"^[-_a-z0-9]{1,32}$")
DESCRIPTION_MAX = 100

#: Each decorator uses its kwargs differently, and conflating them is how a
#: naive scan "finds" a command called "Site you bumped.":
#:   @command(name=..., description=...)  -> one command name, one description
#:   @group(name=..., description=...)    -> same
#:   @describe(param="text")              -> every VALUE is a description
#:   @rename(param="new_name")            -> every VALUE is a name
_NAME_KWARG = {"command", "group"}
_ALL_VALUES_ARE_DESCRIPTIONS = {"describe"}
_ALL_VALUES_ARE_NAMES = {"rename"}
_DECORATORS = _NAME_KWARG | _ALL_VALUES_ARE_DESCRIPTIONS | _ALL_VALUES_ARE_NAMES


def _string_kwarg(call: ast.Call, key: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _string_values(call: ast.Call) -> list[str]:
    return [
        kw.value.value for kw in call.keywords
        if kw.arg and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
    ]


def _collect() -> tuple[list, list]:
    names: list[tuple[Path, int, str]] = []
    descriptions: list[tuple[Path, int, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                fn = dec.func
                attr = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if attr not in _DECORATORS:
                    continue
                if attr in _NAME_KWARG:
                    if (n := _string_kwarg(dec, "name")) is not None:
                        names.append((path, dec.lineno, n))
                    if (d := _string_kwarg(dec, "description")) is not None:
                        descriptions.append((path, dec.lineno, d))
                elif attr in _ALL_VALUES_ARE_NAMES:
                    names += [(path, dec.lineno, v) for v in _string_values(dec)]
                else:
                    descriptions += [(path, dec.lineno, v) for v in _string_values(dec)]
    return names, descriptions


NAMED, DESCRIBED = _collect()


def test_the_scan_actually_finds_commands():
    """A collector that quietly matched nothing would pass every check below."""
    assert len(NAMED) > 20, f"only found {len(NAMED)} named commands — scan broke?"


@pytest.mark.parametrize(
    ("path", "lineno", "name"),
    [pytest.param(p, ln, n, id=f"{p.name}:{ln}:{n}") for p, ln, n in NAMED],
)
def test_command_names_are_shapes_discord_accepts(path, lineno, name):
    assert NAME_RE.match(name), (
        f"{path.name}:{lineno} names a command {name!r}; Discord requires "
        "1-32 chars of lowercase letters, digits, dashes or underscores, and "
        "rejects the whole tree sync over one bad name"
    )


@pytest.mark.parametrize(
    ("path", "lineno", "description"),
    [pytest.param(p, ln, d, id=f"{p.name}:{ln}") for p, ln, d in DESCRIBED],
)
def test_command_descriptions_fit(path, lineno, description):
    assert len(description) <= DESCRIPTION_MAX, (
        f"{path.name}:{lineno} has a {len(description)}-character description; "
        f"Discord's ceiling is {DESCRIPTION_MAX}"
    )


def test_the_name_rule_would_reject_a_real_violation():
    """The rule is only worth having if it fails on the shapes that break sync."""
    for bad in ("Ask", "two words", "", "x" * 33, "emoji🎲"):
        assert not NAME_RE.match(bad), f"{bad!r} should have been rejected"
    assert NAME_RE.match("bank-wallet") and NAME_RE.match("ask")
