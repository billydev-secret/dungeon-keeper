"""Responsive-layout gate: no dashboard panel may hide content off-screen.

Drives the real dashboard in headless Chromium (via Playwright) at phone,
tablet and desktop widths and asserts three things about every panel:

  * nothing extends past the viewport's right edge unless it sits in a genuinely
    scrollable box (a wide data table is fine — the user can scroll to it);
  * no ``overflow-x: hidden`` container is clipping content wider than itself
    (that content is simply gone — this is the announcement-button bug that
    prompted the whole check);
  * no text is broken mid-word repeatedly, i.e. its column has collapsed towards
    one character wide. The first two rules only catch content that is too
    *wide*; this catches the opposite failure, where the layout implodes instead
    (the mod-engagement bug — see the note on KNOWN_OVERFLOW).

All three signals, and the in-page audit script, are shared with
``scripts/mobile_layout_scan.py`` so the gate and the diagnostic tool can never
disagree. The scanner is the tool for *measuring* noise across all panels; this
file is the tool for *failing the build* when it regresses.

Opt-in via the ``browser`` marker (excluded from the default run in
pyproject.toml); also tagged ``mobile`` so `-m mobile` runs just this suite.
Auto-skips where Playwright or Chromium isn't installed, so the ordinary
functional suite — and CI without a browser — is unaffected.

Scope:
  * ``PANEL_SCOPE`` (comma-separated ids) limits the sweep — gate.py --quick
    sets it to just the panels whose assets changed. Unset ⇒ every panel.
  * ``PANEL_VIEWPORTS`` (comma-separated of phone|tablet|desktop) limits widths.
    Unset ⇒ all three.
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = [pytest.mark.browser, pytest.mark.mobile]

# ── availability guard — skip the whole module if the browser stack is absent ──

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="Playwright not installed (pip install playwright && playwright install chromium)",
)

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
import sys  # noqa: E402

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from mobile_layout_scan import (  # noqa: E402
    AUDIT_JS,
    CLIP_SLOP,
    VIEWPORTS,
    _goto_panel,
    _settle,
    audit_on_fresh_context,
    enumerate_panels,
    serve,
)

from tests.db_template import migrated_db  # noqa: E402


def _chromium_available() -> bool:
    """True if a Chromium build is actually installed (import alone isn't enough)."""
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


# ── the served dashboard + browser, once for the module ────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, tmp: Path):
        db = tmp / "mobile.db"
        # Module-scoped: the per-test reaper must not delete this DB while
        # the server is still using it for later tests in the module.
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("mobile"))
    # give uvicorn a beat to bind before the first navigation
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


# ── which panels / viewports ───────────────────────────────────────────────────

def _selected_viewports() -> list[str]:
    raw = os.environ.get("PANEL_VIEWPORTS", "").strip()
    if not raw:
        return list(VIEWPORTS)
    picked = [v.strip() for v in raw.split(",") if v.strip() in VIEWPORTS]
    return picked or list(VIEWPORTS)


def _panel_ids(browser, base: str) -> list[str]:
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


def _describe(items: list[dict]) -> str:
    def _one(it: dict) -> str:
        if "lines" in it:  # collapsed — px is not the useful number here
            return f"{it['sel']} ({it['words']} words over {it['lines']} lines, {it['width']}px)"
        return f"{it['sel']} (+{it.get('by', it.get('hides'))}px)"

    return "; ".join(_one(it) for it in items[:6])


def _faults(res: dict) -> list[str]:
    """The three audit signals rendered as human-readable fault strings."""
    out = []
    if res["viewport"]:
        out.append(f"off-screen — {_describe(res['viewport'])}")
    if res["clipped"]:
        out.append(f"clipped — {_describe(res['clipped'])}")
    if res.get("collapsed"):
        out.append(f"collapsed — {_describe(res['collapsed'])}")
    return out


# Pre-existing mobile debt, allowed to fail while it was being worked off — an
# **allowlist**, not a ratchet, so a listed panel could render clean without
# failing. It is now EMPTY: every panel is enforced, and any overflow or
# collapse fails the build.
#
# The six entries were cleared on 2026-07-28. What they turned out to be is
# worth keeping, because most of the notes were wrong:
#
#   health-mod-engagement  Annotated "a wide data table / card grid overflows its
#                          panel". Not an overflow, and no run had ever seen it:
#                          the sweep serves an empty DB, so the panel only ever
#                          rendered "No moderator messages in this window" — no
#                          tiles, no chart, no table. With data it laid the whole
#                          report out inside a leftover `.panel-loading` (a
#                          centering flex box), collapsing every heading to one
#                          character per line. Fixed in the panel; now covered
#                          with data by test_mod_engagement_populated_fits_on_
#                          phone and by the `collapsed` signal it prompted.
#   help-setup             Annotated "a long inline link overflows". It
#                          *collapsed*: `display: flex` on the step row made each
#                          inline link its own flex item, squeezed to 13px.
#   help-overview          Already fixed before this pass — the ~1195px table
#                          sits in an `overflow-x: auto` container. The note had
#                          outlived the bug.
#   config-ai              No longer reproduces at any width.
#   qa-tracker             No longer reproduces at any width.
#   wellness-caps          No longer reproduces at any width. Its "sized to the
#                          full width before the scrollbar appears" flap is what
#                          `_settle()` in mobile_layout_scan.py was written to
#                          cure, so the fix was in the measurement, not the CSS.
#
# Three of those six descriptions named the wrong mechanism and one named a bug
# the tool structurally could not observe. Treat any future entry as a
# hypothesis, not a measurement — re-measure before trusting it.
#
# qa-tracker and wellness-caps were the two historically borderline ones. They
# were clean at all three widths across five consecutive full sweeps before
# being removed, and wellness-caps' flap is attributable to a fixed cause
# (`_settle`), so this is not an expected-to-flake gate. Recorded only so that
# if one of them ever fails on a commit that did not touch it, that history is
# the first thing to check — confirm with the diagnostic tool and re-add the
# entry rather than chasing phantom CSS.
KNOWN_OVERFLOW: set[str] = set()


# ── the sweep ──────────────────────────────────────────────────────────────────

def test_no_panel_overflows(dashboard, browser):
    """Every in-scope panel, at every in-scope width, keeps its content on-screen.

    A panel in ``KNOWN_OVERFLOW`` is allowed to fail (pre-existing debt); any
    other panel that overflows fails the test — that's a new regression.
    """
    ids = _panel_ids(browser, dashboard.base)
    assert ids, "no panels enumerated from the nav — did the dashboard render?"
    viewports = _selected_viewports()

    failures: list[str] = []
    dirty: set[str] = set()  # panels that overflowed at some width this run
    for vp in viewports:
        for pid in ids:
            # Fresh context per panel — shared-context state bleed made borderline
            # panels flap clean/dirty between runs (see audit_on_fresh_context).
            res = audit_on_fresh_context(browser, dashboard.base, pid, VIEWPORTS[vp])
            faults = _faults(res)
            if faults:
                dirty.add(pid)
                if pid not in KNOWN_OVERFLOW:
                    failures.append(f"[{vp}] {pid}: " + "; ".join(faults))

    # Informational only — never a failure (these panels flap, see the comment on
    # KNOWN_OVERFLOW). A listed panel that renders clean *may* be fixed; confirm
    # with the diagnostic tool before deleting it from the set.
    tested = set(ids)
    stale = (KNOWN_OVERFLOW & tested) - dirty
    if stale:
        print("\n[mobile] KNOWN_OVERFLOW panels that rendered clean this run "
              f"(verify + prune if fixed): {', '.join(sorted(stale))}")

    assert not failures, "Responsive layout faults:\n" + "\n".join(failures)


# ── interaction scenarios: states a plain page load never reaches ───────────────


def _assert_fits(res, label: str) -> None:
    """Fail with the same fault description the panel sweep uses."""
    faults = _faults(res)
    assert not faults, f"{label} does not lay out on phone:\n" + "\n".join(faults)


def test_announcement_button_editor_fits_on_phone(dashboard, browser):
    """Open the announcement editor and add role-button rows — the exact flow
    that shipped broken. A plain page-load never reaches this state, so it needs
    its own scenario."""
    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        _goto_panel(page, f"{dashboard.base}/#/announcements")
        page.wait_for_timeout(400)
        page.click('[data-action="new"]')
        page.wait_for_timeout(300)
        # Two rows: enough for the flex row to have to wrap.
        page.click('[data-action="add-button"]')
        page.wait_for_timeout(150)
        page.click('[data-action="add-button"]')
        _settle(page)
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
    finally:
        context.close()
    _assert_fits(res, "Announcement button editor")


_SWEEP_PREVIEW_STUB = {
    "threshold_days": 30,
    "sweep_cap": 2,
    "inactive_channel_configured": False,
    "eligible_count": 2,
    "blocked_count": 1,
    "members": [
        {
            "user_id": "1234567890123456789",
            "display_name": "a-fairly-long-display-name",
            "days_idle": 412.5,
            "last_seen_ts": 1700000000,
            "has_tracked_messages": False,
            "removed_role_count": 4,
            "removed_role_names": [
                "Verified Member", "Event Volunteer", "Screenshot Enjoyer", "Book Club",
            ],
            "kept_managed_role_names": ["Server Booster"],
        },
        {
            "user_id": "2234567890123456789",
            "display_name": "quiet",
            "days_idle": 40.0,
            "last_seen_ts": 1700000000,
            "has_tracked_messages": True,
            "removed_role_count": 0,
            "removed_role_names": [],
            "kept_managed_role_names": [],
        },
    ],
    "blocked": [
        {
            "user_id": "3234567890123456789",
            "display_name": "outranks-the-bot",
            "days_idle": 99.0,
            "last_seen_ts": 1700000000,
            "has_tracked_messages": True,
            "removed_role_count": 1,
            "removed_role_names": ["Staff"],
            "kept_managed_role_names": [],
        },
    ],
}


def test_inactive_sweep_preview_fits_on_phone(dashboard, browser):
    """The sweep preview's tables only exist after "Check Now" is pressed.

    A page load shows an empty card, so the widest thing this panel renders —
    two tables with an action column, plus an expanded role list — is invisible
    to the plain sweep. The API is stubbed because member data comes from the
    gateway, which the test dashboard has no connection to.
    """
    import json

    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        page.route(
            "**/api/config/inactive/preview",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_SWEEP_PREVIEW_STUB),
            ),
        )
        _goto_panel(page, f"{dashboard.base}/#/config-inactive")
        page.wait_for_timeout(400)
        page.click("[data-preview-btn]")
        page.wait_for_selector(".prune-preview-table")
        # Expand a role list — the detail row is the widest content on the page.
        page.click("[data-toggle-roles]")
        _settle(page)
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
    finally:
        context.close()
    _assert_fits(res, "Inactive sweep preview")


# Shape taken from prod (guild 1469491362444480666, read-only) and anonymised:
# display names there run 1–37 chars (p50 8, p90 15) and the busiest author
# posted ~11.9k messages in 30 days, so these names/volumes sit at or above the
# real worst case. No real member data is reproduced here.
_MOD_ENGAGEMENT_NAMES = [
    "Cordwainer Bibbleworth-Fanshawe III",   # 35 — near prod's 37-char longest
    "a-fairly-long-hyphenated-modname-xyz",  # 36 — unbroken, no spaces to wrap on
    "Fun Bag Fancier", "moth", "Quillon", "sparrowhawk", "Vex",
    "Marigold Thistlewaite", "nn", "Brackenreed",
]
_MOD_ENGAGEMENT_STUB = {
    "mods": [
        {
            "user_id": str(1469491362444480666 + i),
            "user_name": name,
            "public_messages": msgs,
            "initiations": int(msgs * 0.37),
            "channel_breadth": 24 - i,
            "unique_reach": reach,
            "reactions_received": msgs // 3,
            "replies_received": msgs // 5,
            "engagement_rate": round(0.53 - i * 0.04, 2),
            "newcomer_touchpoints": touch,
        }
        for i, (name, msgs, reach, touch) in enumerate(
            zip(
                _MOD_ENGAGEMENT_NAMES,
                [11886, 9672, 5129, 2887, 2410, 1204, 903, 610, 244, 97],
                [806, 731, 588, 402, 355, 210, 168, 96, 41, 12],
                [143, 118, 96, 62, 51, 33, 20, 11, 4, 1],
                strict=True,
            )
        )
    ],
    "days": 30,
    "total_public_messages": 35042,
    "avg_unique_reach": 340.9,
    "total_newcomer_touchpoints": 539,
    "engagement_gini": 0.412,
}


def test_mod_engagement_populated_fits_on_phone(dashboard, browser):
    """The mod-engagement report, *with data*, on a phone.

    This panel sat in KNOWN_OVERFLOW for a bug nobody had ever actually
    observed: the sweep serves a freshly-migrated (empty) DB, so the panel only
    ever rendered "No moderator messages in this window" — no stat tiles, no
    chart, no table. Everything that broke lived behind having rows, so the
    plain sweep scored it clean while the live dashboard rendered every heading
    one character per line inside a leftover ``.panel-loading`` flex box.

    The API is stubbed because the mod roster comes from the gateway, which the
    test dashboard has no connection to.
    """
    import json

    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        page.route(
            "**/api/health/mod-engagement*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_MOD_ENGAGEMENT_STUB),
            ),
        )
        _goto_panel(page, f"{dashboard.base}/#/health-mod-engagement")
        # Wait for the real report, not the loading state.
        page.wait_for_selector(".data-table", timeout=15_000)
        _settle(page)
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
        # The regression signature, asserted directly: the body must not still be
        # the centering spinner box, and the stat tiles must get real width.
        body_is_loader = page.evaluate(
            "() => document.querySelector('[data-body]').classList.contains('panel-loading')"
        )
        label_widths = page.evaluate(
            "() => [...document.querySelectorAll('.home-card-label')]"
            ".map(el => Math.round(el.getBoundingClientRect().width))"
        )
    finally:
        context.close()

    assert not body_is_loader, (
        "[data-body] still carries .panel-loading — that class is a centering "
        "flex box, so the whole report lays out as shrink-to-min-content items"
    )
    assert label_widths, "no stat tiles rendered — did the stub shape drift?"
    assert min(label_widths) > 200, (
        f"stat-tile headings collapsed: widths {label_widths} on a 390px phone"
    )
    _assert_fits(res, "Mod engagement report (populated)")


# A live-season survivor panel is invisible to the plain sweep: the test DB has
# no season, so the sweep only ever sees the create card. Stubbing the overview
# renders the full surface — season + simulator (year >= 2090), the week table
# (four settle buttons per row is the widest thing here), the roster, and the
# rules form — and the dial hints live behind a details toggle now, so opening
# one is part of the scenario (2026-08-18 formatting pass).
_SURVIVOR_SEASON_STUB = {
    "season": {
        "id": 1, "name": "The Golden League", "season_year": 2100,
        "status": "active",
        "config": {
            "channel_id": "0", "role_survivor_id": "0", "role_ghost_id": "0",
            "role_sole_survivor_id": "0", "buyin_coins": 100,
            "pot_seed": 10000, "ghost_pot_pct": 20,
            "gauntlet_fee_per_week": 50, "weekly_win_coins": 25,
            "strikes": 2, "tie_rule": "loss", "late_entry": "gauntlet",
            "missed_pick": "auto_assign", "max_auto_assigns": 3,
            "double_pick_start_week": 14, "double_pick_min_alive": 5,
            "wipeout_annul_through_week": 13, "accord_max_alive": 6,
            "ghost_streak": True, "slate_hour": 9, "lastcall_hour": 18,
            "reckoning_hour": 9,
        },
    },
    "players": [
        {"user_id": "1234567890123456789", "status": "alive",
         "strikes_used": 1, "eliminated_week": None},
        {"user_id": "2234567890123456789", "status": "ghost",
         "strikes_used": 2, "eliminated_week": 3},
    ],
    "archived_seasons": [],
}

_SURVIVOR_WEEK_STUB = {
    "week": 3, "picked": 1, "alive": 1,
    "games": [
        {"game_id": "sim-3-1", "week": 3, "home": "Jaguars",
         "away": "Commanders", "kickoff_ts": 1700000000,
         "status": "scheduled", "winner": None, "kicked": True},
        {"game_id": "sim-3-2", "week": 3, "home": "49ers", "away": "Cardinals",
         "kickoff_ts": 1700003600, "status": "final", "winner": "49ers",
         "kicked": True},
    ],
}


def test_survivor_live_season_fits_on_phone(dashboard, browser):
    import json

    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        page.route(
            "**/api/survivor/overview",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(_SURVIVOR_SEASON_STUB),
            ),
        )
        page.route(
            "**/api/survivor/week",
            lambda route: route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps(_SURVIVOR_WEEK_STUB),
            ),
        )
        _goto_panel(page, f"{dashboard.base}/#/survivor")
        page.wait_for_selector("[data-rules-form]", timeout=15_000)
        # The dial explanations are collapsed by default — open every card's
        # details so the widest hidden content is part of the audit.
        for d in page.query_selector_all("[data-rules-form] details"):
            d.evaluate("el => el.open = true")
        _settle(page)
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
        # The settle row must have rendered — it is the widest content here.
        settle_buttons = page.evaluate(
            "() => document.querySelectorAll('[data-settle]').length"
        )
    finally:
        context.close()
    assert settle_buttons == 8, (
        f"expected 2 games x 4 settle buttons, got {settle_buttons} — did the "
        "week stub shape drift?"
    )
    _assert_fits(res, "Survivor live-season panel")


# Anonymised realistic shapes — ids are fake, names are the test-suite's stock
# ones. 15 rows: 3 completed, 1 missed, 11 pending, like a lived-in list.
_TODO_STUB = {
    "pending_count": 11,
    "completed_count": 3,
    "missed_count": 1,
    "todos": [
        {
            "id": i + 1,
            "added_by": "1469491362444480666",
            "added_by_name": "Billy",
            "task": f"Sample outstanding task {i + 1} with enough words to fill its row",
            "description": None,
            "source_message_url": None,
            "created_at": 1700000000,
            "completed_at": 1700000500 if i < 3 else None,
            "completed_by": "1469491362444480666" if i < 3 else None,
            "completed_by_name": "Billy" if i < 3 else "",
            "recurring_id": None,
            "missed_at": 1700000900 if i == 3 else None,
        }
        for i in range(15)
    ],
    "board": {"channel_id": "0", "message_id": "0",
              "posted": False, "updated_at": 0},
    "can_manage_board": True,
}


def test_todo_panel_populated_flows_top_to_bottom(dashboard, browser):
    """A populated todo list must not collapse the tasks split.

    ``.panel`` is a column flex container, and ``.mod-split`` was its one fully
    shrinkable child (its panes carry ``min-height: 0`` and scroll internally).
    On the one split panel with content *below* the split — mod-todo's add form
    and three board cards — the flex algorithm crushed the split to ~0 height
    and the panes painted over everything after it. All three audit signals
    measure *width*, so a vertical pile-up scored clean.

    The collapsed section still kept a small non-overlapping rect of its own —
    only its panes' *content* spilled past it — so the check is on the pane
    content boxes (list head, list, detail head/body) against the add form
    below, not on the section rects. Those boxes are the scroll containers
    themselves, whose rects never extend past their clip, so a healthy panel
    with a scrolled task list can't false-positive here.
    """
    import json

    for vp in ("desktop", "phone"):
        context = browser.new_context(
            viewport={"width": VIEWPORTS[vp], "height": 900}
        )
        try:
            page = context.new_page()
            page.route(
                "**/api/todos",
                lambda route: route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps(_TODO_STUB),
                ),
            )
            _goto_panel(page, f"{dashboard.base}/#/mod-todo")
            page.wait_for_selector(".ticket-item", timeout=15_000)
            _settle(page)
            rows = page.evaluate(
                "() => document.querySelectorAll('.ticket-item').length"
            )
            overlaps = page.evaluate(
                """() => {
                  const out = [];
                  const formTop = document.querySelector('.todo-add')
                    .getBoundingClientRect().top;
                  for (const sel of ['.ticket-list-head', '.ticket-list',
                                     '.td-head', '.td-body']) {
                    const el = document.querySelector(sel);
                    if (!el) continue;
                    const b = el.getBoundingClientRect().bottom;
                    if (formTop < b - 1) {
                      out.push(`${sel} (bottom ${Math.round(b)}) overlaps the `
                        + `add form (top ${Math.round(formTop)})`);
                    }
                  }
                  const split = document.querySelector('.mod-split')
                    .getBoundingClientRect();
                  if (split.height < 200) {
                    out.push(`.mod-split collapsed to ${Math.round(split.height)}px`
                      + ' — 11 task rows need far more than that');
                  }
                  return out;
                }"""
            )
            res = page.evaluate(AUDIT_JS, CLIP_SLOP)
        finally:
            context.close()
        assert rows == 11, (
            f"[{vp}] expected the 11 pending stub rows, got {rows} — did the "
            "/api/todos stub shape drift?"
        )
        assert not overlaps, f"[{vp}] panel sections overlap:\n" + "\n".join(overlaps)
        _assert_fits(res, f"Todo panel ({vp})")


# Shape taken from prod (guild 1469491362444480666, read-only) and anonymised:
# 155 nodes across 8 Louvain clusters, so the cross-cluster matrix is 8×8 — at
# 42px a cell plus a 90px label gutter that is 446px, wider than a 390px phone.
# Names are at or above prod's longest (37 chars). No real member data here.
_GRAPH_NAMES = [
    "Cordwainer Bibbleworth-Fanshawe III",
    "a-fairly-long-hyphenated-membername-x",
    "moth", "Quillon", "sparrowhawk", "Vex", "Marigold Thistlewaite", "nn",
]
_GRAPH_STUB = {
    "nodes": [
        {
            "user_id": str(1469491362444480666 + i),
            "user_name": name,
            "total_outbound": 2915 - i * 300,
            "total_inbound": 3653 - i * 380,
            "unique_partners": 134 - i * 12,
            "cluster_id": i,
        }
        for i, name in enumerate(_GRAPH_NAMES)
    ],
    "edges": [
        {
            "from_id": str(1469491362444480666 + i),
            "from_name": _GRAPH_NAMES[i],
            "to_id": str(1469491362444480666 + (i + 1) % len(_GRAPH_NAMES)),
            "to_name": _GRAPH_NAMES[(i + 1) % len(_GRAPH_NAMES)],
            "weight": 257 - i * 20,
        }
        for i in range(len(_GRAPH_NAMES))
    ],
    "top_pairs": [
        {
            "from_id": str(1469491362444480666 + i),
            "from_name": _GRAPH_NAMES[i],
            "to_id": str(1469491362444480666 + (i + 1) % len(_GRAPH_NAMES)),
            "to_name": _GRAPH_NAMES[(i + 1) % len(_GRAPH_NAMES)],
            "weight": 508 - i * 40,
        }
        for i in range(len(_GRAPH_NAMES))
    ],
    "metrics": {
        "clustering_coefficient": 0.713,
        "network_density": 0.1917,
        "reciprocity": 0.871,
        "isolates": 13,
        "bridge_count": 5,
        "bridge_users": [
            {"user_id": str(1469491362444480666 + i), "user_name": _GRAPH_NAMES[i],
             "betweenness": round(20.28 - i * 3.1, 2)}
            for i in range(5)
        ],
        "top_betweenness_pct": 20.28,
        "clusters": [
            {"id": i, "size": s}
            for i, s in enumerate([48, 40, 32, 14, 11, 6, 2, 2])
        ],
        "cross_cluster_matrix": [
            [float(4874 - abs(i - j) * 520) for j in range(8)] for i in range(8)
        ],
        "cross_cluster_labels": [f"Cluster {i + 1}" for i in range(8)],
        "avg_path_length": 1.84,
        "small_world_quotient": 3.72,
        "node_count": 155,
        "edge_count": 4575,
        "badge": "excellent",
    },
}


_SERIES_STUB = {
    "bin_seconds": 604800,
    "start": 1_755_000_000,
    "weeks": 30,
    "nodes": [
        {"user_id": str(1469491362444480666 + i),
         "user_name": _GRAPH_NAMES[i % len(_GRAPH_NAMES)],
         "cluster_id": i % 8, "joins": [], "leaves": []}
        for i in range(12)
    ],
    "pairs": [
        {"a": str(1469491362444480666 + i), "b": str(1469491362444480666 + (i + 1) % 12),
         "w": [1] * 30}
        for i in range(12)
    ],
}


def test_connection_graph_populated_fits_on_phone(dashboard, browser):
    """The Connection Graph, *with data*, on a phone.

    The redesigned panel is one full-height canvas with a chip bar above it
    and community chips overlaid on it, and all of that only exists once
    there is a graph to draw. Against the sweep's freshly-migrated DB the
    panel renders only its "no connections match these filters" overlay, so
    the chip bar's wrap behaviour and the canvas overlays are invisible to
    the plain sweep — the same blind spot that hid the mod-engagement faults
    above. The Tuning popover holds the numeric knobs and lives behind a
    click, so it is opened before the audit.
    """
    import json

    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        page.route(
            "**/api/reports/interaction-graph*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_GRAPH_STUB),
            ),
        )
        # Registered after the graph stub so its narrower URL wins the
        # "interaction-graph*" prefix (Playwright checks routes newest-first).
        page.route(
            "**/api/reports/interaction-graph-series*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_SERIES_STUB),
            ),
        )
        _goto_panel(page, f"{dashboard.base}/#/connection-graph")
        page.wait_for_selector("[data-cluster-chips] .graph-cluster-chip")
        page.click(".graph-tuning > summary")
        page.wait_for_selector(".graph-tuning-pop input", state="visible")
        _settle(page)
        chips = page.eval_on_selector_all(
            "[data-cluster-chips] .graph-cluster-chip", "els => els.length"
        )
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
        # The replay transport only exists behind the Replay chip.
        page.click(".graph-tuning > summary")  # fold the popover back away
        page.click("[data-replay]")
        page.wait_for_selector("[data-replaybar]:not([hidden])")
        # Pause first: a playing replay re-renders every 700ms, and the date
        # label's width changes with it ("Sep 2 – Sep 30" vs "Sep 12 – Oct 10"),
        # so an audit taken mid-playback samples a bar whose wrap point is
        # still moving. _settle only watches document scrollWidth, which an
        # absolutely-positioned bar never moves, so it would not catch this.
        page.click("[data-rp-toggle]")
        _settle(page)
        res_replay = page.evaluate(AUDIT_JS, CLIP_SLOP)
    finally:
        context.close()
    assert chips == 8, (
        f"expected a chip per stub cluster (8), got {chips} — did the "
        "/api/reports/interaction-graph stub shape drift?"
    )
    _assert_fits(res, "Connection Graph with data")
    _assert_fits(res_replay, "Connection Graph replay")


def test_activity_overlay_fits_on_phone(dashboard, browser):
    """Switch the Activity panel to the week overlay.

    The overlay adds a fourth control to an already-full row and swaps the
    chart for a 168-point band, and a plain page load never reaches it — the
    view only exists once the Resolution dropdown is changed.
    """
    context = browser.new_context(viewport={"width": VIEWPORTS["phone"], "height": 844})
    try:
        page = context.new_page()
        _goto_panel(page, f"{dashboard.base}/#/activity")
        page.wait_for_selector('[data-control="resolution"]', timeout=15_000)
        page.select_option('[data-control="resolution"]', "week_overlay")
        # The Compare-to picker is revealed by the resolution change.
        page.wait_for_selector('[data-field="compare"]:not([hidden])', timeout=15_000)
        _settle(page)
        res = page.evaluate(AUDIT_JS, CLIP_SLOP)
    finally:
        context.close()
    _assert_fits(res, "Activity week overlay")
