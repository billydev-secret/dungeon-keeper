"""The nav rail's drawn icon set stays complete and stays a set.

The ten sections used to be marked with Unicode dingbats — ⌂ ▤ ⚖ ⚙ ¤ ♥ ⚄ ☺ ⚒ ?
— drawn by different people for different purposes across nine Unicode blocks,
rendering at different weights per platform, and falling outside the latin
subsets the dashboard ships so they came out of a system fallback. They are now
drawn on one grid in ``static/js/nav-icons.js``.

Three ways that quietly rots, none of which look broken in a screenshot:

  * a **new section** ships without an icon and silently falls back to a
    dingbat, so nine sections are drawn and one is not;
  * an icon hardcodes a colour instead of ``currentColor``, so it stops
    following the rail's hover / active / gold-when-current states — it will
    look right in every state except the one that matters;
  * an icon drifts off the shared 16x16 grid or stroke weight, which is the
    only thing making ten unrelated shapes read as one family.

The rendering split is pinned too: the icon names a *section*, so it belongs on
the section header. Stamping it on every item drew eight identical shields down
Moderation. Items keep the element for the collapsed rail, where the label is
gone and the icon is a page's only identifier.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[2] / "src" / "web_server" / "static"
_ICONS = _STATIC / "js" / "nav-icons.js"
_APP = _STATIC / "js" / "app.js"


def _shared_attrs() -> str:
    """The attribute string every icon interpolates as ``${A}``.

    viewBox, stroke weight and currentColor live there precisely so all ten
    icons cannot drift apart — which means the assertions below have to expand
    it, or they would be checking the one part of the markup that is empty.
    """
    src = _ICONS.read_text(encoding="utf-8")
    m = re.search(r"const A = ([\s\S]*?);\n", src)
    assert m, "could not find the shared attribute constant in nav-icons.js"
    return "".join(re.findall(r"'([^']*)'", m.group(1)))


def _icon_bodies() -> dict[str, str]:
    src = _ICONS.read_text(encoding="utf-8")
    attrs = _shared_attrs()
    raw = dict(re.findall(r"\n  (\w+): `(<svg [\s\S]*?</svg>)`", src))
    return {k: v.replace("${A}", attrs) for k, v in raw.items()}


def _section_ids() -> list[str]:
    """Top-level nav section ids, read from app.js's SECTIONS array."""
    src = _APP.read_text(encoding="utf-8")
    return re.findall(r'^\s{2,4}id: "([a-z-]+)", label: "', src, re.M)


def test_every_nav_section_has_a_drawn_icon():
    icons = _icon_bodies()
    sections = _section_ids()
    assert sections, "could not read SECTIONS out of app.js — did the shape change?"
    missing = [s for s in sections if s not in icons]
    assert not missing, (
        f"sections with no drawn icon, which fall back to a Unicode dingbat: {missing}. "
        f"Add one to static/js/nav-icons.js."
    )


def test_no_orphan_icons():
    """An icon for a section that no longer exists is dead weight."""
    icons = _icon_bodies()
    sections = set(_section_ids())
    orphans = [k for k in icons if k not in sections]
    assert not orphans, f"icons for sections that no longer exist: {orphans}"


def _raw_bodies() -> dict[str, str]:
    """Icon markup with ``${A}`` left UNEXPANDED.

    The grid assertions have to run against this, not the expanded form. An
    earlier version of this file expanded the shared constant into every icon
    and then asserted that the constant's own substrings were present — which
    is true by construction, so an icon written as ``<svg ${A} viewBox="0 0
    24 24">`` sailed through while the DOM used the later, overriding
    attribute.
    """
    src = _ICONS.read_text(encoding="utf-8")
    return dict(re.findall(r"\n  (\w+): `(<svg [\s\S]*?</svg>)`", src))


def test_the_shared_constant_defines_the_grid():
    """One place sets the grid, weight and colour for all ten."""
    attrs = _shared_attrs()
    assert 'viewBox="0 0 16 16"' in attrs
    assert 'stroke-width="1.5"' in attrs
    assert 'stroke="currentColor"' in attrs


@pytest.mark.parametrize("name", sorted(_raw_bodies()))
def test_icon_does_not_override_the_shared_grid(name):
    """The drift this file exists to catch: an icon opting out of the family."""
    body = _raw_bodies()[name]
    assert body.startswith("<svg ${A}"), f"{name} does not open with the shared attributes"
    after_open = body[body.index(">", body.index("<svg")) :]
    for attr in ("viewBox=", "stroke-width="):
        assert attr not in body[len("<svg ${A}") : body.index(">")], (
            f"{name} redeclares {attr} on its root, overriding the shared grid"
        )
    assert "stroke-width=" not in after_open, (
        f"{name} sets stroke-width on a child element, so it will not match the "
        f"weight of the other nine"
    )


@pytest.mark.parametrize("name", sorted(_raw_bodies()))
def test_icon_follows_currentcolor(name):
    """No literal colours: the rail recolours these on hover, active and current."""
    body = _raw_bodies()[name]
    literals = re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8}|rgb[^"]*|var\([^"]*)"', body)
    assert not literals, (
        f"{name} hardcodes {literals} — it will stop following the rail's "
        f"active/gold state, which is the one state that matters"
    )
    for value in re.findall(r'(?:fill|stroke)="([^"]+)"', body):
        assert value in ("none", "currentColor"), (
            f"{name} paints with {value!r}; only none and currentColor keep it "
            f"following the rail's states"
        )


def test_icons_render_on_the_section_header_not_on_every_item():
    """The CSS split that stops one section's icon repeating down its item list."""
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    item_rule = re.search(r"\.nav-item \.icn svg \{([^}]*)\}", css)
    assert item_rule, ".nav-item .icn svg rule is gone"
    assert "display: none" in item_rule.group(1), (
        "item icons are visible while the rail is expanded — that repeats one "
        "section's icon on every page inside it"
    )
    collapsed = re.search(r"\.collapsed \.nav-item \.icn svg \{([^}]*)\}", css)
    assert collapsed, ".collapsed .nav-item .icn svg rule is gone"
    assert "display: block" in collapsed.group(1), (
        "collapsed rail hides the item icon, leaving pages with no identifier at all"
    )


def test_unmapped_section_degrades_to_its_glyph():
    """sectionIcon returns null rather than blank markup for an unknown id."""
    src = _ICONS.read_text(encoding="utf-8")
    assert "hasOwnProperty.call(ICONS, sectionId) ? ICONS[sectionId] : null" in src, (
        "sectionIcon must return null for an unmapped section so app.js can "
        "fall back to the declared glyph instead of rendering an empty box"
    )
