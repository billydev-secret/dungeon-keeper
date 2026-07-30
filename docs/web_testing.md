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

**`/static/` is public with one carve-out:** `static/manual.html` is the staff
and moderator guide, so it is served by an explicit authed route registered
*before* the `StaticFiles` mount (which would otherwise serve it to anyone).
Every other asset — css, js, images — stays public because the login page needs
them. The gating is pinned by tests in this file: anonymous gets a redirect to
`/login`, an authenticated session gets 200. If you ever add a second sensitive
static file, it needs the same treatment; the mount does not gate anything.

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

### Picker dropdown — `test_filter_select_dropdown.py`
`js/filter-select.js` backs the searchable pickers in 43 panels, so it gets its
own gate rather than riding on whichever panel happens to mount one. Mounts the
widget straight from the shipped module (no panel data — the bot-less env 503s
the channel/role fetches) and asserts it is `display: none` until focused, opens
anchored flush under its input, and stays anchored across a scroll. Each case
runs twice: once normally, once with `HTMLElement.prototype.showPopover` deleted
to exercise the no-Popover-API path — the iOS failure where the list had no
`display` rule of its own and stranded itself hundreds of pixels from its field.

Note the limit of that simulation: deleting the methods does not stop Chromium
applying `[popover]` UA *styles*, so it reproduces the positioning and
lost-visibility half of the iOS bug, not an engine that ignores the attribute
entirely.

A third case stubs `window.visualViewport` with a non-zero `offsetTop` and a
shrunk `height` — what an iOS on-screen keyboard leaves behind — and asserts the
list still lands flush on its input. Chromium has no software keyboard and
Playwright cannot shift the visual viewport, so reporting the offset is the only
way to reach that branch on this hardware. It guards a real regression: the
placement folded `visualViewport.offsetTop` into the coordinates, which
displaced the list by exactly that many pixels on a phone (the "dropdown
floating loose in the corner" report) while staying a no-op on desktop, where
the offset is always 0 — invisible to every other case in this file. The offset
does still belong in the *fit* test that decides whether to flip above the
field, so one parameter row puts the field low enough to force that flip and
asserts the list is flush on top of the input rather than merely above it.

The desktop-shaped `no-keyboard` row is deliberate: it fails the same way if the
fix ever degrades into a phone-only special case.

A second fixture mounts the widget with real snowflake ids to cover **matching
by id**: pasting a channel id finds its row, a short numeric filter does not
drag in every id containing that digit (the `MIN_ID_FILTER_LEN` gate), and name
search is unaffected. The shipped fixture's `"1"`/`"2"`/`"3"` ids are shorter
than that gate, so they cannot exercise the path. This came from a real
mobile report: copying an id out of Discord — the natural move when the name
carries an emoji and a box separator — returned an empty list, which reads as
"the bot can't see any channels".

The two panel sweeps use a **fresh browser context per panel** and wait for the
layout to settle before measuring — shared-context state bleed and mid-render
snapshots otherwise make results flap between runs.

## Where each runs

| | default suite (per push) | gate.py --quick | gate.py --scoped | nightly |
|---|---|---|---|---|
| authz / snowflake / help-links | ✅ | — (no pytest) | when a `src/web_server/` change maps them in | ✅ |
| mobile layout / panel console | skipped (no browser) | scoped to changed panels* | scoped to changed panels* | full |

`--quick` runs **no pytest at all** (ruff + pyright + the scoped browser panel
checks when dashboard assets changed), so the authz/snowflake/help-links sweeps
never run under it; under `--scoped` they run only when the staged diff touches
`src/web_server/` and the mapping pulls their test files in.

\* gate.py runs the browser suite (`-m browser`) only when a commit touches
dashboard assets, scoped to the affected panels (`PANEL_SCOPE`); all-panel
sweeps run phone-width only to stay fast (`PANEL_VIEWPORTS`). Non-fatal without a
browser. Scope mapping (`mobile_scope`) is covered by
`tests/test_gate_mobile_scope.py`.

## Adding a route? Two freebies
A new route is covered by the authz sweep automatically (add it to
`PUBLIC_PATHS` only if it's genuinely public). If it returns ids, the snowflake
walker (`find_precision_risks`) is importable for your own route test.
