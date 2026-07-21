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
