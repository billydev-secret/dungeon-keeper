# Web / dashboard testing

Beyond the per-feature route tests (`tests/web/test_*_routes.py`), a set of
cross-cutting sweeps guard properties that no single route test owns. Two tiers:

- **Default suite** (every push, no browser) — fast TestClient / static checks.
- **Browser suite** (`browser` marker; gate.py scoped, nightly full) — real
  Chromium via Playwright. Excluded from the default run; auto-skips where
  Playwright/Chromium isn't installed. Needs `python -m playwright install
  chromium` on a new machine.

## Default-suite sweeps

### Authorization — `test_authz_sweep.py`
Enumerates every registered route and, on the *real* `DiscordOAuthAuth` backend
with **no** session, asserts each non-public route returns 401/403 — never a
2xx. A route that forgets `Depends(require_perms(...))` shows up as a leak. The
design rule ("everything gated, never ship an unenforced control") becomes
mechanical instead of per-route vigilance. Public routes (login, OAuth, static,
Swagger) are allowlisted. Perm-*level* checks (admin vs moderator) stay in each
route's own test — this sweep only proves anonymous callers get nothing.

### Snowflake precision — `test_snowflake_precision.py`
Discord ids are ~2^60; a bare JSON number loses precision above 2^53 and the
dashboard rounds it into a different, non-existent id (see the "Snowflake JS
precision" note). A recursive walker flags any int > 2^53 in a response. Applied
three ways: unit tests on the walker; a broad sweep of no-param GET endpoints
with the active guild set to a real snowflake (catches guild-id echoes; heavy /
external endpoints excluded so an in-process handler can't hang the suite); and
round-trips through the two hand-serialized features most likely to regress
(announcements, role menus).

### Broken manual links — `test_help_links.py`
The in-dashboard manual (`static/manual.html`) rewrites `href="#x"` links to
help routes or in-page anchors; a target that's neither is a silent dead link
(a real one, `#role-menus`, shipped that way). Pure text parse: every internal
link must resolve to a help-section anchor or an existing element id, and every
help-nav anchor must have a matching manual section.

## Browser suite (`browser` marker)

Shares one Playwright harness (`scripts/mobile_layout_scan.py`: serve the app
under OpenAuth, enumerate panels from the nav, navigate + settle). The harness
neutralizes the per-IP rate limiter in-process — every browser request comes
from one loopback IP, which the limiter would otherwise 429, burying the signal.

### Responsive layout — `test_mobile_layout.py`
Every panel at 390/768/1280 must keep content on-screen (no viewport overflow,
no clipped-unreachable content). Also tagged `mobile` so `-m mobile` runs just
it. Full detail: [mobile_layout_testing.md](mobile_layout_testing.md).

### Panel load health — `test_panel_console.py`
Every panel must mount with no uncaught JS exception, no `console.error` (beyond
resource-load failures, which the network check owns), and no failed/4xx-5xx
same-origin request. Nothing else exercises the vanilla-JS panels past a syntax
check, so a panel that throws on mount would otherwise ship green. The bot-less
test env makes bot-dependent endpoints return 503 (tolerated — can't happen in
prod); the SSE log stream and favicon are tolerated; `greeter-response`'s
no-data report 404 is allowlisted.

Both browser sweeps use a **fresh browser context per panel** and wait for the
layout to settle before measuring — shared-context state bleed and mid-render
snapshots otherwise make results flap between runs.

## Where each runs

| | default suite (per push) | gate.py --quick/--scoped | nightly |
|---|---|---|---|
| authz / snowflake / help-links | ✅ | ✅ | ✅ |
| mobile layout / panel console | skipped (no browser) | scoped to changed panels* | full |

\* gate.py runs the browser suite (`-m browser`) only when a commit touches
dashboard assets, scoped to the affected panels (`PANEL_SCOPE`); all-panel
sweeps run phone-width only to stay fast (`PANEL_VIEWPORTS`). Non-fatal without a
browser. Scope mapping (`mobile_scope`) is covered by
`tests/test_gate_mobile_scope.py`.

## Adding a route? Two freebies
A new route is covered by the authz sweep automatically (add it to
`PUBLIC_PATHS` only if it's genuinely public). If it returns ids, the snowflake
walker (`find_precision_risks`) is importable for your own route test.
