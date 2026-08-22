"""Every named import in the dashboard's ES modules must resolve to a real export.

The browser is unforgiving here: one bad name in an ``import { … }`` list kills
the *whole module*, so the panel never mounts and the user sees
``Failed to load <Panel>: Importing binding name 'apiGet' is not found.`` —
which is exactly what shipped in the Meadow Mahjong panel, whose only sin was
importing ``apiGet`` from ``api.js`` when the export has always been ``api``.

Nothing in the always-on gate caught it. ``.eslintrc.json`` extends
``eslint:recommended``, which has no import-resolution rule (that needs
``eslint-plugin-import``), and the only other check that would have noticed —
the panel-load health suite — is marked ``browser`` and auto-skips wherever
Playwright/Chromium is absent. So this is a plain source-reading test with no
marker: it runs everywhere, on every one of the ~190 modules, in well under a
second.

The dashboard's module dialect is small and uniform, which is what makes a
regex parse honest here rather than a half-built bundler: imports are all
``import { a, b } from "./x.js"``, and exports are ``export function`` /
``export async function`` / ``export const`` / ``export { … }``, the last of
which may re-export from another module (``config-helpers.js`` forwards four
names straight out of ``api.js``) and so is followed transitively.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_JS_ROOT = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static" / "js"

# `import { a, b } from "./x.js"` — the brace body may span lines.
_IMPORT_RE = re.compile(
    r"^import\s*\{(?P<names>[^}]*)\}\s*from\s*['\"](?P<from>[^'\"]+)['\"]",
    re.MULTILINE | re.DOTALL,
)
# `export { a, b as c }` with an optional `from "./x.js"` tail.
_EXPORT_BRACE_RE = re.compile(
    r"^export\s*\{(?P<names>[^}]*)\}\s*(?:from\s*['\"](?P<from>[^'\"]+)['\"])?",
    re.MULTILINE | re.DOTALL,
)
# `export function f`, `export async function f`, `export const X`, …
_EXPORT_DECL_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+(?P<name>[A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _js_files() -> list[Path]:
    return sorted(
        p for p in _JS_ROOT.rglob("*.js") if "vendor" not in p.relative_to(_JS_ROOT).parts
    )


def _clause_names(body: str) -> list[tuple[str, str]]:
    """Split a `{ … }` clause into (local, exported-or-imported) name pairs.

    For `esc as escapeHtml` the *local* name is what the source module must
    provide and `escapeHtml` is what it publishes, so both are returned.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if " as " in chunk:
            local, _, public = chunk.partition(" as ")
            pairs.append((local.strip(), public.strip()))
        else:
            pairs.append((chunk, chunk))
    return pairs


def _resolve(source: Path, specifier: str) -> Path | None:
    """Map a relative specifier to a file; bare specifiers aren't ours to check.

    A few imports carry a hand-written cache-bust suffix (``help-sections.js?v=25``)
    on top of the automatic per-boot rewrite, so the query/fragment is trimmed.
    """
    if not specifier.startswith("."):
        return None
    specifier = specifier.split("?", 1)[0].split("#", 1)[0]
    return (source.parent / specifier).resolve()


def _exports(path: Path, _seen: frozenset[Path] = frozenset()) -> set[str]:
    """Every name `path` publishes, following `export … from` re-exports."""
    if path in _seen or not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8")
    names = {m.group("name") for m in _EXPORT_DECL_RE.finditer(text)}
    for m in _EXPORT_BRACE_RE.finditer(text):
        for local, public in _clause_names(m.group("names")):
            names.add(public)
            if (origin := m.group("from")) is not None:
                # A forwarded name must exist upstream, too.
                target = _resolve(path, origin)
                if target is not None and local not in _exports(target, _seen | {path}):
                    names.discard(public)
    return names


@pytest.mark.parametrize("path", _js_files(), ids=lambda p: str(p.relative_to(_JS_ROOT)))
def test_every_named_import_resolves(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for m in _IMPORT_RE.finditer(text):
        target = _resolve(path, m.group("from"))
        if target is None:
            continue
        rel = path.relative_to(_JS_ROOT)
        assert target.is_file(), f"{rel} imports from missing module {m.group('from')}"
        available = _exports(target)
        for local, _public in _clause_names(m.group("names")):
            assert local in available, (
                f"{rel} imports '{local}' from {m.group('from')}, which does not "
                f"export it — the browser refuses to load the whole module. "
                f"Available: {', '.join(sorted(available))}"
            )


def test_the_parser_actually_finds_exports() -> None:
    """Guard the guard: a regex that silently matched nothing would pass forever."""
    api = _JS_ROOT / "api.js"
    assert {"api", "apiPost", "apiPut", "apiDelete", "esc"} <= _exports(api)
    # config-helpers re-exports from api.js, including an aliased name.
    helpers = _exports(_JS_ROOT / "config-helpers.js")
    assert {"apiPost", "escapeHtml", "mountAsync"} <= helpers
    assert "apiGet" not in _exports(api)
