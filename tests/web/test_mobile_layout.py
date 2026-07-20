"""Responsive-layout gate: no dashboard panel may hide content off-screen.

Drives the real dashboard in headless Chromium (via Playwright) at phone,
tablet and desktop widths and asserts two things about every panel:

  * nothing extends past the viewport's right edge unless it sits in a genuinely
    scrollable box (a wide data table is fine — the user can scroll to it);
  * no ``overflow-x: hidden`` container is clipping content wider than itself
    (that content is simply gone — this is the announcement-button bug that
    prompted the whole check).

Both signals, and the in-page audit script, are shared with
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

from migrations import apply_migrations_sync  # noqa: E402


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
        apply_migrations_sync(db)
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
    return "; ".join(
        f"{it['sel']} (+{it.get('by', it.get('hides'))}px)" for it in items[:6]
    )


# Panels that already overflow on mobile, predating this gate — an **allowlist**,
# not a strict ratchet. The gate hard-fails only when a panel *outside* this set
# overflows; a listed panel is permitted to fail and permitted to render clean.
# That distinction matters: three of these (config-ai, qa-tracker, wellness-caps)
# overflow only marginally — a few pixels, or a grid sized at a transient width —
# so they flap between clean and dirty across runs. A rule that *required* them
# to stay dirty would itself flake; a plain allowlist stays green while still
# catching any genuinely new overflow on a seventh panel. Across four full sweeps
# the dirty set was always a subset of these six, never a new panel.
#
# Each is a real bug worth fixing (then delete it here — see the diagnostic tool
# in docs/mobile_layout_testing.md to confirm it's gone):
#   help-overview          a ~1195px quick-reference table with no horizontal
#                          scroll — cut off past the panel edge on a phone.
#   health-mod-engagement  a wide data table / card grid overflows its panel.
#   help-setup             a long inline link overflows on a phone.
#   config-ai              a primary button sits a few px off the right edge.
#   qa-tracker             the filter-button row doesn't wrap — same class of
#                          bug as the announcement editor this gate was born from.
#   wellness-caps          the histogram-slider grid is sized to the full width
#                          before the vertical scrollbar appears; its panel can
#                          clip ~197px.
KNOWN_OVERFLOW = {
    "help-overview",
    "health-mod-engagement",
    "help-setup",
    "config-ai",
    "qa-tracker",
    "wellness-caps",
}


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
            faults = []
            if res["viewport"]:
                faults.append(f"off-screen — {_describe(res['viewport'])}")
            if res["clipped"]:
                faults.append(f"clipped — {_describe(res['clipped'])}")
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


# ── interaction scenario: the editor the original bug lived in ──────────────────

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
    faults = []
    if res["viewport"]:
        faults.append("off-screen — " + _describe(res["viewport"]))
    if res["clipped"]:
        faults.append("clipped — " + _describe(res["clipped"]))
    assert not faults, "Announcement button editor overflows on phone:\n" + "\n".join(faults)
