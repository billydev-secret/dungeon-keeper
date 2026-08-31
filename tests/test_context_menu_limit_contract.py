"""Discord allows five context menus of each type, and the tree is built at boot.

``CommandTree.add_command`` raises ``CommandLimitReached`` on the sixth message
(or sixth user) context menu. That happens inside a cog's ``__init__`` /
``cog_load``, i.e. inside ``setup_hook``, i.e. inside ``bot.start()`` -- so it
does not degrade one feature, it stops the bot booting at all. On 2026-08-31 a
sixth message menu ("Transcribe Voice Note") took production down for ~40
minutes, and the exception was destroyed on the way out (see
``music_cog.cog_unload``) so the journal showed a clean exit 0 with no
traceback.

Nothing else catches this: every test loads cogs one or two at a time, and only
a *whole* tree hits the ceiling. So count them statically instead. This is a
source scan rather than a live tree because building one needs a real
``Bot`` plus every cog's dependencies.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

COGS = Path(__file__).resolve().parents[1] / "src" / "bot_modules" / "cogs"

#: Discord's per-scope ceiling, per ``AppCommandType``. Not ours to raise.
LIMIT = 5

#: Annotation of a context menu callback's target parameter -> menu type.
_TARGET_TYPES = {
    "Message": "message",
    "Member": "user",
    "User": "user",
}


def _const_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level ``_NAME = "literal"`` bindings, for non-inline menu names."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value.value
    return out


def _functions_by_name(tree: ast.Module) -> dict[str, ast.AST]:
    """Every def in the module, nested ones included, keyed by bare name."""
    out: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node
    return out


def _callback_ref(node: ast.AST) -> str | None:
    """``callback=foo`` or ``callback=self._foo`` -> ``"foo"`` / ``"_foo"``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _menu_type(func: ast.AST) -> str | None:
    """A menu's type is fixed by its callback's last parameter annotation.

    ``(self, interaction, message)`` and ``(interaction, message)`` both end on
    the target, so the last argument is the one to read regardless of whether
    the callback is a method or a closure.
    """
    args = getattr(func, "args", None)
    if args is None or not args.args:
        return None
    annotation = args.args[-1].annotation
    if isinstance(annotation, ast.Attribute):  # discord.Message
        return _TARGET_TYPES.get(annotation.attr)
    if isinstance(annotation, ast.Name):  # a bare `Message` import
        return _TARGET_TYPES.get(annotation.id)
    return None


def _collect() -> dict[str, list[str]]:
    """Map menu type -> the menu names registered across every cog."""
    found: dict[str, list[str]] = {"message": [], "user": []}

    for path in sorted(COGS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _const_strings(tree)
        funcs = _functions_by_name(tree)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            attr = getattr(target, "attr", None) or getattr(target, "id", None)
            if attr != "ContextMenu":
                continue

            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}

            name_node = kwargs.get("name")
            if isinstance(name_node, ast.Constant):
                name = str(name_node.value)
            elif isinstance(name_node, ast.Name):
                name = consts.get(name_node.id, name_node.id)
            else:
                name = f"<{path.name}:{node.lineno}>"

            ref = _callback_ref(kwargs.get("callback"))
            func = funcs.get(ref) if ref else None
            assert func is not None, (
                f"{path.name}:{node.lineno}: cannot resolve the callback for "
                f"context menu {name!r}; this contract test needs to see its "
                f"target parameter to know which limit it counts against."
            )
            menu_type = _menu_type(func)
            assert menu_type is not None, (
                f"{path.name}:{node.lineno}: context menu {name!r} has an "
                f"unrecognised target parameter annotation -- annotate it "
                f"discord.Message, discord.Member or discord.User."
            )
            found[menu_type].append(name)

    return found


@pytest.mark.parametrize("menu_type", ["message", "user"])
def test_context_menus_stay_under_the_discord_limit(menu_type):
    names = sorted(_collect()[menu_type])
    assert len(names) <= LIMIT, (
        f"{len(names)} {menu_type} context menus registered, but Discord "
        f"allows {LIMIT} globally -- the extra one raises CommandLimitReached "
        f"inside setup_hook and the bot will not boot at all: "
        + ", ".join(names)
        + ". Retire one, or scope the mod-only menus to a guild (a guild gets "
        "its own budget of five)."
    )


def test_the_collector_actually_sees_the_menus():
    """A scan that silently finds nothing would pass the limit check forever."""
    found = _collect()
    assert found["message"], "no message context menus found — the scan broke"
    assert found["user"], "no user context menus found — the scan broke"
