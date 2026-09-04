"""Every render site of a member-naming embed builder passes a real resolver.

House rule (CLAUDE.md, ``docs/embed_style_guide.md``): a member named inside an
embed is resolved through ``services/name_resolver.build_name_fn``, never
written as ``<@id>`` — an embed mention is resolved by the *reading* client from
its own cache, so it shows as a bare number to anyone who hasn't seen that
member. Every builder therefore takes a ``name_fn`` that *defaults to*
``mention`` so an un-wired caller still renders, which is exactly why the
wiring needs its own guard: forgetting the resolver at a call site would bring
the bare-number bug back silently. This table walks each cog's AST and fails
on any call to a naming builder that does not pass the resolver.

Features whose guard already lives beside their logic tests (clapback, MLT,
WYR, session recap, /games game-status, Guess Who, casino) are not repeated
here; this file holds the rows that had nowhere else to go when their
builders gained a ``name_fn`` on 2026-09-02. A new naming builder adds a
``pytest.param`` row, not a new test function.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

import bot_modules.cogs.games_compliment_cog as compliment_cog
import bot_modules.cogs.games_mfk_cog as mfk_cog
import bot_modules.cogs.games_ttl_cog as ttl_cog
import bot_modules.games_compliment.embeds as compliment_embeds
import bot_modules.games_mfk.embeds as mfk_embeds


def _calls_to(source: str, names: set[str]):
    """Every ``Call`` node in ``source`` whose callee is one of ``names``,
    whether called bare (``build_x(...)``) or through a module
    (``embeds.build_x(...)``)."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if callee in names:
            yield callee, node


def _source_of(module) -> str:
    return pathlib.Path(inspect.getfile(module)).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("cog_module", "embeds_module", "kwarg"),
    [
        pytest.param(compliment_cog, compliment_embeds, "name_fn", id="compliment"),
        pytest.param(mfk_cog, mfk_embeds, "name_fn", id="mfk"),
    ],
)
def test_every_render_site_passes_a_resolver(cog_module, embeds_module, kwarg):
    needs = {
        name
        for name, fn in inspect.getmembers(embeds_module, inspect.isfunction)
        if kwarg in inspect.signature(fn).parameters
    }
    assert needs, f"{embeds_module.__name__} has no builder taking {kwarg}"
    fname = pathlib.Path(inspect.getfile(cog_module)).name
    missed = [
        f"{fname}:{node.lineno} {callee}()"
        for callee, node in _calls_to(_source_of(cog_module), needs)
        if not any(kw.arg == kwarg for kw in node.keywords)
    ]
    assert not missed, f"render sites with no {kwarg}: " + ", ".join(missed)


def test_ttl_final_results_name_resolver_comes_from_the_recap_resolvers():
    """TTL's recap builder takes two resolvers on purpose — ``name_resolver``
    for the embed text and ``mention_resolver`` for the winner ping in
    ``content=``. The cog must derive both from ``_recap_resolvers`` (which
    wraps a ``build_name_fn`` resolver); a hand-rolled ``m.mention`` resolver
    is what put ``<@id>`` into Final Results before 2026-09-02."""
    source = _source_of(ttl_cog)
    tree = ast.parse(source)
    bound_from_recap: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        func = node.value.func
        if isinstance(func, ast.Name) and func.id == "_recap_resolvers":
            for target in node.targets:
                if isinstance(target, ast.Tuple) and target.elts:
                    first = target.elts[0]
                    if isinstance(first, ast.Name):
                        bound_from_recap.add(first.id)
    assert bound_from_recap, "games_ttl_cog never unpacks _recap_resolvers(...)"

    sites = list(_calls_to(source, {"build_recap_embed"}))
    assert sites, "games_ttl_cog has no build_recap_embed render site"
    missed = []
    for _, node in sites:
        arg = None
        if len(node.args) >= 2:
            arg = node.args[1]
        for kw in node.keywords:
            if kw.arg == "name_resolver":
                arg = kw.value
        if not (isinstance(arg, ast.Name) and arg.id in bound_from_recap):
            missed.append(f"games_ttl_cog.py:{node.lineno}")
    assert not missed, (
        "build_recap_embed sites whose name_resolver is not the first value "
        "of _recap_resolvers(...): " + ", ".join(missed)
    )
