"""The guide has two renderings and they must keep sharing one stylesheet.

`/static/manual.html` is served two ways: standalone, and parsed by
``help.js`` into the dashboard's Help panel. It used to carry a 428-line
inline ``<style>`` block that redefined ~45 selectors help-panel.css already
owned — headings, ``.cmd-block``, ``.perm-*``, ``.callout-*``, tables, lists,
``.steps``, ``.qr-table`` — in its own light palette built from 31 hardcoded
hexes and a private token set (``--brand``, ``--muted``, ``--surface``…).

Two renderings, two definitions, and nothing holding them together: the panel
had picked up ``.matrix`` styling that the standalone page had and the panel
did not, and the two had drifted apart on every heading level.

It drifted *because it was inline*. An inline block is invisible to stylelint
(which globs ``static/**/*.css``) and to the token-hygiene and contrast sweeps
in this directory, which read ``.css`` and ``.js`` — so every guard this
dashboard has for exactly this failure mode skipped the one file most likely
to hit it. The fix is structural, and these tests hold the structure:

* content shapes live in help-panel.css, shared by both renderings;
* the standalone page adds only chrome it alone has — frame, sidebar TOC,
  cover, print button, print styles;
* nothing is styled inline, so the existing sweeps reach all of it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"
_MANUAL = _STATIC / "manual.html"
_STANDALONE = _STATIC / "manual-standalone.css"
_SHARED = _STATIC / "help-panel.css"


def _strip_comments_css(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def _strip_comments_html(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def _screen_css() -> str:
    """manual-standalone.css with comments and the print block removed.

    @media print re-declares the tokens light and lets the whole document
    follow, which is deliberately a wholesale override rather than a per-shape
    one. It also cannot leak: this sheet is loaded by manual.html alone, so
    nothing in it reaches the dashboard's Help panel in any medium."""
    css = _strip_comments_css(_STANDALONE.read_text(encoding="utf-8"))
    return re.sub(r"@media print\s*\{.*\}\s*$", "", css, flags=re.S)


def test_the_guide_styles_nothing_inline() -> None:
    """The root cause. An inline block is outside every CSS guard we have."""
    html = _strip_comments_html(_MANUAL.read_text(encoding="utf-8"))
    assert "<style" not in html.lower(), (
        "manual.html has an inline <style> block again. stylelint and the "
        "token-hygiene and contrast sweeps only read .css and .js files, so "
        "anything in there is unguarded — put it in manual-standalone.css "
        "(page chrome) or help-panel.css (a content shape)."
    )


@pytest.mark.parametrize(
    "sheet", ["tokens.css", "help-panel.css", "manual-standalone.css"]
)
def test_the_guide_links_the_shared_sheets(sheet: str) -> None:
    html = _MANUAL.read_text(encoding="utf-8")
    assert re.search(rf'<link[^>]+href="/static/{re.escape(sheet)}', html), (
        f"manual.html no longer loads {sheet} — the standalone rendering and "
        f"the Help panel stop matching the moment they stop sharing a sheet"
    )


def test_the_content_wrapper_opts_into_the_shared_sheet() -> None:
    """Every rule in help-panel.css is scoped under `.dk-help`. Without the
    class on the wrapper the standalone page silently loses all of them and
    renders as unstyled prose."""
    html = _MANUAL.read_text(encoding="utf-8")
    m = re.search(r"<main[^>]*class=\"([^\"]+)\"", html)
    assert m, "manual.html has no <main> wrapper"
    classes = set(m.group(1).split())
    assert "dk-help" in classes, (
        "<main> dropped the dk-help class, so none of help-panel.css applies "
        "to the standalone page"
    )
    assert "content" in classes, "<main> dropped the content class"


def _content_shape_classes() -> set[str]:
    """Class names help-panel.css styles — the shapes it owns."""
    css = _strip_comments_css(_SHARED.read_text(encoding="utf-8"))
    selectors = re.findall(r"^([^{@}]+)\{", css, re.M)
    found = set()
    for sel in selectors:
        found.update(re.findall(r"\.([a-z][a-z0-9-]*)", sel))
    # .dk-help is the scope itself, not a shape; the search widgets live in
    # the panel header and have no standalone counterpart.
    return {c for c in found if c != "dk-help" and not c.startswith("dk-help-")}


# Divergences the standalone page is allowed, each for a stated structural
# reason. Anything not on this list belongs in the shared sheet.
_ALLOWED = {
    # help-panel.css hides the print bar: printing is a standalone concern
    # and the panel has no print view. The standalone page shows it back.
    "print-bar",
    # The panel shows one section under a header that already prints its
    # title, so it hides the h2, hides the number, and demotes h3 to an
    # eyebrow. The standalone page is 32 sections in one scroll with no
    # header, so it restores document-scale headings and the section
    # numbers its sidebar TOC refers to.
    "section-num",
}


def test_the_standalone_sheet_does_not_restyle_a_shared_shape() -> None:
    """Chrome here, content shapes there. Redefining a shape in this file is
    how the two renderings drifted apart the first time."""
    css = _screen_css()
    shared = _content_shape_classes()
    offenders = []
    for sel in re.findall(r"^([^{@}]+)\{", css, re.M):
        for cls in re.findall(r"\.([a-z][a-z0-9-]*)", sel):
            if cls in shared and cls not in _ALLOWED:
                offenders.append(f"{sel.strip()}  (.{cls} is owned by help-panel.css)")
    assert not offenders, (
        "manual-standalone.css restyles content shapes that help-panel.css "
        "owns, so the standalone guide and the Help panel will render them "
        "differently:\n  " + "\n  ".join(offenders)
    )


def test_the_heading_overrides_cannot_leak_into_the_panel() -> None:
    """The one deliberate divergence. It must stay scoped to `.content`, which
    only the standalone page has — an unscoped `.dk-help h3` here would undo
    the panel's own heading treatment."""
    css = _screen_css()
    for sel in re.findall(r"^([^{@}]+)\{", css, re.M):
        if ".dk-help" in sel:
            assert ".content" in sel, (
                f"{sel.strip()} targets .dk-help without scoping to .content, "
                f"so it also applies inside the dashboard's Help panel"
            )


def test_the_standalone_sheet_invents_no_colours() -> None:
    """31 hardcoded hexes in the old inline block were how the guide ended up
    with a palette of its own. Outside the print block, which re-declares the
    tokens deliberately, every colour comes from tokens.css."""
    css = _screen_css()
    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", css)
    assert not hexes, (
        f"manual-standalone.css hardcodes {sorted(set(hexes))} outside the "
        f"print block — use a token from tokens.css"
    )
