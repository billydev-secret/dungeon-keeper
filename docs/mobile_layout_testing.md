# Mobile / responsive layout testing

The dashboard is used heavily on phones, but nothing checked that panels
actually *fit* a phone — a role-button editor shipped with its controls running
off the right edge (unreachable) and every automated check stayed green, because
none of them opened a browser. This is that check.

## What it asserts

A real headless Chromium (Playwright) loads every dashboard panel at three
widths — **390 / 768 / 1280** (phone / tablet / desktop) — and each panel must
pass two rules:

- **viewport** — no element's right edge extends past the viewport unless it
  sits inside a genuinely scrollable box. A wide data table in an
  `overflow-x: auto` container is fine; the user scrolls to it. Content that
  just sticks out (or forces the whole page to scroll sideways) is not.
- **clipped** — no `overflow-x: hidden | clip` container may hold content wider
  than itself. That content is silently cut off and can't be reached — exactly
  what the role-button bug did. `text-overflow: ellipsis` is exempt: a
  truncated label with a visible `…` is a deliberate design, not lost content.
- **collapsed** — no text element may be broken mid-word repeatedly (more
  rendered lines than it has words), which means its column has shrunk towards
  one character wide. This is the *opposite* failure to the other two: nothing
  overflows, the layout implodes. A single long token with no spaces (a URL
  wrapping under `break-word`) is exempt — that's deliberate.

Note the asymmetry the third rule exists to fix: the first two only ever detect
content that is too **wide**. `health-mod-engagement` rendered its whole report
inside a leftover `.panel-loading` (a centering flex box), so every stat-tile
heading came out one character per line and the cards overlapped — and both
width rules scored it perfectly clean, because an element collapsed to width 0
is skipped by the visibility filter entirely.

Both rules, and the in-page audit script itself, live in one place —
`scripts/mobile_layout_scan.py` (`AUDIT_JS`) — shared by the diagnostic tool and
the gate so they can never disagree.

**Determinism.** Each panel is audited on a *fresh page*, and the audit waits
for the layout to stop changing width (`_settle` polls `scrollWidth` until two
reads match) before measuring. Both matter: an early snapshot, or residual
scrollbar state left by a reused page, made at least one panel (`wellness-caps`)
flap clean/dirty between runs. A gate that flaps is worse than none, so the
measurement is pinned to a settled layout.

Why not screenshot diffing? It's flaky, needs a baseline regenerated on every
intentional change, and reports "N pixels differ" instead of *what* broke. The
invariant here is machine-checkable and names the offending element, so a
failure reads like a bug report: `[phone] qa-tracker: off-screen — button[data-filter] (+15px)`.

## Two surfaces

- **Diagnostic** — `python scripts/mobile_layout_scan.py [--viewport phone]
  [--limit N] [--json out.json]`. Sweeps panels and prints per-check counts and
  the panels involved. Use it to investigate, or to re-measure after a CSS
  change. It reports faults; it never fails a build.
- **Gate** — `tests/web/test_mobile_layout.py`, marked `browser` (and `mobile`)
  and excluded
  from the default suite (`-m 'not browser'` in `pyproject.toml`'s `addopts`).
  It auto-skips
  where Playwright or Chromium isn't installed, so the ordinary suite and
  per-push CI (which have no browser) are unaffected.

### Coverage beyond page-load

A plain panel load renders most editors empty, so a bug inside a modal editor
wouldn't be seen. Interaction-heavy editors get their own scenario;
`test_announcement_button_editor_fits_on_phone` opens the announcement editor
and adds role-button rows — the exact broken flow — then audits. Add a scenario
when a new editor hides layout behind a click.

**Layout can hide behind *data*, not just behind a click.** The sweep serves a
freshly-migrated (empty) DB, so a report panel renders its empty state and
nothing else. `health-mod-engagement` sat on the allowlist for two years'
worth of sweeps without a single run ever rendering its stat tiles, chart or
table — the panel was badly broken on a real phone the whole time and the gate
scored it clean. If a panel's layout only exists once it has rows, stub its API
and audit that (`test_mod_engagement_populated_fits_on_phone`,
`test_inactive_sweep_preview_fits_on_phone`); a green sweep over an empty state
proves nothing. Prefer a stub over seeding the DB when the data originates at
the gateway, which the test dashboard has no connection to.

## Known debt (the allowlist)

Six panels already overflowed on mobile the day this gate was written. Fixing
six unrelated panels wasn't in scope, so they're listed in `KNOWN_OVERFLOW` — an
**allowlist**: the gate hard-fails only when a panel *outside* the list
overflows (a new regression); a listed panel is allowed to fail.

**All six were cleared on 2026-07-28 and `KNOWN_OVERFLOW` is now empty** — every
panel is enforced, and any overflow *or collapse* fails the build. What the six
turned out to be is worth keeping, because most of the notes were wrong:

| Panel | Was annotated | Actually |
|---|---|---|
| `health-mod-engagement` | "a wide data table / card grid overflows its panel" | Not an overflow, and no run had ever seen it — the sweep's DB is empty, so the panel only rendered its "no moderator messages" state. With data it laid the whole report out inside a leftover `.panel-loading` centering flex box, collapsing every heading to one character per line. Fixed; now covered with data. |
| `help-setup` | "a long inline link overflows" | It *collapsed*, it didn't overflow: `display: flex` on the step row made each inline link its own flex item, squeezed to 13px. Fixed. |
| `help-overview` | "~1195px quick-reference table, no horizontal scroll" | Already fixed — the table sits in an `overflow-x: auto` container. The note had outlived it. |
| `config-ai` | "a primary button sits a few px off the right edge" | No longer reproduces at any width. |
| `qa-tracker` | "filter-button row doesn't wrap" | No longer reproduces at any width. |
| `wellness-caps` | "histogram-slider grid sized to full width before the scrollbar appears" | No longer reproduces. That flap is exactly what `_settle()` was written to cure — the fix was in the measurement, not the CSS. |

The lesson worth keeping: **an allowlist entry is a hypothesis, not a
measurement.** Three of these six descriptions named the wrong mechanism, and
one named a bug the tool structurally could not observe. Re-measure before
trusting an entry — and when you fix one, delete it.

`qa-tracker` and `wellness-caps` were historically borderline. If the gate starts
failing on either from a commit that didn't touch them, suspect the old flap
rather than a new regression: confirm with the diagnostic tool and re-add the
entry rather than chasing phantom CSS.

## Where it runs

- **Per-commit** (`gate.py`, `--quick` and `--scoped`): when a commit touches
  dashboard assets, the check runs **scoped to the affected panels** — a
  one-panel JS edit visits just that panel (all three widths, it's cheap); a
  CSS change or shared-JS edit sweeps all panels but **phone-width only**, since
  that's where nearly every overflow shows and a full three-width sweep would be
  minutes long in a pre-commit tier. Non-fatal without a browser, so a machine
  that never ran `playwright install` still commits. Scope mapping is
  `mobile_scope()` in `gate.py`, covered by `tests/test_gate_mobile_scope.py`.
- **Nightly** (`.github/workflows/nightly.yml`): installs Chromium and runs the
  **full** sweep (every panel × every width).
- **Per-push CI** (`test.yml`): unchanged — no browser installed, mobile tests
  skip. The functional suite stays fast.

## Setup on a new machine

```
pip install playwright           # already in requirements-dev.lock
python -m playwright install chromium
```

Scope one run by hand:

```
PANEL_SCOPE=announcements,role-menus PANEL_VIEWPORTS=phone \
  python -m pytest -m mobile tests/web/test_mobile_layout.py
```
