"""Keyboard-operability sweep for click-only dashboard rows/cards.

Several panels render rows (or cards) whose only affordance was a click
handler: a keyboard-only user could tab past them but never activate them.
The repo pattern (see app.js nav headers, rules-watch.js queue rows) is
`tabindex="0"` + `role="button"` on the element and a delegated `keydown`
listener that mirrors the click on Enter/Space.

These are static source assertions — the panels are vanilla JS with no test
runner on this box, so the regression guard lives here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PANELS = Path("src/web_server/static/js/panels")

# (panel file, substring identifying the interactive row's markup)
ROW_MARKUP = [
    ("qa-tracker.js", 'class="qa-row"'),
    ("role-menus.js", "data-menu-id="),
    ("docs.js", "data-doc-key="),
]


def _source(name: str) -> str:
    return (PANELS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(("panel", "marker"), ROW_MARKUP)
def test_click_rows_are_focusable_buttons(panel: str, marker: str) -> None:
    """The row element carries tabindex + role so it lands in the tab order."""
    src = _source(panel)
    # Grab the element opening tag that contains the marker.
    tags = [t for t in re.findall(r"<\w+[^>]*>", src, re.S) if marker in t]
    assert tags, f"{panel}: no element found containing {marker!r}"
    for tag in tags:
        assert 'tabindex="0"' in tag, f"{panel}: row is not focusable: {tag}"
        assert 'role="button"' in tag, f"{panel}: row has no button role: {tag}"


@pytest.mark.parametrize(
    "panel", ["qa-tracker.js", "role-menus.js", "docs.js", "economy-quests.js"]
)
def test_panels_bind_enter_space_activation(panel: str) -> None:
    """A keydown listener mirrors the click handler for Enter/Space."""
    src = _source(panel)
    assert 'addEventListener("keydown"' in src, f"{panel}: no keydown listener"
    assert re.search(r'e\.key !== "Enter" && e\.key !== " "', src), (
        f"{panel}: keydown handler does not gate on Enter/Space"
    )


def test_quest_idea_cards_are_focusable_buttons() -> None:
    """economy-quests builds its idea cards imperatively, not from markup."""
    src = _source("economy-quests.js")
    assert "card.tabIndex = 0;" in src
    assert 'card.setAttribute("role", "button");' in src


def test_qa_row_exposes_expanded_state() -> None:
    """The QA board row is a disclosure — screen readers need aria-expanded."""
    src = _source("qa-tracker.js")
    assert "aria-expanded=" in src
