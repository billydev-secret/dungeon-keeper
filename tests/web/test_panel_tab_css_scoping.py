"""Regression test: a tabs.js pane must be as transparent to app.css's
panel-level layout rules as `.panel` itself.

THE BUG (already fixed — this pins it so it cannot return). Four app.css
rules that shape a panel's top-level content were written as *direct child
of `.panel`* selectors:

    .panel > .form                    { max-width: none; }        (lifts the 640px cap)
    .panel > .form.form-cards         { background:none; border:none; padding:0; }
    .panel > section, .panel > .form  { background/border/padding box }
    .panel > :is(section,.form,.card,.home-grid,.card-grid) + :is(...) { margin-top }

tabs.js (static/js/tabs.js) renders each tab's content into a
`<div data-pane="KEY">` that lives *inside* `.panel`
(`.panel > [data-tabs] > [data-pane]`), so a `.form` that used to sit
directly in `.panel` and got moved into a tab pane is no longer a direct
child of `.panel` — none of the four rules above match it any more. It falls
back to the bare `.form` rule's 640px cap and loses its box styling, with
**no console error and no viewport overflow** — invisible to both existing
browser sweeps (test_mobile_layout.py's width/clip/collapse signals,
test_panel_console.py's error signal). This really happened to config-bios
and music-playlist in commit 8becdd37.

THE FIX: those four selectors now read `:is(.panel, [data-pane]) > ...` so a
tab pane is as transparent to them as `.panel` itself.

Marked ``browser``; auto-skips without Playwright/Chromium, matching every
other file in tests/web/. Harness copied from test_tabs_widget.py (the
server/browser fixture shape, `_goto_panel`/`serve` from
scripts/mobile_layout_scan.py) rather than reinvented.
"""

from __future__ import annotations

import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed (pip install playwright && playwright install chromium)",
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from mobile_layout_scan import _goto_panel, serve  # noqa: E402

from tests.db_template import migrated_db  # noqa: E402


def _chromium_available() -> bool:
    try:
        with playwright_sync.sync_playwright() as pw:
            path = pw.chromium.executable_path
            return bool(path) and Path(path).exists()
    except Exception:
        return False


if not _chromium_available():
    pytest.skip(
        "Chromium not installed — run `python -m playwright install chromium`",
        allow_module_level=True,
    )


# ── served dashboard + browser (module-scoped) ──────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "tab-css-scoping.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("tab-css-scoping"))
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", srv.port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def browser(dashboard) -> Iterator[object]:
    with playwright_sync.sync_playwright() as pw:
        b = pw.chromium.launch()
        yield b
        b.close()


# ── the scenario ──────────────────────────────────────────────────────────

def _pane_form_style(page, selector: str) -> dict:
    """Computed max-width / rendered width / background / border of one
    element, read straight from the live page — never inferred from source."""
    result = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return {
                maxWidth: cs.maxWidth,
                width: r.width,
                background: cs.backgroundColor,
                borderTopWidth: cs.borderTopWidth,
            };
        }""",
        selector,
    )
    assert result is not None, f"no element matched {selector!r}"
    return result


# Both tabs asserted here are each panel's *initial* tab (index 0 in its
# mountTabs() call), so it renders on load — no click required, matching how
# config-bios's own settings tab is exercised in test_tabs_widget.py.
PANEL_CASES = [
    pytest.param(
        "config-bios", '[data-pane="config"] [data-form]',
        id="config-bios-settings-form",
    ),
    pytest.param(
        "music-playlist", '[data-pane="settings"] form.form-cards',
        id="music-playlist-settings-form",
    ),
]


@pytest.mark.parametrize("panel_id, form_selector", PANEL_CASES)
def test_tab_pane_form_is_full_width_and_boxless(dashboard, browser, panel_id, form_selector):
    """A `.form.form-cards` rendered inside a tabs.js pane must match a form
    sitting directly in `.panel`: no 640px cap, no outer box.

    The two panels prove the fix at different depths, and it's worth being
    explicit about that rather than pretending they're interchangeable:

    * config-bios's Settings form is a *direct child* of its `[data-pane]`
      (`.panel > [data-tabs] > [data-pane] > form.form-cards`), so its
      rendered width genuinely depends on the CSS rule under test — revert
      app.css's four `:is(.panel, [data-pane])` selectors back to plain
      `.panel >` and this param's width assertion fails right here (see this
      repo's manual bite-check output for the verbatim failure).

    * music-playlist's own Settings form sits one level deeper, inside a
      local wrapper `<div>` music-playlist.js builds for its own spacing
      (a pre-existing, already-shipped workaround for the same class of
      bug — see that file's comments), and that same code gives the form
      itself an inline `style.maxWidth = "none"`. Both make this param's
      width assertion true regardless of app.css, so it can't independently
      catch *this* regression reappearing — it still earns its place: it
      pins the end state music-playlist's own workaround depends on staying
      true, and the box-drop half is a real, narrower guard (a
      `.form.form-cards` is boxless whether or not the panel-scoping rule
      even matches it — the rule sets a box only to strip it straight back
      off for `.form-cards` — so a future regression that reverts only the
      `.form.form-cards` selector while leaving the general `.form` rule
      fixed *would* be caught here, on either panel).
    """
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        _goto_panel(page, f"{dashboard.base}/#/{panel_id}")
        page.wait_for_selector(form_selector, timeout=5000)

        style = _pane_form_style(page, form_selector)

        # ── not capped at 640px — same width treatment as a panel-level form ──
        assert style["maxWidth"] == "none", (
            f"{panel_id}: a .form inside a tab pane computed max-width: "
            f"{style['maxWidth']!r} instead of 'none' — it fell back to the "
            f"bare .form rule's 640px cap instead of getting the same "
            f"no-cap treatment app.css gives a .form sitting directly in "
            f".panel"
        )
        assert style["width"] > 700, (
            f"{panel_id}: the form actually rendered {style['width']}px "
            f"wide — no wider than the old 640px cap would have allowed"
        )

        # ── .form-cards still drops its outer box inside a pane ──────────────
        assert style["background"] == "rgba(0, 0, 0, 0)", (
            f"{panel_id}: a .form.form-cards inside a tab pane picked up a "
            f"background ({style['background']}) instead of staying "
            f"boxless the way it does at panel level"
        )
        assert style["borderTopWidth"] == "0px", (
            f"{panel_id}: a .form.form-cards inside a tab pane picked up a "
            f"border ({style['borderTopWidth']}) instead of staying "
            f"boxless the way it does at panel level"
        )
    finally:
        context.close()
