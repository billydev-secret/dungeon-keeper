// Searchable, mobile-friendly select widgets shared across the dashboard.
//
// Two flavours, both driven by a text input + filtered dropdown (no native
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

function styleInput(input, placeholder) {
  input.type = "text";
  input.placeholder = placeholder;
  input.className = "filter-select-input";
  // Keep mobile keyboards from auto-capitalising / autocorrecting names.
  input.autocomplete = "off";
  input.autocapitalize = "off";
  input.spellcheck = false;
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
 * @returns {{el, getValue, setValue, setOptions, setFilter, getInput}}
 */
export function filterSelect(placeholder, options, opts = {}) {
  let predicate = typeof opts.filter === "function" ? opts.filter : null;
  const emptyLabel = opts.emptyLabel != null ? opts.emptyLabel : "(any)";
  const emptyValue = opts.emptyValue != null ? String(opts.emptyValue) : "";
  let items = options.slice();

  const wrap = document.createElement("div");
  wrap.className = "filter-select";

  const input = document.createElement("input");
  styleInput(input, placeholder);
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);

  let selectedId = emptyValue;
  let selectedLabel = "";

  function visible() {
    return predicate ? items.filter(predicate) : items;
  }

  function render(filter) {
    const lc = filter.toLowerCase();
    const base = visible();
    const matches = lc
      ? base.filter((o) => o.label.toLowerCase().includes(lc))
      : base;
    const show = lc ? matches : matches.slice(0, 300);
    list.innerHTML =
      `<div class="filter-select-item" data-id="${esc(emptyValue)}">
         <em style="color:var(--ink-dim)">${esc(emptyLabel)}</em>
       </div>` +
      show
        .map((o) => `<div class="filter-select-item" data-id="${esc(o.id)}">${esc(o.label)}</div>`)
        .join("");
  }

  input.addEventListener("focus", () => {
    render(input.value);
    list.style.display = "block";
  });
  input.addEventListener("input", () => {
    selectedId = emptyValue;
    selectedLabel = "";
    render(input.value);
    list.style.display = "block";
  });
  list.addEventListener("mousedown", (e) => {
    // mousedown (not click) so it fires before the input's blur hides the list.
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    const id = item.dataset.id;
    if (id === emptyValue) {
      selectedId = emptyValue;
      selectedLabel = "";
      input.value = "";
    } else {
      selectedId = id;
      selectedLabel = item.textContent.trim();
      input.value = selectedLabel;
    }
    list.style.display = "none";
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { list.style.display = "none"; }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { list.style.display = "none"; input.blur(); }
  });

  function setValue(id) {
    selectedId = (id == null || String(id) === emptyValue) ? emptyValue : String(id);
    if (selectedId === emptyValue) {
      selectedLabel = "";
      input.value = "";
      return;
    }
    const match = items.find((o) => o.id === selectedId);
    selectedLabel = match ? match.label : selectedId;
    input.value = selectedLabel;
  }

  function setOptions(next) {
    items = next.slice();
    setValue(selectedId); // refresh the displayed label against the new options
  }

  function setFilter(fn) {
    predicate = typeof fn === "function" ? fn : null;
    if (list.style.display === "block") render(input.value);
  }

  return {
    el: wrap,
    getValue: () => selectedId,
    setValue,
    setOptions,
    setFilter,
    getInput: () => input,
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
 * @returns {{el, getValues, setValues, setOptions, setFilter, getInput}}
 */
export function multiFilterSelect(placeholder, options, opts = {}) {
  let predicate = typeof opts.filter === "function" ? opts.filter : null;
  let items = options.slice();

  const wrap = document.createElement("div");
  wrap.className = "filter-select multi-filter-select";

  const chipsRow = document.createElement("div");
  chipsRow.className = "filter-select-chips";
  wrap.appendChild(chipsRow);

  const input = document.createElement("input");
  styleInput(input, placeholder);
  wrap.appendChild(input);

  const list = document.createElement("div");
  list.className = "filter-select-list";
  wrap.appendChild(list);

  const selected = new Map();

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
      x.setAttribute("aria-label", "Remove");
      x.textContent = "×";
      chip.appendChild(x);
      chipsRow.appendChild(chip);
    }
  }

  function visible() {
    return predicate ? items.filter(predicate) : items;
  }

  function renderList(filter) {
    const lc = filter.toLowerCase();
    const base = visible();
    const matches = lc
      ? base.filter((o) => o.label.toLowerCase().includes(lc))
      : base;
    const show = lc ? matches : matches.slice(0, 300);
    while (list.firstChild) list.removeChild(list.firstChild);
    for (const o of show) {
      const item = document.createElement("div");
      item.className = "filter-select-item";
      item.dataset.id = o.id;
      const taken = selected.has(o.id);
      item.textContent = taken ? `${o.label} ✓` : o.label;
      if (taken) item.classList.add("taken");
      list.appendChild(item);
    }
  }

  input.addEventListener("focus", () => {
    renderList(input.value);
    list.style.display = "block";
  });
  input.addEventListener("input", () => {
    renderList(input.value);
    list.style.display = "block";
  });
  list.addEventListener("mousedown", (e) => {
    const item = e.target.closest(".filter-select-item");
    if (!item) return;
    const id = item.dataset.id;
    if (!id || selected.has(id)) return;
    const opt = items.find((o) => o.id === id);
    selected.set(id, opt ? opt.label : id);
    input.value = "";
    renderChips();
    renderList("");
  });
  chipsRow.addEventListener("click", (e) => {
    const x = e.target.closest(".filter-chip-x");
    if (!x) return;
    const chip = x.closest(".filter-chip");
    if (!chip) return;
    selected.delete(chip.dataset.id);
    renderChips();
    renderList(input.value);
  });
  input.addEventListener("blur", () => {
    setTimeout(() => { list.style.display = "none"; }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { list.style.display = "none"; input.blur(); }
  });

  function setValues(ids) {
    selected.clear();
    for (const raw of ids || []) {
      const id = String(raw);
      const opt = items.find((o) => o.id === id);
      selected.set(id, opt ? opt.label : id);
    }
    renderChips();
  }

  function setOptions(next) {
    items = next.slice();
    // Refresh chip labels for ids whose option text may have loaded/changed.
    for (const [id] of selected) {
      const opt = items.find((o) => o.id === id);
      if (opt) selected.set(id, opt.label);
    }
    renderChips();
  }

  function setFilter(fn) {
    predicate = typeof fn === "function" ? fn : null;
    if (list.style.display === "block") renderList(input.value);
  }

  return {
    el: wrap,
    getValues: () => Array.from(selected.keys()),
    setValues,
    setOptions,
    setFilter,
    getInput: () => input,
  };
}
