"""Deep-link / URL state for the mod workflow panels (IA3).

The router's convention (app.js, "Hash parsing") is that panel-local state —
tab, filter, search, selection — lives in the hash query so a refresh keeps the
view and a link hands a colleague the *same* view. Sixteen analytics panels
adopted it early; the mod queues, where "link me to that ticket" is the actual
daily need, were finished in ``9d39040b``. Nothing pinned the behavior, so this
does, at the level a user feels it:

  * a hash param puts the panel in that state on mount;
  * clicking a control rewrites the hash (``history.replaceState`` — no new
    history entry, no remount);
  * reloading the rewritten URL comes back to the same state;
  * a garbage/stale param falls back to the default instead of erroring — the
    realistic case, since these URLs get pasted into Discord and edited by hand.

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
from mobile_layout_scan import serve  # noqa: E402

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
        db = tmp / "mod-queue-deeplinks.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("mod-queue-deeplinks"))
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


# ── the panels under test ───────────────────────────────────────────────────
#
# `control` is the CSS selector for the button strip that carries the state,
# `attr` the data attribute holding each button's value. `numeric` is the
# panel's row-selection param, which takes an id — the one place a hand-edited
# URL most easily goes bad.

class Panel:
    def __init__(self, page_id, control, attr, param, default, alt, numeric):
        self.page_id = page_id
        self.control = control
        self.attr = attr
        self.param = param
        self.default = default
        self.alt = alt
        self.numeric = numeric

    def __repr__(self) -> str:  # readable parametrize ids
        return self.page_id


PANELS = [
    Panel("mod-tickets", "[data-filter-group]", "data-filter", "filter", "open", "closed", "ticket"),
    Panel("mod-jails", "[data-filter-group]", "data-filter", "filter", "active", "released", "jail"),
    # rules-watch mounts its own queue; initialParams come from the hash.
    Panel("rules-watch", "[data-tabs]", "data-tab", "tab", "queue", "ledger", "event"),
    Panel("qa-tracker", "[data-filter-group]", "data-filter", "filter", "all", "failed", "test"),
    Panel("mod-todo", "[data-filter-group]", "data-filter", "filter", "pending", "completed", "task"),
]

_ACTIVE = """
([sel, attr]) => {
  const btn = document.querySelector(sel + ' button.active[' + attr + ']');
  return btn ? btn.getAttribute(attr) : null;
}
"""


def _open(page, base: str, hash_: str) -> None:
    page.goto(f"{base}/#/{hash_}", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector(".panel", timeout=20_000)


def _active(page, panel: Panel) -> str | None:
    page.wait_for_selector(f"{panel.control} button.active", timeout=10_000)
    return page.evaluate(_ACTIVE, [panel.control, panel.attr])


@pytest.fixture()
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(str(e)))
    p.dk_errors = errors  # type: ignore[attr-defined]
    yield p
    ctx.close()


@pytest.mark.parametrize("panel", PANELS, ids=repr)
def test_hash_param_restores_panel_state(page, dashboard, panel: Panel):
    """A pasted link opens the panel on the tab/filter it names."""
    _open(page, dashboard.base, f"{panel.page_id}?{panel.param}={panel.alt}")
    assert _active(page, panel) == panel.alt
    assert not page.dk_errors, page.dk_errors


@pytest.mark.parametrize("panel", PANELS, ids=repr)
def test_control_writes_the_hash_and_survives_reload(page, dashboard, panel: Panel):
    """Round trip: click → hash → reload → same state (and no remount loop)."""
    _open(page, dashboard.base, panel.page_id)
    assert _active(page, panel) == panel.default

    page.click(f"{panel.control} button[{panel.attr}='{panel.alt}']")
    page.wait_for_function(
        "([id, param, alt]) => location.hash.startsWith('#/' + id + '?')"
        " && new URLSearchParams(location.hash.split('?')[1]).get(param) === alt",
        arg=[panel.page_id, panel.param, panel.alt],
        timeout=10_000,
    )

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector(".panel", timeout=20_000)
    assert _active(page, panel) == panel.alt
    assert not page.dk_errors, page.dk_errors


@pytest.mark.parametrize("panel", PANELS, ids=repr)
def test_garbage_hash_params_degrade_to_defaults(page, dashboard, panel: Panel):
    """A stale or hand-mangled URL must not error, blank the panel, or route
    away — every enumerated param is validated against its own value list and
    the id params are numbers that simply match no row."""
    junk = f"{panel.param}=%3Cscript%3Ex&{panel.numeric}=not-a-number&bogus=1"
    _open(page, dashboard.base, f"{panel.page_id}?{junk}")
    assert _active(page, panel) == panel.default
    assert "Page Not Available" not in page.inner_text("#panel-root")
    assert not page.dk_errors, page.dk_errors
