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

import html as html_lib
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


# ── the page title vs the manual's own heading ──────────────────────────────
#
# The help panel prints the nav label as the page title and then strips the
# manual's own heading from the rendered fragment — but only when the two read
# the same (help.js dropDuplicateHeading normalizes and compares them). A label
# edited without the heading, or the reverse, silently leaves two titles stacked
# on the page. The comparison is a string match at runtime with nothing watching
# it, so it is watched here.


def _heading_text(html: str, anchor: str) -> str | None:
    """The manual's own h2/h3 text for an anchor, as help.js reads it.

    Mirrors dropDuplicateHeading: the section number and the Mod/Admin
    permission chip are chrome the nav label never carries, so both come out
    before the comparison.
    """
    m = re.search(rf'<h[23] id="{re.escape(anchor)}"[^>]*>(.*?)</h[23]>', html, re.S)
    if not m:
        return None
    inner = re.sub(
        r'<span class="(?:section-num|perm)[^"]*">.*?</span>', "", m.group(1), flags=re.S
    )
    return html_lib.unescape(re.sub(r"<[^>]+>", "", inner))


def _normalize_title(text: str) -> str:
    """help.js's normalizeTitle: case- and punctuation-insensitive."""
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _titled_entries() -> list[tuple[str, str, str]]:
    """(anchor, title, source) per help entry, where `title` is what help.js
    compares the manual heading against — `manualHeading` when the entry states
    one, the `label` otherwise."""
    src = HELP_SECTIONS.read_text(encoding="utf-8")
    out = []
    for m in re.finditer(r"\{\s*page:[^{}]*\}", src, re.S):
        entry = m.group(0)
        anchor = re.search(r'anchor:\s*"([\w-]+)"', entry)
        if not anchor:
            continue
        override = re.search(r'manualHeading:\s*"([^"]+)"', entry)
        label = re.search(r'label:\s*"([^"]+)"', entry)
        if override:
            out.append((anchor.group(1), override.group(1), "manualHeading"))
        elif label:
            out.append((anchor.group(1), label.group(1), "label"))
        else:
            # A computed label (the assistant's, which carries a per-guild name)
            # can never match a heading the manual ships for every guild, so it
            # has to state `manualHeading` — flag it rather than skip it.
            out.append((anchor.group(1), "", "computed label, no manualHeading"))
    return out


def test_every_help_page_title_matches_its_manual_heading():
    entries = _titled_entries()
    assert len(entries) > 40, f"only parsed {len(entries)} help entries — parser drifted"
    html = _manual_text()
    bad = []
    for anchor, title, source in entries:
        heading = _heading_text(html, anchor)
        if heading is None:
            continue  # test_help_section_anchors_have_a_home owns this case
        if _normalize_title(heading) != _normalize_title(title):
            bad.append(f"#{anchor}: manual says {heading.strip()!r}, {source} says {title!r}")
    assert not bad, (
        "help page title and manual heading disagree, so the panel will show "
        "both:\n  " + "\n  ".join(bad)
    )


def test_the_assistant_page_states_its_manual_heading():
    """The one entry that must use `manualHeading`, and why.

    The nav and panel title carry the guild's own name for the assistant
    (Config → Branding); the manual is a single file served to every guild and
    so names nobody. Those two can never match, which is what makes the
    override load-bearing rather than stylistic — remove it and every guild
    sees "Ask Sparkles (AI)" above a second "Ask the AI Assistant".
    """
    src = HELP_SECTIONS.read_text(encoding="utf-8")
    entry = re.search(r'\{\s*page:\s*"help-ask"[^{}]*\}', src, re.S)
    assert entry, "the help-ask entry moved — this guard can no longer see it"
    assert "manualHeading:" in entry.group(0), (
        "the help-ask entry lost its manualHeading; its label is the guild's "
        "assistant name, which no shipped manual heading can equal"
    )
    assert "billy" in entry.group(0).lower(), (
        "the old assistant name is gone from the keywords — it is how people "
        "who know it by that name still find the page in the sidebar filter"
    )


def test_the_manual_never_names_the_assistant():
    """The manual ships to every guild, so it must not hardcode one guild's
    name for the assistant. Three paragraphs said "Billy-bot" long after the
    name became per-guild branding (todo #164)."""
    html = _manual_text()
    assert "Billy" not in html, (
        "manual.html names the assistant again — it is renamed per guild under "
        "Config → Branding, so the guide has to describe it without a name"
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
