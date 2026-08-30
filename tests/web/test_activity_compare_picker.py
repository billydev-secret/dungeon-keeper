"""Browser gate for the Activity panel's "Compare to" picker.

The picker carries two things at once — how far back the band looks, and which
days go into it (every day, or only days sharing today's weekday). Two things
in one ``<select>`` is what the panel's own design rule asks for, but it makes
the *option set* change under the reader as they switch resolution or mode, and
that is where this control goes wrong:

  * Assigning a ``<select>`` a value it has no option for silently blanks it.
    The windows offered differ per resolution ("Last 7 days" exists, "Last 7
    weeks" does not), so carrying the number across a resolution change left an
    empty picker sitting over a chart drawn to the server's default window —
    the reader is told one thing and shown another.
  * XP cannot reach past raw retention, so some options are disabled per mode.
    Landing on a disabled one has the same effect.

The invariant both cases violate: after any resolution/mode change the picker's
value is always one of its own enabled options, and it is that value the next
request carries.

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
        db = tmp / "activity-picker.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("activity-picker"))
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


# An overlay-shaped payload, enough for the panel to draw something. The picker
# assertions never depend on it: the value is chosen before the request goes out.
_OVERLAY_BODY = {
    "resolution": "day_overlay",
    "window_label": "Today vs Last 4 days",
    "band_label": "Typical day",
    "mode": "messages",
    "labels": [f"{h}:00" for h in range(24)],
    "counts": [1.0] * 24,
    "member_counts": [],
    "show_members": False,
    "y_label": "Messages",
    "tz_label": "UTC",
    "x_label": "Hour of day",
    "series": [],
    "band_low": [1.0] * 24,
    "band_mid": [2.0] * 24,
    "band_high": [3.0] * 24,
    "periods_sampled": 4,
}

# Records every GET so a test can read the query the panel actually sent.
_STUB_FETCH = """
(body) => {
  window.__gets = [];
  window.fetch = async (url, opts = {}) => {
    const href = String(url);
    if ((opts.method || 'GET').toUpperCase() === 'GET') window.__gets.push(href);
    const payload = href.includes('/api/meta/') ? [] : body;
    return new Response(JSON.stringify(payload), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
}
"""

_MOUNT = """
async () => {
  document.body.innerHTML = '<div id="host"></div>';
  const mod = await import('/static/js/panels/activity.js');
  mod.mount(document.getElementById('host'), {});
  await new Promise((r) => setTimeout(r, 120));
  return true;
}
"""

# Drives one control and reports the picker's resulting state.
_SET = """
async ({ selector, value }) => {
  const el = document.querySelector(selector);
  el.value = value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 120));
  const cmp = document.querySelector('[data-control="compare"]');
  const opts = [...cmp.querySelectorAll('option')];
  const chosen = opts.find((o) => o.value === cmp.value);
  return {
    value: cmp.value,
    exists: !!chosen,
    disabled: !!(chosen && chosen.disabled),
    label: chosen ? chosen.textContent : '',
    lastGet: window.__gets[window.__gets.length - 1] || '',
  };
}
"""


def _mount(page) -> None:
    page.evaluate(_STUB_FETCH, _OVERLAY_BODY)
    page.evaluate(_MOUNT)


def _set(page, selector: str, value: str) -> dict:
    return page.evaluate(_SET, {"selector": selector, "value": value})


_RES = '[data-control="resolution"]'
_MODE = '[data-control="mode"]'
_COMPARE = '[data-control="compare"]'


def test_picker_survives_a_resolution_change(page):
    """"Last 7 days" has no counterpart in weeks; the picker must not blank."""
    _mount(page)
    _set(page, _RES, "day_overlay")
    _set(page, _COMPARE, "all:7")
    state = _set(page, _RES, "week_overlay")

    assert state["exists"], f"picker blanked: {state}"
    assert not state["disabled"]
    # Whatever it settled on is what the request carries — no silent default.
    n = state["value"].split(":")[1]
    assert f"compare_periods={n}" in state["lastGet"]


def test_picker_never_lands_on_an_out_of_reach_window(page):
    """XP cannot read 26 same weekdays back; picking it in messages and then
    switching to XP has to pull the window in, not keep a greyed option."""
    _mount(page)
    _set(page, _RES, "day_overlay")
    _set(page, _MODE, "messages")
    _set(page, _COMPARE, "weekday:26")
    state = _set(page, _MODE, "xp")

    assert state["exists"] and not state["disabled"], state
    # The basis is the reader's choice and survives; only the window shortens.
    assert state["value"].startswith("weekday:")
    assert int(state["value"].split(":")[1]) <= 12


def test_same_weekday_basis_reaches_the_server(page):
    _mount(page)
    _set(page, _RES, "day_overlay")
    state = _set(page, _COMPARE, "weekday:8")

    assert "same_weekday=true" in state["lastGet"]
    assert "compare_periods=8" in state["lastGet"]
    assert "same weekdays" in state["label"]


def test_every_day_basis_sends_no_weekday_flag(page):
    _mount(page)
    _set(page, _RES, "day_overlay")
    state = _set(page, _COMPARE, "all:28")

    assert "same_weekday" not in state["lastGet"]
    assert "compare_periods=28" in state["lastGet"]
