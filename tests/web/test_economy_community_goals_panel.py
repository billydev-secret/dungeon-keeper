"""Community goals moved from the bank page to the quests page.

A community goal is a row in the quests table with ``qtype = 'community'``, and
the card that tracks and settles one calls ``/api/economy/quests/{id}/progress``
and ``/settle``. It sat on Economy › Operations › Bank, which is the page for
grants, rentals and the ledger — none of which it touches.

These pin the move rather than a layout preference: the card must render on the
quests page off the list that page already fetched, its Save must reach the
quest progress endpoint, and the bank page must no longer carry a Community
Goals card at all.

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
        db = tmp / "community-goals.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("community-goals"))
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
(routes) => {
  window.__writes = [];
  window.fetch = async (url, opts = {}) => {
    const href = String(url);
    const method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET') window.__writes.push({ method, href, body: opts.body || null });
    const needles = Object.keys(routes).sort((a, b) => b.length - a.length);
    for (const needle of needles) {
      if (href.includes(needle)) {
        return new Response(JSON.stringify(routes[needle]), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        });
      }
    }
    return new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
}
"""

_MOUNT = """
async ({ module }) => {
  document.body.innerHTML = '<div id="host"></div>';
  await (await import('/static/js/config-helpers.js')).resetMetaCaches();
  const mod = await import(module);
  const handle = mod.mount(document.getElementById('host'));
  if (handle && handle.ready) await handle.ready;
  await new Promise((r) => setTimeout(r, 120));
  return true;
}
"""

_COMMUNITY_QUEST = {
    "id": 7,
    "title": "Fill the meadow",
    "qtype": "community",
    "reward": 50,
    "reward_xp": 0,
    "active": True,
    "signoff": False,
    "trigger_kind": None,
    "trigger_words": None,
    "target_count": 1,
    "community_target": 400,
    "community_current": 120,
    "community_completed_at": None,
    "community_settled_at": None,
}

_ROUTES = {
    "/api/economy/quests": {"quests": [_COMMUNITY_QUEST]},
    "/api/economy/config": {"enabled": True, "quest_board_daily": 3, "quest_board_weekly": 2},
    "/api/meta/channels": [],
    "/api/economy/rentals": {"rentals": []},
    "/api/economy/ledger": {"entries": []},
    "/api/meta/members": [],
}


def _mount(page, module: str) -> None:
    page.evaluate(_STUB_FETCH, _ROUTES)
    page.evaluate(_MOUNT, {"module": module})


def test_quests_page_shows_the_community_goal_card(page):
    _mount(page, "/static/js/panels/economy-quests.js")
    card = page.locator("#host [data-sec='community']")
    assert card.count() == 1
    assert card.is_visible()
    assert "Fill the meadow" in card.inner_text()
    assert "120 / 400" in card.inner_text()


def test_quests_page_saves_progress_to_the_quest_endpoint(page):
    _mount(page, "/static/js/panels/economy-quests.js")
    page.fill("#host [data-cprogress='7']", "250")
    page.click("#host [data-cprogress-save='7']")
    page.wait_for_function("() => window.__writes.length > 0")
    write = page.evaluate("() => window.__writes[0]")
    assert write["href"].endswith("/api/economy/quests/7/progress")
    assert '"current":250' in write["body"].replace(" ", "")


def test_bank_page_no_longer_carries_community_goals(page):
    _mount(page, "/static/js/panels/economy-bank-manager.js")
    assert page.locator("#host [data-sec='community']").count() == 0
    assert "Community Goals" not in page.locator("#host").inner_text()
