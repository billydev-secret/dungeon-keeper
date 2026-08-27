"""Suggested Setup rows can be cleared, and the way back exists.

The tile recomputed its list on every load, so the same three features came
back forever and whatever queued behind them never got a turn. Rows now carry
a dismiss control; dismissal is guild-level and permanent, and the full list
with a Restore button lives on the AI Assistant page the tile already links to.

Two things here can only break in a browser:

  * **The dismiss click must not navigate.** widget-grid binds a click-through
    to the report page on the whole tile card, so a × inside it fires that
    handler too unless the row stops propagation — the admin would be thrown
    onto another page mid-action. The host div below stands in for that card:
    it carries the same kind of outer click listener.
  * **The manage card must render a dismissed row and offer Restore**, off the
    ``include_dismissed`` payload.

Marked ``browser``. Auto-skips without Playwright / Chromium.
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "suggestions.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("suggestions"))
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


@pytest.fixture()
def page(browser, dashboard):
    context = browser.new_context(viewport={"width": 1200, "height": 900})
    pg = context.new_page()
    _goto_panel(pg, f"{dashboard.base}/")
    yield pg
    context.close()


_SUGGESTIONS = {
    "guild_id": "1",
    "suggestions": [
        {"slug": "welcome", "label": "Welcome messages", "blurb": "Greets newcomers.",
         "panel": "Config → Welcome", "status": "unconfigured", "effort": 2,
         "dismissed": False, "missing": [{"key": "welcome_channel_id", "label": "Channel"}]},
        {"slug": "birthdays", "label": "Birthdays", "blurb": "Celebrates birthdays.",
         "panel": "Config → Birthdays", "status": "partial", "effort": 1,
         "dismissed": True, "missing": []},
    ],
}

_STUB_FETCH = """
(body) => {
  window.__writes = [];
  window.fetch = async (url, opts = {}) => {
    const href = String(url);
    const method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET') window.__writes.push({ method, href });
    return new Response(
      JSON.stringify(href.includes('/api/help/suggestions') ? body : {}),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    );
  };
}
"""

# Stands in for the widget-grid card, which binds the click-through to the
# report page on the whole tile.
_MOUNT_TILE = """
async (data) => {
  document.body.innerHTML = '<div id="card"><div id="host"></div></div>';
  window.__cardClicked = false;
  document.getElementById('card').addEventListener('click', () => {
    window.__cardClicked = true;
  });
  const mod = await import('/static/js/tiles/setup-suggestions.js');
  mod.renderTile(document.getElementById('host'), data);
  return true;
}
"""

_MOUNT_PANEL = """
async () => {
  document.body.innerHTML = '<div id="host"></div>';
  const mod = await import('/static/js/panels/config-advisor.js');
  const handle = mod.mount(document.getElementById('host'));
  if (handle && handle.ready) await handle.ready;
  await new Promise((r) => setTimeout(r, 150));
  return true;
}
"""


def test_dismissing_a_row_posts_without_navigating_away(page):
    page.evaluate(_STUB_FETCH, _SUGGESTIONS)
    page.evaluate(_MOUNT_TILE, _SUGGESTIONS)
    page.click("#host [data-dismiss='welcome']")
    page.wait_for_function("() => window.__writes.length > 0")
    write = page.evaluate("() => window.__writes[0]")
    assert write["method"] == "POST"
    assert write["href"].endswith("/api/help/suggestions/welcome/dismiss")
    # The click-through on the surrounding card must not have fired.
    assert page.evaluate("() => window.__cardClicked") is False


def test_the_tile_points_at_where_dismissed_rows_come_back(page):
    page.evaluate(_STUB_FETCH, _SUGGESTIONS)
    page.evaluate(_MOUNT_TILE, _SUGGESTIONS)
    assert page.locator("#host .sugg-foot a[href='#/config-advisor']").count() == 1


def test_the_assistant_page_lists_dismissed_rows_with_restore(page):
    page.evaluate(_STUB_FETCH, _SUGGESTIONS)
    page.evaluate(_MOUNT_PANEL)
    card = page.locator("#host [data-sec='suggestions']")
    assert card.count() == 1
    assert "Birthdays" in card.inner_text()
    restore = page.locator("#host [data-sugg-toggle='birthdays']")
    assert restore.inner_text().strip() == "Restore"
    assert page.locator("#host [data-sugg-toggle='welcome']").inner_text().strip() == "Dismiss"
    restore.click()
    page.wait_for_function("() => window.__writes.length > 0")
    write = page.evaluate("() => window.__writes[0]")
    assert write["method"] == "DELETE"
    assert write["href"].endswith("/api/help/suggestions/birthdays/dismiss")
