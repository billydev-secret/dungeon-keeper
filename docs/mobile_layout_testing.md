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
  from the default suite (`-m "not mobile"` in `pyproject.toml`). It auto-skips
  where Playwright or Chromium isn't installed, so the ordinary suite and
  per-push CI (which have no browser) are unaffected.

### Coverage beyond page-load

A plain panel load renders most editors empty, so a bug inside a modal editor
wouldn't be seen. Interaction-heavy editors get their own scenario;
`test_announcement_button_editor_fits_on_phone` opens the announcement editor
and adds role-button rows — the exact broken flow — then audits. Add a scenario
when a new editor hides layout behind a click.

## Known debt (the allowlist)

Six panels already overflowed on mobile the day this gate was written. Fixing
six unrelated panels wasn't in scope, so they're listed in `KNOWN_OVERFLOW` — an
**allowlist**: the gate hard-fails only when a panel *outside* the list
overflows (a new regression); a listed panel is allowed to fail. Across four
full sweeps the dirty set was always a subset of these six, never a new panel.

| Panel | Problem |
|---|---|
| `help-overview` | ~1195px quick-reference table, no horizontal scroll — cut off on a phone |
| `health-mod-engagement` | wide data table / card grid overflows its panel |
| `help-setup` | a long inline link overflows on a phone |
| `config-ai` | a primary button sits a few px off the right edge |
| `qa-tracker` | filter-button row doesn't wrap (same class as the announcement editor) |
| `wellness-caps` | histogram-slider grid sized to full width before the scrollbar appears — clips ~197px |

The first three overflow by a lot (tens to ~1200px) and fail every run; the last
three overflow only marginally (a few px, or a grid sized at a transient width)
and flap between clean and dirty. A strict "must stay dirty" ratchet would itself
flake on those, so the list is a plain allowlist instead — not a ratchet. When a
run finds a listed panel clean it prints a note (verify with the diagnostic tool,
then delete it from the set); it never fails on that.

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
