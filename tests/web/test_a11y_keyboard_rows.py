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
    # docs.js is deliberately absent: its document list was a column of
    # click-rows, and the editor-width rework replaced it with a native
    # <select> carrying a real <label for>. A native control is in the tab
    # order and arrow-navigable on its own, so the hand-rolled
    # tabindex/role/Enter-Space pair this test enforces is not just
    # unnecessary there but impossible -- there are no rows left to carry
    # it. Re-add a row here if the list ever goes back to custom markup.
    # The moderation queues. Each renders a list of `.ticket-item` rows whose
    # only affordance was a delegated click handler, and each drives a detail
    # pane entirely from that selection — so without keyboard activation the
    # right-hand half of Tickets, Jails, Warnings and Todo was unreachable.
    ("mod-tickets.js", "data-ticket-id="),
    ("mod-jails.js", "data-jail-id="),
    ("mod-warnings.js", "data-warn-id="),
    ("todo.js", "data-todo-id="),
]

# Panels that get activation from the shared binder in ui.js rather than
# hand-rolling the keydown pair.
SHARED_BINDER = ["mod-tickets.js", "mod-jails.js", "mod-warnings.js", "todo.js"]


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


# docs.js dropped here for the same reason as in ROW_MARKUP above: its rows
# became a native <select>, which needs no hand-rolled Enter/Space binding.
@pytest.mark.parametrize("panel", ["qa-tracker.js", "role-menus.js"])
def test_panels_bind_enter_space_activation(panel: str) -> None:
    """A keydown listener mirrors the click handler for Enter/Space."""
    src = _source(panel)
    assert 'addEventListener("keydown"' in src, f"{panel}: no keydown listener"
    assert re.search(r'e\.key !== "Enter" && e\.key !== " "', src), (
        f"{panel}: keydown handler does not gate on Enter/Space"
    )


@pytest.mark.parametrize("panel", SHARED_BINDER)
def test_queues_activate_rows_through_the_shared_binder(panel: str) -> None:
    """These four had four identical copies of the same click handler, so the
    keyboard half lives in one place rather than being pasted a fifth time."""
    src = _source(panel)
    assert "bindRowActivation(listEl" in src, f"{panel}: not using the shared binder"
    assert 'bindRowActivation } from "../ui.js"' in src or "bindRowActivation," in src, (
        f"{panel}: bindRowActivation is used but never imported"
    )


def test_the_shared_binder_answers_enter_and_space() -> None:
    """Guard the guard: the four panels above delegate their whole keyboard
    story to this one function."""
    src = (PANELS.parent / "ui.js").read_text(encoding="utf-8")
    assert "export function bindRowActivation" in src
    body = src.split("export function bindRowActivation", 1)[1].split("\nexport ", 1)[0]
    assert 'addEventListener("click"' in body
    assert 'addEventListener("keydown"' in body
    assert 'e.key !== "Enter" && e.key !== " "' in body
    assert "e.preventDefault()" in body, "Space would scroll the queue"


@pytest.mark.parametrize("panel", SHARED_BINDER)
def test_queue_selection_is_not_conveyed_by_colour_alone(panel: str) -> None:
    """`.active` painted the selected row and said nothing to a screen reader."""
    assert "aria-current=" in _source(panel), f"{panel}: selection is colour-only"


def test_sortable_table_headers_are_operable() -> None:
    """renderSortableTable backs 14 panels. Its headers were bare <th> with a
    delegated click — sorting was mouse-only, and the current sort was conveyed
    only by a ::after arrow, with `aria-sort` appearing nowhere in the tree."""
    src = (PANELS.parent / "table.js").read_text(encoding="utf-8")
    assert 'aria-sort="${ariaSort}"' in src, "header does not announce sort state"
    assert 'ariaSort = c.key === sortKey' in src, "sort state is not derived per column"
    assert 'tabindex="0"' in src, "header is not focusable"
    assert 'addEventListener("keydown"' in src or "onKeydown" in src
    assert 'e.key !== "Enter" && e.key !== " "' in src


def test_qa_row_exposes_expanded_state() -> None:
    """The QA board row is a disclosure — screen readers need aria-expanded."""
    src = _source("qa-tracker.js")
    assert "aria-expanded=" in src


def test_policy_ticket_rows_are_focusable_without_losing_row_semantics() -> None:
    """Unlike the four div-based queues, this is a real <tr>. Overriding its
    implicit `row` role with role="button" would cost the row/column semantics
    a screen-reader user navigates the table by, so it gets focus and
    Enter/Space and keeps the role it already had."""
    src = _source("mod-policy-tickets.js")
    assert 'class="clickable-row" tabindex="0"' in src, "row is not focusable"
    assert 'role="button"' not in src, "a <tr> must keep its row role"
    assert 'addEventListener("keydown"' in src
    assert 'e.key !== "Enter" && e.key !== " "' in src
