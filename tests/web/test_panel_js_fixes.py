"""Browser gate for the panel-layer fixes from the 2026-08-06 website deep review.

Panels are mounted directly against a stubbed ``window.fetch`` rather than
through the router: the test environment runs no bot, so the real
``/api/config`` and ``/api/meta/*`` calls 503 and nothing would render. The stub
lets each test drive the exact server state the bug needed.

What is covered, and why each one is here:

  * **F2 — stored XSS in config-cleanup.** Auto-delete schedules can target
    *threads*, and a thread name is set by whichever member opened it. The
    schedule's heading went into ``innerHTML`` unescaped, so the name executed
    in an admin's dashboard on every page view.
  * **F1 — the permanent spinner.** ~27 config panels ran their loader in a bare
    ``(async () => {…})()`` with no ``.catch``: one failed fetch left "Loading
    configuration…" up forever plus an unhandled rejection. ``mountAsync``
    renders a real error state with a Try again button instead.
  * **F4 — a save eating a sibling card's edits.** Multi-card panels re-fetch
    and rebuild *every* card after a successful save; anything typed into
    another card was silently discarded, and the unsaved-changes guard couldn't
    warn because the save had just cleared the dirty flag.
  * **config-prune's preview row.** The row was dropped whether or not the
    exemption write landed, so a failed write read as success while the member
    stayed removable.
  * **config-spoiler's blank threshold.** ``Number("")`` is 0, and a
    0 confidence threshold makes the classifier judge *everything* explicit.

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
        db = tmp / "panel-js.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("panel-js"))
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


# A thread name any member can set. `onerror` on a broken image needs no
# interaction, so merely rendering the panel is the whole exploit.
_EVIL_NAME = '<img src=x onerror="window.__pwned = true">'

# Installs a fetch stub driven by a {url-substring: response} routing table, and
# records every write so a test can assert what the panel actually sent. Each
# entry is {status, body}; a missing route answers 200 with `{}`.
_STUB_FETCH = """
(routes) => {
  window.__pwned = false;
  window.__writes = [];
  window.fetch = async (url, opts = {}) => {
    const href = String(url);
    const method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET') window.__writes.push({ method, href, body: opts.body || null });
    // Longest needle first: "/api/config/prune/preview" must win over the
    // "/api/config" entry it contains.
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
  // Pickers and post-render wiring settle a tick after the loader resolves.
  await new Promise((r) => setTimeout(r, 60));
  return true;
}
"""


def _mount(page, module: str, routes: dict) -> None:
    page.evaluate(_STUB_FETCH, routes)
    page.evaluate(_MOUNT, {"module": module})


def _fresh_meta(page) -> None:
    """Drop config-helpers' module caches so each test's stub is the one used."""
    page.evaluate(
        "async () => (await import('/static/js/config-helpers.js')).resetMetaCaches()"
    )


# ── F2: config-cleanup renders a member-supplied thread name ─────────────


_CLEANUP_ROUTES = {
    "/api/meta/channels": {
        "body": [{"id": "900000000000000001", "name": _EVIL_NAME, "type": "text"}],
    },
    "/api/config": {
        "body": {
            "bulk_cleanup": {},
            "auto_delete": [
                {
                    "channel_id": "900000000000000001",
                    "max_age_seconds": 86400,
                    "interval_seconds": 86400,
                    "media_only": False,
                    "last_run_ts": None,
                }
            ],
        }
    },
}


def test_config_cleanup_escapes_a_member_supplied_thread_name(page):
    """F2. Fails against the pre-fix panel: the `<img>` lands in the schedule
    heading, `onerror` fires, and the flag is set — stored XSS in an admin's
    session from nothing but opening Config → Cleanup."""
    _fresh_meta(page)
    _mount(page, "/static/js/panels/config-cleanup.js", _CLEANUP_ROUTES)

    out = page.evaluate(
        """() => {
          const label = document.querySelector('#host [data-channel] .section-label');
          return {
            imgs: document.querySelectorAll('#host .section-label img').length,
            pwned: window.__pwned === true,
            text: label ? label.textContent : null,
          };
        }"""
    )
    assert out["text"] is not None, "the schedule card did not render at all"
    assert out["imgs"] == 0, "the thread name was parsed as HTML"
    assert not out["pwned"], "injected onerror handler executed"
    assert _EVIL_NAME in out["text"], "the name must still be readable, verbatim"


# ── F1: a failed first fetch is an error state, not a forever-spinner ────


def test_failed_load_renders_an_error_state_with_a_retry(page):
    """F1, through a converted panel (config-welcome).

    Before the conversion the rejection escaped the un-awaited IIFE: the shell's
    "Loading configuration…" stayed up forever and the only trace was an
    unhandled rejection in the console.
    """
    _fresh_meta(page)
    _mount(
        page,
        "/static/js/panels/config-welcome.js",
        {"/api/config": {"status": 503, "body": {"detail": "bot not ready"}}},
    )

    out = page.evaluate(
        """() => ({
          text: document.getElementById('host').textContent,
          retries: document.querySelectorAll('#host [data-retry]').length,
        })"""
    )
    assert out["retries"] == 1, "no Try again button — the user has no way forward"
    assert "Couldn" in out["text"] and "welcome settings" in out["text"], out["text"]
    assert "Loading configuration" not in out["text"], "spinner never cleared"


def test_a_successful_load_still_returns_a_usable_handle(page):
    """mountAsync's handle must stay synchronous — app.js calls unmount() on it
    directly, so returning a promise from mount() would break every teardown."""
    _fresh_meta(page)
    page.evaluate(_STUB_FETCH, {"/api/config": {"body": {"welcome": {}}}})
    out = page.evaluate(
        """async () => {
          document.body.innerHTML = '<div id="host"></div>';
          const mod = await import('/static/js/panels/config-welcome.js');
          const handle = mod.mount(document.getElementById('host'));
          return {
            hasUnmount: typeof handle?.unmount === 'function',
            hasReady: typeof handle?.ready?.then === 'function',
          };
        }"""
    )
    assert out == {"hasUnmount": True, "hasReady": True}


# ── F4: a save must not rebuild over a sibling card's unsaved edits ──────


_CARD_DIRT = """
async ({ dirtySibling }) => {
  document.body.innerHTML = `
    <div id="host">
      <form data-card="a"><input name="x" /></form>
      <form data-card="b"><input name="x" /></form>
    </div>`;
  const cfg = await import('/static/js/config-helpers.js');
  const root = document.getElementById('host');
  const [a, b] = root.querySelectorAll('[data-card]');
  cfg.trackCard(a);
  cfg.trackCard(b);

  // The user types into card B, then saves card A.
  if (dirtySibling) {
    const inp = b.querySelector('input');
    inp.value = 'half-typed rule';
    inp.dispatchEvent(new Event('input', { bubbles: true }));
  }

  let rebuilt = 0;
  const didRerender = cfg.rerenderUnlessDirty(root, a, () => {
    rebuilt += 1;
    root.innerHTML = '<div>rebuilt from server</div>';
  });
  return {
    didRerender,
    rebuilt,
    survivingText: b.isConnected ? b.querySelector('input').value : null,
  };
}
"""


def test_save_holds_the_rebuild_while_a_sibling_card_is_dirty(page):
    """F4. The rebuild that used to run unconditionally is what discarded the
    other card's edits — and the dirty flag couldn't warn, because the save had
    just cleared it."""
    out = page.evaluate(_CARD_DIRT, {"dirtySibling": True})
    assert out["didRerender"] is False
    assert out["rebuilt"] == 0
    assert out["survivingText"] == "half-typed rule"


def test_save_rebuilds_normally_when_nothing_else_is_dirty(page):
    """The guard must not become "never refresh": with no pending edits the
    panel still picks up the server's version of every card."""
    out = page.evaluate(_CARD_DIRT, {"dirtySibling": False})
    assert out["didRerender"] is True
    assert out["rebuilt"] == 1


def test_a_saved_cards_own_edits_stop_blocking_the_rebuild(page):
    """clearCardDirty runs on the card that just saved, or its own typing would
    veto every refresh forever."""
    out = page.evaluate(
        """async () => {
          document.body.innerHTML = '<div id="host"><form data-c></form></div>';
          const cfg = await import('/static/js/config-helpers.js');
          const root = document.getElementById('host');
          const card = cfg.trackCard(root.querySelector('[data-c]'));
          card.dispatchEvent(new Event('input', { bubbles: true }));
          let rebuilt = 0;
          const did = cfg.rerenderUnlessDirty(root, card, () => { rebuilt += 1; });
          return { did, rebuilt };
        }"""
    )
    assert out == {"did": True, "rebuilt": 1}


# ── config-prune: the preview row and the exemption write ────────────────


_PRUNE_ROUTES_BASE = {
    "/api/meta/roles": {"body": [{"id": "800000000000000001", "name": "Verified"}]},
    "/api/meta/members": {
        "body": [
            {
                "id": "700000000000000001",
                "name": "quietperson",
                "display_name": "Quiet Person",
                "left_server": False,
            }
        ]
    },
    "/api/config": {
        "body": {
            "prune": {
                "role_id": "800000000000000001",
                "inactivity_days": 30,
                "exemptions": [],
            }
        }
    },
}


def _prune_preview_state(page, exemption_status: int) -> dict:
    routes = dict(_PRUNE_ROUTES_BASE)
    routes["/api/config/prune/preview"] = {
        "body": {
            "candidates": [
                {
                    "id": "700000000000000001",
                    "name": "Quiet Person",
                    "days_inactive": 91,
                    "last_activity_ts": 1_700_000_000,
                }
            ],
            "role_member_count": 4,
            "considered_count": 4,
        }
    }
    routes["/api/config/prune/exemptions/"] = {
        "status": exemption_status,
        "body": {"detail": "role is gone"},
    }
    _fresh_meta(page)
    _mount(page, "/static/js/panels/config-prune.js", routes)
    page.evaluate("() => document.querySelector('#host [data-preview-btn]').click()")
    page.wait_for_selector("#host [data-exempt-from-preview]")
    page.evaluate(
        "() => document.querySelector('#host [data-exempt-from-preview]').click()"
    )
    page.wait_for_timeout(150)
    return page.evaluate(
        """() => ({
          rows: document.querySelectorAll('#host [data-exempt-from-preview]').length,
          chips: document.querySelectorAll('#host [data-remove-exempt]').length,
        })"""
    )


def test_prune_keeps_the_preview_row_when_the_exemption_write_fails(page):
    """Fails against the pre-fix panel, which removed the row unconditionally:
    the screen said "exempt" while the member was still removable by the next
    scheduled sweep."""
    out = _prune_preview_state(page, exemption_status=500)
    assert out["rows"] == 1, "the row was dropped even though the write failed"
    assert out["chips"] == 0, "a failed write must not add an exemption chip"


def test_prune_drops_the_preview_row_once_the_write_lands(page):
    """Control: the row still goes away on a write that actually succeeded, and
    the member turns up in the exempt chip list."""
    out = _prune_preview_state(page, exemption_status=200)
    assert out["rows"] == 0
    assert out["chips"] == 1


# ── config-spoiler: a blanked threshold must not post as 0 ───────────────


_SPOILER_ROUTES = {
    "/api/meta/channels": {
        "body": [{"id": "600000000000000001", "name": "general", "type": "text"}]
    },
    "/api/nsfw-classifier/metrics": {"body": {"classified": 0}},
    "/api/config": {
        "body": {
            "spoiler": {"spoiler_required_channels": []},
            "nsfw_classifier": {
                "threshold": 0.7,
                "sfw_threshold": 0.9,
                # The panel validates all three thresholds before it posts, so
                # a field missing here renders value="undefined" and blocks the
                # save — which is what a stale fixture looks like from the
                # test's side. GET /api/config always sends this one.
                "chest_floor": 0.4,
                "sfw_mode": "log",
                "sfw_log_channel_id": "0",
                "sfw_exempt_channels": [],
                "observe_age_gated": False,
            },
        }
    },
}


def _submit_spoiler_with_threshold(page, value: str) -> dict:
    _fresh_meta(page)
    _mount(page, "/static/js/panels/config-spoiler.js", _SPOILER_ROUTES)
    # A dispatched submit rather than requestSubmit(): the latter runs the
    # browser's own constraint validation first, which would mask the panel's
    # guard on an out-of-range value (and is bypassed anyway by anything that
    # submits the form from script).
    page.evaluate(
        """(v) => {
          const el = document.querySelector('#host [data-field="threshold"]');
          el.value = v;
          document.querySelector('#host [data-form]').dispatchEvent(
            new Event('submit', { bubbles: true, cancelable: true }),
          );
        }""",
        value,
    )
    page.wait_for_timeout(150)
    return page.evaluate(
        """() => ({
          writes: window.__writes.filter((w) => w.href.includes('nsfw-classifier')),
          status: document.querySelector('#host .save-status')?.textContent || '',
        })"""
    )


def test_spoiler_refuses_a_blank_confidence_threshold(page):
    """`Number("")` is 0, and a 0 threshold makes the classifier judge every
    image explicit. The pre-fix panel posted it without comment."""
    out = _submit_spoiler_with_threshold(page, "")
    assert out["writes"] == [], "a blank threshold was still posted"
    assert "Confidence Threshold" in out["status"], out["status"]


def test_spoiler_refuses_an_out_of_range_threshold(page):
    """Same guard, the other end: the input's own min/max are advisory only
    once the form is submitted programmatically or the value is pasted."""
    out = _submit_spoiler_with_threshold(page, "5")
    assert out["writes"] == []
    assert "Confidence Threshold" in out["status"]


def test_spoiler_posts_a_valid_threshold(page):
    """Control: the validation must not block an ordinary save."""
    out = _submit_spoiler_with_threshold(page, "0.55")
    assert len(out["writes"]) == 1, out
    assert '"threshold":0.55' in out["writes"][0]["body"].replace(" ", "")


# ── games-config: the audit channel can be cleared again ────────────────


_GAMES_CONFIG_ROUTES = {
    "/api/meta/channels": {
        "body": [{"id": "500000000000000001", "name": "mod-log", "type": "text"}]
    },
    "/api/meta/roles": {"body": [{"id": "500000000000000009", "name": "Host"}]},
    "/api/games/config/channels": {"body": {"channels": []}},
    "/api/games/config/editor-role": {"body": {"role_id": None}},
    "/api/games/config/audit": {"body": {"channel_id": "500000000000000001"}},
}


def test_games_config_clears_the_audit_channel_with_a_delete(page):
    """The hint above this control has always said the audit channel may be left
    unset, but Save rejected "(none)" with "Pick a channel first" — so once one
    was chosen there was no way to stop recording. CLAUDE.md's "never ship a
    preference that isn't enforced"."""
    _fresh_meta(page)
    _mount(page, "/static/js/panels/games-config.js", _GAMES_CONFIG_ROUTES)
    # Pick "(none)" the way a user does: focus the audit picker, then mousedown
    # its empty row (the widget listens on mousedown so the input's blur can't
    # close the list first).
    picked = page.evaluate(
        """() => {
          const sections = Array.from(document.querySelectorAll('#host section'));
          const audit = sections.find(
            (s) => s.querySelector('[data-action="save-audit"]'),
          );
          const input = audit.querySelector('.filter-select input');
          input.dispatchEvent(new FocusEvent('focus'));
          const empty = audit.querySelector('.filter-select-item[data-id="0"]');
          if (!empty) return false;
          empty.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
          audit.querySelector('[data-action="save-audit"]').click();
          return true;
        }"""
    )
    assert picked, 'the audit picker has no "(none)" row to choose'
    page.wait_for_timeout(200)
    out = page.evaluate(
        """() => ({
          deletes: window.__writes.filter(
            (w) => w.method === 'DELETE' && w.href.includes('/api/games/config/audit'),
          ).length,
          puts: window.__writes.filter(
            (w) => w.method === 'PUT' && w.href.includes('/api/games/config/audit'),
          ).length,
          status: document.querySelector('#host [data-status="audit"]').textContent,
        })"""
    )
    assert out["deletes"] == 1, f"no DELETE issued; status was {out['status']!r}"
    assert out["puts"] == 0, "cleared audit channel was still PUT"
    assert "Pick a channel first" not in out["status"]
    assert "Cleared" in out["status"], out["status"]


# ── The Gini panel that rendered nothing but its empty state ─────────────

# The panel guarded on `(d.tiers || []).length`. `tiers` is an object keyed by
# tier name, so `.length` is undefined and the guard was always true — the
# panel showed nothing but its empty state from 2026-07-23, over a server with
# 41k messages, and raised no console error to give it away.
#
# Fixed on main independently and better: `posters` is the count of distinct
# authors in the window, which is exactly what the empty state claims, where a
# sum over the tiers only approximated it. Compared against 0 rather than
# falsy, so a payload cached before the field existed still renders.
_GINI_BODY = {
    "gini": 0.62,
    "posters": 53,
    "badge": "healthy",
    "tiers": {"lurker": 4, "light": 30, "moderate": 12, "active": 5, "power": 2},
    "lorenz": [[0, 0], [50, 20], [100, 100]],
    "sparkline": [0.6, 0.61, 0.62, 0.62],
    "gini_history": [],
    "top_share": 41.0,
    "median_msgs": 7,
    "contributors": 53,
}


def test_gini_renders_its_data_instead_of_the_empty_state(page):
    """Fails against the pre-fix panel, which showed the empty state for a
    month with no console error to give it away."""
    _mount(page, "/static/js/panels/health-gini.js",
           {"/api/health/gini": {"body": _GINI_BODY}})
    html = page.inner_html("#host")
    assert "No messages in the last 30 days" not in html, (
        "panel still short-circuits to its empty state with data present"
    )
    assert "0.62" in html, "the headline Gini figure never rendered"


def test_gini_still_shows_the_empty_state_when_nobody_posted(page):
    """The guard has to keep working — the fix is a real emptiness test, not
    its removal."""
    body = {**_GINI_BODY, "posters": 0,
            "tiers": {"lurker": 0, "light": 0, "moderate": 0,
                      "active": 0, "power": 0}}
    _mount(page, "/static/js/panels/health-gini.js",
           {"/api/health/gini": {"body": body}})
    assert "No messages in the last 30 days" in page.inner_html("#host")


# ── Comboboxes announce the field they sit in ────────────────────────────


def test_a_picker_takes_its_name_from_the_field_it_sits_in(page):
    """A picker's slot is replaced by the widget, so `field()` can never pair
    the visible <label> with it by id. Callers that forgot `label` shipped a
    combobox whose only accessible name was its placeholder."""
    name = page.evaluate("""
      async () => {
        const h = await import('/static/js/config-helpers.js');
        document.body.innerHTML = '<div id="host"></div>';
        const slot = document.createElement('span');
        const fieldEl = h.field('Starboard Channel', slot, 'where posts go');
        document.getElementById('host').appendChild(fieldEl);
        h.mountChannelPicker(slot, [], '0');
        return document.querySelector('#host input[role="combobox"]')
                 ?.getAttribute('aria-label');
      }
    """)
    assert name == "Starboard Channel", f"combobox announced {name!r}"


def test_an_explicit_label_still_wins_over_the_derived_one(page):
    name = page.evaluate("""
      async () => {
        const h = await import('/static/js/config-helpers.js');
        document.body.innerHTML = '<div id="host"></div>';
        const slot = document.createElement('span');
        document.getElementById('host').appendChild(h.field('Visible', slot));
        h.mountChannelPicker(slot, [], '0', { label: 'Explicit' });
        return document.querySelector('#host input[role="combobox"]')
                 ?.getAttribute('aria-label');
      }
    """)
    assert name == "Explicit"


# ── One dirty bit per form, not per page ────────────────────────────────


_TWO_FORMS = """
  async () => {
    const h = await import('/static/js/config-helpers.js');
    window.__dkDirtyReset();
    document.body.innerHTML =
      '<form id="a"><input name="x"><span id="sa"></span></form>' +
      '<form id="b"><input name="y"><span id="sb"></span></form>';
    const a = h.guardForm(document.getElementById('a'));
    const b = h.guardForm(document.getElementById('b'));
    // Type in both, so both hold unsaved edits.
    for (const f of [a, b]) {
      f.querySelector('input').value = 'edited';
      f.querySelector('input').dispatchEvent(new Event('input', { bubbles: true }));
    }
    const bothDirty = window.__dkDirty();
    // Save only form A.
    h.showStatus(document.getElementById('sa'), true, 'Saved');
    return { bothDirty, stillDirty: window.__dkDirty() };
  }
"""


def test_saving_one_form_leaves_a_sibling_forms_warning_armed(page):
    """Fails against the pre-fix helper: `_dirty` was one module global that any
    successful save cleared, so form B's unsaved edits stopped being guarded."""
    r = page.evaluate(_TWO_FORMS)
    assert r["bothDirty"] is True, "editing two guarded forms did not mark the page dirty"
    assert r["stillDirty"] is True, (
        "saving form A disarmed the unsaved-edits warning protecting form B"
    )


def test_saving_the_last_dirty_form_does_clear_the_warning(page):
    """The flag must still clear, or every navigation prompts forever."""
    cleared = page.evaluate("""
      async () => {
        const h = await import('/static/js/config-helpers.js');
        window.__dkDirtyReset();
        document.body.innerHTML = '<form id="a"><input name="x"><span id="sa"></span></form>';
        const a = h.guardForm(document.getElementById('a'));
        a.querySelector('input').dispatchEvent(new Event('input', { bubbles: true }));
        h.showStatus(document.getElementById('sa'), true, 'Saved');
        return window.__dkDirty();
      }
    """)
    assert cleared is False


def test_a_status_element_outside_any_guarded_form_still_clears_everything(page):
    """The fallback: with nothing to attribute the save to, keep the old
    page-wide clear rather than leave a panel permanently claiming edits."""
    cleared = page.evaluate("""
      async () => {
        const h = await import('/static/js/config-helpers.js');
        window.__dkDirtyReset();
        document.body.innerHTML =
          '<form id="a"><input name="x"></form><span id="loose"></span>';
        const a = h.guardForm(document.getElementById('a'));
        a.querySelector('input').dispatchEvent(new Event('input', { bubbles: true }));
        h.showStatus(document.getElementById('loose'), true, 'Saved');
        return window.__dkDirty();
      }
    """)
    assert cleared is False


# ── audit cells are readable without a tooltip ──────────────────────────


_LONG = "x" * 900


def test_a_short_reason_needs_no_expander(page):
    """Every reason, note, filename and details value in prod is under 85
    characters, so three lines shows all of them outright. An expander on a
    cell that is not cut would be noise."""
    out = page.evaluate("""
      async () => {
        const { initClampCells } = await import('/static/js/clamp-cell.js');
        document.body.innerHTML =
          '<table class="data-table"><tbody><tr>' +
          '<td class="reason-cell">spamming the same link in four channels</td>' +
          '</tr></tbody></table>';
        initClampCells(document.body);
        const cell = document.querySelector('.reason-cell');
        return {
          wrapped: !!cell.querySelector('.clamp'),
          hasButton: !!cell.querySelector('.clamp-more'),
          text: cell.querySelector('.clamp').textContent,
        };
      }
    """)
    assert out["wrapped"] is True
    assert out["hasButton"] is False, "an uncut cell should not offer More"
    assert "four channels" in out["text"]


def test_a_long_confession_expands_from_the_keyboard(page):
    """The value the page exists to show was reachable only through a title
    attribute — invisible to touch, unreachable by keyboard, not selectable.
    A native button gets Enter and Space without a role or a keydown handler."""
    out = page.evaluate("""
      async (long) => {
        const { initClampCells } = await import('/static/js/clamp-cell.js');
        document.body.innerHTML =
          '<table class="data-table"><tbody><tr>' +
          '<td class="reason-cell">' + long + '</td></tr></tbody></table>';
        initClampCells(document.body);
        const cell = document.querySelector('.reason-cell');
        const btn = cell.querySelector('.clamp-more');
        if (!btn) return { hasButton: false };
        const before = btn.getAttribute('aria-expanded');
        btn.focus();
        const focused = document.activeElement === btn;
        btn.click();
        return {
          hasButton: true, before, focused,
          after: btn.getAttribute('aria-expanded'),
          open: cell.classList.contains('is-open'),
          label: btn.textContent,
          tag: btn.tagName,
          fullTextPresent: cell.querySelector('.clamp').textContent.length,
        };
      }
    """, _LONG)
    assert out["hasButton"] is True, "a 900-character cell was not given an expander"
    assert out["tag"] == "BUTTON", "must be a real button, for free Enter/Space"
    assert out["focused"] is True
    assert (out["before"], out["after"]) == ("false", "true")
    assert out["open"] is True
    assert out["label"] == "Less"
    # The whole value is in the DOM, not a 120-character slice of it.
    assert out["fullTextPresent"] == 900


def test_the_expander_does_not_open_the_row_behind_it(page):
    """mod-policy-tickets binds a delegated tbody click that opens the
    transcript modal for anything inside the row. Without stopPropagation,
    reading a cell launches a modal over the table you were reading."""
    fired = page.evaluate("""
      async (long) => {
        const { initClampCells } = await import('/static/js/clamp-cell.js');
        document.body.innerHTML =
          '<table class="data-table"><tbody><tr class="clickable-row">' +
          '<td class="reason-cell">' + long + '</td></tr></tbody></table>';
        initClampCells(document.body);
        let rowClicks = 0;
        document.querySelector('tbody').addEventListener('click', () => { rowClicks++; });
        document.querySelector('.clamp-more').click();
        return rowClicks;
      }
    """, _LONG)
    assert fired == 0, "expanding a cell also triggered the row's click handler"


def test_reinitialising_after_a_rerender_does_not_double_wrap(page):
    """auditPanel calls this on every refresh."""
    depth = page.evaluate("""
      async () => {
        const { initClampCells } = await import('/static/js/clamp-cell.js');
        document.body.innerHTML =
          '<table class="data-table"><tbody><tr>' +
          '<td class="reason-cell">a reason</td></tr></tbody></table>';
        initClampCells(document.body);
        initClampCells(document.body);
        return document.querySelectorAll('.reason-cell .clamp').length;
      }
    """)
    assert depth == 1


def test_the_clamp_still_wraps_at_phone_width(browser, dashboard):
    """app.css sets `.data-table { white-space: nowrap }` under 768px so wide
    tables scroll sideways, and white-space inherits. Without an explicit
    override on the clamp wrapper the three-line clamp collapses to one line
    running off the side — worse than the tooltip it replaced.

    Measured: 4 lines with the override, one sideways line without it. The
    existing mobile layout scan does NOT catch this (it passes either way),
    which is why the check lives here.
    """
    ctx = browser.new_context(viewport={"width": 390, "height": 700})
    page = ctx.new_page()
    try:
        page.goto(f"{dashboard.base}/", wait_until="domcontentloaded")
        result = page.evaluate("""
          () => {
            document.body.innerHTML =
              '<table class="data-table"><tbody><tr><td class="reason-cell">' +
              'the member kept reposting the same referral link '.repeat(6) +
              '</td></tr></tbody></table>';
            const td = document.querySelector('.reason-cell');
            const clamp = document.createElement('div');
            clamp.className = 'clamp';
            while (td.firstChild) clamp.append(td.firstChild);
            td.append(clamp);
            const cs = getComputedStyle(clamp);
            return {
              whiteSpace: cs.whiteSpace,
              runsSideways: clamp.scrollWidth > clamp.clientWidth + 1,
            };
          }
        """)
        assert result["whiteSpace"] == "normal", (
            "the phone rule's nowrap reached the clamp; it will render one line"
        )
        assert result["runsSideways"] is False
    finally:
        ctx.close()

