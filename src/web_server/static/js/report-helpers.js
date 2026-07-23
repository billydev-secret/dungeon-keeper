// Shared helpers for the analytics report panels (js/panels/*.js).
//
// Three exports:
//   rangePicker() — builds the day-range <select> control shown in a panel's
//                   `.controls` row.
//   withLoading() — wraps an in-flight request so the target element shows the
//                   refresh spinner (the `.is-loading` state styled in app.css).
//   syncHash()   — mirrors a panel's control state into the URL hash so
//                  filters/tabs/selection survive refresh and deep-link.

// Quick-select day ranges offered by every range picker. A panel's requested
// `value` is injected on top of these when it isn't already present, so any
// default (e.g. 10) still renders even if it's not in this list.
const DEFAULT_RANGES = [1, 2, 3, 7, 14, 30, 60, 90];

/**
 * Build a labelled day-range picker for a panel's `.controls` row.
 *
 * Returns the wrapping <label> element. Read the chosen value via
 *   rangeEl.querySelector("select").value
 * which is the number of days as a string, or "" for the "All time" option
 * (only present when `allowAll` is true).
 *
 * @param {object}        opts
 * @param {number|string} opts.value     initially-selected value ("" = All)
 * @param {boolean}       opts.allowAll  include an "All time" (no-limit) option
 * @param {string}        opts.label     control label text
 * @param {number[]}      [opts.ranges]  override the offered day counts
 *                                       (defaults to DEFAULT_RANGES)
 * @returns {HTMLLabelElement}
 */
export function rangePicker({ value = "", allowAll = false, label = "Range", ranges = null } = {}) {
  const wrap = document.createElement("label");
  const select = document.createElement("select");

  const days = [...(Array.isArray(ranges) && ranges.length ? ranges : DEFAULT_RANGES)];
  const numeric = parseInt(value, 10);
  if (!isNaN(numeric) && numeric > 0 && !days.includes(numeric)) {
    days.push(numeric);
  }
  days.sort((a, b) => a - b);

  if (allowAll) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "All time";
    select.appendChild(opt);
  }
  for (const n of days) {
    const opt = document.createElement("option");
    opt.value = String(n);
    opt.textContent = n === 1 ? "1 day" : `${n} days`;
    select.appendChild(opt);
  }

  // "" selects the All option when present; otherwise fall back to the first
  // real option so the control is never left on a phantom value.
  select.value = value == null ? "" : String(value);
  if (select.selectedIndex < 0) select.selectedIndex = 0;

  wrap.append(`${label} `, select);
  return wrap;
}

/**
 * Run an async request while showing the refresh-in-flight spinner on `el`.
 *
 * Adds the `.is-loading` class (styled in app.css) for the lifetime of
 * `promise`, then resolves or rejects with its result.
 *
 * @template T
 * @param {Element|null} el       element to overlay with the spinner
 * @param {Promise<T>}   promise  the in-flight request
 * @returns {Promise<T>}
 */
export async function withLoading(el, promise) {
  if (el) el.classList.add("is-loading");
  try {
    return await promise;
  } finally {
    if (el) el.classList.remove("is-loading");
  }
}

/**
 * Write a panel's control state into the URL hash via history.replaceState
 * (no new history entries), so filters/tabs/search/selection survive a
 * refresh and can be shared as a link. The router (app.js parseHash) hands
 * the query params back to mount() as `initialParams`.
 *
 * Entries whose value is null/undefined/""/false are omitted, so defaults
 * keep the URL clean.
 *
 * @param {string} panelId  route id, e.g. "mod-tickets"
 * @param {object} params   control state to encode
 */
export function syncHash(panelId, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    qs.set(k, String(v));
  }
  const q = qs.toString();
  history.replaceState(null, "", `#/${panelId}${q ? "?" + q : ""}`);
}
