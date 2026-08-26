"""The self-hosted webfonts are actually reachable.

This suite exists because of a defect that survived unnoticed for the whole
life of the dashboard: ``--sans`` began with ``"gg sans"``, which is Discord's
proprietary face and ships with no browser, and there was no ``@font-face``
anywhere in the static tree. Every page ever served silently rendered in the
next stack entry instead. A missing font never errors — it just quietly looks
like something else — so nothing caught it.

Now that the faces are self-hosted, the equivalent silent failure is a font
file that stops being served: a moved directory, a dropped subset, a build
that ships the CSS without the ``fonts/`` folder. Same symptom, same silence.
These assertions are cheap and they fail loudly.

The rail's "you are here" signal depends on Archivo specifically being a
*variable* font, so the axis declarations are pinned too — see
``test_nav_current_section.py`` for the behaviour that rides on them, and
``docs/dashboard_visual_language.md`` for why width rather than colour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"
_CSS = _STATIC / "app.css"

_EXPECTED = [
    "fonts/archivo-var-latin.woff2",
    "fonts/archivo-var-latin-ext.woff2",
    "fonts/publicsans-var-latin.woff2",
    "fonts/publicsans-var-latin-ext.woff2",
]


@pytest.mark.parametrize("rel", _EXPECTED)
def test_font_file_is_served(open_client, rel):
    """Every face app.css asks for comes back over HTTP, not just off disk."""
    resp = open_client.get(f"/static/{rel}")
    assert resp.status_code == 200, f"{rel} -> {resp.status_code}"
    # woff2 magic number: a truncated or LFS-pointer file would still 200.
    assert resp.content[:4] == b"wOF2", f"{rel} is not a woff2 payload"


def test_css_references_only_fonts_that_exist():
    """No @font-face src may point at a file that isn't in the repo."""
    css = _CSS.read_text(encoding="utf-8")
    refs = set(re.findall(r'src:\s*url\("([^"]+\.woff2)"\)', css))
    assert refs, "app.css declares no @font-face — the faces have gone missing"
    missing = sorted(r for r in refs if not (_STATIC / r).is_file())
    assert not missing, f"app.css points at absent font files: {missing}"


def _css_without_comments() -> str:
    """app.css explains the gg-sans history in prose, so strip comments first."""
    return re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)


def test_gg_sans_is_gone():
    """The font that never loaded must not creep back into a live declaration."""
    assert "gg sans" not in _css_without_comments().lower()


def test_archivo_is_declared_variable_on_width():
    """The nav rail's signal is Archivo's wdth axis; a static face kills it."""
    css = _CSS.read_text(encoding="utf-8")
    archivo = [
        block
        for block in re.findall(r"@font-face\s*\{[^}]*\}", css)
        if '"Archivo"' in block
    ]
    assert archivo, "no @font-face for Archivo"
    for block in archivo:
        assert "font-stretch: 62% 125%" in block, (
            "Archivo must be declared across its full width axis or the rail's "
            "current-section marker has nothing to interpolate"
        )


def test_no_third_party_font_host():
    """Self-hosted means self-hosted — no CDN request at page load."""
    for path in (_CSS, _STATIC / "login.html", _STATIC / "index.html"):
        text = path.read_text(encoding="utf-8")
        for host in ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit"):
            assert host not in text, f"{path.name} reaches out to {host}"
