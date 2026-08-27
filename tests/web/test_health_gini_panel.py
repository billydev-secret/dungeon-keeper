"""Browser gate for the Participation Gini panel's empty-state guard.

``compute_gini`` returns ``tiers`` as a **dict** with five fixed keys, but the
panel guarded on ``!(d.tiers || []).length``. A dict has no ``.length``, so the
expression was always ``!undefined`` — true — and every visit rendered "No
messages in the last 30 days" over a server with 41k of them. The panel had
never displayed a number.

The guard now hangs off ``posters``, the count of distinct authors in the
window, which is the thing the empty state actually claims. Two cases pin it:
a populated payload must render its numbers, and a genuinely silent server must
still get the empty state.

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
        db = tmp / "gini-panel.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("gini-panel"))
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


_STUB_FETCH = """
(body) => {
  window.fetch = async (url) => new Response(
    JSON.stringify(String(url).includes('/api/health/gini') ? body : {}),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  );
}
"""

_MOUNT = """
async () => {
  document.body.innerHTML = '<div id="host"></div>';
  const mod = await import('/static/js/panels/health-gini.js');
  mod.mount(document.getElementById('host'));
  await new Promise((r) => setTimeout(r, 120));
  return document.getElementById('host').innerText;
}
"""

# The shape ``compute_gini`` actually returns, with the numbers prod reported
# on 2026-08-26 — the visit that showed "No messages" over 41k of them.
_LIVE_PAYLOAD = {
    "gini": 0.731,
    "badge": "warning",
    "lorenz": [{"x": 0.0, "y": 0.0}, {"x": 100.0, "y": 100.0}],
    "top5_share": 36.4,
    "top10_share": 52.0,
    "palma": 40.83,
    "tiers": {"lurker": 31, "light": 53, "moderate": 21, "active": 34, "power": 66},
    "sparkline": [0.7, 0.72, 0.73, 0.731],
    "per_channel": [],
    "weighted_gini": 0.805,
    "xp_gini": 0.699,
    "gini_history": [],
    "posters": 174,
    "total_messages": 41414,
}

_SILENT_PAYLOAD = {
    **_LIVE_PAYLOAD,
    "gini": 0,
    "top5_share": 0,
    "top10_share": 0,
    "palma": 0,
    "tiers": {"lurker": 12, "light": 0, "moderate": 0, "active": 0, "power": 0},
    "weighted_gini": 0,
    "xp_gini": 0,
    "posters": 0,
    "total_messages": 0,
}


def _mount(page, payload: dict) -> str:
    page.evaluate(_STUB_FETCH, payload)
    return page.evaluate(_MOUNT)


def test_gini_panel_renders_numbers_when_members_posted(page):
    text = _mount(page, _LIVE_PAYLOAD)
    assert "No messages" not in text
    assert "0.731" in text
    assert "36.4" in text


def test_gini_panel_shows_empty_state_when_nobody_posted(page):
    text = _mount(page, _SILENT_PAYLOAD)
    assert "No messages" in text
