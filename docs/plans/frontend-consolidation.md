# Frontend consolidation — one fetch core, one esc, shared strips/states/utilities

Review finding U3a–U3h / #13. Status: **implemented (2026-07-02)** — stages 1–3
shipped (894987b fetch core, e782b4b esc convergence, 617c855 tab-strip/state
fragments/utilities).

## 1. Context & goal

The dashboard (`src/web_server/static/js/`) has grown by copy-paste:

- **Four fetch implementations.** `api.js` (`api`/`apiPost`, 401→`/login`),
  `config-helpers.js` (`apiPut`/`apiDelete`, **no** 401 handling),
  `wellness-helpers.js` (`wGet`/`wPost`/`wPut`/`wDelete`,
  401→`/auth/discord?return_to=…`, mutators also check `data.ok === false`),
  plus **8 panels using raw `fetch()`** (15 sites; `help.js` is the one
  legitimate holdout — it fetches HTML, not JSON).
- **24 `esc()` definitions** (verified inventory below — two more than the
  collector's 22: `config-global.js` and `config-booster-roles.js` each hide a
  local `_esc`). The DOM-based copies don't escape quotes, so they are
  attribute-unsafe; null-handling differs per copy.
- **6 hand-rolled filter/tab strips** across 5 panels, identical
  delegated-click + `.active`-toggle wiring.
- **141 `class="empty"` loading/empty/error sites** with three dominant
  shapes, no shared builder.
- **Heavy inline-style repetition** (`width:100%` ×27, `margin-top:14px` ×22,
  …); `app.css` has no utilities section.

This refactor converges all HTTP on one private request core in `api.js`
(existing public names preserved via re-exports, so the 35
`config-helpers.js` importers and 7 `wellness-helpers.js` importers don't
change), converges all escaping on `api.js` `esc` (made null-safe), adds
`js/tab-strip.js` + `js/states.js`, and adds a small `/* Utilities */`
CSS section — adopting states/utilities only where specified, **no grand
sweep**.

**Constraints (hard):** vanilla ES modules, no build step, no Node on the
box. Syntax checking is `gjs` (SpiderMonkey `Reflect.parse`) — included in
every stage's verification. The `_CacheBustJS` middleware in
`src/web_server/server.py` rewrites every JS import specifier to
`?v={boot id}` per boot, so **cache busting is automatic**; new modules
(`tab-strip.js`, `states.js`) need no registration anywhere — panels export
`mount(container)` and are dynamically imported, and static imports inside
them are rewritten too. Restart the server after editing to bust caches.

**Honesty note on verification:** the pytest suite covers Python only. JS
correctness here rests on (a) `gjs` parse checks, (b) the grep gates below,
(c) careful review against the exact edit rules, and (d) the manual
browser checks at the end of each stage. There is no JS test runner on the
box; CI's `js-lint` job (eslint `eslint:recommended` + stylelint,
`continue-on-error: true`) runs on push but does not execute code.

**Working-tree note:** `git status` currently shows uncommitted changes in
several files this plan touches (`games-panel-shared.js`, `games-price.js`,
`games-rushmore.js`, `games-clapback.js`, `app.css`, `index.html`, …).
Implement this plan **only on a clean tree** (let those changes land first).
If line numbers have drifted, the quoted code blocks are authoritative —
locate by content, not by line.

## 2. The design

### 2.1 `js/api.js` — the request core (full new file content)

Replace the entire file with:

```js
// Tiny fetch wrapper. All endpoints are same-origin JSON.

/** Escape a string for safe insertion into innerHTML, including attributes.
 *  null/undefined render as "" (matches every panel-local variant this
 *  replaced; nobody wants a literal "null" in the page). */
export function esc(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

/**
 * Canonical timestamp formatter: "Jun 10, 3:42 PM", with the year added
 * when it isn't the current year. Accepts unix seconds, ISO strings, or Date.
 */
export function fmtTs(ts) {
  if (!ts) return "—";
  const d = ts instanceof Date ? ts : new Date(typeof ts === "number" ? ts * 1000 : ts);
  if (isNaN(d.getTime())) return "—";
  const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleString(undefined, opts);
}

/** Compact age/duration from seconds: "5d 4h", "3h 12m", "45m", "30s". */
export function fmtAge(seconds) {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/**
 * The one fetch core. Everything HTTP in the dashboard goes through here
 * (help.js's HTML fetch is the sole exception).
 *
 * - `params`: query-string object; null/undefined/"" entries are skipped.
 * - `body`: JSON-encoded with a Content-Type header. A FormData body is
 *   passed through untouched (the browser sets the multipart boundary).
 *   `body === undefined` sends no body and no Content-Type.
 * - `on401`: override the default `/login` redirect (wellness pages
 *   redirect to Discord OAuth instead).
 *
 * Errors throw `Error("<status>: <detail>")`, preferring the body's
 * `error` field (wellness endpoints), then `detail` (FastAPI — arrays of
 * validation errors are joined), then statusText.
 */
export async function request(method, path, { params, body, on401 } = {}) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      url.searchParams.set(k, v);
    }
  }
  const opts = { method, credentials: "same-origin" };
  if (body instanceof FormData) {
    opts.body = body;
  } else if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401) {
    if (on401) on401(); else window.location = "/login";
    return new Promise(() => {}); // hang — page is navigating away
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const b = await res.json();
      if (b.error) detail = String(b.error);
      else if (b.detail) detail = Array.isArray(b.detail)
        ? b.detail.map((e) => e.msg || JSON.stringify(e)).join("; ")
        : String(b.detail);
    } catch (_) {}
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export function api(path, params) { return request("GET", path, { params }); }
export function apiPost(path, body) { return request("POST", path, { body: body || {} }); }
export function apiPut(path, body) { return request("PUT", path, { body }); }
export function apiDelete(path) { return request("DELETE", path); }
```

Notes for the implementer:

- `fmtTs`/`fmtAge` are byte-for-byte the current code — do not "improve".
- `apiPost(path)` with no body sends `{}`, exactly like today's `apiPost`.
- The old duplicated 401/error blocks in `api`/`apiPost` are gone; both are
  now one-liners over `request`.

### 2.2 `js/config-helpers.js` — shrink to re-exports

**Edit A — imports (lines 1–3).** Change

```js
// Shared helpers for config panels.
import { api } from "./api.js";
import { filterSelect, multiFilterSelect } from "./filter-select.js";
```

to

```js
// Shared helpers for config panels.
import { api, apiPut, esc } from "./api.js";
import { filterSelect, multiFilterSelect } from "./filter-select.js";

// Canonical escaping + write verbs live in api.js; re-exported here so the
// 35 existing panel importers keep working unchanged.
export { esc, esc as escapeHtml, apiPut, apiDelete } from "./api.js";
```

(The `import` line binds `api`, `apiPut` (used by `saveSection`), and `esc`
(used by the select builders) locally; the `export … from` clause does not
create local bindings, so there is no redeclaration conflict.)

**Edit B — delete the local `escapeHtml` + alias (current lines 5–17):**

```js
// HTML-escape a value before interpolating it into innerHTML.
// Use this whenever a Discord name (channel, role, category) goes into a template string.
export function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Module-local alias so the select builders below stay readable.
const esc = escapeHtml;
```

Delete the whole block. All later `esc(...)` uses in this file now resolve
to the api.js import from Edit A.

**Edit C — delete the local `apiPut` and `apiDelete` functions** (currently
lines 209–236, the two blocks starting `// Patched api() that supports PUT
with JSON body`, including that comment). `saveSection` (lines 205–207)
stays exactly as is and now calls the imported `apiPut`.

Everything else in the file (loaders, pickers, `showStatus`, `buildField`,
select builders) is untouched.

### 2.3 `js/wellness-helpers.js` — delegate to the core (full new file content)

```js
// Shared helpers for wellness SPA panels.
//
// Wellness pages are member-facing: an expired session redirects to Discord
// OAuth with a return_to (not the staff /login page), and mutation endpoints
// signal soft failure via {ok: false, error: "..."} on a 200.
import { request } from "./api.js";

function redirectToWellnessLogin() {
  const url = new URL("/auth/discord", window.location.origin);
  url.searchParams.set("return_to", window.location.href);
  window.location = url.toString();
}

export function wGet(path) {
  return request("GET", path, { on401: redirectToWellnessLogin });
}

async function _mutate(method, path, body) {
  const data = await request(method, path, { body, on401: redirectToWellnessLogin });
  if (data.ok === false) throw new Error(data.error || data.detail || "Request failed");
  return data;
}

export function wPost(path, body) { return _mutate("POST", path, body); }
export function wPut(path, body) { return _mutate("PUT", path, body); }
export function wDelete(path) { return _mutate("DELETE", path); }

export { esc } from "./api.js";
export { showStatus } from "./config-helpers.js";
```

Behavior notes (all deliberate — see §5): the old wrapper's
`s == null ? "" : _esc(s)` esc shim is now literally the canonical `esc`
(null-safe since §2.1), so a plain re-export preserves behavior exactly.
`wDelete` passes `body === undefined` → core sends no body, same as today.
Error strings gain a `"<status>: "` prefix, and the core prefers `b.error`
(the wellness shape) exactly like the old code did.

### 2.4 Raw-fetch panel conversions (7 files; `help.js` untouched)

General rule: convert every `fetch()` site to `api`/`apiPost`/`apiDelete`
from `../api.js`, deleting the local `res.ok` boilerplate. Exact per-file
rules — locate by the quoted code, not line numbers:

**`panels/config-voice-master.js`** — delete the three local wrappers
(`apiGet`, `apiPost`, `apiDel`, currently lines 4–29 including their bodies)
and add to the top of the file, above the config-helpers import:

```js
import { api, apiPost, apiDelete } from "../api.js";
```

Then rename call sites: `apiGet(` → `api(` (4 sites: the three
`Promise.all` loads and `const fresh = await apiGet("/api/voice-master/config")`),
`apiDel(` → `apiDelete(` (1 site: name-blocklist delete). The 3 `apiPost(`
call sites keep their name and now resolve to the import. (The query string
in `api("/api/meta/channels?types=text,voice,category")` is preserved by
`new URL(path, origin)` — no `params` object needed.)

**`panels/config-ai.js`** — line 2 becomes
`import { api, esc, apiPost, apiDelete } from "../api.js";`. Three sites:

1. model-reload:
   `await fetch("/api/config/ai/model-reload", { method: "POST", credentials: "same-origin" });`
   → `await apiPost("/api/config/ai/model-reload");`
2. prompt reset — replace

   ```js
            const res = await fetch(`/api/config/ai/prompts/${key}`, {
              method: "DELETE",
              credentials: "same-origin",
            });
            if (!res.ok) throw new Error(`${res.status}`);
   ```

   with

   ```js
            await apiDelete(`/api/config/ai/prompts/${key}`);
   ```

3. run-test — replace the whole `try` body and catch:

   ```js
          try {
            const result = await apiPost(`/api/config/ai/prompts/${key}/test`, { user_input: testInput.value });
            testStatus.textContent = "Done";
            testOutput.textContent = result.result;
          } catch (err) {
            testStatus.textContent = "";
            testOutput.textContent = `Error: ${err.message}`;
          }
   ```

   (This drops the old `res.ok` branch **and** the `esc()` inside the old
   catch — escaping inside `textContent` was a double-escape bug.)

**`panels/config-booster-roles.js`** — add
`import { api, apiPost } from "../api.js";` above the config-helpers
import. Four sites:

1. `loadSwatches`: the `fetch`+`if (!res.ok) throw new Error(res.statusText)`
   pair → `renderSwatchList(await api("/api/config/booster-roles/swatches"));`
2. swatch upload (FormData): the `fetch(..., { method: "POST", …, body: fd })`
   + `if (!res.ok) { … }` + `const data = await res.json();` block →
   `const data = await apiPost("/api/config/booster-roles/swatches", fd);`
3. sync-swatches: same shape →
   `const data = await apiPost("/api/config/booster-roles/sync-swatches");`
4. post-panel: →
   `const data = await apiPost("/api/config/booster-roles/post-panel", { channel_id: channelId });`

Leave the local `_esc` alone in this stage (Stage 2 removes it).

**`panels/config-branding.js`** — add `import { apiPost } from "../api.js";`.
The bot-identity FormData site (fetch + 401-less res.ok block +
`const data = await res.json();`) → `const data = await apiPost("/api/config/bot-identity", fd);`

**`panels/config-dms.js`** — add `import { apiPost } from "../api.js";`.
The post-panel site → `await apiPost("/api/config/dms/post-panel", { channel_id: channelId });`
(the old code never used the parsed response — do not bind `data`).

**`panels/config-global.js`** — add `import { api } from "../api.js";`.
Change

```js
      fetch("/api/config/support-access", { credentials: "same-origin" }).then(r => r.ok ? r.json() : { enabled: false }),
```

to

```js
      api("/api/config/support-access").catch(() => ({ enabled: false })),
```

(keeps the graceful fallback; the sibling `loadConfig()` in the same
`Promise.all` already redirects on 401, so behavior on auth loss is
unchanged). Leave `_esc` alone in this stage.

**`panels/config-prune.js`** — line 2 becomes
`import { api, esc, apiPost } from "../api.js";`. The preview site
(fetch POST + res.ok block + `const data = await res.json();`) →

```js
        const data = await apiPost("/api/config/prune/preview", {
          role_id: roleId,
          inactivity_days: days,
          exempt_user_ids: exemptions.map((e) => String(e.id)),
        });
```

### 2.5 esc convergence (Stage 2) — full verified inventory

24 definitions exist; after this stage exactly **one** remains
(`api.js`). `config-helpers.js` / `wellness-helpers.js` were handled in
Stage 1. Remaining 21 files:

| File | Local to delete | Edit |
|---|---|---|
| `js/tiles/tile-helpers.js` | `export function esc(s) { if (!s) return ""; … }` (lines 49–54) | Delete; add at top: `export { esc } from "../api.js";` |
| `js/transcript-modal.js` | `function esc(s) { if (!s) return ""; … }` (lines 8–13) | Delete; line 1 → `import { api, fmtTs, esc } from "./api.js";` |
| `panels/home.js` | none (imports from tile-helpers) | Line 4 `import { esc } from "../tiles/tile-helpers.js";` → `import { esc } from "../api.js";` |
| `panels/activity.js` | `function escHtml(s) { const d = … }` (~line 50) | Delete; rename its 1 other `escHtml(` call site to `esc(` (file already imports `esc` from `../api.js`) |
| `panels/live-log.js` | `function escHtml(s) { return s.replace(/&/g,…) }` (~lines 54–56) | Delete; rename 2 call sites to `esc(`; add as first line: `import { esc } from "../api.js";` (file currently has **no** imports) |
| `panels/connection-graph.js` | one-liner `function esc(s) { const d = … }` (line 19) | Delete; line 1 → `import { api, esc } from "../api.js";` |
| `panels/config-needle.js` | `function esc(s) { return String(s ?? "")… }` (lines 36–40) | Delete; add `import { esc } from "../api.js";` after existing imports (local was semantically identical — pure dedupe) |
| `panels/config-global.js` | `function _esc(str) { … }` (lines 3–9) | Delete; rename 3 `_esc(` sites to `esc(`; add `import { esc } from "../api.js";` |
| `panels/config-booster-roles.js` | `function _esc(str) { … }` (lines 4–10) | Delete; rename 6 `_esc(` sites to `esc(`; add `esc` to the `../api.js` import added in Stage 1 |
| 13 × `panels/health-*.js` (`channel-health`, `churn-risk`, `cohort-retention`, `composite-score`, `dau-mau`, `gini`, `heatmap`, `message-feed`, `mod-engagement`, `mod-workload`, `newcomer-funnel`, `sentiment-feed`, `sentiment`) | one-liner `function esc(s) { const d = document.createElement("div"); … }` near top | Delete the line; add `esc` to each file's existing `import { … } from "../api.js";` list |

Null/falsy-handling ruling (the subtle bit — decided, do not re-litigate):

- Canonical `esc` is now **null-safe** (§2.1): `esc(null)`/`esc(undefined)`
  → `""`. This matches the DOM-based locals on `null`, the
  `String(s ?? "")` locals on both, and *improves* on the DOM locals'
  `undefined` → `"undefined"`.
- The two falsy-guard locals (`tile-helpers`, `transcript-modal`) mapped
  `0`/`false`/`""` → `""` as well; canonical gives `"0"`/`"false"`/`""`.
  **No wrapper is added.** Call-site audit (all 23 `esc(` sites reachable
  from tile-helpers/home tiles, and transcript-modal's) shows every argument
  is either a plain string or an `x || y`-guarded string — `0`/`false` can't
  reach them. Converging without a wrapper is the point of the exercise.
- The DOM-based locals and the two `_esc` variants did **not** escape `'`
  (DOM ones not `"` either). Canonical escapes both — output differs only
  by `&quot;`/`&#39;` entities, which render identically and are strictly
  safer in attribute contexts.

### 2.6 `js/tab-strip.js` (new file, Stage 3)

```js
// Shared wiring for .ctrl-group button strips (filter strips and tab strips).
//
// Markup contract (unchanged from the adopting panels — keep their existing
// role="group"/aria-label attributes):
//   <div class="ctrl-group" role="group" aria-label="Filter jails" data-filter-group>
//     <button class="active" data-filter="active">Active</button>
//     <button data-filter="all">All</button>
//   </div>
//
// Wires one delegated click listener on groupEl, keeps exactly one button
// .active, and calls onChange(value) with the clicked button's attribute
// value ("" is a valid value — several strips use it for "All"). Clicking
// the already-active button fires onChange again; panels rely on that as a
// cheap refresh. Returns { setActive } for programmatic sync.
export function makeFilterStrip(groupEl, onChange, { attr = "data-filter" } = {}) {
  function setActive(value) {
    groupEl.querySelectorAll(`button[${attr}]`).forEach((b) => {
      b.classList.toggle("active", b.getAttribute(attr) === value);
    });
  }
  groupEl.addEventListener("click", (e) => {
    const btn = e.target.closest(`button[${attr}]`);
    if (!btn || !groupEl.contains(btn)) return;
    setActive(btn.getAttribute(attr));
    onChange(btn.getAttribute(attr));
  });
  return { setActive };
}
```

Adoptions (each panel adds `import { makeFilterStrip } from "../tab-strip.js";`
and deletes its `filterGroup.addEventListener("click", …)` block; markup and
ARIA are untouched):

- **`panels/mod-jails.js`** and **`panels/todo.js`** — replace the delegated
  block with:

  ```js
  makeFilterStrip(filterGroup, (value) => {
    state.filter = value;
    state.activeId = null;
    render();
  });
  ```

- **`panels/mod-tickets.js`** —

  ```js
  makeFilterStrip(filterGroup, (value) => {
    state.filter = value;
    state.activeId = null;
    if (state.filter === "closed" && state.closedTickets === null) {
      refreshClosed();
    } else {
      render();
    }
  });
  ```

  `setFilterBadge` (rewrites button labels/badges by value) is **not**
  active-state syncing — leave it exactly as is.

- **`panels/mod-policy-tickets.js`** —

  ```js
  makeFilterStrip(filterGroup, (value) => {
    currentFilter = value;
    refresh();
  });
  ```

- **`panels/rules-watch.js`**, tier strip (buttons use `data-tier`) —

  ```js
  makeFilterStrip(filterGroup, (tier) => {
    currentTier = tier;
    loadQueue();
  }, { attr: "data-tier" });
  ```

- **`panels/rules-watch.js`**, `data-tabs` pane switcher — **adopt it**: the
  shared part (single-active toggle + click wiring) is exactly what
  `makeFilterStrip` does; the pane show/hide + lazy stats load belong in the
  callback. Replace the whole `tabBtns.forEach(btn => { btn.addEventListener(…) })`
  block with:

  ```js
  makeFilterStrip(container.querySelector("[data-tabs]"), (tab) => {
    activeTab = tab;
    queuePane.style.display = activeTab === "queue" ? "" : "none";
    statsPane.style.display = activeTab === "stats" ? "" : "none";
    if (activeTab === "stats") loadStats();
  }, { attr: "data-tab" });
  ```

  Then delete the now-unused `const tabBtns = container.querySelectorAll("[data-tabs] button");`
  declaration (eslint `no-unused-vars` would warn).

### 2.7 `js/states.js` (new file, Stage 3)

New file, not `ui.js`: `ui.js` is imperative DOM (toasts, focus-trapped
dialogs); these are pure HTML-string builders and importing them shouldn't
drag dialog plumbing into every panel.

```js
// Shared loading/empty/error fragments for panel innerHTML.
// These exist so new code stops hand-rolling `<div class="empty">…` — only
// the highest-traffic panels were converted retroactively (see plan #13).
import { esc } from "./api.js";

/** Pass the full message: renderLoading("Loading jails…"). */
export function renderLoading(msg = "Loading…") {
  return `<div class="empty">${esc(msg)}</div>`;
}

export function renderEmpty(msg) {
  return `<div class="empty">${esc(msg)}</div>`;
}

/** Accepts an Error or a string. Renders with the .error style. */
export function renderError(err) {
  const msg = err instanceof Error ? err.message : String(err);
  return `<div class="error">Error: ${esc(msg)}</div>`;
}
```

Adopt in exactly these 10 panels (all touched by this plan anyway or
top-traffic): `mod-jails.js`, `mod-tickets.js`, `mod-policy-tickets.js`,
`todo.js`, `rules-watch.js`, `home.js`, `activity.js`, `message-search.js`,
`games-panel-shared.js`, `connection-graph.js`. Rules:

- Replace only sites matching these shapes (skip anything fancier):
  - `` `<div class="empty">Loading …</div>` `` → `renderLoading("Loading …")`
    (keep the message text verbatim; when wrapped as
    `` `<div class="panel"><div class="empty">…</div></div>` ``, produce
    `` `<div class="panel">${renderLoading("…")}</div>` ``).
  - `` `<div class="empty">No …</div>` `` (any static empty message) →
    `renderEmpty("No …")`.
  - `` `<div class="empty">Error: ${esc(err.message)}</div>` `` and
    `` `<div class="error">${esc(err.message)}</div>` `` (with or without a
    `style="padding:20px"`) → `renderError(err)`.
- **Deliberate visual change:** `renderError` standardizes on
  `class="error"` + `"Error: "` prefix. Former `.empty` error sites gain the
  error styling; former `.error` sites gain the prefix. Both classes already
  exist in `app.css`. If a converted site loses a `style="padding:20px"`,
  that's accepted.
- Do **not** sweep the other ~130 sites (follow-up).

### 2.8 `app.css` utilities (Stage 3)

Append at the end of `src/web_server/static/app.css` (after the
`.btn-secondary:hover` block; there is no existing utilities section):

```css
/* ── Utilities ───────────────────────────────────────────────────────────
   Single-purpose classes for the most-repeated inline styles (audited
   2026-07: width:100% ×27, margin-top:14px ×22, margin-bottom:10px ×14, …).
   Keep this list short — add a utility only when the same declaration
   repeats ~8+ times; anything richer deserves a component class. */
.w-full { width: 100%; }
.m-0 { margin: 0; }
.mt-8 { margin-top: 8px; }
.mt-14 { margin-top: 14px; }
.mb-8 { margin-bottom: 8px; }
.mb-10 { margin-bottom: 10px; }
.mb-16 { margin-bottom: 16px; }
/* Horizontal control row: flex, 8px gap, vertically centered. */
.row-8 { display: flex; gap: 8px; align-items: center; }
```

Convert inline styles **only** in the 5 worst files
(`games-legitlibs.js`, `games-panel-shared.js`, `games-scheduling.js`,
`games-studio.js`, `connection-graph.js`) and **only** where the entire
`style` attribute value equals one utility's declaration (modulo trailing
semicolon/whitespace), or equals exactly
`display:flex;gap:8px;align-items:center;` → `row-8`:

- `style="width:100%;"` / `style="width:100%"` → drop the style attr, add
  `w-full` to the element's `class` (create `class="w-full"` if absent,
  else append with a space).
- Same pattern for `margin:0;`→`m-0`, `margin-top:8px;`→`mt-8`,
  `margin-top:14px;`→`mt-14`, `margin-bottom:8px;`→`mb-8`,
  `margin-bottom:10px;`→`mb-10`, `margin-bottom:16px;`→`mb-16`.
- **Never split a composite style attribute** (e.g.
  `style="width:300px;flex-shrink:0;"` stays). If a `style` contains a
  utility declaration plus anything else, leave it whole. NO renames, no
  other files.

## 3. Stage plan (Sonnet implementers — follow verbatim)

All commands run from `/home/ben/discord-bots/dungeon-keeper`. Each stage is
independently committable; commit at the end of each stage. `STATIC` below
means `src/web_server/static`.

The gjs syntax check used in every stage (no Node on this box; gjs =
SpiderMonkey; `{ target: "module" }` is required because panels use
top-level `import`):

```bash
cd src/web_server/static && gjs -c '
const GLib = imports.gi.GLib;
let fail = 0;
for (const f of ARGV) {
  const [ok, bytes] = GLib.file_get_contents(f);
  try { Reflect.parse(new TextDecoder().decode(bytes), { target: "module" }); }
  catch (e) { fail = 1; print(f + ": " + e.message); }
}
print(fail ? "SYNTAX ERRORS" : "ALL OK");
' <files>; cd -
```

### Stage 1 — One fetch core

**Files:** `STATIC/js/api.js`, `STATIC/js/config-helpers.js`,
`STATIC/js/wellness-helpers.js`, and `STATIC/js/panels/`:
`config-voice-master.js`, `config-ai.js`, `config-booster-roles.js`,
`config-branding.js`, `config-dms.js`, `config-global.js`,
`config-prune.js`.

**Edits:** §2.1 (full file), §2.2 (Edits A–C), §2.3 (full file), §2.4 —
exactly as written. Do **not** touch `help.js` (fetches HTML — stays raw),
`app.js` (one raw fetch at guild-select — follow-up), or any esc definition
outside §2.2/§2.3.

**Verification (all must pass):**

```bash
# gjs parse — every edited file (run the gjs block above with):
#   js/api.js js/config-helpers.js js/wellness-helpers.js \
#   js/panels/config-voice-master.js js/panels/config-ai.js \
#   js/panels/config-booster-roles.js js/panels/config-branding.js \
#   js/panels/config-dms.js js/panels/config-global.js js/panels/config-prune.js
# expect: ALL OK

grep -c "fetch(" src/web_server/static/js/api.js
# expect: 1  (the core)
grep -n "fetch(" src/web_server/static/js/config-helpers.js src/web_server/static/js/wellness-helpers.js \
  src/web_server/static/js/panels/config-voice-master.js src/web_server/static/js/panels/config-ai.js \
  src/web_server/static/js/panels/config-booster-roles.js src/web_server/static/js/panels/config-branding.js \
  src/web_server/static/js/panels/config-dms.js src/web_server/static/js/panels/config-global.js \
  src/web_server/static/js/panels/config-prune.js
# expect: no output
grep -n "escapeHtml\|apiPut\|apiDelete" src/web_server/static/js/config-helpers.js
# expect: exactly 2 lines — the import line and the re-export line — plus saveSection's apiPut call

.venv/bin/ruff check .            # Python untouched — must stay clean
.venv/bin/python -m pytest -q     # Python-only suite; must stay green (does not test JS)
```

**Browser check (JS can't be executed locally — do this after restarting
the server; the boot-id middleware busts all module caches):** load the
dashboard; open Voice Master, AI, Booster Roles, Branding, DMs, Global, and
Prune config panels; exercise one write per panel (e.g. save AI prompt test,
prune preview, booster swatch upload — the two FormData paths in Booster
Roles/Branding are the highest-risk sites). Open a wellness page while
logged in. Log out in another tab and click a filter → expect the `/login`
redirect.

**STOP conditions:** grep finds a `fetch(` remaining in the 7 panels or a
site whose shape doesn't match §2.4's quoted code (inventory was wrong) →
abort and report the file. Any call site is discovered passing FormData to
`apiPut`/`apiDelete` or relying on a non-JSON success response → abort and
report (the core assumes JSON responses). Do not improvise extra options on
`request`.

**Commit:** `Dashboard: one fetch core in api.js; helpers + 7 panels converge (U3 #13, stage 1)`

### Stage 2 — One esc

**Files:** the 21 files in §2.5's table.

**Edits:** §2.5 exactly as written. Behavior deltas are pre-approved in
§2.5/§5 — do not add compatibility wrappers.

**Verification:**

```bash
cd src/web_server/static/js && grep -rn "function esc\|function escHtml\|function _esc\|const esc = \|const escHtml = \|const _esc = " . | grep -v "esc as\|api.js:"; cd -
# expect: no output (api.js's own definition is filtered; nothing else defines an escaper)
grep -rn "escHtml(\|_esc(" src/web_server/static/js
# expect: no output
grep -n 'from "../tiles/tile-helpers.js"' src/web_server/static/js/panels/home.js
# expect: no output (home.js now imports esc from ../api.js)
# gjs parse on all 21 edited files (same one-liner) — expect: ALL OK

.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

**Browser check:** restart, then spot-check panels that render
user-controlled strings: a health panel (e.g. Channel Health), Activity,
Live Log (watch a log line with `<` or `&` in it render escaped),
Connection Graph, transcript modal from Mod Tickets, home tiles. Names
containing quotes/apostrophes must render normally.

**STOP conditions:** the inventory grep reveals a definition not in §2.5's
table → abort and report (inventory incomplete). Any local variant turns
out to do something other than HTML-escaping (e.g. truncation, markdown) →
abort and report; do not fold extra behavior into `esc`.

**Commit:** `Dashboard: converge 24 esc variants on null-safe api.js esc (stage 2)`

### Stage 3 — Tab strips, state fragments, CSS utilities

**Files:** new `STATIC/js/tab-strip.js`, new `STATIC/js/states.js`,
`STATIC/app.css`, and `STATIC/js/panels/`: `mod-jails.js`, `mod-tickets.js`,
`mod-policy-tickets.js`, `todo.js`, `rules-watch.js`, `home.js`,
`activity.js`, `message-search.js`, `games-panel-shared.js`,
`connection-graph.js`, `games-legitlibs.js`, `games-scheduling.js`,
`games-studio.js`.

**Edits:** §2.6, §2.7, §2.8 exactly as written. No `index.html` change —
the new modules are reached only via ES imports, and the server's boot-id
rewrite covers them automatically.

**Verification:**

```bash
grep -rn 'addEventListener("click"' src/web_server/static/js/panels/mod-jails.js \
  src/web_server/static/js/panels/mod-tickets.js src/web_server/static/js/panels/mod-policy-tickets.js \
  src/web_server/static/js/panels/todo.js src/web_server/static/js/panels/rules-watch.js \
  | grep -i "filterGroup\|tabBtns"
# expect: no output (strip wiring fully replaced; other click listeners remain)
grep -rln "makeFilterStrip" src/web_server/static/js/panels | sort
# expect: exactly the 5 strip panels
grep -rn "data-filter-group\|data-tabs" src/web_server/static/js/panels/mod-jails.js src/web_server/static/js/panels/rules-watch.js | head
# expect: markup lines unchanged (role="group"/aria-label intact)
grep -n 'style="width:100%;\?"\|style="margin:0;\?"\|style="margin-top:14px;\?"\|style="margin-bottom:10px;\?"\|style="display:flex;gap:8px;align-items:center;\?"' \
  src/web_server/static/js/panels/games-legitlibs.js src/web_server/static/js/panels/games-panel-shared.js \
  src/web_server/static/js/panels/games-scheduling.js src/web_server/static/js/panels/games-studio.js \
  src/web_server/static/js/panels/connection-graph.js
# expect: no output (exact-match attributes converted; composites untouched)
grep -c "Utilities" src/web_server/static/app.css
# expect: 1
# gjs parse on tab-strip.js, states.js, and all 13 edited panels — expect: ALL OK

.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

**Browser check:** restart, then: Mod Jails / Mod Tickets / Policy Tickets
/ To-Do / Rules Watch — click every filter button (including re-clicking
the active one: list must refresh, not dead-click), switch Rules Watch
Queue↔Stats tabs (stats must lazy-load on first switch); Mod Tickets
"Closed" must trigger the closed fetch exactly once. Then eyeball the five
style-converted panels (LegitLibs, games shared strip, Scheduling, Studio,
Connection Graph) for layout regressions — full-width inputs still full
width, control rows still aligned. Force an error (e.g. stop the bot API or
use devtools offline) on one converted panel to see `renderError`'s red
`Error: …` block.

**STOP conditions:** a strip's existing click handler is found to do
anything beyond toggle-active + set-state + refresh (doesn't map onto
`onChange`) → leave that strip unconverted and report it. A style
conversion would require splitting a composite `style` attribute → skip
that site (it stays inline), never split. `setFilterBadge` in mod-tickets
stops showing counts → abort (its buttons' children were disturbed).

**Commit:** `Dashboard: shared tab-strip + state fragments + CSS utilities (stage 3)`

### CI expectations (all stages)

The `js-lint` job (`.github/workflows/test.yml`, `continue-on-error: true`)
runs `npx eslint src/web_server/static/js` (eslint:recommended, module
sourceType) and stylelint. It cannot be run locally (no Node). Expectations:
**no new eslint errors** — the patterns used here (ES module re-exports,
optional destructured params, `catch (_)` with `no-empty` allowEmptyCatch,
`no-unused-vars` warns with `^_` argsIgnorePattern) are all clean under the
repo's `.eslintrc.json`. Deleting local functions must delete their *only*
references too, or `no-unused-vars` will flag them — the per-file rules
above account for every call site. The utilities CSS block is plain
declarations; stylelint-safe.

## 4. Risks & deliberate behavior changes

Every change below is intentional; anything not listed must be
byte-for-byte behavior-preserving.

- **`esc(null)`/`esc(undefined)` now return `""`** (canonical previously
  returned `"null"`/`"undefined"`, as did `config-helpers.escapeHtml`).
  Safe: a literal "null" in the page is always a display bug; 21 of the 23
  duplicate variants already suppressed at least `null`.
- **`esc(0)`/`esc(false)` return `"0"`/`"false"`** at former
  tile-helpers/transcript-modal call sites (previously `""`). Audited: no
  reachable call site can pass a non-string falsy (all use `x || y` guards
  or plain strings).
- **Quote escaping everywhere.** DOM-based variants didn't escape `"`/`'`
  (attribute-injection risk); the two `_esc` variants didn't escape `'`.
  Output gains `&quot;`/`&#39;` entities — renders identically, strictly
  safer.
- **`apiPut`/`apiDelete` now redirect on 401** (previously threw
  `401: Unauthorized` into the panel's catch). Safe: matches
  `api`/`apiPost`; a 401 means the session is gone and every subsequent
  call would fail anyway.
- **Wellness error messages gain a `"<status>: "` prefix** and non-JSON
  error bodies no longer surface as `SyntaxError` (the core parses
  defensively). Redirect target and `data.ok === false` semantics
  unchanged. The core checking `b.error` before `b.detail` is new for
  non-wellness endpoints — harmless, staff endpoints don't emit `error`.
- **Raw-fetch panels gain 401 redirects** (previously an expired session
  produced a confusing error toast). FastAPI array-of-validation-errors
  details are now joined readably.
- **`config-ai` model-reload now surfaces HTTP errors** (old code ignored
  `res.ok` entirely and always showed "Reload started"). Improvement.
- **`config-ai` run-test error text is no longer double-escaped**
  (`esc()` inside `textContent` was a bug).
- **booster sync-swatches POST now sends a `{}` body** (old code sent a
  Content-Type header with no body). FastAPI endpoints without a body param
  ignore it; with an optional body, `{}` is equivalent.
- **`renderError` standardizes `class="error"` + `"Error: "` prefix** at
  the 10 adopted panels (former `.empty` error sites change style, former
  `.error` sites gain the prefix). Deliberate: errors should look like
  errors; both classes already styled in `app.css`.
- **`makeFilterStrip` toggles only `button[<attr>]` elements** (old code
  toggled every `button` in the group). Today every button in every adopted
  group carries the attribute, so no visible change; badge `<span>`s inside
  buttons still work via `closest()`.
- **FormData support lives in the core.** If a future endpoint needs a
  non-JSON *response*, `request` is the wrong tool (it always `res.json()`s)
  — that's why `help.js` stays raw.
- **No JS execution in verification.** gjs parses, it doesn't run; the
  browser checks are mandatory, not optional polish.

## 5. Rejected alternatives

- **Unifying wellness onto the `/login` redirect:** wellness pages are
  member-facing deep links; `/auth/discord?return_to=…` preserves the
  user's place through OAuth. The `on401` hook is the entire cost.
- **Keeping `esc(null) → "null"`:** would force every panel-local variant
  to keep a wrapper (wellness-helpers proves the wrapper already existed);
  no call site wants the literal text.
- **A falsy-guard wrapper in tile-helpers (wellness-style):** audit showed
  no reachable non-string falsy args; keeping a second contract alive
  defeats the convergence.
- **`makeFilterStrip` owning panes (`data-tab-content` show/hide):**
  one consumer (rules-watch); pane logic in the callback keeps the helper
  a 15-liner.
- **States helpers in `ui.js`:** ui.js is imperative dialog/toast plumbing
  with its own module state; string builders don't belong there and would
  fatten every panel's import.
- **Sweeping all 141 empty-sites / all inline styles / renaming classes:**
  churn without payoff; the helpers and utilities exist so *new* code has a
  canonical spelling. Explicitly out of scope.
- **A generic `on401`-less core with global config:** implicit global state
  across panels is how the wellness/staff split got confusing to begin
  with; an explicit per-call option is self-documenting.
- **Node-based tooling (bundler, test runner):** forbidden constraint — no
  Node on the box, no build step; gjs + CI eslint is the toolchain.

## 6. Follow-ups (out of scope)

- `app.js:679` guild-select raw `fetch` → `apiPost` (needs a look at its
  redirect-on-401 expectations during guild switching).
- Opportunistic `renderLoading`/`renderEmpty`/`renderError` adoption in the
  remaining ~130 sites as panels get touched.
- Convert remaining exact-match inline styles (`width:100%` etc.) outside
  the 5 worst files; consider `.mt-20`, `.wrap-12` if counts justify.
- `mod-tickets` could use the returned `setActive` if it ever gains
  programmatic filter switching (deep links).
- Flip CI `js-lint` off `continue-on-error` once the backlog of pre-existing
  warnings is burned down.
- `filter-select.js` vs the private pickers in `activity.js` /
  `connection-graph.js` — a bigger dedupe, separate plan.
