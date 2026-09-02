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

Also covers the Home panel's Suggested Setup *widget* (``panels/home.js``),
one layer up from the tile above — Billy asked for the card to disappear
entirely once nothing is left to suggest, instead of showing an "all done"
message:

  * with nothing outstanding, the widget renders no card at all;
  * with something outstanding, it still renders (the tile above owns its
    dismiss control — this only proves the card actually shows up through the
    real fetch → filter → widget-grid pipeline);
  * a *failed* fetch is advisory and must not collapse into the same "nothing
    outstanding" empty state — the card stays up rather than reading as "all
    set" when the truth is "couldn't check".

And the drag-reorder path in the same file: a widget hidden from view (perm
gate, or setup-suggestions with nothing left to suggest) must be re-inserted
at its original spot in the *saved* layout, not dropped, when the visible
widgets around it are reordered. All of this is DOM/layout-only behavior with
no logic-layer counterpart, hence the browser tier rather than a unit test.

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

# No suggestions left — the widget must render nothing at all.
_NO_SUGGESTIONS = {"guild_id": "1", "suggestions": []}

# Mounts the real Home panel (not the tile in isolation): sets up the admin
# user + a fixed widget layout in localStorage the way a real login would,
# then imports panels/home.js and calls its mount() — exercising the actual
# fetch → filter → widget-grid pipeline the "disappears when done" fix lives
# in. `perms` defaults to admin (setup-suggestions is admin-only); `layout`
# is a plain array of widget ids, written under the same
# `dk_layout_<user_id>` / version-3 key home.js itself reads.
_MOUNT_HOME = """
async ({ perms, layout }) => {
  document.body.innerHTML = '<div id="host"></div>';
  try { localStorage.clear(); } catch (_) {}
  window.__dk_user = { user_id: '0', perms: new Set(perms || ['admin']) };
  // home.js one-time-injects "unseen" admin widgets (setup-suggestions,
  // config-problems) into whatever layout it finds — mark both seen so the
  // explicit `layout` below is what actually renders, not that plus a
  // surprise extra card.
  localStorage.setItem('dk_seen_setup_suggestions_0', '1');
  localStorage.setItem('dk_seen_config_problems_0', '1');
  localStorage.setItem('dk_layout_0', JSON.stringify({ version: 3, widgets: layout }));
  const mod = await import('/static/js/panels/home.js');
  window.__homeHandle = mod.mount(document.getElementById('host'));
  return true;
}
"""

# Drags the card for `srcId` and drops it on the card for `tgtId`, firing the
# same dragstart/dragover/drop/dragend sequence widget-grid.js's
# `_setupDragDrop` listens for. Real OS-level drag simulation is exactly the
# kind of thing that flakes headless; dispatching the DragEvents directly at
# the cards is what the listeners actually key off (they never inspect how
# the drag was produced), so this reaches the same code deterministically.
_DRAG_REORDER = """
([srcId, tgtId]) => {
  const cards = [...document.querySelectorAll("#host .home-grid .home-card[data-widget-id]")];
  const src = cards.find((c) => c.dataset.widgetId === srcId);
  const tgt = cards.find((c) => c.dataset.widgetId === tgtId);
  if (!src || !tgt) throw new Error(`drag card not found: ${srcId} -> ${tgtId}`);
  const dt = new DataTransfer();
  const fire = (type, el) => {
    const r = el.getBoundingClientRect();
    el.dispatchEvent(new DragEvent(type, {
      bubbles: true, cancelable: true, dataTransfer: dt,
      clientX: r.x + r.width / 2, clientY: r.y + r.height / 2,
    }));
  };
  fire("dragstart", src);
  fire("dragover", tgt);
  fire("drop", tgt);
  fire("dragend", src);
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


# ── The footer names the guild's own assistant (todo #164) ──────────────────


def test_the_footer_uses_the_guild_s_own_assistant_name(page):
    """"Ask <name> to set any of these up" was a hardcoded "Billy-bot".

    The name is per-guild branding (Config → Branding), so a server that had
    renamed its assistant still read the default here — the one sentence on
    this tile that names it at all.
    """
    branded = {**_SUGGESTIONS, "assistant_name": "Sam-bot"}
    page.evaluate(_STUB_FETCH, branded)
    page.evaluate(_MOUNT_TILE, branded)
    foot = page.locator("#host .sugg-foot").inner_text()
    assert "Ask Sam-bot to set any of these up" in foot
    assert "Billy-bot" not in foot


def test_the_footer_falls_back_when_the_name_is_missing(page):
    """A payload without the field (an older cached response) still reads."""
    page.evaluate(_STUB_FETCH, _SUGGESTIONS)
    page.evaluate(_MOUNT_TILE, _SUGGESTIONS)
    assert "Ask Billy-bot to set any of these up" in page.locator("#host .sugg-foot").inner_text()


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


# ── Home panel: the widget itself, not the tile in isolation ──────────────


def test_home_panel_shows_nothing_when_setup_is_all_done(page):
    page.evaluate(_STUB_FETCH, _NO_SUGGESTIONS)
    page.evaluate(_MOUNT_HOME, {"perms": ["admin"], "layout": ["setup-suggestions"]})
    # An empty grid has no size, so it counts as "hidden" under Playwright's
    # default visibility check — wait for it to exist in the DOM, not for it
    # to have a non-zero box (that's exactly what the empty-grid test wants
    # to prove is absent).
    page.wait_for_selector("#host .home-grid", state="attached")
    # The layout has nothing else in it, so an "all done" card would be the
    # grid's only child — a truly empty grid proves the widget rendered
    # nothing, not just that it rendered small.
    assert page.locator("#host .home-grid .home-card").count() == 0


def test_home_panel_still_shows_the_widget_when_suggestions_remain(page):
    page.evaluate(_STUB_FETCH, _SUGGESTIONS)
    page.evaluate(_MOUNT_HOME, {"perms": ["admin"], "layout": ["setup-suggestions"]})
    card = page.locator("#host .home-grid .home-card[data-widget-id='setup-suggestions']")
    card.wait_for(state="attached", timeout=5000)
    assert card.count() == 1
    # Its per-row dismiss control (the tile's own behavior, proven above)
    # made it through the real panel pipeline intact.
    assert card.locator("[data-dismiss='welcome']").count() == 1


def test_a_failed_suggestions_fetch_does_not_read_as_all_done(page):
    page.evaluate("""
      () => {
        window.fetch = async (url) => {
          const href = String(url);
          if (href.includes('/api/help/suggestions')) {
            return new Response(
              JSON.stringify({ detail: 'boom' }),
              { status: 500, headers: { 'Content-Type': 'application/json' } },
            );
          }
          return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
        };
      }
    """)
    page.evaluate(_MOUNT_HOME, {"perms": ["admin"], "layout": ["setup-suggestions"]})
    # An empty grid has no size, so it counts as "hidden" under Playwright's
    # default visibility check — wait for it to exist in the DOM, not for it
    # to have a non-zero box (that's exactly what the empty-grid test wants
    # to prove is absent).
    page.wait_for_selector("#host .home-grid", state="attached")
    # Same empty suggestions list a genuine "nothing outstanding" response
    # produces, but arrived at via a fetch failure — the card must still be
    # there, distinguishing "couldn't check" from "all set".
    assert page.locator("#host .home-grid .home-card[data-widget-id='setup-suggestions']").count() == 1


def test_reorder_does_not_drop_a_hidden_widget_from_the_saved_layout(page):
    page.evaluate(_STUB_FETCH, _NO_SUGGESTIONS)  # keeps setup-suggestions hidden
    page.evaluate(_MOUNT_HOME, {
        "perms": ["admin"],
        "layout": ["home-messages", "setup-suggestions", "home-presence"],
    })
    # An empty grid has no size, so it counts as "hidden" under Playwright's
    # default visibility check — wait for it to exist in the DOM, not for it
    # to have a non-zero box (that's exactly what the empty-grid test wants
    # to prove is absent).
    page.wait_for_selector("#host .home-grid", state="attached")

    # Confirm the setup is what it claims before reordering around it: two
    # visible cards, setup-suggestions hidden.
    assert page.locator(
        "#host .home-grid .home-card[data-widget-id='setup-suggestions']"
    ).count() == 0
    visible_before = page.evaluate(
        "() => [...document.querySelectorAll("
        "  '#host .home-grid .home-card[data-widget-id]'"
        ")].map((c) => c.dataset.widgetId)"
    )
    assert visible_before == ["home-messages", "home-presence"]

    page.click("#host .home-edit-toggle")
    page.wait_for_selector("#host .home-grid .home-card[draggable]")

    page.evaluate(_DRAG_REORDER, ["home-messages", "home-presence"])

    # onReorder → saveLayout → render() is synchronous JS, but give it a
    # tick and assert on what got *persisted*, not just what's on screen —
    # the bug this guards drops the hidden widget from the saved layout,
    # which a screen-only check wouldn't catch.
    page.wait_for_function(
        "() => {"
        "  try {"
        "    const raw = localStorage.getItem('dk_layout_0');"
        "    if (!raw) return false;"
        "    const w = JSON.parse(raw).widgets;"
        "    return w.length && w[0] !== 'home-messages';"
        "  } catch (_) { return false; }"
        "}",
        timeout=5000,
    )
    saved = page.evaluate(
        "() => JSON.parse(localStorage.getItem('dk_layout_0')).widgets"
        ".map((e) => typeof e === 'string' ? e : e.id)"
    )
    assert "setup-suggestions" in saved, (
        "reordering while setup-suggestions was hidden (nothing left to "
        "suggest) dropped it from the saved layout entirely"
    )
    assert saved.index("setup-suggestions") == 1, (
        "the hidden widget must be re-inserted at its original position, "
        f"not just appended or left wherever — got {saved!r}"
    )
    assert saved[0] == "home-presence" and saved[2] == "home-messages", (
        f"the visible widgets around it should still have been reordered — got {saved!r}"
    )
