"""Browser gate for the shared searchable-picker dropdown (js/filter-select.js).

``filterSelect``/``multiFilterSelect`` back the pickers in 43 panels, so a
regression here is a regression almost everywhere. The bug this guards against
showed up only on iOS: the list had no ``display`` rule of its own and leaned
entirely on the UA's ``[popover]:not(:popover-open) { display: none }``, so
wherever the Popover API was missing it stayed on screen forever as a stray
``position: fixed`` box painting over the panel — and ``matches(":popover-open")``
threw SyntaxError on the same engines, killing the focus handler outright.

The widget is mounted directly from the shipped module rather than through a
panel, so the assertions don't depend on a panel's data (the test env runs no
bot, so channel/role option fetches 503).

Covered:
  * closed by default, and hidden again after blur — asserted on *computed*
    style, so it fails if visibility ever regresses to the UA's care;
  * open on focus, anchored under its input rather than floating loose;
  * still anchored after the page scrolls;
  * the no-Popover-API path (the actual iOS failure) hides and anchors too.

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
        db = tmp / "filter-select.db"
        # Module-scoped, so the per-test reaper must not delete it mid-run.
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("filter-select"))
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


# Mounts the real widget on whatever page is loaded. `spacer` pushes the input
# down the page so there is somewhere to scroll to; `strip_popover` simulates an
# engine without the Popover API, which is the iOS case the fix is really about.
# `keyboard` stubs visualViewport as an on-screen keyboard leaves it — see
# test_dropdown_ignores_visual_viewport_offset. `optionCount` grows the list
# past its 240px max-height, so the flip-above branch has something to flip.
_MOUNT = """
async ({ spacer, stripPopover, keyboard, optionCount }) => {
  if (stripPopover) {
    delete HTMLElement.prototype.showPopover;
    delete HTMLElement.prototype.hidePopover;
    delete HTMLElement.prototype.togglePopover;
  }
  if (keyboard) {
    // A stub, not a real keyboard: Chromium has no software keyboard and
    // Playwright cannot shift the visual viewport, so the only way to exercise
    // the offset iOS actually produces is to report one.
    Object.defineProperty(window, 'visualViewport', {
      configurable: true,
      value: {
        offsetLeft: 0, offsetTop: keyboard.offsetTop, scale: 1,
        width: window.innerWidth, height: window.innerHeight - keyboard.height,
        addEventListener() {}, removeEventListener() {},
      },
    });
  }
  document.body.innerHTML = '';
  const pad = document.createElement('div');
  pad.style.height = spacer + 'px';
  document.body.appendChild(pad);
  const host = document.createElement('div');
  host.id = 'host';
  document.body.appendChild(host);
  // Cache-busted at serve time; the query keeps a stripped-popover import from
  // resolving to the module instance a previous test already evaluated.
  const mod = await import('/static/js/filter-select.js?t=' + (stripPopover ? 'np' : 'p'));
  const names = ['general', 'todo', 'random'];
  const options = Array.from({ length: optionCount }, (_, i) => ({
    id: String(i + 1), label: names[i] || 'channel-' + (i + 1),
  }));
  const fs = mod.filterSelect('Type to filter…', options, { label: 'Channel' });
  host.appendChild(fs.el);
  const tall = document.createElement('div');
  tall.style.height = '2000px';
  document.body.appendChild(tall);
}
"""

_GEOMETRY = """
() => {
  const input = document.querySelector('.filter-select-input');
  const list = document.querySelector('.filter-select-list');
  const ir = input.getBoundingClientRect();
  const lr = list.getBoundingClientRect();
  return {
    display: getComputedStyle(list).display,
    dx: Math.abs(lr.left - ir.left),
    dy: Math.abs(lr.top - ir.bottom),
    // Gap on the flip-above side: 0 when the list sits on top of the input.
    gapAbove: Math.abs(ir.top - lr.bottom),
    options: list.querySelectorAll('.filter-select-item').length,
    expanded: input.getAttribute('aria-expanded'),
  };
}
"""


def _page(
    browser,
    dashboard,
    *,
    spacer: int = 0,
    strip_popover: bool = False,
    keyboard: dict | None = None,
    option_count: int = 3,
):
    context = browser.new_context(viewport={"width": 420, "height": 780})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    _goto_panel(page, f"{dashboard.base}/")
    page.evaluate(
        _MOUNT,
        {
            "spacer": spacer,
            "stripPopover": strip_popover,
            "keyboard": keyboard,
            "optionCount": option_count,
        },
    )
    return context, page, errors


@pytest.mark.parametrize("strip_popover", [False, True], ids=["popover", "no-popover"])
def test_dropdown_hidden_until_focused(browser, dashboard, strip_popover):
    """Closed is `display: none` in CSS we control, not by the UA's grace."""
    context, page, errors = _page(browser, dashboard, strip_popover=strip_popover)
    try:
        assert page.evaluate(_GEOMETRY)["display"] == "none"

        page.click(".filter-select-input")
        opened = page.evaluate(_GEOMETRY)
        assert opened["display"] != "none"
        assert opened["options"] == 4  # the "(any)" sentinel + three channels
        assert opened["expanded"] == "true"

        # Blur closes it again (after the 150ms grace the widget allows for a
        # click on an option to land first).
        page.evaluate("() => document.querySelector('.filter-select-input').blur()")
        page.wait_for_timeout(300)
        closed = page.evaluate(_GEOMETRY)
        assert closed["display"] == "none"
        assert closed["expanded"] == "false"

        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()


@pytest.mark.parametrize("strip_popover", [False, True], ids=["popover", "no-popover"])
def test_dropdown_stays_anchored_to_its_input(browser, dashboard, strip_popover):
    """The list sits under its input — before and after the page scrolls.

    The iOS symptom was a list stranded hundreds of pixels from its field, so
    the assertion is on the input↔list gap, not on absolute coordinates.
    """
    context, page, errors = _page(browser, dashboard, spacer=600, strip_popover=strip_popover)
    try:
        page.click(".filter-select-input")
        at_rest = page.evaluate(_GEOMETRY)
        assert at_rest["dx"] < 2, f"list not left-aligned with input: {at_rest}"
        assert at_rest["dy"] < 2, f"list not flush under input: {at_rest}"

        page.evaluate("() => window.scrollTo(0, 400)")
        page.wait_for_timeout(150)
        scrolled = page.evaluate(_GEOMETRY)
        assert scrolled["display"] != "none"
        assert scrolled["dx"] < 2, f"list drifted horizontally on scroll: {scrolled}"
        assert scrolled["dy"] < 2, f"list drifted vertically on scroll: {scrolled}"

        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()


# (visual-viewport offsetTop, keyboard height, page spacer) — the offset an iOS
# keyboard leaves behind, and how far down the page the field sits. 0/0 is the
# desktop control: it must stay anchored too, so the fix can't be a phone-only
# special case.
@pytest.mark.parametrize(
    ("offset_top", "kb_height", "spacer"),
    [
        pytest.param(0, 0, 300, id="no-keyboard"),
        pytest.param(120, 340, 300, id="keyboard-shifted"),
        pytest.param(260, 340, 300, id="keyboard-shifted-far"),
        pytest.param(180, 340, 620, id="keyboard-shifted-flips-above"),
    ],
)
def test_dropdown_ignores_visual_viewport_offset(
    browser, dashboard, offset_top, kb_height, spacer
):
    """A shifted visual viewport must not move the list off its input.

    The reported symptom — a dropdown "floating off in the corner" on iOS,
    disconnected from the field that opened it. ``position: fixed`` and
    ``getBoundingClientRect()`` are both resolved against the *layout* viewport,
    so the visual viewport's offset belongs nowhere near the coordinates; the
    keyboard shifts only the visual one. Folding it in displaced the list by
    exactly ``visualViewport.offsetTop``, which is 0 on desktop — invisible to
    every other test here, and to the two above at any scroll position.

    The offset still belongs in the *fit* test (is there room below the field
    the user can actually see?), so the last case puts the field low enough that
    the keyboard forces a flip and asserts the list lands on top of the input —
    flush there too, not merely somewhere above it.
    """
    keyboard = {"offsetTop": offset_top, "height": kb_height}
    context, page, errors = _page(
        browser, dashboard, spacer=spacer, keyboard=keyboard, option_count=40
    )
    try:
        page.click(".filter-select-input")
        page.wait_for_timeout(150)
        g = page.evaluate(_GEOMETRY)
        assert g["display"] != "none"
        assert g["dx"] < 2, f"list drifted horizontally: {g}"
        # Anchored on one side or the other: flush under the input, or — when
        # the keyboard leaves no room below — flush above it.
        assert g["dy"] < 2 or g["gapAbove"] < 2, (
            f"list floated loose from its input (vv.offsetTop={offset_top}): {g}"
        )

        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()


# Realistic ids: the shipped fixture above uses "1"/"2"/"3", which are shorter
# than MIN_ID_FILTER_LEN and so can't exercise the paste-an-id path at all.
_MOUNT_SNOWFLAKES = """
async ({ }) => {
  document.body.innerHTML = '';
  const host = document.createElement('div');
  host.id = 'host';
  document.body.appendChild(host);
  const mod = await import('/static/js/filter-select.js?t=sf');
  const fs = mod.filterSelect('Type to filter…', [
    { id: '1532059736038441200', label: '#🚧│bounty-board' },
    { id: '1526455991514955817', label: '#shop' },
    { id: '1530328883449040967', label: '#the-casino' },
  ], { label: 'Channel' });
  host.appendChild(fs.el);
  return fs.getValue();
}
"""

_COUNT = """
() => document.querySelectorAll('.filter-select-item').length
"""


def _snowflake_page(browser, dashboard):
    context = browser.new_context(viewport={"width": 420, "height": 780})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)[:200]))
    _goto_panel(page, f"{dashboard.base}/")
    page.evaluate(_MOUNT_SNOWFLAKES, {})
    page.click(".filter-select-input")
    return context, page, errors


def test_pasting_a_channel_id_finds_the_channel(browser, dashboard):
    """Copying an id out of Discord is the obvious move on mobile, where the
    name carries an emoji and a box-drawing separator. A label-only filter
    answered that paste with an empty list, which reads as "the bot can't see
    any channels" — and then Post failed with a channel-type error."""
    context, page, errors = _snowflake_page(browser, dashboard)
    try:
        page.fill(".filter-select-input", "1532059736038441200")
        # the "(any)" sentinel row + exactly the pasted channel
        assert page.evaluate(_COUNT) == 2
        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()


def test_a_short_numeric_filter_does_not_match_every_id(browser, dashboard):
    """Guard the gate on MIN_ID_FILTER_LEN: typing a digit while searching a
    name must not drag in every option whose id happens to contain it."""
    context, page, errors = _snowflake_page(browser, dashboard)
    try:
        page.fill(".filter-select-input", "1")
        # Only the sentinel — no label holds a "1" and the id path is off.
        assert page.evaluate(_COUNT) == 1
        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()


def test_filtering_by_name_still_works(browser, dashboard):
    context, page, errors = _snowflake_page(browser, dashboard)
    try:
        page.fill(".filter-select-input", "casino")
        assert page.evaluate(_COUNT) == 2
        assert not errors, f"uncaught JS: {errors}"
    finally:
        context.close()
