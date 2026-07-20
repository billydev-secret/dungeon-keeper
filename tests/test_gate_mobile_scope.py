"""gate.py's static-asset → mobile-panel scoping (pure logic, no browser).

This is the map that decides which panels the per-commit mobile check visits, so
a wrong answer means either a regression slips through (too narrow) or every
commit pays for all 173 panels (too wide). Each branch gets a case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gate  # noqa: E402

S = "src/web_server/static/"


def test_single_panel_scopes_to_that_panel():
    run, panels = gate.mobile_scope([S + "js/panels/announcements.js"])
    assert run is True
    assert panels == {"announcements"}


def test_two_panels_scope_to_both():
    run, panels = gate.mobile_scope(
        [S + "js/panels/announcements.js", S + "js/panels/role-menus.js"]
    )
    assert run is True
    assert panels == {"announcements", "role-menus"}


def test_css_sweeps_all_panels():
    # A single CSS rule can restyle every panel, so there's no safe narrow scope.
    assert gate.mobile_scope([S + "app.css"]) == (True, None)


def test_shared_js_sweeps_all_panels():
    assert gate.mobile_scope([S + "js/config-helpers.js"]) == (True, None)


def test_help_js_sweeps_all_panels():
    # Every help-* page shares panels/help.js, so it can't be scoped to one id.
    assert gate.mobile_scope([S + "js/panels/help.js"]) == (True, None)


def test_unknown_panel_module_falls_back_to_all():
    # A module we can't map to any nav id (a helper, a renamed file) is treated
    # as shared rather than silently skipped.
    assert gate.mobile_scope([S + "js/panels/does-not-exist.js"]) == (True, None)


def test_html_only_change_is_not_a_layout_scope():
    run, panels = gate.mobile_scope([S + "manual.html"])
    assert run is False


def test_non_static_change_is_ignored():
    assert gate.mobile_scope(["src/bot_modules/whatever.py"]) == (False, None)


def test_a_panel_plus_css_still_sweeps_all():
    # CSS widens the scope even alongside a single-panel edit.
    run, panels = gate.mobile_scope(
        [S + "js/panels/announcements.js", S + "app.css"]
    )
    assert run is True
    assert panels is None


def test_panel_edit_alongside_html_stays_scoped():
    run, panels = gate.mobile_scope(
        [S + "js/panels/qa-tracker.js", S + "manual.html"]
    )
    assert run is True
    assert panels == {"qa-tracker"}


@pytest.mark.parametrize("pid", ["announcements", "qa-tracker", "config-ai", "role-menus"])
def test_known_panels_are_present_in_the_registry_map(pid):
    # If app.js's registry format drifts, the regex would silently map nothing
    # and every edit would sweep all panels — catch that here.
    assert gate._panel_id_to_module().get(pid) == f"{pid}.js"
