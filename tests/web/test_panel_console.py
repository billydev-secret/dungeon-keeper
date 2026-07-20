"""Panel health gate: no dashboard panel may error on load.

The mobile gate opened a browser to check *layout*; this opens one to check that
a panel actually *works* on load — no uncaught JS exception, no console error, no
broken fetch. Nothing else exercises the vanilla-JS panels beyond a syntax check,
so a panel that throws on mount (a renamed endpoint, a bad import, a null deref)
would ship green. This catches it.

Per panel, in a fresh browser context, it asserts:
  * no uncaught JS exception (`pageerror`);
  * no `console.error` other than resource-load failures (those are the network
    layer's concern, checked below, and would double-count);
  * no same-origin request that failed or returned 4xx/5xx.

The test env has no connected bot, so endpoints needing one legitimately return
503 ("Bot is not connected") — tolerated, since it can't happen in prod where
the bot is up. The SSE log stream and favicon are tolerated too. One panel with
a pre-existing report 404 is allowlisted.

Marked ``browser`` (excluded from the default suite; run scoped by gate.py and
fully by nightly). Auto-skips without Playwright / Chromium.
"""

from __future__ import annotations

import os
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
from mobile_layout_scan import (  # noqa: E402
    _goto_panel,
    _settle,
    enumerate_panels,
    serve,
)

from migrations import apply_migrations_sync  # noqa: E402


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


# ── tolerances ──────────────────────────────────────────────────────────────

# The test env runs no Discord bot, so endpoints that need one return 503 — that
# can't happen in prod, so it isn't a panel bug.
TOLERATED_STATUS = {503}
# Requests that never cleanly finish or aren't a panel's doing.
TOLERATED_PATH_SUBSTR = ("/api/logs/stream", "/favicon")
# Panels with a pre-existing load error unrelated to this gate — an allowlist,
# same idea as the mobile gate's KNOWN_OVERFLOW. Their *network* errors are
# tolerated; a JS exception would still fail. (greeter-response's report 404s
# until greeter data is seeded — a no-data quirk, not a broken panel.)
KNOWN_LOAD_ERRORS = {"greeter-response"}


# ── served dashboard + browser (module-scoped) ──────────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "console.db"
        apply_migrations_sync(db)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("console"))
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


def _scoped_ids(browser, base: str) -> list[str]:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        ids = enumerate_panels(page, base)
    finally:
        page.close()
    scope = os.environ.get("PANEL_SCOPE", "").strip()
    if scope:
        wanted = {p.strip() for p in scope.split(",") if p.strip()}
        ids = [i for i in ids if i in wanted]
    return ids


def _tolerated_path(url: str) -> bool:
    return any(s in url for s in TOLERATED_PATH_SUBSTR)


def _audit_panel(browser, base: str, pid: str) -> dict:
    """Load one panel in a fresh context; collect JS + network problems."""
    console_errors: list[str] = []
    page_errors: list[str] = []
    bad_net: list[str] = []

    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()

    def on_console(msg):
        if msg.type != "error":
            return
        text = msg.text
        # Resource-load failures echo the network layer — counted there, not here.
        if text.startswith("Failed to load resource"):
            return
        console_errors.append(text[:160])

    def on_response(resp):
        url = resp.url
        if not url.startswith(base):
            return
        if resp.status >= 400 and resp.status not in TOLERATED_STATUS and not _tolerated_path(url):
            bad_net.append(f"{resp.status} {url[len(base):][:80]}")

    def on_requestfailed(req):
        if req.url.startswith(base) and not _tolerated_path(req.url):
            bad_net.append(f"FAILED {req.url[len(base):][:80]}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: page_errors.append(str(e)[:160]))
    page.on("response", on_response)
    page.on("requestfailed", on_requestfailed)

    try:
        _goto_panel(page, f"{base}/#/{pid}")
        _settle(page)
    except Exception as e:  # a navigation that outright fails is its own finding
        page_errors.append(f"navigation: {str(e)[:120]}")
    finally:
        context.close()

    return {"console": console_errors, "pageerror": page_errors, "net": sorted(set(bad_net))}


# ── the sweep ───────────────────────────────────────────────────────────────

def test_no_panel_errors_on_load(dashboard, browser):
    """Every in-scope panel mounts without a JS exception, console error, or
    broken same-origin request (bot-dependent 503s aside)."""
    ids = _scoped_ids(browser, dashboard.base)
    assert ids, "no panels enumerated from the nav — did the dashboard render?"

    failures: list[str] = []
    for pid in ids:
        res = _audit_panel(browser, dashboard.base, pid)
        # A JS exception is always a bug — even on an allowlisted panel.
        if res["pageerror"]:
            failures.append(f"{pid}: uncaught JS — {'; '.join(res['pageerror'][:3])}")
        if res["console"]:
            failures.append(f"{pid}: console.error — {'; '.join(res['console'][:3])}")
        if res["net"] and pid not in KNOWN_LOAD_ERRORS:
            failures.append(f"{pid}: bad request — {'; '.join(res['net'][:4])}")

    assert not failures, "Panel load errors:\n" + "\n".join(failures)
