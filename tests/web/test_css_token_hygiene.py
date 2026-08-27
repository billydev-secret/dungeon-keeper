"""Every `var(--x)` in a dashboard asset must name a token `:root` defines.

A custom property that was never declared is not an error anywhere in the
stack. `color: var(--danger)` with no `--danger` is an *invalid* declaration
that the browser drops, so the element silently inherits; `color:
var(--danger, #e55)` is worse, because it always renders the fallback and so
looks deliberate while ignoring the theme entirely. Neither raises a console
error, so the browser panel-health tier passes them too.

A whole-tree sweep found 64 such uses across 19 names — `--border` (29 uses,
12 files) landing on a hardcoded `#333` that measures 1.00:1 against `--bg`,
`--ink-muted` (a typo for `--ink-mute`) inside the shell that ~10 game panels
render through, and `--danger`, `--warn`, `--ok`, `--surface`, `--fg`,
`--muted`, `--dim`, `--font-mono` and friends shadowing tokens that already
existed under the house names.

Panel JS is where this concentrates, because inline styles written from a
template literal are invisible to a stylesheet linter. So this sweep reads the
JS too, which is the same gap that let the saturated/-text rule in
test_css_contrast_tiers.py hold in CSS and break ~50 times in JS.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"

_DECL = re.compile(r"^\s*(--[a-z0-9-]+)\s*:", re.M)
_USE = re.compile(r"var\(\s*(--[a-z0-9-]+)")

# Tokens a caller sets inline on the element itself, so they are never declared
# in `:root` by design. Keep this list short and say who sets each one.
_LOCAL = {
    "--bucket-count",  # health-heatmap.js sets it per grid, on the grid
}


def _declared() -> set[str]:
    root = re.search(r"^:root\s*\{(.*?)^\}", (_STATIC / "app.css").read_text(encoding="utf-8"), re.S | re.M)
    assert root, "could not find the :root block in app.css"
    return set(_DECL.findall(root.group(1)))


def _assets() -> list[Path]:
    return sorted(
        p
        for p in list(_STATIC.rglob("*.css")) + list(_STATIC.rglob("*.js"))
        if "vendor" not in p.parts and "node_modules" not in p.parts
    )


def test_every_var_reference_names_a_declared_token():
    declared = _declared() | _LOCAL
    offenders = []
    for path in _assets():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for name in _USE.findall(line):
                if name not in declared:
                    rel = path.relative_to(_STATIC)
                    offenders.append(f"{rel}:{i}: var({name}) — not declared in :root")
    assert not offenders, (
        f"{len(offenders)} reference(s) to undefined custom properties. Either "
        "use the token that already carries this meaning, or declare the new "
        "one in :root in app.css:\n" + "\n".join(offenders)
    )


def test_the_sweep_actually_reads_panel_js():
    """Guard the guard: the bug above lived in JS, so JS must be in scope."""
    names = {p.name for p in _assets()}
    assert "app.css" in names
    assert "config-helpers.js" in names
    assert any(p.parts[-2] == "panels" for p in _assets()), "panel JS not swept"
