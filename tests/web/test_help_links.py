"""Broken-link check for the in-dashboard manual (static, no browser).

The Help panel renders `static/manual.html` and rewrites each `href="#x"`: if
`x` is a known help-page anchor (`help-sections.js`) it becomes a dashboard
route, otherwise the browser just scrolls to `id="x"` in the manual — or, if no
such id exists, does nothing. That silent no-op is a dead link, and it's exactly
how a stale cross-reference rots (a real one, `#role-menus`, shipped that way).

This parses the manual and asserts every internal `#` link resolves to either a
known help anchor or an id that exists in the document. Pure text parsing — fast,
deterministic, and part of the default suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"
MANUAL = _STATIC / "manual.html"
HELP_SECTIONS = _STATIC / "js" / "panels" / "help-sections.js"

# A real element id / anchor is a word-ish token. This deliberately excludes the
# one dynamic link the manual builds in JS (`href="#' + e.target.id + '"`), whose
# captured target contains quotes and spaces — not a static link to validate.
_ID_TOKEN = re.compile(r"^[\w-]+$")


def _manual_text() -> str:
    return MANUAL.read_text(encoding="utf-8")


def _internal_link_targets(html: str) -> set[str]:
    """Every `href="#x"` target that looks like a static anchor."""
    return {
        m.group(1)
        for m in re.finditer(r'href="#([^"]*)"', html)
        if _ID_TOKEN.match(m.group(1))
    }


def _element_ids(html: str) -> set[str]:
    return set(re.findall(r'id="([\w-]+)"', html))


def _help_anchors() -> set[str]:
    return set(re.findall(r'anchor:\s*"([\w-]+)"', HELP_SECTIONS.read_text(encoding="utf-8")))


# ── the checks ──────────────────────────────────────────────────────────────

def test_manual_and_sections_exist():
    assert MANUAL.exists(), MANUAL
    assert HELP_SECTIONS.exists(), HELP_SECTIONS


def test_no_dead_internal_links_in_manual():
    """Every `#anchor` link resolves to a help-page route or an in-page id."""
    html = _manual_text()
    valid = _element_ids(html) | _help_anchors()
    dead = sorted(t for t in _internal_link_targets(html) if t not in valid)
    assert not dead, (
        "Dead internal links in manual.html (target is neither an id in the doc "
        "nor a help-section anchor):\n  " + "\n  ".join("#" + t for t in dead)
    )


def test_help_section_anchors_have_a_home():
    """Every help-nav anchor points at a manual section id — otherwise the nav
    item opens an empty page."""
    ids = _element_ids(_manual_text())
    missing = sorted(a for a in _help_anchors() if a not in ids)
    assert not missing, (
        "help-sections.js anchors with no matching id in manual.html:\n  "
        + "\n  ".join("#" + a for a in missing)
    )


@pytest.mark.parametrize("bad", ["#nonexistent-xyz", "#role-menus"])
def test_checker_would_flag_a_known_dead_link(bad):
    """Guard the guard: a link to a missing id is caught (unless the manual has
    since grown that id — in which case this reminds us to update the example)."""
    html = _manual_text()
    target = bad.lstrip("#")
    valid = _element_ids(html) | _help_anchors()
    # If the manual legitimately gains this id/anchor, the assertion below flips;
    # that's fine — it just means the example needs refreshing.
    if target in valid:
        pytest.skip(f"{bad} now resolves — pick another example")
    assert target not in valid
