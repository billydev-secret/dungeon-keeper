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


@pytest.mark.parametrize(
    "name",
    ["app.css", "login.html", "index.html", "manual.html", "help-panel.css"],
)
def test_no_third_party_font_host(name):
    """Self-hosted means self-hosted — no CDN request at page load.

    manual.html matters most here and was originally missed: it is hand-edited
    prose, so it is the file most likely to acquire a Google Fonts link from
    someone pasting in a snippet.
    """
    text = (_STATIC / name).read_text(encoding="utf-8")
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit"):
        assert host not in text, f"{name} reaches out to {host}"


def test_every_page_declaring_a_face_can_actually_use_it():
    """A page that ships an @font-face but never references the family has paid
    the download cost for nothing — and, worse, looks different from the same
    content rendered elsewhere. manual.html did exactly this: it declared
    Archivo and then set every heading in the body face, while the Help panel
    rendered the same sections in Archivo."""
    for name in ("login.html", "manual.html"):
        text = (_STATIC / name).read_text(encoding="utf-8")
        families = set(re.findall(r'@font-face\s*\{[^}]*?font-family:\s*"([^"]+)"', text))
        if not families:
            continue
        # Strip @font-face blocks AND the :root token declarations. Naming a
        # family in `--display: "Archivo", ...` is not using it; a font-family
        # declaration on a real selector is. (Checking only for the name would
        # pass on the very file this test was written for.)
        body = re.sub(r"@font-face\s*\{[^}]*\}", "", text)
        used = re.findall(r"font-family:\s*([^;]+);", body)
        used = [u for u in used if not u.strip().startswith('"')]
        tokens_used = {t for u in used for t in re.findall(r"var\(--([a-z-]+)\)", u)}
        token_defs = dict(re.findall(r"--([a-z-]+):\s*(\"[^;]+);", text))
        reachable = {
            fam
            for tok in tokens_used
            for fam in re.findall(r'"([^"]+)"', token_defs.get(tok, ""))
        }
        missing = families - reachable
        assert not missing, (
            f"{name} declares @font-face for {sorted(missing)} but no rule "
            f"reaches it — either use it or drop the download. Naming it in a "
            f":root token does not count."
        )


# ── native control chrome ───────────────────────────────────────────────────


def test_every_page_declares_its_colour_scheme():
    """`color-scheme` is the only lever over browser-painted control chrome.

    Number-input spinners, checkbox and radio glyphs, scrollbars, date pickers
    and the default focus ring are drawn by the browser, not by our CSS — no
    background/color rule reaches them. Without the declaration a dark page
    gets light-mode chrome: the visible symptom was a near-white spinner block
    inside every one of ~125 dark number inputs.

    Each surface needs its own, because they do not share a stylesheet:
    index.html loads app.css, login.html has its own <style>, and manual.html
    is a genuinely light page whose declaration is `light`, not an oversight.
    """
    expected = {
        "app.css": "dark",       # index.html gets it from here
        "login.html": "dark",    # own stylesheet, never loads app.css
        "manual.html": "light",  # long-form docs, deliberately light
    }
    for name, scheme in expected.items():
        text = (_STATIC / name).read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        assert re.search(rf"color-scheme:\s*{scheme}\b", text), (
            f"{name} does not declare color-scheme: {scheme} — its native "
            f"controls will be painted for the wrong theme"
        )


def test_no_retired_ink_mute_survives_in_a_data_uri():
    """--ink-mute was lightened from #80848e to clear WCAG AA at 4.5:1.

    Two select-chevron SVGs had the old value baked inside a url()-encoded
    data URI, so all 117 selects kept the failing grey through the very
    contrast pass that produced the new one. A hex inside an encoded SVG
    inside a CSS url() is invisible to an ordinary search for hard-coded
    colours, which is exactly why it survived.
    """
    for name in ("app.css", "help-panel.css"):
        text = (_STATIC / name).read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        assert "80848e" not in text.lower(), (
            f"{name} still carries the retired --ink-mute (#80848e), which "
            f"measures 3.4-4.4:1 against the surfaces it is used on"
        )
