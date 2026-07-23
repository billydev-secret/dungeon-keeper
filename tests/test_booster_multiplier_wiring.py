"""A boosted credit must name the guild's multiplier, not inherit the default.

``apply_credit`` takes ``multiplier: float = 1.5``. That default exists so the
signature reads sensibly on its own, but it makes an easy trap: a call site that
passes ``booster=booster`` and *forgets* ``multiplier=`` still boosts — silently
at a hardcoded 1.5, ignoring whatever the guild set on the dashboard. Nothing
raises, the payout is plausible, and only a guild that tuned
``econ_booster_multiplier`` away from 1.5 ever sees the divergence.

That is exactly how the `photo_post` faucet shipped (``economy_cog.py``, fixed
2026-07-23): every other faucet threaded the setting through, one did not, and
the bug was invisible because the live guild happened to be on 1.5.

Detection is AST-based rather than textual — a regex over ``apply_credit(...)``
can't tell which keywords belong to *that* call once the arguments span lines or
nest other calls, which most of these do.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("src", "scripts")

# Credit helpers that take both keywords. Keep in sync with economy_service.
CREDIT_CALLS = frozenset({"apply_credit", "transfer_currency"})


def _python_files() -> list[Path]:
    """Every scanned .py file except this one (it contains literal examples)."""
    here = Path(__file__).resolve()
    return [
        path
        for directory in SCANNED_DIRS
        for path in (REPO / directory).rglob("*.py")
        if "__pycache__" not in path.parts and path.resolve() != here
    ]


def _keywords(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg is not None}


def _offenders(tree: ast.AST) -> list[tuple[int, str]]:
    """Calls that pass ``booster=`` but no ``multiplier=``.

    ``booster=False`` is exempt: an explicitly unboosted credit never reads the
    multiplier, and demanding one there would be noise (the quest set bonus is
    deliberately unboosted, for instance).
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name not in CREDIT_CALLS:
            continue
        kwargs = _keywords(node)
        if "booster" not in kwargs or "multiplier" in kwargs:
            continue
        booster = next(kw.value for kw in node.keywords if kw.arg == "booster")
        if isinstance(booster, ast.Constant) and booster.value is False:
            continue  # never boosts, so the multiplier is irrelevant
        hits.append((node.lineno, ast.unparse(node)))
    return hits


def test_no_boosted_credit_inherits_the_default_multiplier():
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.relative_to(REPO)}:{line}: {src.splitlines()[0]}"
            for line, src in _offenders(tree)
        ]
    assert not offenders, (
        "these boosted credits would silently use apply_credit's hardcoded 1.5 "
        "instead of the guild's econ_booster_multiplier — pass "
        "multiplier=settings.booster_multiplier:\n  " + "\n  ".join(offenders)
    )


# ── self-tests: the guard has to actually catch the shape it claims to ──


def test_guard_flags_a_missing_multiplier():
    tree = ast.parse("apply_credit(conn, g, u, 5, 'photo_post', booster=booster)")
    assert _offenders(tree)


def test_guard_accepts_an_explicit_multiplier():
    tree = ast.parse(
        "apply_credit(conn, g, u, 5, 'k', booster=b, multiplier=s.booster_multiplier)"
    )
    assert not _offenders(tree)


def test_guard_ignores_explicitly_unboosted_credits():
    tree = ast.parse("apply_credit(conn, g, u, 5, 'quest_bonus', booster=False)")
    assert not _offenders(tree)


def test_guard_sees_through_multiline_and_nested_arguments():
    # The shape a regex gets wrong: nested call, arguments across lines.
    tree = ast.parse(
        "apply_credit(\n"
        "    conn, guild_id, member.id,\n"
        "    compute(settings.reward_photo_post, {'day': day}),\n"
        "    'photo_post',\n"
        "    booster=booster,\n"
        ")"
    )
    assert _offenders(tree)
