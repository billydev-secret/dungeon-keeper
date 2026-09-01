// Shared helpers for config panels.
import { api, apiDelete, apiPut, esc } from "./api.js";
import { filterSelect, multiFilterSelect } from "./filter-select.js";
import { renderError } from "./states.js";
import { confirmDialog, toast } from "./ui.js";

// Canonical escaping + write verbs live in api.js; re-exported here so the
// 35 existing panel importers keep working unchanged.
export { esc, esc as escapeHtml, apiPost, apiPut, apiDelete } from "./api.js";

let _configCache = null;
let _channels = null;
let _roles = null;

// ── Unsaved-changes tracker (W-C1) ─────────────────────────────────────
// Contract (app.js consumes the window globals):
//   - guardForm(form) — call once per panel mount on the form/container
//     element; any `input` or `change` event inside it marks the page dirty.
//   - window.__dkDirty()      → boolean: are there unsaved edits?
//   - window.__dkDirtyReset() → clear the flag (app.js calls this after the
//     user confirms discarding, and on every panel mount).
//   - A successful save shown via showStatus(el, true, …) clears the flag.
//   - A beforeunload handler (wired once, below) warns when dirty.
// Tracked per guarded container rather than as one page-level boolean. It was
// a single flag that ANY successful save cleared, so on the fourteen panels
// that guard two to four forms, saving one form — or any unrelated action that
// reported success, a toggle, an upload, posting a panel — silently disarmed
// the unsaved-edits warning protecting half-typed values in all the others.
const _dirtyForms = new Set();

window.__dkDirty = () => _dirtyForms.size > 0;
window.__dkDirtyReset = () => { _dirtyForms.clear(); };

window.addEventListener("beforeunload", (e) => {
  if (!_dirtyForms.size) return;
  e.preventDefault();
  e.returnValue = ""; // legacy browsers need a non-null returnValue
});

/**
 * Track unsaved edits on a config form. Attach once per panel mount to the
 * form (or any container) element; edits inside it set the dirty flag that
 * app.js checks before navigation/guild switches. Returns `form` for
 * chaining. See the contract comment above.
 */
export function guardForm(form) {
  const mark = (e) => {
    // A searchable picker's SEARCH box is not a value. Typing "gen" to find
    // #general changes nothing the save would send, yet it fired `input` (and
    // `change` on blur, since it is a real text input) and left the panel
    // claiming unsaved edits on the way out. The widget announces genuine value
    // changes with a bubbling `dk:change` instead — see filter-select.js.
    if (e?.target?.classList?.contains("filter-select-input")) return;
    _dirtyForms.add(form);
  };
  // Marks the container as one showStatus can find from a status element
  // inside it, so a save clears its own form and leaves its siblings alone.
  form.dataset.dkGuard = "1";
  form.addEventListener("input", mark);
  form.addEventListener("change", mark);
  form.addEventListener("dk:change", mark);
  return form;
}

// ── Meta loaders (W-C2: failures are remembered, never silently []) ────
// A failed /api/meta/* fetch used to be swallowed to [] — legacy selects
// then rendered "(disabled)" and an unrelated save posted "0" for every
// channel/role field. Now: the failure is recorded (metaLoadFailed()),
// nothing is cached so a remount retries, and the *Select builders below
// fail SAFE by preserving the currently-saved id as a synthetic option.
const _metaFailed = new Set();

/** True when any /api/meta/* load has failed and not since succeeded. */
export function metaLoadFailed() { return _metaFailed.size > 0; }

/**
 * Inline warning banner for panels to prepend when metaLoadFailed().
 * Returns an HTML string ("" when everything loaded fine).
 */
export function renderMetaWarning() {
  if (!metaLoadFailed()) return "";
  return '<div class="meta-warning" role="alert">'
    + "Channel and role lists failed to load. Your saved settings are kept, "
    + "but reload the page before changing channel or role fields.</div>";
}

export async function loadConfig() {
  _configCache = await api("/api/config");
  return _configCache;
}

export function getCachedConfig() { return _configCache; }

export async function loadChannels() {
  if (_channels) return _channels;
  try {
    _channels = await api("/api/meta/channels");
    _metaFailed.delete("channels");
  } catch (_) {
    _metaFailed.add("channels");
    return [];
  }
  return _channels;
}

let _categories = null;
export async function loadCategories() {
  if (_categories) return _categories;
  try {
    _categories = await api("/api/meta/channels?types=category");
    _metaFailed.delete("categories");
  } catch (_) {
    _metaFailed.add("categories");
    return [];
  }
  return _categories;
}

// Fail-safe option HTML for legacy <select> builders when the backing meta
// list failed to load: keep the saved id selected (so a save on an unrelated
// field can't zero it) and surface the failure as a disabled option.
function _failedSelectOptions(kind, selected, noneLabel) {
  let html = "";
  const id = String(selected || "0");
  if (id !== "0") {
    html += `<option value="${esc(id)}" selected>Current setting (id ${esc(id)})</option>`;
  } else {
    html += `<option value="0" selected>${noneLabel}</option>`;
  }
  html += `<option disabled>${kind} failed to load — reload before saving</option>`;
  return html;
}

// A saved id whose role/channel/category the guild no longer has is not in the
// meta list, so no <option> carries `selected` and the browser falls back to
// the first one — "(none)". That reads exactly like a setting nobody ever
// made, and because these panels save the whole form at once, the next save of
// any unrelated field writes 0 over it. Keep the id selected and say what it
// is: the setting survives an unrelated save, and the admin can see there is
// something to fix. Same shape as _failedSelectOptions, which does this for
// the other reason a list can't name an id (the fetch failed).
function _isDangling(options, selected) {
  const id = String(selected == null ? "" : selected);
  if (!id || id === "0") return false;           // "0" is a real "(none)"
  return !options.some((o) => String(o.id) === id);
}

function _danglingOption(kind, selected) {
  const id = esc(String(selected));
  return `<option value="${id}" selected>\u26a0 Missing ${kind} (id ${id})</option>`;
}

export function categorySelect(categories, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("categories") && !categories.length) {
    return _failedSelectOptions("Categories", selected, "(none)");
  }
  let html = _isDangling(categories, selected) ? _danglingOption("category", selected) : "";
  html += allowNone ? '<option value="0">(none)</option>' : "";
  for (const c of categories) {
    const sel = c.id === String(selected) ? " selected" : "";
    html += `<option value="${c.id}"${sel}>${esc(c.name)}</option>`;
  }
  return html;
}

export async function loadRoles() {
  if (_roles) return _roles;
  try {
    _roles = await api("/api/meta/roles");
    _metaFailed.delete("roles");
  } catch (_) {
    _metaFailed.add("roles");
    return [];
  }
  return _roles;
}

let _members = null;
let _bots = null;

// ── The bounded member list ────────────────────────────────────────────
//
// /api/meta/members is paginated (routes/meta.py): it returns the live roster
// first and only as much of the ever-growing departed-member tail as fits. That
// is safe for pickers ONLY because two lookups reach past the page:
//
//   searchMembers(q)    — what a picker calls as the user types, so a member
//                         5,000 rows down is one keystroke away, not missing;
//   resolveMembers(ids) — exact ids, so a config pointing at someone who left
//                         still renders their name instead of a snowflake.
//
// Both memoize, and both are scoped to the ACTIVE guild exactly like _members —
// so both are cleared in resetMetaCaches() below.
const _memberSearches = new Map(); // lowercased query -> MemberMeta[]
const _membersById = new Map();    // id -> MemberMeta | null (null = looked up, absent)
// Bounds a long typing session; queries are a prefix chain, so evicting the
// oldest drops the ones the user has already typed past.
const MEMBER_SEARCH_CACHE_MAX = 60;

/** Index members by id so memberNameLookup and the pickers can find them later. */
function _rememberMembers(members) {
  for (const m of members) _membersById.set(String(m.id), m);
  return members;
}

export async function loadMembers() {
  if (_members) return _members;
  try {
    _members = _rememberMembers(await api("/api/meta/members"));
    _metaFailed.delete("members");
  } catch (_) {
    _metaFailed.add("members");
    return [];
  }
  return _members;
}

/**
 * Server-side member search — the half of a picker that reaches past the
 * bounded first page. Matches username, display name, and (for an all-digit
 * query) the id, across both current and departed members.
 *
 * Resolves to [] rather than rejecting: a failed lookup must leave the locally
 * filtered options standing, because a blanked dropdown reads as "no such
 * member" rather than "the search didn't answer".
 */
export async function searchMembers(q) {
  const key = String(q || "").trim().toLowerCase();
  if (!key) return [];
  if (_memberSearches.has(key)) return _memberSearches.get(key);
  let rows;
  try {
    rows = await api(`/api/meta/members?q=${encodeURIComponent(key)}`);
  } catch (_) {
    return []; // not cached — a retry on the next keystroke is the right move
  }
  if (_memberSearches.size >= MEMBER_SEARCH_CACHE_MAX) {
    _memberSearches.delete(_memberSearches.keys().next().value);
  }
  _memberSearches.set(key, rows);
  return _rememberMembers(rows);
}

/**
 * Look up specific member ids the bounded first page may not include.
 * Returns only the ids that resolved, in the order asked for; ids already
 * known (from the page or an earlier search) cost no request at all.
 */
export async function resolveMembers(ids) {
  const want = [...new Set((ids || []).map(String))].filter(
    (id) => /^\d+$/.test(id) && id !== "0",
  );
  const missing = want.filter((id) => !_membersById.has(id));
  if (missing.length) {
    try {
      _rememberMembers(await api(`/api/meta/members?ids=${missing.join(",")}`));
    } catch (_) {
      return want.map((id) => _membersById.get(id)).filter(Boolean);
    }
    // Remember the misses too, so a row full of genuinely unknown ids doesn't
    // re-ask on every render.
    for (const id of missing) {
      if (!_membersById.has(id)) _membersById.set(id, null);
    }
  }
  return want.map((id) => _membersById.get(id)).filter(Boolean);
}

/**
 * Async counterpart to memberNameLookup(): `(id) => display name` over the
 * bounded page PLUS whichever of `ids` the page didn't reach.
 *
 * Panels that render historical rows (audit logs, ledgers) want this — the
 * people in those rows are exactly the ones most likely to have left.
 */
export async function memberNames(ids) {
  const members = await loadMembers();
  const known = new Set(members.map((m) => String(m.id)));
  const extra = await resolveMembers(
    (ids || []).map(String).filter((id) => !known.has(id)),
  );
  return memberNameLookup(members.concat(extra));
}

/**
 * A picker's `search` callback (see filter-select.js): server-side member
 * lookup, mapped through the same option builder the picker was seeded with so
 * late arrivals are labelled identically to the prefetch.
 */
export function memberSearch(toOptions = toMemberOptions) {
  return async (q) => toOptions(await searchMembers(q));
}

export async function loadBots() {
  if (_bots) return _bots;
  try {
    _bots = await api("/api/meta/bots");
    _metaFailed.delete("bots");
  } catch (_) {
    _metaFailed.add("bots");
    return [];
  }
  return _bots;
}

/**
 * Drop every cached /api/config and /api/meta/* payload (S2).
 *
 * All of it is scoped to the ACTIVE guild server-side, but the caches above are
 * module globals that outlive a guild switch — and switchGuild re-mounts panels
 * without reloading the page. Left stale, every config panel listed the
 * *previous* guild's channels / roles / members, and a save then wrote a
 * foreign guild's snowflake into the new guild's config.
 *
 * Called by app.js's applyMeData() — on boot, and on every guild switch,
 * alongside _resetPanelSpecCache(). Any new guild-scoped module cache added
 * here must be cleared here too.
 */
export function resetMetaCaches() {
  _configCache = null;
  _channels = null;
  _categories = null;
  _roles = null;
  _members = null;
  _bots = null;
  _memberSearches.clear();
  _membersById.clear();
  _metaFailed.clear();
  // Not a meta cache, but it shares the property that matters here: it holds
  // form elements, and a guild switch tears every panel down and rebuilds it
  // from the new guild's config. Left alone, the set would retain detached
  // nodes and report unsaved edits on forms that no longer exist. The switch
  // path already ran confirmLeaveDirty() before reaching this point.
  _dirtyForms.clear();
}

// ── Async panel mount wrapper (F1) ─────────────────────────────────────
//
// The shape ~27 config panels are written in:
//
//     export function mount(container) {
//       container.innerHTML = shell;          // "Loading configuration…"
//       (async () => { const cfg = await loadConfig(); render(cfg); })();
//     }
//
// There is no `.catch`. One failed fetch (503 while the bot reconnects, a
// dropped wifi) leaves the spinner up forever plus an unhandled rejection in
// the console, and the user has no way to tell a hung panel from a slow one.
//
// mountAsync runs the loader, renders a real error state with a Retry button
// when it rejects, and returns the SYNCHRONOUS handle app.js expects
// (`mod.mount()`'s return is used as-is — returning a promise from mount()
// would give app.js a thenable with no unmount()).
//
// Usage:
//     import { mountAsync } from "../config-helpers.js";
//     export function mount(container, params) {
//       container.innerHTML = shell;
//       return mountAsync(container, async () => {
//         const cfg = await loadConfig();
//         render(cfg);
//         return { unmount() { clearInterval(poll); } };  // optional
//       }, { errorMsg: "Couldn't load the welcome settings." });
//     }
//
// A panel that already returns its own handle merges the two:
//
//     const async_ = mountAsync(container, load, { errorMsg: "…" });
//     return { unmount() { async_.unmount(); chart?.destroy(); } };
//
// @param {HTMLElement} container  the element the panel owns
// @param {() => (any|Promise<any>)} loader  does the loading + rendering; may
//        resolve to an inner handle ({ unmount() }) which is forwarded
// @param {object} [opts]
// @param {string} [opts.errorMsg]  human sentence shown above the retry button
// @param {() => void} [opts.retry]  what "Try again" does. The default re-mounts
//        the whole page through app.js's router, which is the only universally
//        safe choice: the error state has replaced the shell the panel built,
//        so simply re-running the loader would hand it a container whose
//        elements and listeners are gone. Pass your own only if the loader
//        rebuilds everything it needs.
// @returns {{ unmount(): void, ready: Promise<void> }}
export function mountAsync(container, loader, opts = {}) {
  const errorMsg = opts.errorMsg || "Couldn't load this page.";
  let inner = null;
  let dead = false;

  const retry = typeof opts.retry === "function"
    ? opts.retry
    : () => window.dispatchEvent(new HashChangeEvent("hashchange"));

  function renderFailure(err) {
    container.innerHTML =
      // .panel-missing / .error / .btn are existing app.css classes — the error
      // state needs no new CSS.
      `<div class="panel-missing">${renderError(errorMsg)}` +
      `<div class="field-hint" style="margin:6px 0 10px;">${esc(err?.message || String(err))}</div>` +
      '<button type="button" class="btn" data-retry>Try again</button></div>';
    container.querySelector("[data-retry]")?.addEventListener("click", retry);
  }

  function run() {
    // Promise.resolve().then wraps a loader that throws SYNCHRONOUSLY too —
    // a TypeError before the first await would otherwise escape past us and
    // leave exactly the hung spinner this helper exists to prevent.
    return Promise.resolve()
      .then(() => loader(container))
      .then((handle) => {
        if (dead) { try { handle?.unmount?.(); } catch (_) { /* ignore */ } return; }
        inner = handle || null;
      })
      .catch((err) => {
        if (dead) return;
        renderFailure(err);
      });
  }

  const ready = run();

  return {
    ready,
    unmount() {
      dead = true;
      try { inner?.unmount?.(); } catch (_) { /* teardown must not throw */ }
      inner = null;
    },
  };
}

// ── Re-render-on-save without eating sibling edits (F4) ────────────────
//
// Multi-card panels (auto-react rules, role menus, cap lists…) re-fetch and
// rebuild EVERY card after a successful save. Anything the user had typed into
// a different card is silently discarded — and the unsaved-changes guard can't
// warn them, because the save that just succeeded cleared the dirty flag.
//
// The mechanism is per-card dirt:
//
//     const card = trackCard(buildCard(rule));      // once, when the card is built
//     ...
//     await apiPut(...);                            // in the card's save handler
//     showStatus(statusEl, true, "Saved");
//     clearCardDirty(card);
//     rerenderUnlessDirty(listEl, card, () => reloadAndRenderAllCards());
//
// When a sibling card has unsaved edits the rebuild is skipped: the saved card
// already shows what the user typed, so the screen stays correct and their work
// in the other card survives. The next navigation/refresh picks up the server
// state as usual.

/** Mark `card` as an edit-tracked card for rerenderUnlessDirty(). */
export function trackCard(card) {
  card.dataset.dkCard = "1";
  const mark = (e) => {
    if (e?.target?.classList?.contains("filter-select-input")) return;
    card.dataset.dkDirty = "1";
  };
  card.addEventListener("input", mark);
  card.addEventListener("change", mark);
  card.addEventListener("dk:change", mark);
  return card;
}

/** Forget the pending edits on `card` — call after its own save succeeds. */
export function clearCardDirty(card) {
  if (card) card.dataset.dkDirty = "";
}

/** Forget the edits tracked on `form`.
 *
 *  For a panel that destroys and rebuilds its own guarded node: wellness-caps
 *  rewrites the histogram on every mode or lookback change, and the old node
 *  would otherwise sit in the registry forever, reporting unsaved edits on an
 *  element no longer in the document. */
export function clearFormDirty(form) {
  _dirtyForms.delete(form);
}

/** True when `form` — a container passed to guardForm — has unsaved edits.
 *
 *  For panels that rebuild themselves after an unrelated action (mahjong
 *  remounts the whole page after a card upload or Set Active) and would
 *  otherwise throw away whatever is half-typed elsewhere on it. */
export function isFormDirty(form) {
  return _dirtyForms.has(form);
}

/** True when a tracked card other than `except` still holds unsaved edits. */
export function hasDirtySibling(root, except = null) {
  return Array.from(root.querySelectorAll("[data-dk-card]")).some(
    (c) => c !== except && c.dataset.dkDirty === "1",
  );
}

/**
 * Rebuild every card only when doing so can't destroy someone's work.
 *
 * @param {HTMLElement} root      container holding the tracked cards
 * @param {HTMLElement|null} saved the card whose save just succeeded
 * @param {() => void} rerender   rebuilds all cards from fresh data
 * @returns {boolean} whether it actually re-rendered
 */
export function rerenderUnlessDirty(root, saved, rerender) {
  clearCardDirty(saved);
  if (hasDirtySibling(root, saved)) return false;
  rerender();
  return true;
}

// ── Searchable picker adapters ──────────────────────────────────────────
// Convert the /api/meta/* records into the {id, label} option shape the
// filter-select widgets expect, then mount a picker in place of a slot node.
// Panels render a placeholder (e.g. <span data-picker="welcome_channel_id">)
// in their innerHTML and call one of the mount* helpers below afterwards,
// holding the returned handle to read getValue()/getValues() on save.

export function toChannelOptions(channels) {
  return channels.map((c) => ({ id: String(c.id), label: `#${c.name}` }));
}
export function toRoleOptions(roles) {
  return roles.map((r) => ({ id: String(r.id), label: `@${r.name}` }));
}
export function toCategoryOptions(categories) {
  return categories.map((c) => ({ id: String(c.id), label: c.name }));
}
export function toMemberOptions(members) {
  return members.map((m) => {
    const left = m.left_server ? " (left)" : "";
    const base = m.display_name && m.display_name !== m.name
      ? `${m.display_name} (${m.name})`
      : m.name;
    return { id: String(m.id), label: `${base}${left}` };
  });
}

/**
 * Member options for a picker whose whole job is finding one person in a long
 * roster (the exemption pickers on config-prune and config-inactive).
 *
 * Differs from toMemberOptions() in two ways, both about browsing rather than
 * confirming a choice already made:
 *   - members who have left sort to the BOTTOM, so the people an admin is
 *     actually looking for are not interleaved with departures;
 *   - the departure is spelled out — "(left the server)" rather than the terse
 *     " (left)" — because here it is the difference between exempting a real
 *     member and exempting a ghost.
 * Everything still in the server is ordered by label with localeCompare, so
 * "Zoe" and "zoe" sort together instead of by code point.
 *
 * Each option carries `left` alongside {id, label}; filterSelect ignores the
 * extra key, and a caller can filter or style on it.
 *
 * Was a private copy in each of those two panels (byte-identical). Kept here so
 * a change to the ordering or the departure wording lands in both at once.
 */
export function toSortedMemberOptions(members) {
  return members
    .map((m) => ({
      id: String(m.id),
      label: m.display_name && m.display_name !== m.name
        ? `${m.display_name} (${m.name})`
        : m.name,
      left: !!m.left_server,
    }))
    // Sorted on the bare label, before the suffix below is appended — the
    // annotation must not decide where a departed member lands among the rest.
    .sort((a, b) => a.left - b.left || a.label.localeCompare(b.label))
    .map((o) => (o.left ? { ...o, label: `${o.label} (left the server)` } : o));
}

/**
 * Build `(id) => display name` over a /api/meta/members payload.
 *
 * A factory rather than a `memberName(members, id)` pair for channelName() and
 * roleName() because the callers are chip lists: the Map is built once per
 * mount and read on every add, where those two answer a single lookup.
 *
 * Panels need this because a chip's label must come from the member RECORD, not
 * from unpicking the picker's "Display (username)" label — a member whose own
 * name has brackets ("Ana (EU)") loses them to that. Unknown ids (a member who
 * left between the config load and the click) answer with the id itself.
 *
 * Falls back to the module's id index before giving up: since /api/meta/members
 * became bounded, the member a picker just found through searchMembers() is
 * routinely NOT in the `members` page a panel was handed at mount, and a chip
 * built from one of those was showing a bare snowflake.
 */
export function memberNameLookup(members) {
  const byId = new Map(
    members.map((m) => [String(m.id), m.display_name || m.name || String(m.id)]),
  );
  return (id) => {
    const key = String(id);
    if (byId.has(key)) return byId.get(key);
    const found = _membersById.get(key);
    return (found && (found.display_name || found.name)) || key;
  };
}

function _normalizeIds(values) {
  if (Array.isArray(values)) return values.map(String).filter(Boolean);
  return String(values || "").split(",").map((s) => s.trim()).filter(Boolean);
}

// Mount a single-value searchable picker, replacing `slotEl`. `opts` is passed
// through to filterSelect (so callers can supply `filter`, `emptyLabel`, etc.).
// Pass `label` with the visible field label to give the search input an
// accessible name (aria-label) — otherwise every picker announces only its
// placeholder. Applies to every mount* helper below.
// A picker's slot is *replaced* by the widget, so `field()` can never pair the
// visible <label> with it by id the way it does for a real input — and a
// caller who forgets `label` ships a combobox whose only accessible name is
// "Type to filter…". Read the label off the field the slot is sitting in
// instead, so the default is a named control and `label` is the override.
function _withDerivedLabel(slotEl, opts) {
  if (opts.label) return opts;
  const lbl = slotEl.closest?.(".field, .ctrl-field")?.querySelector("label");
  const text = lbl ? lbl.textContent.trim().replace(/\s+/g, " ") : "";
  return text ? { ...opts, label: text } : opts;
}

export function mountPicker(slotEl, options, value, opts = {}) {
  opts = _withDerivedLabel(slotEl, opts);
  const fs = filterSelect(opts.placeholder || "Type to filter…", options, opts);
  fs.setValue(value);
  slotEl.replaceWith(fs.el);
  return fs;
}

// Mount a multi-value chip picker, replacing `slotEl`.
export function mountMultiPicker(slotEl, options, values, opts = {}) {
  opts = _withDerivedLabel(slotEl, opts);
  const fs = multiFilterSelect(opts.placeholder || "Type to filter…", options, opts);
  fs.setValues(_normalizeIds(values));
  slotEl.replaceWith(fs.el);
  return fs;
}

/** Fire `cb` when a filterSelect's value changes.
 *
 *  filterSelect has no change event of its own — selecting closes the popover
 *  and focus leaves afterwards, hence focusout plus a short settle delay.
 *  Note the listener goes on `fs.el`, not the slot element you passed to
 *  mountPicker: that slot was replaced out of the DOM by the mount. */
export function onPickerChange(fs, cb) {
  let last = fs.getValue();
  fs.el.addEventListener("focusout", () => {
    setTimeout(() => {
      const cur = fs.getValue();
      if (cur !== last) { last = cur; cb(); }
    }, 200);
  });
}

// filterSelect keeps an unmatched id as its own label, so these pickers never
// blanked the way the legacy <select> builders did — but a bare snowflake in
// the box doesn't tell an admin the role was deleted either. Name it, on the
// same terms as the <select> path.
//
// Only when the list actually loaded: an empty list means "still loading" or
// "the fetch failed", and flagging every saved id there would be a lie. It is
// also why the primary guild's own ids are safe — a dial inherited from the
// guild-0 row genuinely does not resolve on the guild reading it, and saying so
// is the honest render, not a false alarm.
function _withDanglingOption(options, value, kind) {
  if (!options.length || !_isDangling(options, value)) return options;
  return [...options, { id: String(value), label: `\u26a0 Missing ${kind} (id ${String(value)})` }];
}

/**
 * Select `value` on a native <select>, keeping it visible when the option list
 * doesn't offer it.
 *
 * Assigning a <select> a value it has no <option> for silently blanks it, and a
 * blank picker sitting over a form is indistinguishable from an unset one. This
 * is the third shape of that bug (see roleSelect/channelSelect for the other
 * two): the stored value is a *number* the server accepts across a far wider
 * range than the preset list the panel offers, so any value set outside the
 * panel — an API call, an older preset list — renders as nothing at all.
 */
export function selectValueOrAdd(sel, value, label) {
  const v = String(value);
  if (!Array.from(sel.options).some((o) => o.value === v)) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label ? label(v) : v;
    sel.append(o);
  }
  sel.value = v;
}

// Typed conveniences — build the option list and the right empty sentinel.
// Single-pickers default to emptyValue "0" (the unset id config uses) so
// getValue() returns "0" when cleared, matching the old <select> behavior.
export function mountChannelPicker(slotEl, channels, value, opts = {}) {
  return mountPicker(slotEl, _withDanglingOption(toChannelOptions(channels), value, "channel"), value,
    { emptyValue: "0", emptyLabel: "(disabled)", ...opts });
}
export function mountRolePicker(slotEl, roles, value, opts = {}) {
  return mountPicker(slotEl, _withDanglingOption(toRoleOptions(roles), value, "role"), value,
    { emptyValue: "0", emptyLabel: "(none)", ...opts });
}
export function mountCategoryPicker(slotEl, categories, value, opts = {}) {
  return mountPicker(slotEl, _withDanglingOption(toCategoryOptions(categories), value, "category"), value,
    { emptyValue: "0", emptyLabel: "(none)", ...opts });
}
export function mountChannelMultiPicker(slotEl, channels, values, opts = {}) {
  return mountMultiPicker(slotEl, toChannelOptions(channels), values, opts);
}
export function mountRoleMultiPicker(slotEl, roles, values, opts = {}) {
  return mountMultiPicker(slotEl, toRoleOptions(roles), values, opts);
}

/**
 * Fill in the labels of saved ids the bounded member page didn't include.
 *
 * Without this a config referencing someone who left long ago would render as a
 * bare snowflake wherever the departed tail was trimmed. The lookup runs after
 * the picker is already on screen and hands it a widened option list;
 * setOptions()/setValues() both re-derive their displayed labels, so the name
 * simply appears a moment later.
 *
 * Bails when the widget has been detached — a panel unmounting mid-request must
 * not write into a dead tree. A picker that was never attached keeps showing the
 * raw id, which is exactly what it showed before.
 */
function _hydrateSavedMembers(fs, options, ids) {
  const known = new Set(options.map((o) => String(o.id)));
  const missing = ids
    .map(String)
    .filter((id) => /^\d+$/.test(id) && id !== "0" && !known.has(id));
  if (!missing.length) return fs;
  resolveMembers(missing)
    .then((extra) => {
      if (!extra.length || !fs.el.isConnected) return;
      fs.setOptions(options.concat(toMemberOptions(extra)));
    })
    .catch(() => { /* the raw id stays on screen — no worse than before */ });
  return fs;
}

export function mountMemberPicker(slotEl, members, value, opts = {}) {
  const options = toMemberOptions(members);
  const fs = mountPicker(slotEl, options, value,
    { emptyValue: "0", emptyLabel: "(none)", search: memberSearch(), ...opts });
  return _hydrateSavedMembers(fs, options, [value]);
}
export function mountMemberMultiPicker(slotEl, members, values, opts = {}) {
  const options = toMemberOptions(members);
  const fs = mountMultiPicker(slotEl, options, values,
    { search: memberSearch(), ...opts });
  return _hydrateSavedMembers(fs, options, _normalizeIds(values));
}

// A deleted channel/role is absent from the meta list forever — Discord has
// forgotten it too, so no retry or reload brings a name back the way a
// dangling *saved* id elsewhere in this file might resolve once the meta
// fetch that failed succeeds. Say so in plain language instead of handing
// back the bare snowflake (S3): a moderator reading an audit row could not
// tell "deleted channel" from "some unrelated number" apart from a raw id,
// and a `(disabled)`/`(none)`-shaped label would be actively misleading —
// nobody chose to turn this off, it went away. The id stays in the text so
// two different deleted channels are still distinguishable for forensics.
// Same "⚠ Missing <kind> (id …)" wording as _danglingOption's <select>
// equivalent just above, so the dashboard uses one visual language for "this
// stored id doesn't resolve" everywhere it comes up.
//
// Both stay plain strings, same as the resolved-name case: callers pass the
// result through esc() themselves, or interpolate it straight into HTML
// (docs.js, role-menus.js), so this must never contain markup. Nothing reads
// this string back to make a decision — every caller (grepped) only ever
// displays it — so returning prose here instead of the bare id does not
// change any comparison or link-building behavior.
export function channelName(channels, id) {
  if (!id || id === "0") return "(disabled)";
  const ch = channels.find((c) => c.id === id);
  if (ch) return `#${ch.name}`;
  return `⚠ Missing channel (id ${id})`;
}

export function roleName(roles, id) {
  if (!id || id === "0") return "(none)";
  const r = roles.find((x) => x.id === id);
  if (r) return `@${r.name}`;
  return `⚠ Missing role (id ${id})`;
}

export function channelSelect(channels, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("channels") && !channels.length) {
    return _failedSelectOptions("Channels", selected, "(disabled)");
  }
  let html = _isDangling(channels, selected) ? _danglingOption("channel", selected) : "";
  html += allowNone ? '<option value="0">(disabled)</option>' : "";
  for (const ch of channels) {
    const sel = ch.id === String(selected) ? " selected" : "";
    html += `<option value="${ch.id}"${sel}>#${esc(ch.name)}</option>`;
  }
  return html;
}

export function roleSelect(roles, selected, { allowNone = true } = {}) {
  if (_metaFailed.has("roles") && !roles.length) {
    return _failedSelectOptions("Roles", selected, "(none)");
  }
  let html = _isDangling(roles, selected) ? _danglingOption("role", selected) : "";
  html += allowNone ? '<option value="0">(none)</option>' : "";
  for (const r of roles) {
    const sel = r.id === String(selected) ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${esc(r.name)}</option>`;
  }
  return html;
}

// Fail-safe options for the legacy multi-selects: every saved id stays
// selected so a save on an unrelated field can't drop the list.
function _failedMultiOptions(kind, selectedIds) {
  let html = "";
  for (const id of selectedIds) {
    html += `<option value="${esc(id)}" selected>Current setting (id ${esc(id)})</option>`;
  }
  html += `<option disabled>${kind} failed to load — reload before saving</option>`;
  return html;
}

export function channelSelectMulti(channels, selected) {
  const selectedIds = new Set(
    (Array.isArray(selected)
      ? selected
      : String(selected || "").split(","))
      .map((s) => String(s).trim())
      .filter(Boolean),
  );
  if (_metaFailed.has("channels") && !channels.length) {
    return _failedMultiOptions("Channels", selectedIds);
  }
  // Ids the guild no longer has would otherwise vanish from the list — no
  // option, nothing selected, and the next save posts the remainder.
  let html = "";
  for (const id of selectedIds) {
    if (!channels.some((o) => String(o.id) === id)) html += _danglingOption("channel", id);
  }
  for (const ch of channels) {
    const sel = selectedIds.has(ch.id) ? " selected" : "";
    html += `<option value="${ch.id}"${sel}>#${esc(ch.name)}</option>`;
  }
  return html;
}

export function roleSelectMulti(roles, selected) {
  const selectedIds = new Set(
    (Array.isArray(selected)
      ? selected
      : String(selected || "").split(","))
      .map((s) => String(s).trim())
      .filter(Boolean),
  );
  if (_metaFailed.has("roles") && !roles.length) {
    return _failedMultiOptions("Roles", selectedIds);
  }
  // Ids the guild no longer has would otherwise vanish from the list — no
  // option, nothing selected, and the next save posts the remainder.
  let html = "";
  for (const id of selectedIds) {
    if (!roles.some((o) => String(o.id) === id)) html += _danglingOption("role", id);
  }
  for (const r of roles) {
    const sel = selectedIds.has(r.id) ? " selected" : "";
    html += `<option value="${r.id}"${sel}>@${esc(r.name)}</option>`;
  }
  return html;
}

export function multiIdList(ids, nameMap) {
  if (!ids || !ids.length) return "<em>none</em>";
  return ids.map((id) => esc(nameMap[id] || id)).join(", ");
}

export async function saveSection(section, body) {
  return apiPut(`/api/config/${section}`, body);
}

export function showStatus(el, ok, msg) {
  if (ok) {
    // Clear the form this status element belongs to. When it sits outside any
    // guarded container there is nothing to attribute the save to, so fall
    // back to the old page-wide clear rather than leave a panel permanently
    // claiming unsaved edits.
    const owner = el.closest?.("[data-dk-guard]");
    if (owner) _dirtyForms.delete(owner);
    else _dirtyForms.clear();
  }
  el.className = `save-status ${ok ? "save-ok" : "save-err"}`;
  el.textContent = msg || (ok ? "Saved" : "Error");
  // Errors linger longer than successes, but both clear — a stale "Error"
  // next to a button outlives its usefulness once the user moves on.
  clearTimeout(el._statusTimer);
  el._statusTimer = setTimeout(() => { el.textContent = ""; }, ok ? 3000 : 8000);
}

// A "these members are exempt" chip list backed by PUT/DELETE
// `${endpoint}/${user_id}`. Config-prune grew the same widget by hand first;
// this is the shared form so a fix to the chips, the confirm flow, or the
// failed-write handling lands once. `picker` is an optional member picker whose
// suggestions are kept clear of already-exempt members.
//
// Returns { add(id, name), ids() } — `add` resolves to whether the write
// actually landed, so a caller removing a row from its own list can hold off
// when it didn't.
export function mountExemptionList(listEl, opts) {
  const { endpoint, picker, emptyText, confirmTitle, confirmLabel, confirmText } = opts;
  let items = (opts.items || []).slice();
  // Held as a live Set rather than rebuilt per candidate: the picker's filter
  // runs once per member on every keystroke.
  const excluded = new Set(items.map((e) => String(e.id)));

  function refresh() {
    if (!items.length) {
      listEl.innerHTML = `<div class="empty" style="padding:8px 0;">${esc(emptyText)}</div>`;
    } else {
      listEl.innerHTML = `<div class="exempt-chips">${items
        .map(
          (e) =>
            `<span class="exempt-chip"><span>${esc(e.name)}</span><button type="button" data-remove-exempt="${esc(e.id)}" aria-label="Stop exempting ${esc(e.name)}" title="Stop exempting ${esc(e.name)}">×</button></span>`
        )
        .join("")}</div>`;
      listEl.querySelectorAll("[data-remove-exempt]").forEach((btn) => {
        btn.addEventListener("click", () => remove(btn.dataset.removeExempt));
      });
    }
    if (picker) picker.setFilter((o) => !excluded.has(String(o.id)));
  }

  async function remove(id) {
    const entry = items.find((e) => String(e.id) === String(id));
    const ok = await confirmDialog(confirmText(entry ? entry.name : "this member"), {
      title: confirmTitle,
      danger: true,
      confirmLabel,
    });
    if (!ok) return;
    try {
      await apiDelete(`${endpoint}/${id}`);
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    items = items.filter((e) => String(e.id) !== String(id));
    excluded.delete(String(id));
    refresh();
  }

  async function add(id, name) {
    try {
      // Member ids stay strings on the wire.
      await apiPut(`${endpoint}/${id}`, {});
    } catch (err) {
      toast(err.message, "error");
      return false;
    }
    items.push({ id: String(id), name });
    items.sort((a, b) => a.name.localeCompare(b.name));
    excluded.add(String(id));
    refresh();
    return true;
  }

  refresh();
  return { add, ids: () => excluded };
}

// Cancel a pending showStatus blanking and drop its styling. Callers that write
// their own text into a status element afterwards need this — otherwise the
// timer armed by an earlier error wipes the new message mid-read, and the
// save-err class leaves it red. Owned here so showStatus's timer field stays
// private to this module.
export function clearStatus(el) {
  clearTimeout(el._statusTimer);
  el._statusTimer = null;
  el.className = "";
  el.textContent = "";
}

export function buildField(labelText, control, hint) {
  const div = document.createElement("div");
  div.className = "field";
  const lbl = document.createElement("label");
  lbl.textContent = labelText;
  div.appendChild(lbl);
  div.appendChild(control);
  if (hint) {
    const h = document.createElement("div");
    h.className = "field-hint";
    h.textContent = hint;
    div.appendChild(h);
  }
  return div;
}

// ── Labelled form controls ─────────────────────────────────────────────
// buildField renders a bare <label>; `field` additionally pairs it with its
// control by id so screen readers announce the label and a label tap focuses
// the input (W-A7). Every panel wants that, so it lives here rather than
// being re-derived per panel. These are now the only copies — the private
// `field`/`labeledField`, `card` and `toggleField` that birthday-settings,
// config-bios, config-cleanup, config-confessions, config-dms and
// config-starboard used to carry all import from here instead.
//
// `buildNumberInput` is the one exception still outstanding: config-cleanup,
// config-confessions and config-starboard keep private copies because they
// aren't interchangeable with `numInput` below — different argument order
// (config-confessions' differs again from the other two) and 140px against
// numInput's 160px, so unifying is a visible change rather than an import
// swap.
let _fieldSeq = 0;

export function field(labelText, control, hint) {
  const div = buildField(labelText, control, hint);
  if (control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName)) {
    const id = control.id || `dk-field-${++_fieldSeq}`;
    control.id = id;
    div.querySelector("label").htmlFor = id;
  }
  return div;
}

export function numInput(name, value, min, step = "1", max = null) {
  const inp = document.createElement("input");
  inp.type = "number";
  inp.name = name;
  inp.required = true;
  inp.min = String(min);
  if (max != null) inp.max = String(max);
  inp.step = step;
  inp.value = String(value);
  inp.style.maxWidth = "160px";
  return inp;
}

export function checkbox(name, checked, labelText) {
  const label = document.createElement("label");
  label.style.cssText = "display:flex; gap:6px; align-items:center;";
  const inp = document.createElement("input");
  inp.type = "checkbox";
  inp.name = name;
  inp.checked = !!checked;
  label.append(inp, document.createTextNode(" " + labelText));
  return label;
}

/** A checkbox row plus the hint that states what the toggle changes.
 *
 * Distinct from `checkbox` above: this returns `{ wrap, box }` rather than a
 * bare label, because every caller needs the input back to read on submit and
 * the wrapper back to append. Config panels settled on this shape
 * independently three times; it lives here now.
 */
export function toggleField(name, labelText, checked, hint) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const lbl = document.createElement("label");
  lbl.style.cssText = "display:flex; align-items:center; gap:8px; cursor:pointer;";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.name = name;
  box.checked = !!checked;
  lbl.appendChild(box);
  lbl.appendChild(document.createTextNode(labelText));
  wrap.appendChild(lbl);
  const h = document.createElement("div");
  h.className = "field-hint";
  h.textContent = hint;
  wrap.appendChild(h);
  return { wrap, box };
}

/** A titled `.card` section appended to `parent` (usually the panel's form). */
export function sectionCard(parent, title) {
  const el = document.createElement("div");
  el.className = "card";
  const lbl = document.createElement("div");
  lbl.className = "section-label";
  lbl.textContent = title;
  el.appendChild(lbl);
  parent.appendChild(el);
  return el;
}

// ── In-page permission gating ─────────────────────────────────────────
//
// A page that shows a moderator-level report *and* its admin-level settings
// needs both halves on one pane without leaking write access. The nav already
// had this idea: an adminOnly page renders for moderators as a locked entry so
// its existence isn't invisible (W-N5). These helpers are the in-page analogue.
//
// Enforcement stays where it always was — on the server. Every config write is
// behind require_perms({"admin"}); this only stops a moderator from filling in
// a form whose save could never succeed. GET /api/config is moderator-gated, so
// the values themselves are already theirs to read and are shown, not blanked.

/** True when the signed-in viewer holds the admin permission. */
export function viewerIsAdmin() {
  const perms = window.__dk_user?.perms;
  return !!(perms && typeof perms.has === "function" && perms.has("admin"));
}

/**
 * Render *root* read-only: every control inside is disabled, submits are
 * swallowed, and a lock chip is appended to the section label naming who can
 * change it. Idempotent — calling twice adds one chip.
 *
 * Pass the element wrapping the settings half of a merged page. Controls that
 * only read (a preview button, say) are disabled too: their endpoints are
 * admin-gated as well, so leaving them live would just produce a 403.
 */
export function lockSection(root, { requires = "an admin", labelEl = null } = {}) {
  if (!root || root.dataset.locked === "1") return root;
  root.dataset.locked = "1";

  for (const el of root.querySelectorAll("input, select, textarea, button")) {
    el.disabled = true;
    el.setAttribute("aria-disabled", "true");
  }
  // Pickers mount their own inputs; block the interactions that would re-enable
  // editing through a keyboard or a click on a custom widget.
  root.addEventListener("submit", (e) => e.preventDefault(), true);
  root.classList.add("section-locked");

  const target = labelEl || root.querySelector(".section-label");
  if (target && !target.querySelector("[data-lock-chip]")) {
    const chip = document.createElement("span");
    chip.dataset.lockChip = "1";
    chip.className = "chip";
    chip.style.cssText = "margin-left:8px; font-weight:400; text-transform:none; letter-spacing:0;";
    chip.textContent = `🔒 Only ${requires} can change these`;
    target.appendChild(chip);
  }
  return root;
}

/**
 * Lock *root* unless the viewer is an admin. Returns true when it locked, so a
 * caller can skip wiring save handlers it no longer needs.
 */
export function lockUnlessAdmin(root, opts = {}) {
  if (viewerIsAdmin()) return false;
  lockSection(root, opts);
  return true;
}
