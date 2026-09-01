"""Behavioral coverage for the shared tabs.js widget (S1 extraction).

tabs.js has no bespoke logic layer to unit-test in Python — it is a DOM
widget, and this repo's only way to exercise dashboard JS behavior is a real
headless browser (see docs/web_testing.md). config-bios.js is the widget's
reference caller, so this drives the Bios panel through Playwright to prove
the three things the extraction promised to preserve exactly from the
original inline implementation:

  * lazy per-tab loading — a tab's data is fetched only once its button is
    clicked, never on initial mount;
  * the rejection-reaches-the-guard contract — a tab's ``render`` rejecting
    turns into a Retry button in that tab's own pane (mountAsync's error
    state), not a stuck "Loading…" and not an unhandled promise rejection;
    and that retry re-runs only that tab, leaving an already-loaded sibling
    tab's content untouched;
  * ``aria-pressed`` / the ``active`` class track the visible tab as it
    switches.

Layout (does it fit at phone width) is the mobile gate's job, not this
file's — see test_mobile_layout.py.

Marked ``browser``; auto-skips without Playwright/Chromium. The server and
browser fixtures copy test_panel_console.py's shape (OpenAuth, a fresh
migrated DB, a module-scoped uvicorn instance).
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
        db = tmp / "tabs-widget.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("tabs-widget"))
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

def test_bios_tabs_load_lazily_and_survive_a_failed_tab(dashboard, browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()

        requested: list[str] = []
        page.on("request", lambda req: requested.append(req.url))

        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        console_errors: list[str] = []

        def on_console(msg):
            if msg.type != "error":
                return
            # A stubbed 500 legitimately logs a resource-load failure — that
            # is the network layer echoing itself, not an app bug. What must
            # never appear is an *unhandled* rejection, which is exactly what
            # the un-caught version of this widget used to leave behind (F1).
            if msg.text.startswith("Failed to load resource"):
                return
            console_errors.append(msg.text)

        page.on("console", on_console)

        # Fail the Icebreakers tab's fetch until the test flips this off.
        failing = {"questions": True}

        def _route_questions(route):
            if failing["questions"]:
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"detail":"boom"}',
                )
            else:
                route.continue_()

        page.route("**/api/bios/questions", _route_questions)

        _goto_panel(page, f"{dashboard.base}/#/config-bios")
        page.wait_for_selector('[data-tab="config"]')
        page.wait_for_selector('[data-pane="config"] [data-form]', timeout=5000)

        # ── lazy loading ─────────────────────────────────────────────
        assert not any("/api/bios/fields" in u for u in requested), (
            "Profile Questions tab's data was fetched before its button was "
            "ever clicked — lazy loading was lost in the extraction"
        )
        assert not any("/api/bios/questions" in u for u in requested), (
            "Icebreakers tab's data was fetched before its button was ever "
            "clicked — lazy loading was lost in the extraction"
        )

        fields_btn = page.locator('[data-tab="fields"]')
        config_btn = page.locator('[data-tab="config"]')
        assert fields_btn.get_attribute("aria-pressed") == "false"
        fields_btn.click()
        page.wait_for_selector('[data-pane="fields"] table', timeout=5000)

        assert any("/api/bios/fields" in u for u in requested), (
            "clicking Profile Questions never fetched its data"
        )
        # ── aria-pressed / active class track the visible tab ──────────
        assert fields_btn.get_attribute("aria-pressed") == "true"
        assert "active" in (fields_btn.get_attribute("class") or "")
        assert config_btn.get_attribute("aria-pressed") == "false"
        assert "active" not in (config_btn.get_attribute("class") or "")
        assert page.locator('[data-pane="config"]').is_hidden()
        assert page.locator('[data-pane="fields"]').is_visible()

        # ── rejection reaches the guard ─────────────────────────────
        page.locator('[data-tab="questions"]').click()
        retry_btn = page.locator('[data-pane="questions"] [data-retry]')
        retry_btn.wait_for(state="visible", timeout=5000)
        pane_text = page.locator('[data-pane="questions"]').inner_text().lower()
        assert "icebreaker" in pane_text, (
            "the tab's own errorMsg ('Couldn't load the icebreaker questions.') "
            "did not reach its pane's error state"
        )

        assert not page_errors, (
            f"a rejected tab render surfaced as an uncaught JS error: {page_errors}"
        )
        assert not console_errors, (
            f"a rejected tab render leaked a console error instead of staying "
            f"inside mountAsync's catch: {console_errors}"
        )

        # Retry re-runs only the Icebreakers loader — Fields, already loaded,
        # must be untouched (a whole-page remount would have discarded it).
        failing["questions"] = False
        retry_btn.click()
        page.wait_for_selector('[data-pane="questions"] table', timeout=5000)
        assert page.locator('[data-pane="questions"] [data-retry]').count() == 0

        config_btn.click()
        assert page.locator('[data-pane="config"] [data-form]').count() == 1, (
            "switching back to Settings lost its already-loaded content"
        )
        fields_btn.click()
        assert page.locator('[data-pane="fields"] table').count() == 1, (
            "Profile Questions' content was gone after the Icebreakers retry "
            "— retry must have remounted the whole page instead of just its "
            "own tab"
        )
    finally:
        context.close()
