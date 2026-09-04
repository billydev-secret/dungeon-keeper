"""Browser gate for the Feature Rotation panel's Save Settings button.

From the 2026-09-02 games deep review (rotation-rooms-155): every click on
Save Settings died with an uncaught TypeError inside the async listener — the
body builder still read a ``tz_offset_hours`` input that commit 6f44c8d1 had
removed from the form (the timezone moved to Server Settings). No toast, no
PUT, no config row; an admin who ticked Rotation: On and pressed Save saw
nothing, and the feature stayed dark. The panel-load health suite mounts the
panel but never presses a button, which is how it shipped.

Like its siblings the panel is mounted against a stubbed ``window.fetch``: the
test environment runs no bot, so the real API would 503. The stub records
every non-GET call so the test can assert what the panel actually sent.

Marked ``browser``. Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import json
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
        db = tmp / "feature-rotation-panel.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("feature-rotation-panel"))
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
    pg.on("pageerror", lambda err: _PAGE_ERRORS.append(str(err)))
    _PAGE_ERRORS.clear()
    _goto_panel(pg, f"{dashboard.base}/")
    yield pg
    context.close()


# Uncaught errors (an unhandled rejection from an async click handler included)
# land here. The bug was exactly one of those, so the test asserts on it too.
_PAGE_ERRORS: list[str] = []

_STUB_FETCH = """
(routes) => {
  window.__writes = [];
  window.fetch = async (url, opts = {}) => {
    const href = String(url);
    const method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET') window.__writes.push({ method, href, body: opts.body || null });
    const needles = Object.keys(routes).sort((a, b) => b.length - a.length);
    for (const needle of needles) {
      const spec = routes[needle];
      if (href.includes(needle)) {
        return new Response(JSON.stringify(spec.body ?? {}), {
          status: spec.status ?? 200,
          headers: { 'Content-Type': 'application/json' },
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
  const mod = await import(module);
  const handle = mod.mount(document.getElementById('host'));
  if (handle && handle.ready) await handle.ready;
  await new Promise((r) => setTimeout(r, 60));
  return true;
}
"""

_ANNOUNCE = "900000000000000001"

# The GET payload as the route builds it — a disabled rotation with an empty
# pool, which is exactly the state prod sat in.
_ROUTES = {
    "/api/meta/channels": {
        "body": [{"id": _ANNOUNCE, "name": "rotation-news", "type": "text"}],
    },
    "/api/feature-rotation/config": {"body": {"ok": True}},
    "/api/feature-rotation": {
        "body": {
            "config": {
                "enabled": False,
                "announce_channel_id": "0",
                "announce_hour": 9,
                "tz_offset_hours": -5,
                "rooms_per_day": 1,
                "last_flip_date": None,
                "last_announce_date": None,
            },
            "rooms": [],
            "today": {
                "local_day": "2026-09-02",
                "featured": [],
                "hidden": [],
                "blocked_quest_kinds": [],
            },
            "tomorrow": {"local_day": "2026-09-03", "featured": []},
            "trigger_kinds": [],
            "launchable_games": [],
        }
    },
}


def _mount(page) -> None:
    page.evaluate(
        "async () => (await import('/static/js/config-helpers.js')).resetMetaCaches()"
    )
    page.evaluate(_STUB_FETCH, _ROUTES)
    page.evaluate(_MOUNT, {"module": "/static/js/panels/feature-rotation.js"})


def test_save_settings_puts_the_form_to_the_config_route(page):
    """rotation-rooms-155. Fails against the pre-fix panel: the click handler
    throws before ``apiPut`` runs, so ``__writes`` stays empty and the page
    reports an uncaught TypeError."""
    _mount(page)
    assert page.locator("#host [data-save-settings]").count() == 1, "settings form did not render"

    page.select_option("#host #fr-enabled", "1")
    page.select_option("#host #fr-announce-channel", _ANNOUNCE)
    page.fill("#host #fr-announce-hour", "18")
    page.fill("#host #fr-rooms-per-day", "2")
    page.click("#host [data-save-settings]")
    page.wait_for_timeout(150)

    writes = page.evaluate("() => window.__writes")
    assert not _PAGE_ERRORS, f"Save threw instead of saving: {_PAGE_ERRORS}"
    puts = [w for w in writes if w["method"] == "PUT" and w["href"].endswith("/api/feature-rotation/config")]
    assert len(puts) == 1, f"expected one PUT to the config route, saw {writes}"

    body = json.loads(puts[0]["body"])
    assert body == {
        "enabled": True,
        "announce_channel_id": _ANNOUNCE,
        "announce_hour": 18,
        "rooms_per_day": 2,
    }, "the body must carry only the fields the route accepts, the snowflake as a string"

    # The success path finishes with a toast, so the admin can tell it landed.
    assert "Settings saved" in page.evaluate("() => document.body.textContent")
