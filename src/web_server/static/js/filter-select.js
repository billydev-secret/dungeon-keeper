// Searchable, mobile-friendly select widgets shared across the dashboard.
//
// Two flavors, both driven by a text input + filtered dropdown (no native
// <select>, which is clumsy for multi-pick and long lists on touch devices):
//   - filterSelect:      single value, clears to an "empty" sentinel
//   - multiFilterSelect: many values, rendered as removable chips
//
// Both accept a programmable `filter` predicate (and a setFilter() to change it
// at runtime) so a panel can narrow the options based on other form state.
//
// Styling lives in app.css under .filter-select / .filter-chip (theme-variable
// based, so it inherits the active theme).
import { esc } from "./api.js";

let _fsSeq = 0;

// Shortest all-digit filter that also searches option ids. Discord snowflakes
// are 17-19 digits, so a pasted id always qualifies while a few typed digits
// (searching a name like "Rule 34") stay a name-only search.
const MIN_ID_FILTER_LEN = 5;

/** Options matching `lc` (already lowercased) by label, or by id when `lc`
 *  looks like a pasted snowflake.
 *
 *  Copying an id out of Discord is the natural move on mobile, where a name
 *  like "#🪧│bounty-board" is awkward to retype — and a label-only filter
 *  answered that paste with an empty list, which reads as "the bot can't see
 *  any channels" rather than "wrong search". Shared by both widgets so the
 *  single- and multi-select pickers can't drift apart.
 */
function matchOptions(base, lc) {
  const byId = /^\d+$/.test(lc) && lc.length >= MIN_ID_FILTER_LEN;
  return base.filter(
    (o) =>
      o.label.toLowerCase().includes(lc) ||
      (byId && String(o.id).includes(lc)),
  );
}

function styleInput(input, placeholder) {
  input.type = "text";
  input.placeholder = placeholder;
  input.className = "filter-select-input";
  // Keep mobile keyboards from auto-capitalizing / autocorrecting names.
  input.autocomplete = "off";
  input.autocapitalize = "off";
  input.spellcheck = false;
}

// Does this engine actually implement the Popover API? Assigning
// `list.popover = "manual"` where it doesn't sets a plain JS expando instead of
// the content attribute, so the UA's
// `[popover]:not(:popover-open) { display: none }` rule never applies — which
// left the list permanently on screen as a stray `position: fixed` box painting
// over the panel. Visibility is the `.is-open` class's job now (see app.css);
// this flag only picks the positioning strategy.
const SUPPORTS_POPOVER =
  typeof HTMLElement !== "undefined" &&
  typeof HTMLElement.prototype.showPopover === "function";

// Promote the dropdown list into the browser's top layer via the Popover API
// and pin it under the input with fixed coordinates. The top layer escapes
// ancestor overflow clipping, `transform`/`filter` containing blocks, and every
// z-index stacking context — which is why an absolutely-positioned list used to
// disappear behind cards and panels. Returns { open, close, isOpen }.
function attachPopover(input, list) {
  // Open state is tracked here rather than read back from `:popover-open`:
  // `matches()` throws SyntaxError on an engine that doesn't know the
  // pseudo-class, and that throw took the whole focus handler down with it.
  let opened = false;

  if (SUPPORTS_POPOVER) {
    // "manual" (not "auto"): we drive show/hide from focus/blur ourselves, so
    // we don't want auto light-dismiss racing with the focus-to-open pattern.
    list.popover = "manual";
  } else {
    // Anchored inside `.filter-select` (position: relative) instead. It can be
    // clipped by an ancestor's overflow — the tradeoff the top layer exists to
    // avoid — but a clipped list beats one floating loose over the page.
    list.style.position = "absolute";
  }

  function positionFixed() {
    const r = input.getBoundingClientRect();
    // Two viewports, and each half of this function wants a different one.
    //
    // PLACEMENT uses the *layout* viewport, and needs no correction at all:
    // getBoundingClientRect() and `position: fixed` are both resolved against
    // it, on every engine — the top layer included. Subtracting the visual
    // viewport's offset here does not cancel an error, it introduces one
    // exactly that size: on iOS the keyboard shifts the visual viewport
    // without firing a document scroll, and the list jumped that far from its
    // field ("floating loose in the corner"). It is 0 on an unzoomed desktop,
    // which is why the bug was invisible everywhere but a phone.
    //
    // THE FIT TEST does want the visual viewport: "is there room below?" means
    // room the user can actually see, and the keyboard covers part of the
    // layout viewport without changing its size. So `dy`/`vh` stay in the
    // flip decision, and stay out of the coordinates.
    const vv = window.visualViewport;
    const dy = vv ? vv.offsetTop : 0;
    const vh = vv ? vv.height : window.innerHeight;
    list.style.width = r.width + "px";
    list.style.left = r.left + "px";
    // Flip above the input when there isn't room below it on screen.
    const h = list.offsetHeight;
    const below = (dy + vh) - r.bottom;
    const above = r.top - dy;
    list.style.top =
      (h > below && above > below ? r.top - h : r.bottom) + "px";
  }

  function positionAnchored() {
    // Offsets are relative to the `.filter-select` wrapper, so the list tracks
    // its input through scroll, zoom and keyboard shifts with no math at all.
    list.style.width = input.offsetWidth + "px";
    list.style.left = input.offsetLeft + "px";
    list.style.top = (input.offsetTop + input.offsetHeight) + "px";
  }

  const position = SUPPORTS_POPOVER ? positionFixed : positionAnchored;

  function reposition() {
    // A panel can unmount while its popover is open (navigate away with the
    // list showing): close() never runs, and the window/visualViewport
    // listeners below outlive the widget — one leaked set per such visit.
    // Detaching from the document is the signal; tear down on the next event.
    if (!list.isConnected) { close(); return; }
    if (opened) position();
  }

  // visualViewport fires where document scroll doesn't: keyboard show/hide and
  // pinch-zoom on mobile. Without it the list stays put while the page moves.
  function listen(on) {
    const fn = on ? "addEventListener" : "removeEventListener";
    window[fn]("scroll", reposition, true);
    window[fn]("resize", reposition);
    if (window.visualViewport) {
      window.visualViewport[fn]("scroll", reposition);
      window.visualViewport[fn]("resize", reposition);
    }
  }

  function open() {
    if (!list.isConnected) return;
    if (opened) { position(); return; }
    opened = true;
    list.classList.add("is-open");
    // Guarded: showPopover() throws if the element is already in the top layer,
    // and an escaping throw here is what used to kill the focus handler.
    if (SUPPORTS_POPOVER) {
      try { list.showPopover(); } catch { /* already shown — harmless */ }
    }
    input.setAttribute("aria-expanded", "true");
    position();
    listen(true);
  }

  function close() {
    if (!opened) return;
    opened = false;
    if (SUPPORTS_POPOVER) {
      try { list.hidePopover(); } catch { /* already hidden — harmless */ }
    }
    list.classList.remove("is-open");
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    listen(false);
  }

  return { open, close, isOpen: () => opened };
}

// Announce a real value change on the widget's wrapper, bubbling so a
// containing form hears it.
//
// The search input's own `input`/`change` events are noise — typing "gen" to
// find #general edits nothing — and guardForm now ignores them (config-helpers).
// This is the event that means the picker's VALUE moved.
function announceChange(wrap) {
  wrap.dispatchEvent(new CustomEvent("dk:change", { bubbles: true }));
}

// ── Remote search: the long tail behind a bounded prefetch ──────────────
//
// Some option lists can't be shipped whole. /api/meta/members returns a bounded
// page (see routes/meta.py) because the departed-member tail grows forever with
// server churn — but a picker that only ever filters its prefetch would make
// everyone outside that page silently unselectable, which is exactly the bug a
// naive server-side cap would have caused.
//
// `opts.search` — an async (query) => [{id, label}] — closes that gap. The
// widget keeps filtering its prefetch locally on every keystroke, so typing
// stays instant, and in parallel asks the server for anything it doesn't have.
// Late arrivals are merged into a side pool and folded into the open list.
//
// Nothing about a picker without `opts.search` changes.
const SEARCH_DEBOUNCE_MS = 200;
// One character matches so much of any prefetch that a round trip adds nothing;
// two is where a search starts to be about something the client may not hold.
const SEARCH_MIN_LEN = 2;

/**
 * Debounced, staleness-guarded driver for `opts.search`.
 *
 * @param {HTMLElement} list  the dropdown, used as the liveness signal
 * @param {Function|undefined} search  the caller's async lookup
 * @param {(results: Array, query: string) => void} onResults
 * @returns {{ query(text: string): void, cancel(): void }}
 */
function attachRemoteSearch(list, search, onResults) {
  if (typeof search !== "function") {
    return { query: () => {}, cancel: () => {} };
  }
  let timer = null;
  let seq = 0;

  function cancel() {
    clearTimeout(timer);
    timer = null;
    seq++; // invalidates anything already in flight
  }

  function query(text) {
    clearTimeout(timer);
    const q = String(text || "").trim();
    if (q.length < SEARCH_MIN_LEN) return;
    const mine = ++seq;
    timer = setTimeout(() => {
      timer = null;
      Promise.resolve()
        .then(() => search(q))
        .then((results) => {
          // Two ways this result is no longer wanted, and both used to be the
          // shape of bug the review kept finding: the user has typed past this
          // query (mine !== seq), or the panel unmounted while the request was
          // in flight — rendering then writes into a detached tree.
          if (mine !== seq || !list.isConnected) return;
          onResults(Array.isArray(results) ? results : [], q);
        })
        .catch(() => {
          // A failed lookup leaves the local matches standing. Blanking the
          // dropdown would read as "this member doesn't exist".
        });
    }, SEARCH_DEBOUNCE_MS);
  }

  return { query, cancel };
}

/**
 * Searchable single-select.
 *
 * @param {string} placeholder
 * @param {Array<{id: string, label: string}>} options
 * @param {object} [opts]
 * @param {(option) => boolean} [opts.filter]  applied before the text filter
 * @param {string} [opts.emptyLabel="(any)"]   label of the clear-selection row
 * @param {string|number} [opts.emptyValue=""] value getValue() returns when empty
 * @param {string} [opts.label]  accessible name (aria-label) for the input —
 *   pass the visible field label so AT doesn't announce only the placeholder
 * @param {(query: string) => Promise<Array>} [opts.search]  optional server-side
 *   lookup for options outside `options` — see attachRemoteSearch above
 * @returns {{el, getValue, setValue, setOptions, setFilter, getInput, destroy}}
 */
export function filterSelect(placeholder, options, opts = {}) {
  let predicate = typeof opts.filter === "function" ? opts.filter : null;
  const emptyLabel = opts.emptyLabel != null ? opts.emptyLabel : "(any)";
  const emptyValue = opts.emptyValue != null ? String(opts.emptyValue) : "";
  let items = options.slice();

  const wrap = document.createElement("div");
  wrap.className = "filter-select";

  const uid = `fs-${++_fsSeq}`;

  const input = document.createElement("input");
  styleInput(input, placeholder);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", `${uid}-list`);
  if (opts.label) input.setAttribute("aria-label", opts.label);
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  list.id = `${uid}-list`;
  list.setAttribute("role", "listbox");
  wrap.appendChild(list);
  const popover = attachPopover(input, list);

  let selectedId = emptyValue;
  let selectedLabel = "";
  let hi = -1; // index of the keyboard-highlighted option

  // Options that arrived from opts.search, kept beside `items` rather than
  // merged into it: the no-query list must stay the prefetch the caller chose,
  // not accumulate every name typed at it this session.
  const found = new Map();

  /** Everything selectable right now — the prefetch plus search residue. */
  function pool() {
    if (!found.size) return items;
    const known = new Set(items.map((o) => String(o.id)));
    return items.concat(
      Array.from(found.values()).filter((o) => !known.has(String(o.id))),
    );
  }

  function visible(all) {
    return predicate ? all.filter(predicate) : all;
  }

  function render(filter) {
    const lc = filter.toLowerCase();
    const base = visible(lc ? pool() : items);
    const matches = lc ? matchOptions(base, lc) : base;
    const show = lc ? matches : matches.slice(0, 300);
    const rows = [{ id: emptyValue, label: emptyLabel, empty: true }, ...show];
    list.innerHTML = rows
      .map((o, i) => {
        const sel = String(o.id) === String(selectedId);
        const inner = o.empty
          ? `<em style="color:var(--ink-dim)">${esc(emptyLabel)}</em>`
          : esc(o.label);
        return `<div class="filter-select-item" role="option" id="${uid}-opt-${i}" data-id="${esc(String(o.id))}" aria-selected="${sel}">${inner}</div>`;
      })
      .join("");
    hi = -1;
    input.removeAttribute("aria-activedescendant");
  }

  function optionEls() {
    return Array.from(list.querySelectorAll(".filter-select-item"));
  }

  function highlight(idx) {
    const els = optionEls();
    if (!els.length) return;
    hi = (idx + els.length) % els.length;
    els.forEach((el, i) => el.classList.toggle("active", i === hi));
    const cur = els[hi];
    input.setAttribute("aria-activedescendant", cur.id);
    cur.scrollIntoView({ block: "nearest" });
  }

  function selectItem(item) {
    const id = item.dataset.id;
    const before = selectedId;
    if (id === emptyValue) {
      selectedId = emptyValue;
      selectedLabel = "";
      input.value = "";
    } else {
      selectedId = id;
      selectedLabel = item.textContent.trim();
      input.value = selectedLabel;
    }
    popover.close();
    if (selectedId !== before) announceChange(wrap);
  }

  const remote = attachRemoteSearch(list, opts.search, (results) => {
    let added = false;
    for (const o of results) {
      const id = String(o.id);
      if (!found.has(id)) { found.set(id, { ...o, id }); added = true; }
    }
    // Re-rendering resets the keyboard highlight, so leave the list alone while
    // the user is arrow-keying through it — the new rows are in the pool and
    // appear on their next keystroke.
    if (added && hi < 0) render(input.value);
  });

  input.addEventListener("focus", () => {
    render(input.value);
    popover.open();
  });
  input.addEventListener("input", () => {
    // Typing over a chosen option really does clear it, so that case is a value
    // change; typing in an already-empty picker is just searching.
    const cleared = selectedId !== emptyValue;
    selectedId = emptyValue;
    selectedLabel = "";
    render(input.value);
    popover.open();
    remote.query(input.value);
    if (cleared) announceChange(wrap);
  });
  list.addEventListener("mousedown", (e) => {
    // mousedown (not click) so it fires before the input's blur hides the list.
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    selectItem(item);
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { popover.close(); }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { popover.close(); input.blur(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!popover.isOpen()) { render(input.value); popover.open(); }
      highlight(hi + (e.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if (e.key === "Enter" && popover.isOpen() && hi >= 0) {
      const el = optionEls()[hi];
      if (el) { e.preventDefault(); selectItem(el); }
    }
  });

  function setValue(id) {
    selectedId = (id == null || String(id) === emptyValue) ? emptyValue : String(id);
    if (selectedId === emptyValue) {
      selectedLabel = "";
      input.value = "";
      return;
    }
    const match = pool().find((o) => String(o.id) === selectedId);
    selectedLabel = match ? match.label : selectedId;
    input.value = selectedLabel;
  }

  function setOptions(next) {
    items = next.slice();
    setValue(selectedId); // refresh the displayed label against the new options
  }

  function setFilter(fn) {
    predicate = typeof fn === "function" ? fn : null;
    if (popover.isOpen()) render(input.value);
  }

  return {
    el: wrap,
    getValue: () => selectedId,
    setValue,
    setOptions,
    setFilter,
    getInput: () => input,
    // Deterministic teardown for a panel that unmounts with the list open.
    // reposition() also self-heals on the next scroll/resize, but a panel
    // holding the handle can just say so. Cancelling the search drops a pending
    // debounce timer and orphans any reply still in flight.
    destroy: () => { remote.cancel(); popover.close(); },
  };
}

/**
 * Searchable multi-select. Picking an option appends a removable chip and
 * clears the input for the next pick.
 *
 * @param {string} placeholder
 * @param {Array<{id: string, label: string}>} options
 * @param {object} [opts]
 * @param {(option) => boolean} [opts.filter]  applied before the text filter
 * @param {string} [opts.label]  accessible name (aria-label) for the input —
 *   pass the visible field label so AT doesn't announce only the placeholder
 * @param {(query: string) => Promise<Array>} [opts.search]  optional server-side
 *   lookup for options outside `options` — see attachRemoteSearch above
 * @returns {{el, getValues, setValues, setOptions, setFilter, getInput, destroy}}
 */
export function multiFilterSelect(placeholder, options, opts = {}) {
  let predicate = typeof opts.filter === "function" ? opts.filter : null;
  let items = options.slice();

  const wrap = document.createElement("div");
  wrap.className = "filter-select multi-filter-select";

  const chipsRow = document.createElement("div");
  chipsRow.className = "filter-select-chips";
  wrap.appendChild(chipsRow);

  const uid = `mfs-${++_fsSeq}`;

  const input = document.createElement("input");
  styleInput(input, placeholder);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-haspopup", "listbox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", `${uid}-list`);
  if (opts.label) input.setAttribute("aria-label", opts.label);
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  list.id = `${uid}-list`;
  list.setAttribute("role", "listbox");
  wrap.appendChild(list);
  const popover = attachPopover(input, list);

  const selected = new Map();
  let hi = -1; // index of the keyboard-highlighted option

  function renderChips() {
    while (chipsRow.firstChild) chipsRow.removeChild(chipsRow.firstChild);
    for (const [id, label] of selected.entries()) {
      const chip = document.createElement("span");
      chip.className = "filter-chip";
      chip.dataset.id = id;
      chip.textContent = label;
      const x = document.createElement("button");
      x.type = "button";
      x.className = "filter-chip-x";
      x.setAttribute("aria-label", `Remove ${label}`);
      x.textContent = "×";
      chip.appendChild(x);
      chipsRow.appendChild(chip);
    }
  }

  // See filterSelect: search results live beside the prefetch, not inside it.
  const found = new Map();

  function pool() {
    if (!found.size) return items;
    const known = new Set(items.map((o) => String(o.id)));
    return items.concat(
      Array.from(found.values()).filter((o) => !known.has(String(o.id))),
    );
  }

  function visible(all) {
    return predicate ? all.filter(predicate) : all;
  }

  function renderList(filter) {
    const lc = filter.toLowerCase();
    const base = visible(lc ? pool() : items);
    const matches = lc ? matchOptions(base, lc) : base;
    const show = lc ? matches : matches.slice(0, 300);
    while (list.firstChild) list.removeChild(list.firstChild);
    show.forEach((o, i) => {
      const item = document.createElement("div");
      item.className = "filter-select-item";
      item.dataset.id = o.id;
      item.setAttribute("role", "option");
      item.id = `${uid}-opt-${i}`;
      const taken = selected.has(o.id);
      item.setAttribute("aria-selected", taken ? "true" : "false");
      item.textContent = taken ? `${o.label} ✓` : o.label;
      if (taken) item.classList.add("taken");
      list.appendChild(item);
    });
    hi = -1;
    input.removeAttribute("aria-activedescendant");
  }

  function optionEls() {
    return Array.from(list.querySelectorAll(".filter-select-item"));
  }

  function highlight(idx) {
    const els = optionEls();
    if (!els.length) return;
    hi = (idx + els.length) % els.length;
    els.forEach((el, i) => el.classList.toggle("active", i === hi));
    const cur = els[hi];
    input.setAttribute("aria-activedescendant", cur.id);
    cur.scrollIntoView({ block: "nearest" });
  }

  function selectItem(item) {
    const id = item.dataset.id;
    if (!id || selected.has(id)) return;
    const opt = pool().find((o) => String(o.id) === id);
    selected.set(id, opt ? opt.label : id);
    input.value = "";
    renderChips();
    renderList("");
    popover.open();
    announceChange(wrap);
  }

  const remote = attachRemoteSearch(list, opts.search, (results) => {
    let added = false;
    for (const o of results) {
      const id = String(o.id);
      if (!found.has(id)) { found.set(id, { ...o, id }); added = true; }
    }
    if (added && hi < 0) renderList(input.value);
  });

  input.addEventListener("focus", () => {
    renderList(input.value);
    popover.open();
  });
  input.addEventListener("input", () => {
    renderList(input.value);
    popover.open();
    remote.query(input.value);
  });
  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    selectItem(item);
  });
  chipsRow.addEventListener("click", (e) => {
    const x = e.target.closest(".filter-chip-x");
    if (!x) return;
    const chip = x.closest(".filter-chip");
    if (!chip) return;
    selected.delete(chip.dataset.id);
    renderChips();
    renderList(input.value);
    announceChange(wrap);
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { popover.close(); }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { popover.close(); input.blur(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!popover.isOpen()) { renderList(input.value); popover.open(); }
      highlight(hi + (e.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if (e.key === "Enter" && popover.isOpen() && hi >= 0) {
      const el = optionEls()[hi];
      if (el) { e.preventDefault(); selectItem(el); }
    }
  });

  function setValues(ids) {
    selected.clear();
    const all = pool();
    for (const raw of ids || []) {
      const id = String(raw);
      const opt = all.find((o) => String(o.id) === id);
      selected.set(id, opt ? opt.label : id);
    }
    renderChips();
  }

  function setOptions(next) {
    items = next.slice();
    // Refresh chip labels for ids whose option text may have loaded/changed —
    // including a saved member resolved after mount, whose chip was showing a
    // bare snowflake until the lookup came back.
    const all = pool();
    for (const [id] of selected) {
      const opt = all.find((o) => String(o.id) === id);
      if (opt) selected.set(id, opt.label);
    }
    renderChips();
  }

  function setFilter(fn) {
    predicate = typeof fn === "function" ? fn : null;
    if (popover.isOpen()) renderList(input.value);
  }

  return {
    el: wrap,
    getValues: () => Array.from(selected.keys()),
    setValues,
    setOptions,
    setFilter,
    getInput: () => input,
    destroy: () => { remote.cancel(); popover.close(); },
  };
}
