"""Per-audience nav visibility for the Games / Social / Photo Challenge routes.

The 2026-08 IA pass (IA1) regroups the Games section into subgroups and moves
the four social features (Guess Who, Whisper, Pen Pals, Confessions) into their
own **Social** section, retiring the per-item ``perms`` hack they needed to
survive the game-host section gate. A regroup is only safe if it is *invisible*
to permissions: the same audiences must keep the same pages, and an admin-only
page must not become openable by a moderator.

Nothing else pins that. ``app.js``'s gating (``sectionGateOk`` + ``resolveItem``)
lives entirely in the browser, so this drives the real dashboard with a stubbed
``/api/me`` per audience and reads what the sidebar actually rendered:

  * ``"open"``   — an enabled nav button the user can click through to;
  * ``"locked"`` — rendered but disabled (the W-N5 "you can see it exists,
    admins only" treatment);
  * ``"hidden"`` — no nav entry at all.

The expectation table below was captured against the **pre-regroup** structure
and must survive the regroup unchanged, with one reviewed exception (see
``KNOWN_DELTAS``).

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
        db = tmp / "nav-visibility.db"
        migrated_db(db, reap=False)
        self.port = _free_port()
        self._server = serve(db, self.port)
        self.base = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> Iterator[_Server]:
    srv = _Server(tmp_path_factory.mktemp("nav-visibility"))
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


# ── audiences ───────────────────────────────────────────────────────────────

_GUILD = "1"
_HOST_ROLE = "555000111"

# `admin` implies `moderator` and `manage_server` server-side (auth.py), so the
# admin profile carries all three — modelling it as admin-only would test a
# state that cannot exist.
AUDIENCES: dict[str, dict] = {
    "admin": {"perms": ["admin", "manage_server", "moderator"], "role_ids": []},
    "moderator": {"perms": ["moderator"], "role_ids": []},
    # A game-host who is *not* a moderator: the audience the Games section gate
    # exists for.
    "game-host": {"perms": [], "role_ids": [_HOST_ROLE]},
    # Moderator who also holds the host role — the only audience that sees
    # admin-only Games pages as locked rather than hidden today.
    "mod-host": {"perms": ["moderator"], "role_ids": [_HOST_ROLE]},
    "member": {"perms": [], "role_ids": []},
}


def _me_payload(profile: dict) -> dict:
    return {
        "user_id": "42",
        "username": "nav-test",
        "perms": profile["perms"],
        "role_ids": profile["role_ids"],
        "role_names": [],
        "guild_id": _GUILD,
        "guild_name": "Test Guild",
        "guilds": [{"id": _GUILD, "name": "Test Guild", "icon": None}],
        "primary_guild_id": _GUILD,
        "avatar_url": None,
        "status": "online",
        "games_editor_role_id": _HOST_ROLE,
        "economy_manager_role_id": None,
        "wellness_opted_in": False,
    }


_READ_NAV = """
() => {
  const out = {};
  for (const b of document.querySelectorAll('.nav-item[data-page-id]')) {
    out[b.dataset.pageId] = b.disabled || b.classList.contains('nav-locked')
      ? 'locked' : 'open';
  }
  return out;
}
"""


def _nav_state(browser, base: str, audience: str) -> dict[str, str]:
    """Render the dashboard as `audience` and return {page id: open|locked}."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/me",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_me_payload(AUDIENCES[audience])),
        ),
    )
    try:
        page.goto(base, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_selector(".nav-item", timeout=30_000)
        return page.evaluate(_READ_NAV)
    finally:
        ctx.close()


# ── the expectation table ───────────────────────────────────────────────────
#
# Captured from the pre-regroup nav (Games as a 23-item flat list + the
# one-item Photo Challenge section). Order: admin, moderator, game-host,
# mod-host, member. Anything not "open"/"locked" must be absent from the nav.

_O, _L, _H = "open", "locked", "hidden"

EXPECTED: dict[str, dict[str, str]] = {
    # ── Games: operations ───────────────────────────────────────────
    "games-logs":        {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-scheduling":  {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-config":      {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    # Explicit `perms: ["moderator"]`, so it survives a failed section gate.
    "games-external":    {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    # ── Games: live games ───────────────────────────────────────────
    "games-legitlibs":   {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "config-risky-rolls":            {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-pressure":         {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-quickdraw":        {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-hotpotato":        {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-hotpotatogroup":   {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-chicken":          {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    "config-games-musicalchairs":    {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
    # Its own one-item section before the regroup; folded into Games after.
    # Same gate either way (admin or game host).
    "photo-challenge":   {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    # ── Games: question banks ───────────────────────────────────────
    "games-wyr":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-nhie":        {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-mlt":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-rushmore":    {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-price":       {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-clapback":    {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-ama":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-ffa":         {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    "games-traditional": {"admin": _O, "moderator": _H, "game-host": _O, "mod-host": _O, "member": _H},
    # ── the four social features (moved to their own section) ───────
    "config-guess":      {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    "config-whisper":    {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    "pen-pals":          {"admin": _O, "moderator": _O, "game-host": _H, "mod-host": _O, "member": _H},
    "config-confessions": {"admin": _O, "moderator": _H, "game-host": _H, "mod-host": _L, "member": _H},
}

# The one reviewed difference the regroup introduces.
#
# Confessions is admin-only. Before the regroup it lived in the host-gated Games
# section, so a plain moderator (no host role) failed the *section* gate and the
# page vanished entirely — while a moderator who happened to hold the host role
# saw it as a locked entry. That split is an artifact of the wrong parent
# section, not a policy. In the moderator-gated Social section the flag it
# carries (`adminOnly`) gives every moderator the same locked entry, which is
# how every other admin-only page in a moderator-visible section already
# behaves — including **Confessions Audit** under Moderation, which moderators
# have always seen locked. Nothing becomes openable: locked buttons are
# `disabled`, and every Confessions endpoint is admin-gated server-side.
KNOWN_DELTAS: dict[tuple[str, str], str] = {
    ("config-confessions", "moderator"): "locked",
}


@pytest.fixture(scope="module")
def nav_by_audience(browser, dashboard) -> dict[str, dict[str, str]]:
    return {name: _nav_state(browser, dashboard.base, name) for name in AUDIENCES}


@pytest.mark.parametrize("audience", list(AUDIENCES))
def test_moved_route_visibility_is_unchanged(nav_by_audience, audience):
    """Every regrouped route keeps the audience it had before the regroup."""
    rendered = nav_by_audience[audience]
    actual = {pid: rendered.get(pid, _H) for pid in EXPECTED}
    expected = {
        pid: KNOWN_DELTAS.get((pid, audience), states[audience])
        for pid, states in EXPECTED.items()
    }
    assert actual == expected


def test_audience_stubs_actually_differ(nav_by_audience):
    """Guard the harness: if the /api/me stub stopped taking effect, every
    audience would render the same (admin) nav and every assertion above would
    pass for the wrong reason."""
    admin = nav_by_audience["admin"]
    member = nav_by_audience["member"]
    assert len(admin) > len(member) + 20, (admin.keys(), member.keys())
    assert "games-logs" in admin and "games-logs" not in member


def test_no_admin_only_page_is_openable_by_a_non_admin(nav_by_audience):
    """The failure mode a regroup must never introduce: an admin page whose
    nav entry a moderator (or host, or member) can actually click."""
    # `mod-host` passes the Games gate and is a moderator, so it is the one
    # audience that renders an `adminOnly` page as locked — which makes "locked
    # for mod-host" the reliable way to name the admin-only set from the table.
    admin_only = [pid for pid, s in EXPECTED.items() if s["mod-host"] == _L]
    assert len(admin_only) >= 8, admin_only
    for audience in ("moderator", "game-host", "mod-host", "member"):
        openable = [
            pid for pid in admin_only if nav_by_audience[audience].get(pid) == _O
        ]
        assert not openable, f"{audience} can open admin-only pages: {openable}"


# ── IA5: the Help nav carries the guild's own name for the assistant ────────


_BRANDED = "Sparkles"


def _page_with_brand(browser, base: str, hash_: str = ""):
    """A page whose /api/help/advisor/name says the guild renamed its assistant."""
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/help/advisor/name",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"assistant_name": _BRANDED}),
        ),
    )
    page.goto(f"{base}/#/{hash_}", wait_until="domcontentloaded", timeout=15000)
    return ctx, page


def test_help_nav_label_uses_the_guilds_assistant_name(browser, dashboard):
    """The nav hardcoded "Ask Billy-bot (AI)" while Config had already been
    neutralised for per-guild branding. It now re-labels itself from the same
    endpoint the help panel uses."""
    ctx, page = _page_with_brand(browser, dashboard.base)
    try:
        page.wait_for_selector(".nav-item", timeout=30_000)
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-item')]"
            ".some(b => b.dataset.pageId === 'help-ask' && /Ask Sparkles/.test(b.textContent))",
            timeout=10_000,
        )
        # The old name stays searchable so the sidebar filter still finds it.
        search = page.evaluate(
            "() => document.querySelector('.nav-item[data-page-id=\"help-ask\"]').dataset.search"
        )
        assert "billy" in search
    finally:
        ctx.close()


def test_help_panel_title_uses_the_guilds_assistant_name(browser, dashboard):
    """Same for the panel's own title — and the manual's duplicate heading is
    still dropped, since that comparison uses the static fallback label."""
    ctx, page = _page_with_brand(browser, dashboard.base, "help-ask")
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.wait_for_selector("#panel-root h2", timeout=30_000)
        page.wait_for_function(
            "() => /Ask Sparkles \\(AI\\)/.test(document.querySelector('#panel-root h2').textContent)",
            timeout=10_000,
        )
        body = page.inner_text("#panel-root .dk-help")
        assert "Ask Billy-bot (AI)" not in body, "manual heading rendered as a second title"
        assert not errors, errors
    finally:
        ctx.close()


# ── IA4: the Ctrl/Cmd+K command palette ─────────────────────────────────────
#
# Additive to the sidebar filter (which these tests also re-check is untouched).
# The load-bearing property is the last one: a palette that surfaces a page the
# viewer can't open would be a permission leak dressed as a convenience.


def _open_palette(page):
    page.keyboard.press("Control+k")
    page.wait_for_selector(".dk-palette", timeout=5_000)


def _results(page) -> list[str]:
    return page.evaluate(
        "() => [...document.querySelectorAll('.dk-palette-option')]"
        ".map(o => o.textContent.trim())"
    )


def _booted(browser, base: str, audience: str = "admin"):
    ctx = browser.new_context()
    page = ctx.new_page()
    page.route(
        "**/api/me",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_me_payload(AUDIENCES[audience])),
        ),
    )
    page.goto(base, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_selector(".nav-item", timeout=30_000)
    return ctx, page


def test_palette_opens_on_ctrl_k_and_closes_on_escape(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        page.click("#panel-root")  # focus somewhere outside the palette
        _open_palette(page)
        assert page.evaluate("() => document.activeElement.className") == "dk-palette-input"
        page.keyboard.press("Escape")
        page.wait_for_selector(".dk-palette", state="detached", timeout=5_000)
        # Focus is returned rather than dropped on <body>.
        assert page.evaluate("() => document.activeElement !== document.body")
    finally:
        ctx.close()


def test_palette_lists_section_and_label_and_opens_with_the_keyboard(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        _open_palette(page)
        # Pages tier: results read "Label" over "Section", not a filtered tree.
        page.fill(".dk-palette-input", "jails")
        page.wait_for_function(
            "() => document.querySelectorAll('.dk-palette-option').length > 0",
            timeout=10_000,
        )
        rows = _results(page)
        assert any("Jails" in r and "Moderation" in r for r in rows), rows

        # Second tier: the manual's own headings, routed to the help page.
        page.fill(".dk-palette-input", "jail")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".some(o => o.textContent.includes('Guide'))",
            timeout=10_000,
        )

        # Enter opens whatever is selected.
        page.fill(".dk-palette-input", "jails")
        page.wait_for_function(
            "() => (document.querySelector('.dk-palette-option.active') || {}).textContent"
            "     ?.includes('Jails')",
            timeout=10_000,
        )
        page.keyboard.press("Enter")
        page.wait_for_function("() => location.hash.startsWith('#/mod-jails')", timeout=5_000)
        assert page.query_selector(".dk-palette") is None
    finally:
        ctx.close()


def test_palette_arrow_keys_move_the_selection(browser, dashboard):
    ctx, page = _booted(browser, dashboard.base)
    try:
        _open_palette(page)
        page.fill(".dk-palette-input", "config")
        page.wait_for_function(
            "() => document.querySelectorAll('.dk-palette-option').length > 1",
            timeout=10_000,
        )
        first = page.evaluate(
            "() => document.querySelector('.dk-palette-option.active').id"
        )
        page.keyboard.press("ArrowDown")
        second = page.evaluate(
            "() => document.querySelector('.dk-palette-option.active').id"
        )
        assert first != second
        assert page.evaluate(
            "() => document.querySelector('.dk-palette-input')"
            ".getAttribute('aria-activedescendant')"
        ) == second
    finally:
        ctx.close()


def test_palette_never_surfaces_a_page_the_viewer_cannot_open(browser, dashboard):
    """The permission property: same filtering as the nav, no back door."""
    ctx, page = _booted(browser, dashboard.base, audience="moderator")
    try:
        _open_palette(page)
        # "Confessions" is admin-only: a moderator sees a locked nav entry and
        # must get no palette result that opens it.
        page.fill(".dk-palette-input", "confessions")
        page.wait_for_timeout(600)
        hrefs = page.evaluate(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".map(o => o.dataset.href)"
        )
        assert not any(h.startswith("#/config-confessions") for h in hrefs), hrefs
        # …while a page the moderator *can* open is offered.
        page.fill(".dk-palette-input", "whisper")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.dk-palette-option')]"
            ".some(o => o.textContent.includes('Whisper'))",
            timeout=10_000,
        )
    finally:
        ctx.close()


def test_sidebar_filter_still_works_alongside_the_palette(browser, dashboard):
    """IA4 is additive: the existing nav filter is untouched."""
    ctx, page = _booted(browser, dashboard.base)
    try:
        page.fill("[data-nav-filter]", "jails")
        page.wait_for_function(
            "() => [...document.querySelectorAll('.nav-item:not(.filtered-out)')]"
            ".some(b => b.dataset.pageId === 'mod-jails')",
            timeout=5_000,
        )
        hidden = page.evaluate(
            "() => document.querySelector('.nav-item[data-page-id=\"home\"]')"
            ".classList.contains('filtered-out')"
        )
        assert hidden
    finally:
        ctx.close()
