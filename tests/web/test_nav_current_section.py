"""The nav rail's "you are here" signal.

The rail marks the section containing the active page by setting its header
**wider** — Archivo's ``wdth`` axis, 125 against 85 — rather than by giving it
a colour. Colour on this dashboard is spoken for (semantic red/green), and
width survives greyscale and every form of colour blindness.

Two things can quietly kill that signal, and neither shows up as a broken
page, so they are pinned here:

  * ``.current`` drifting onto ``aria-expanded``. Several sections can be open
    at once — the user's open set is persisted across navigations — so
    "expanded" and "the section I am in" are *different questions*. Keying the
    marker off expansion would light up three sections at once and the signal
    would mean nothing.
  * The width itself regressing to a plain colour or weight change, e.g. if
    Archivo were ever swapped for a non-variable face. The class surviving
    while the width does not is the failure mode worth catching, so the
    computed ``font-variation-settings`` is asserted, not just the class.

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
        db = tmp / "nav-current.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("nav-current"))
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


def _open(browser, base: str, page_id: str):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(f"{base}/#/{page_id}", wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_selector(".nav-group.current", timeout=30_000)
    return page


def _current_labels(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.nav-group.current')]"
        ".map(g => g.textContent.trim())"
    )


# ── the signal points at the right section ──────────────────────────────────


@pytest.mark.parametrize(
    ("page_id", "section"),
    [
        pytest.param("xp-leaderboard", "Reports", id="reports"),
        pytest.param("mod-todo", "Moderation", id="moderation"),
    ],
)
def test_current_marks_the_active_pages_section(browser, dashboard, page_id, section):
    page = _open(browser, dashboard.base, page_id)
    try:
        assert _current_labels(page) == [section]
    finally:
        page.close()


def test_current_moves_when_you_navigate(browser, dashboard):
    """Not just correct on first paint — it has to follow you."""
    page = _open(browser, dashboard.base, "xp-leaderboard")
    try:
        assert _current_labels(page) == ["Reports"]
        page.evaluate("() => { window.location.hash = '#/mod-todo'; }")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-group.current')]"
            ".some(g => g.textContent.trim() === 'Moderation')",
            timeout=15_000,
        )
        assert _current_labels(page) == ["Moderation"]
    finally:
        page.close()


# ── the signal is not "expanded" ────────────────────────────────────────────


def test_expanding_another_section_does_not_make_it_current(browser, dashboard):
    """Open a second section by hand; only one header stays marked.

    This is the regression that would silently gut the design: several sections
    can be expanded at once, so if `.current` were ever keyed off aria-expanded
    the rail would claim you are in three places.
    """
    page = _open(browser, dashboard.base, "xp-leaderboard")
    try:
        economy = page.locator(".nav-group", has_text="Economy").first
        economy.click()
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-group')]"
            ".filter(g => g.getAttribute('aria-expanded') === 'true').length >= 2",
            timeout=15_000,
        )
        assert _current_labels(page) == ["Reports"]
    finally:
        page.close()


# ── the signal is width, not just a class ───────────────────────────────────


def test_current_section_is_actually_set_wider(browser, dashboard):
    """The class is the mechanism; the width is the signal. Assert the width.

    Guards against Archivo being replaced by a static face, or the rule being
    reduced to a colour change — either leaves `.current` present and correct
    while the thing the user actually perceives is gone.
    """
    page = _open(browser, dashboard.base, "xp-leaderboard")
    try:
        widths = page.evaluate(
            """() => {
              const read = (el) => getComputedStyle(el).fontVariationSettings || '';
              const cur = document.querySelector('.nav-group.current');
              const other = [...document.querySelectorAll('.nav-group')]
                .find(g => !g.classList.contains('current'));
              return { current: read(cur), other: read(other) };
            }"""
        )
        assert '"wdth" 125' in widths["current"], widths
        assert '"wdth" 85' in widths["other"], widths
    finally:
        page.close()
